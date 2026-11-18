"""Independent small-scale oracle and differential tests for the inventory
merge pipeline.

The oracle deliberately shares *no* production merge code: it builds its own
undirected reachability groups, enumerates expected candidate sets with its
own tiny SPDX-expression parser, intersects each whole group directly and
runs the mandated deletion core literally, one trial at a time.  Only the
strict JSON line decoder is reused, because JSON decoding itself is not the
logic under test.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from license_planner.inventory import set_fault_hook  # noqa: E402
import license_planner.inventory as inventory  # noqa: E402


# ---------------------------------------------------------------------------
# Independent oracle
# ---------------------------------------------------------------------------

def oracle_candidates(expression):
    """Enumerate complete candidate licence sets with a standalone parser.

    Grammar: OR < AND < atom; atom is LICENSE or "LICENSE WITH ExceptionID"
    or a parenthesised group.  Returns a tuple of sorted unique tuples.
    """
    tokens = expression.replace('(', ' ( ').replace(')', ' ) ').split()
    state = {'pos': 0}

    def atom():
        token = tokens[state['pos']]
        state['pos'] += 1
        if token == '(':
            result = disjunction()
            assert tokens[state['pos']] == ')'
            state['pos'] += 1
            return result
        license_id = token
        if state['pos'] < len(tokens) and tokens[state['pos']] == 'WITH':
            state['pos'] += 1
            license_id += ' WITH ' + tokens[state['pos']]
            state['pos'] += 1
        return {(license_id,)}

    def conjunction():
        result = atom()
        while state['pos'] < len(tokens) and tokens[state['pos']] == 'AND':
            state['pos'] += 1
            right = atom()
            result = {tuple(sorted(set(a) | set(b)))
                      for a in result for b in right}
        return result

    def disjunction():
        result = conjunction()
        while state['pos'] < len(tokens) and tokens[state['pos']] == 'OR':
            state['pos'] += 1
            result = result | conjunction()
        return result

    family = disjunction()
    assert state['pos'] == len(tokens)
    return tuple(sorted(set(tuple(sorted(choice)) for choice in family)))


def oracle_groups(claims, aliases):
    """Connected components from undirected alias edges (oracle DSU)."""
    endpoints = sorted(claims)
    parent = {endpoint: endpoint for endpoint in endpoints}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in aliases:
        for node in (left, right):
            if node not in parent:
                parent[node] = node
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_r] = root_l

    grouped = {}
    for endpoint in parent:
        # Alias-only endpoints are dangling references; the production
        # pipeline rejects them, so the oracle only models claimed ones.
        if endpoint in claims:
            grouped.setdefault(find(endpoint), []).append(endpoint)
    entities = {}
    for members in grouped.values():
        members = sorted(members)
        key = members[0]
        families = {endpoint: oracle_candidates(claims[endpoint])
                    for endpoint in members if endpoint in claims}
        entities[key] = {'members': sorted(families), 'families': families}
    return entities


def oracle_intersection(families, members):
    result = None
    for endpoint in members:
        family = set(families[endpoint])
        result = set(family) if result is None else result & family
    return set() if result is None else result


def oracle_core(families, members):
    """Literal mandated procedure: descending endpoints, one trial each."""
    kept = list(members)

    def intersection_of(group):
        result = None
        for endpoint in group:
            family = set(families[endpoint])
            result = set(family) if result is None else result & family
        return set() if result is None else result

    if intersection_of(kept):
        return []
    for endpoint in sorted(members, reverse=True):
        trial = [member for member in kept if member != endpoint]
        if not intersection_of(trial):
            kept = trial
    return sorted(kept)


def oracle_policy(policy, consensus):
    table = {entry['license']: (entry['allowed'],
                                frozenset(entry['obligations']))
             for entry in policy}
    permitted, rejected = [], []
    for choice in sorted(consensus):
        blocked = sorted(key for key in choice
                         if key not in table or not table[key][0])
        if blocked:
            rejected.append({'licenses': list(choice),
                             'blocked_or_unknown': blocked})
            continue
        duties = set()
        for key in choice:
            duties |= table[key][1]
        permitted.append({'licenses': list(choice),
                          'obligations': sorted(duties)})
    permitted.sort(key=lambda item: (len(item['obligations']),
                                     len(item['licenses']),
                                     item['licenses']))
    selected = permitted[0] if permitted else None
    return selected, permitted, rejected


def oracle_report(claims, aliases, policy):
    groups = oracle_groups(claims, aliases)
    lines = []
    for key in sorted(groups):
        entity = groups[key]
        members, families = entity['members'], entity['families']
        consensus = oracle_intersection(families, members)
        endpoint = {'source': key[0], 'id': key[1]}
        if not consensus:
            group = {'kind': 'group', 'entity': endpoint,
                     'member_count': len(members), 'status': 'conflict',
                     'selected': None, 'alternatives': [], 'rejected': [],
                     'core': [{'source': endpoint[0], 'id': endpoint[1]}
                              for endpoint in oracle_core(families, members)]}
        else:
            selected, alternatives, rejected = oracle_policy(policy, consensus)
            status = 'allowed' if alternatives else 'blocked'
            group = {'kind': 'group', 'entity': endpoint,
                     'member_count': len(members), 'status': status,
                     'selected': selected, 'alternatives': alternatives,
                     'rejected': rejected, 'core': []}
        lines.append(group)
        for member in members:
            lines.append({'kind': 'member', 'entity': endpoint,
                          'ref': {'source': member[0], 'id': member[1]},
                          'expression': claims[member]})
    return lines


def dumps(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
    {'license': 'BSD-3-Clause', 'allowed': True, 'obligations': []},
    {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
]


class OracleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_inventory(self, claims, aliases, files=('one.ndjson',),
                        policy=None, permute=False, seed=7):
        import random
        rows = []
        for (source, ref_id), expression in sorted(claims.items()):
            rows.append({'kind': 'claim', 'source': source, 'id': ref_id,
                         'expression': expression})
        for left, right in aliases:
            rows.append({'kind': 'alias',
                         'left': {'source': left[0], 'id': left[1]},
                         'right': {'source': right[0], 'id': right[1]}})
        rng = random.Random(seed)
        rng.shuffle(rows)
        chunks = [[] for _ in files]
        for index, row in enumerate(rows):
            chunks[index % len(chunks)].append(row)
        if permute:
            for chunk in chunks:
                rng.shuffle(chunk)
        paths = []
        for name, chunk in zip(files, chunks):
            target = self.dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            content = '\n'.join(dumps(row) for row in chunk)
            target.write_text(content + ('\n' if content else ''),
                              encoding='utf-8')
            paths.append(name)
        manifest = self.dir / 'manifest.json'
        manifest.write_text(dumps({
            'policy': POLICY if policy is None else policy,
            'files': sorted(paths),
        }), encoding='utf-8')
        return manifest

    def run_cli(self, manifest, report_name='report.ndjson'):
        report = self.dir / report_name
        result = subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', '--inventory',
             manifest, '--output', report],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        return result, report

    def assert_matches_oracle(self, claims, aliases, policy=None,
                              files=('one.ndjson',)):
        manifest = self.write_inventory(claims, aliases, files, policy)
        result, report = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = [json.loads(line) for line in
                  report.read_text().splitlines()[1:]]
        expected = oracle_report(claims, aliases,
                                 POLICY if policy is None else policy)
        self.assertEqual(actual, expected)
        return result, report

    # -- functional scenarios --------------------------------------------

    def test_acceptance_pairwise_consistent_overall_conflict(self):
        claims = {
            ('s', 'a'): 'MIT OR Apache-2.0',
            ('s', 'b'): 'MIT OR BSD-3-Clause',
            ('s', 'c'): 'Apache-2.0 OR BSD-3-Clause',
            ('s', 'z'): 'MIT OR Apache-2.0 OR BSD-3-Clause',
        }
        aliases = [(('s', 'a'), ('s', 'b')), (('s', 'b'), ('s', 'c')),
                   (('s', 'c'), ('s', 'z'))]
        _result, report = self.assert_matches_oracle(claims, aliases)
        groups = [row for row in map(json.loads, report.read_text().splitlines())
                  if row['kind'] == 'group']
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['status'], 'conflict')
        self.assertIsNone(group['selected'])
        self.assertEqual(group['alternatives'], [])
        self.assertEqual(group['rejected'], [])
        self.assertEqual(group['core'], [
            {'source': 's', 'id': 'a'},
            {'source': 's', 'id': 'b'},
            {'source': 's', 'id': 'c'}])
        self.assertEqual(group['member_count'], 4)

    def test_three_way_conflict_without_redundancy(self):
        claims = {
            ('s', 'a'): 'MIT OR Apache-2.0',
            ('s', 'b'): 'MIT OR BSD-3-Clause',
            ('s', 'c'): 'Apache-2.0 OR BSD-3-Clause',
        }
        self.assert_matches_oracle(
            claims, [(('s', 'a'), ('s', 'b')), (('s', 'b'), ('s', 'c'))])

    def test_multiple_conflict_cores_deterministic_order(self):
        # Two disjoint conflict triples inside one connected entity.
        claims = {
            ('s', 'a'): 'MIT OR Apache-2.0',
            ('s', 'b'): 'MIT OR BSD-3-Clause',
            ('s', 'c'): 'Apache-2.0 OR BSD-3-Clause',
            ('s', 'd'): 'MIT OR Apache-2.0',
            ('s', 'e'): 'MIT OR GPL-3.0-only',
            ('s', 'f'): 'Apache-2.0 OR GPL-3.0-only',
        }
        aliases = [(('s', 'a'), ('s', 'b')), (('s', 'b'), ('s', 'c')),
                   (('s', 'c'), ('s', 'd')), (('s', 'd'), ('s', 'e')),
                   (('s', 'e'), ('s', 'f'))]
        self.assert_matches_oracle(claims, aliases)

    def test_non_first_member_is_final_entity_key(self):
        # The smallest endpoint is introduced in the last shard.
        claims = {('s', 'm'): 'MIT', ('s', 'z'): 'MIT',
                  ('s', 'a'): 'MIT'}
        aliases = [(('s', 'm'), ('s', 'z')), (('s', 'z'), ('s', 'a'))]
        _result, report = self.assert_matches_oracle(
            claims, aliases, files=('f1.ndjson', 'f2.ndjson'))
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['entity'], {'source': 's', 'id': 'a'})

    def test_same_id_across_sources_stays_distinct_without_alias(self):
        claims = {('src-1', 'x'): 'MIT', ('src-2', 'x'): 'GPL-3.0-only'}
        _result, report = self.assert_matches_oracle(claims, [])
        groups = [json.loads(line) for line in report.read_text().splitlines()
                  if json.loads(line)['kind'] == 'group']
        self.assertEqual(len(groups), 2)

    def test_separator_chars_in_source_and_id_do_not_collide(self):
        # Delimiter-looking characters must never create a composite key.
        claims = {('a/b', 'c|d'): 'MIT', ('a', 'b/c|d'): 'MIT',
                 ('a|b', 'c/d'): 'MIT'}
        self.assert_matches_oracle(claims, [])

    def test_with_key_is_kept_whole(self):
        policy = POLICY + [
            {'license': 'GPL-2.0-only WITH Classpath-exception-2.0',
             'allowed': True, 'obligations': []}]
        claims = {('s', 'w1'): 'GPL-2.0-only WITH Classpath-exception-2.0',
                  ('s', 'w2'): 'GPL-2.0-only WITH Classpath-exception-2.0'}
        aliases = [(('s', 'w1'), ('s', 'w2'))]
        _result, report = self.assert_matches_oracle(
            claims, aliases, policy=policy)
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['status'], 'allowed')
        self.assertEqual(group['selected']['licenses'],
                         ['GPL-2.0-only WITH Classpath-exception-2.0'])

    def test_unknown_license_consensus_is_blocked_not_conflict(self):
        claims = {('s', 'u1'): 'Weirdware-1.0',
                  ('s', 'u2'): 'Weirdware-1.0'}
        _result, report = self.assert_matches_oracle(
            claims, [(('s', 'u1'), ('s', 'u2'))])
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['status'], 'blocked')
        self.assertIsNone(group['selected'])
        self.assertEqual(group['alternatives'], [])
        self.assertEqual(group['rejected'], [{
            'licenses': ['Weirdware-1.0'],
            'blocked_or_unknown': ['Weirdware-1.0']}])
        self.assertEqual(group['core'], [])

    def test_allowed_entity_has_consensus_candidate(self):
        claims = {('s', 'p1'): 'MIT OR GPL-3.0-only',
                  ('s', 'p2'): 'MIT OR Apache-2.0'}
        _result, report = self.assert_matches_oracle(
            claims, [(('s', 'p1'), ('s', 'p2'))])
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['status'], 'allowed')
        self.assertEqual(group['selected'],
                         {'licenses': ['MIT'], 'obligations': ['notice']})

    def test_and_or_equivalent_syntax_collapses(self):
        claims = {('s', 'e1'): 'MIT OR MIT', ('s', 'e2'): 'MIT'}
        _result, report = self.assert_matches_oracle(
            claims, [(('s', 'e1'), ('s', 'e2'))])
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['status'], 'allowed')
        self.assertEqual(group['member_count'], 2)

    def test_no_subset_elimination(self):
        # {MIT} and {MIT,Apache} are distinct complete candidates.
        claims = {('s', 'n1'): 'MIT',
                  ('s', 'n2'): 'MIT AND Apache-2.0'}
        _result, report = self.assert_matches_oracle(
            claims, [(('s', 'n1'), ('s', 'n2'))])
        group = next(json.loads(line)
                     for line in report.read_text().splitlines()
                     if json.loads(line)['kind'] == 'group')
        self.assertEqual(group['status'], 'conflict')
        self.assertEqual(group['core'], [
            {'source': 's', 'id': 'n1'},
            {'source': 's', 'id': 'n2'}])

    def test_duplicate_edges_self_loops_cycles_and_forward_refs(self):
        claims = {('s', 'a'): 'MIT', ('s', 'b'): 'MIT', ('s', 'c'): 'MIT'}
        aliases = [
            (('s', 'c'), ('s', 'a')),       # forward reference to a
            (('s', 'a'), ('s', 'a')),       # self loop
            (('s', 'a'), ('s', 'b')),
            (('s', 'b'), ('s', 'a')),       # duplicate undirected edge
            (('s', 'b'), ('s', 'c')),       # closes a cycle
        ]
        self.assert_matches_oracle(claims, aliases)

    def test_permutation_and_shard_layout_byte_identical_output(self):
        claims = {
            ('s', 'a'): 'MIT OR Apache-2.0',
            ('s', 'b'): 'MIT OR BSD-3-Clause',
            ('s', 'c'): 'Apache-2.0 OR BSD-3-Clause',
            ('s', 'z'): 'MIT OR Apache-2.0 OR BSD-3-Clause',
            ('s', 'lone'): 'MIT',
            ('t', 'a'): 'GPL-3.0-only',
        }
        aliases = [(('s', 'a'), ('s', 'b')), (('s', 'b'), ('s', 'c')),
                   (('s', 'c'), ('s', 'z'))]
        manifest_a = self.write_inventory(
            claims, aliases, files=('one.ndjson',), seed=1)
        manifest_b = self.write_inventory(
            claims, aliases, files=('p1.ndjson', 'p2.ndjson', 'p3.ndjson'),
            permute=True, seed=2)
        report_a = self.dir / 'a.ndjson'
        report_b = self.dir / 'b.ndjson'
        first = subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', '--inventory',
             manifest_a, '--output', report_a],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        second = subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', '--inventory',
             manifest_b, '--output', report_b],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        body_a = report_a.read_text().splitlines()[1:]
        body_b = report_b.read_text().splitlines()[1:]
        self.assertEqual(body_a, body_b)

    def test_empty_inventory_emits_header_only(self):
        manifest = self.dir / 'manifest.json'
        manifest.write_text(dumps({'policy': POLICY, 'files': []}))
        result, report = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = report.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        header = json.loads(lines[0])
        self.assertEqual(header['kind'], 'header')
        self.assertEqual(header['schema_version'], 1)
        self.assertEqual(header['files'], [])

    def test_empty_shard_file_is_legal(self):
        shard = self.dir / 'blank.ndjson'
        shard.write_text('')
        manifest = self.dir / 'manifest.json'
        manifest.write_text(dumps({'policy': POLICY, 'files': ['blank.ndjson']}))
        result, report = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = report.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['files'],
                         [{'path': 'blank.ndjson',
                           'sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}])

    def test_late_alias_merges_two_allowed_entities_into_conflict(self):
        claims = {('s', 'a'): 'MIT', ('s', 'b'): 'GPL-3.0-only'}
        manifest = self.write_inventory(claims, [])
        # Append a late alias in a second shard listed after the first.
        late = self.dir / 'late.ndjson'
        late.write_text(dumps({'kind': 'alias',
                               'left': {'source': 's', 'id': 'a'},
                               'right': {'source': 's', 'id': 'b'}}) + '\n')
        manifest.write_text(dumps({'policy': POLICY,
                                   'files': ['late.ndjson', 'one.ndjson']}))
        result, report = self.run_cli(manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        groups = [json.loads(line) for line in report.read_text().splitlines()
                  if json.loads(line)['kind'] == 'group']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['status'], 'conflict')


LICENSE_POOL = ['MIT', 'Apache-2.0', 'BSD-3-Clause', 'GPL-3.0-only',
                'GPL-2.0-only WITH Classpath-exception-2.0']
EXPRESSION_POOL = [
    'MIT', 'Apache-2.0', 'BSD-3-Clause', 'GPL-3.0-only',
    'MIT OR Apache-2.0', 'MIT OR BSD-3-Clause',
    'Apache-2.0 OR BSD-3-Clause', 'MIT AND Apache-2.0',
    'MIT OR Apache-2.0 OR BSD-3-Clause',
    'MIT OR GPL-2.0-only WITH Classpath-exception-2.0',
    '(MIT OR Apache-2.0) AND BSD-3-Clause',
    'MIT OR MIT', 'Apache-2.0 OR Apache-2.0 OR MIT',
]


class FuzzDifferential(unittest.TestCase):
    def test_random_scenarios_match_oracle(self):
        import random
        rng = random.Random(20261118)
        policy = POLICY + [
            {'license': 'GPL-2.0-only WITH Classpath-exception-2.0',
             'allowed': True, 'obligations': []}]
        for scenario in range(120):
            tmp = tempfile.TemporaryDirectory()
            try:
                directory = Path(tmp.name)
                sources = ('src-a', 'src/b', 'src|c')
                claims = {}
                endpoint_count = rng.randint(1, 18)
                for index in range(endpoint_count):
                    source = rng.choice(sources)
                    ref_id = 'id-%02d/%d' % (index, rng.randint(0, 3))
                    claims.setdefault((source, ref_id),
                                      rng.choice(EXPRESSION_POOL))
                aliases = []
                endpoints = list(claims)
                for _ in range(rng.randint(0, 12)):
                    if len(endpoints) < 2:
                        break
                    left, right = rng.sample(endpoints, 2)
                    aliases.append((left, right))
                # Sprinkle duplicate edges and self loops.
                if aliases:
                    aliases.append(aliases[0])
                    aliases.append((endpoints[0], endpoints[0]))
                rows = []
                for (source, ref_id), expression in claims.items():
                    rows.append({'kind': 'claim', 'source': source,
                                 'id': ref_id, 'expression': expression})
                for left, right in aliases:
                    rows.append({'kind': 'alias',
                                 'left': {'source': left[0], 'id': left[1]},
                                 'right': {'source': right[0],
                                           'id': right[1]}})
                rng.shuffle(rows)
                shard_count = rng.randint(1, 3)
                names = ['shard-%d.ndjson' % i for i in range(shard_count)]
                chunks = [[] for _ in names]
                for index, row in enumerate(rows):
                    chunks[rng.randrange(shard_count)].append(row)
                for name, chunk in zip(names, chunks):
                    rng.shuffle(chunk)
                    (directory / name).write_text(
                        '\n'.join(dumps(row) for row in chunk)
                        + ('\n' if chunk else ''), encoding='utf-8')
                manifest = directory / 'manifest.json'
                manifest.write_text(dumps({'policy': policy,
                                           'files': sorted(names)}))
                report = directory / 'report.ndjson'
                result = subprocess.run(
                    [sys.executable, '-m', 'license_planner.cli',
                     '--inventory', manifest, '--output', report],
                    capture_output=True, text=True,
                    env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
                self.assertEqual(result.returncode, 0,
                                 'scenario %d: %s' % (scenario, result.stderr))
                actual = [json.loads(line) for line in
                          report.read_text().splitlines()[1:]]
                expected = oracle_report(claims, aliases, policy)
                self.assertEqual(
                    actual, expected,
                    'scenario %d mismatch' % scenario)
                # Independently verify every reported core's irreducibility
                # invariants directly on the raw claims.
                groups = [row for row in actual if row['kind'] == 'group']
                member_rows = {}
                current = None
                for row in actual:
                    if row['kind'] == 'group':
                        current = (row['entity']['source'],
                                   row['entity']['id'])
                        member_rows[current] = []
                    elif row['kind'] == 'member':
                        member_rows[current].append(
                            (row['ref']['source'], row['ref']['id']))
                for group in groups:
                    if group['status'] != 'conflict':
                        self.assertEqual(group['core'], [])
                        continue
                    key = (group['entity']['source'],
                           group['entity']['id'])
                    core_endpoints = [(item['source'], item['id'])
                                      for item in group['core']]
                    families = {
                        endpoint: set(oracle_candidates(claims[endpoint]))
                        for endpoint in member_rows[key]}
                    core_families = {endpoint: families[endpoint]
                                     for endpoint in core_endpoints}
                    # The core itself has an empty intersection.
                    self.assertFalse(oracle_intersection(
                        core_families, core_endpoints))
                    # Removing any single core member makes it non-empty.
                    for endpoint in core_endpoints:
                        rest = [item for item in core_endpoints
                                if item != endpoint]
                        self.assertTrue(oracle_intersection(
                            core_families, rest),
                            'core not irreducible at %s' % (endpoint,))
            finally:
                tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
