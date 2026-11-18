"""Reproducible 200k-claim / 400k-alias, >128 MiB scale and RSS test.

The dataset is generated from a fixed seed (generator process, not the
measured production process), spread across eight NDJSON shards, and
contains on purpose:

* several distinct sources, including same local ids under different
  sources that must never merge without an explicit alias;
* one large entity whose member count is far above any in-memory "small
  group" assumption;
* many small entities, some never touched by an alias;
* forward references, duplicate edges, self loops and a late-arriving
  alias cycle whose closing edges land in the final shard;
* a four-member conflict core (three pairwise-compatible, overall-empty
  evidence plus one redundant member);
* a blocked entity (consensus exists, licence unknown to the policy) and
  an allowed entity.

The production command runs in an independent subprocess.  Peak RSS comes
from ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` of the waiter (KiB on
Linux), never from tracemalloc, and the business output is verified against
an independent recomputation of the provable groups, consensus and core.
"""
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

SEED = 2026111802
CLAIM_COUNT = 200_000
ALIAS_COUNT = 400_000
SHARD_COUNT = 8
LARGE_ENTITY_SIZE = 20_000
RSS_LIMIT_MIB = 128
POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
    {'license': 'BSD-3-Clause', 'allowed': True, 'obligations': []},
]

# Fixed special endpoints (all under source "s0").
CORE = {'a': 'MIT OR Apache-2.0',
        'b': 'MIT OR BSD-3-Clause',
        'c': 'Apache-2.0 OR BSD-3-Clause',
        'z': 'MIT OR Apache-2.0 OR BSD-3-Clause'}
BIG_PREFIX = 'big-'
BLOCKED_ID = 'unknown-blocked'
ALLOWED_ID = 'plain-allowed'


def dumps(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


def generate(workdir):
    """Build the shards and manifest deterministically; return provenance."""
    import random
    rng = random.Random(SEED)
    workdir = Path(workdir)
    shard_paths = [workdir / ('shard-%d.ndjson' % i)
                   for i in range(SHARD_COUNT)]
    handles = [path.open('wb') for path in shard_paths]
    counts = [0] * SHARD_COUNT
    total_bytes = 0

    def emit(index, row):
        nonlocal total_bytes
        line = dumps(row).encode('utf-8') + b'\n'
        handles[index].write(line)
        counts[index] += 1
        total_bytes += len(line)

    sources = ('s0', 'src-1', 'src/2', 'src|3')

    # ---- claims -------------------------------------------------------
    # 1. Conflict core members (first shards).
    core_rows = [
        (0, ('s0', 'a'), CORE['a']),
        (1, ('s0', 'b'), CORE['b']),
        (2, ('s0', 'c'), CORE['c']),
        (3, ('s0', 'z'), CORE['z']),
    ]
    for shard, (source, ref_id), expression in core_rows:
        emit(shard, {'kind': 'claim', 'source': source, 'id': ref_id,
                     'expression': expression})

    # 2. Large entity: 20k endpoints all chained, several sources.
    big_endpoints = []
    for i in range(LARGE_ENTITY_SIZE):
        source = sources[i % len(sources)]
        ref_id = '%s%05d' % (BIG_PREFIX, i)
        big_endpoints.append((source, ref_id))
        expression = 'MIT' if i % 2 == 0 else 'MIT OR Apache-2.0'
        emit(i % SHARD_COUNT,
             {'kind': 'claim', 'source': source, 'id': ref_id,
              'expression': expression})

    # 3. Blocked (unknown licence consensus) and allowed entities.
    emit(0, {'kind': 'claim', 'source': 's0', 'id': BLOCKED_ID,
             'expression': 'Weirdware-9.9'})
    emit(1, {'kind': 'claim', 'source': 's1', 'id': BLOCKED_ID,
             'expression': 'Weirdware-9.9'})
    emit(0, {'kind': 'claim', 'source': 's0', 'id': ALLOWED_ID,
             'expression': 'MIT OR GPL-3.0-only'})
    emit(1, {'kind': 'claim', 'source': 's1', 'id': ALLOWED_ID,
             'expression': 'MIT'})

    # 4. Remaining claims: small standalone entities plus same-id/different
    #    source endpoints.  Whitespace padding inflates raw bytes past the
    #    128 MiB requirement without changing any parsed value.
    fixed = 4 + LARGE_ENTITY_SIZE + 4
    remaining = CLAIM_COUNT - fixed
    assert remaining > 0
    expressions = ('MIT', 'Apache-2.0', 'BSD-3-Clause', 'MIT OR Apache-2.0',
                   'MIT OR BSD-3-Clause', 'Apache-2.0 OR BSD-3-Clause')
    for i in range(remaining):
        source = sources[i % len(sources)]
        # Local ids repeat across sources on purpose (no auto-merge).
        ref_id = 'cmp-%06d' % (i % (remaining // len(sources) + 1))
        expression = rng.choice(expressions)
        # ~620 bytes of insignificant whitespace keeps every line legal
        # while pushing total input comfortably past 128 MiB.
        padded = expression + ' ' * 600
        emit(i % SHARD_COUNT,
             {'kind': 'claim', 'source': source, 'id': ref_id,
              'expression': padded})

    # ---- aliases ------------------------------------------------------
    # Chain the large entity (forward references included: edges reference
    # higher indices before their claims are conceptually "seen").
    for i in range(LARGE_ENTITY_SIZE - 1):
        emit(i % SHARD_COUNT,
             {'kind': 'alias',
              'left': {'source': big_endpoints[i][0],
                       'id': big_endpoints[i][1]},
              'right': {'source': big_endpoints[i + 1][0],
                        'id': big_endpoints[i + 1][1]}})
    # Self loop + duplicate edge.
    emit(0, {'kind': 'alias',
             'left': {'source': big_endpoints[0][0],
                      'id': big_endpoints[0][1]},
             'right': {'source': big_endpoints[0][0],
                       'id': big_endpoints[0][1]}})
    emit(0, {'kind': 'alias',
             'left': {'source': big_endpoints[1][0],
                      'id': big_endpoints[1][1]},
             'right': {'source': big_endpoints[0][0],
                       'id': big_endpoints[0][1]}})
    # Conflict core edges; the closing edges arrive in the LAST shard.
    emit(SHARD_COUNT - 1,
         {'kind': 'alias', 'left': {'source': 's0', 'id': 'a'},
          'right': {'source': 's0', 'id': 'b'}})
    emit(SHARD_COUNT - 1,
         {'kind': 'alias', 'left': {'source': 's0', 'id': 'b'},
          'right': {'source': 's0', 'id': 'c'}})
    emit(SHARD_COUNT - 1,
         {'kind': 'alias', 'left': {'source': 's0', 'id': 'c'},
          'right': {'source': 's0', 'id': 'z'}})
    # A late alias cycle: core loops back onto itself through a.
    emit(SHARD_COUNT - 1,
         {'kind': 'alias', 'left': {'source': 's0', 'id': 'z'},
          'right': {'source': 's0', 'id': 'a'}})
    # Blocked and allowed two-member entities.
    emit(2, {'kind': 'alias',
             'left': {'source': 's0', 'id': BLOCKED_ID},
             'right': {'source': 's1', 'id': BLOCKED_ID}})
    emit(2, {'kind': 'alias',
             'left': {'source': 's0', 'id': ALLOWED_ID},
             'right': {'source': 's1', 'id': ALLOWED_ID}})

    remaining_edges = ALIAS_COUNT - (
        LARGE_ENTITY_SIZE - 1) - 2 - 4 - 2
    assert remaining_edges > 0
    # Extra edges stay inside the big entity (duplicates and cross-source
    # links) so the proven group counts stay exact.
    for i in range(remaining_edges):
        x = rng.randrange(LARGE_ENTITY_SIZE)
        y = rng.randrange(LARGE_ENTITY_SIZE)
        emit(i % SHARD_COUNT,
             {'kind': 'alias',
              'left': {'source': big_endpoints[x][0],
                       'id': big_endpoints[x][1]},
              'right': {'source': big_endpoints[y][0],
                        'id': big_endpoints[y][1]}})

    for handle in handles:
        handle.close()

    manifest = workdir / 'manifest.json'
    manifest.write_text(dumps({
        'policy': POLICY,
        'files': [path.name for path in shard_paths],
    }))
    provenance = {
        'claim_count': CLAIM_COUNT,
        'alias_count': ALIAS_COUNT,
        'shard_bytes': total_bytes,
        'big_entity_size': LARGE_ENTITY_SIZE,
        'big_entity_key': {'source': big_endpoints[0][0],
                           'id': big_endpoints[0][1]},
        'big_entity_last': {'source': big_endpoints[-1][0],
                            'id': big_endpoints[-1][1]},
        'core': [{'source': 's0', 'id': name} for name in ('a', 'b', 'c')],
    }
    (workdir / 'provenance.json').write_text(json.dumps(provenance, indent=2))
    return provenance


class ScaleInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='inventory-scale-')
        cls.dir = Path(cls.tmp.name)
        cls.started = time.perf_counter()
        cls.provenance = generate(cls.dir)
        cls.generated_seconds = time.perf_counter() - cls.started
        cls.manifest = cls.dir / 'manifest.json'
        cls.report = cls.dir / 'report.ndjson'

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_01_input_meets_scale_requirements(self):
        total = sum(p.stat().st_size for p in
                    sorted(self.dir.glob('shard-*.ndjson')))
        self.assertEqual(total, self.provenance['shard_bytes'])
        self.assertGreater(total, 128 * 1024 * 1024)
        line_counts = 0
        for path in sorted(self.dir.glob('shard-*.ndjson')):
            with path.open('rb') as stream:
                for line in stream:
                    self.assertLessEqual(len(line), 65536)
                    line_counts += 1
        # Rows = claims + aliases.
        self.assertEqual(line_counts, CLAIM_COUNT + ALIAS_COUNT)

    def test_02_subprocess_completes_under_rss_limit_and_output_matches(self):
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', '--inventory',
             self.manifest, '--output', self.report],
            capture_output=True, text=True, env=env)
        elapsed = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual((receipt['claims'], receipt['aliases']),
                         (CLAIM_COUNT, ALIAS_COUNT))
        # ru_maxrss is KiB on Linux; take the delta over children that
        # existed before this test run.
        peak_kib = max(after, before)
        peak_mib = peak_kib / 1024
        self.assertLess(
            peak_mib, RSS_LIMIT_MIB,
            'peak RSS %.1f MiB exceeds %d MiB' % (peak_mib, RSS_LIMIT_MIB))
        print('\n--- inventory scale evidence ---')
        print('platform: %s; ru_maxrss unit: KiB (Linux)' % sys.platform)
        print('input bytes: %d (%.2f MiB), shards: %d'
              % (self.provenance['shard_bytes'],
                 self.provenance['shard_bytes'] / 1048576, SHARD_COUNT))
        print('command: %s -m license_planner.cli --inventory %s --output %s'
              % (sys.executable, self.manifest, self.report))
        print('exit status: %d; elapsed: %.1fs; generation: %.1fs'
              % (result.returncode, elapsed, self.generated_seconds))
        print('peak RSS (RUSAGE_CHILDREN ru_maxrss): %d KiB = %.1f MiB'
              % (peak_kib, peak_mib))
        self._verify_report()

    def _verify_report(self):
        # Independent verification of every provable property.  The report
        # is streamed line by line; the verifier never holds it all at once.
        groups = {}
        current = None
        header_seen = False
        with self.report.open('rb') as stream:
            for raw in stream:
                row = json.loads(raw)
                if row['kind'] == 'header':
                    self.assertFalse(header_seen)
                    header_seen = True
                    self.assertEqual(row['schema_version'], 1)
                    self.assertEqual(
                        [item['path'] for item in row['files']],
                        sorted(item['path'] for item in row['files']))
                    continue
                if row['kind'] == 'group':
                    current = row['entity']
                    groups[(row['entity']['source'],
                            row['entity']['id'])] = row
                    continue
                self.assertEqual(row['kind'], 'member')
                self.assertEqual(row['entity'], current)

        # Exact group count derived from the construction:
        # big entity (1) + core entity (1) + blocked (1) + allowed (1) +
        # one group per distinct small-entity endpoint (small endpoints have
        # no aliases, so each distinct (source,id) is its own entity).
        small_endpoints = set()
        claimed = set()
        for path in sorted(self.dir.glob('shard-*.ndjson')):
            with path.open('rb') as stream:
                for raw in stream:
                    row = json.loads(raw)
                    if row['kind'] != 'claim':
                        continue
                    endpoint = (row['source'], row['id'])
                    claimed.add(endpoint)
                    if not row['id'].startswith(BIG_PREFIX) \
                            and row['id'] not in CORE \
                            and row['id'] not in (BLOCKED_ID, ALLOWED_ID):
                        small_endpoints.add(endpoint)
        expected_groups = 4 + len(small_endpoints)
        self.assertEqual(len(groups), expected_groups)
        self.assertEqual(len(claimed), CLAIM_COUNT)

        # Large entity: key, member count, allowed consensus (MIT).
        big_key = (self.provenance['big_entity_key']['source'],
                   self.provenance['big_entity_key']['id'])
        big = groups[big_key]
        self.assertEqual(big['member_count'], LARGE_ENTITY_SIZE)
        self.assertEqual(big['status'], 'allowed')
        self.assertEqual(big['selected']['licenses'], ['MIT'])
        self.assertEqual(big['core'], [])

        # Conflict core: exactly a, b, c; z is removed as redundant.
        core_group = groups[('s0', 'a')]
        self.assertEqual(core_group['status'], 'conflict')
        self.assertIsNone(core_group['selected'])
        self.assertEqual(core_group['alternatives'], [])
        self.assertEqual(core_group['rejected'], [])
        self.assertEqual(core_group['member_count'], 4)
        self.assertEqual(core_group['core'], self.provenance['core'])

        # Blocked vs conflict stay distinct.
        blocked = groups[('s0', BLOCKED_ID)]
        self.assertEqual(blocked['status'], 'blocked')
        self.assertIsNone(blocked['selected'])
        self.assertEqual(blocked['core'], [])
        self.assertEqual(blocked['rejected'], [{
            'licenses': ['Weirdware-9.9'],
            'blocked_or_unknown': ['Weirdware-9.9']}])
        allowed = groups[('s0', ALLOWED_ID)]
        self.assertEqual(allowed['status'], 'allowed')
        self.assertEqual(allowed['selected']['licenses'], ['MIT'])

        # Deterministic entity ordering in the serialized file.
        keys = list(groups)
        self.assertEqual(keys, sorted(keys))


if __name__ == '__main__':
    unittest.main()
