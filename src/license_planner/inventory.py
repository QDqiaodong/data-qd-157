"""Offline multi-source licence inventory merge pipeline.

The caller supplies *deterministic* evidence only: claim rows (one complete
expression per ``(source, id)`` endpoint) and undirected alias edges.  The
pipeline never guesses an identity match and never makes a legal judgement;
it computes three things exactly:

1. entities  -- connected components of the alias graph over claim endpoints,
   keyed by the lexicographically smallest endpoint ``(source, id)``;
2. consensus -- the precise intersection of the members' complete candidate
   families (a candidate is a complete licence set produced by the existing
   :func:`~license_planner.expression.alternatives`);
3. a conflict core -- when the consensus is empty, the irreducible evidence
   kernel left by the mandated descending-endpoint deletion procedure.

Scale design (single process, peak RSS bounded, temporary disk allowed):

* Shards are streamed as binary blocks with a hard 65536-byte per-line cap;
  neither the manifest 8 MB rule nor any whole-file read is applied to a
  shard, and the 2 GiB cap is counted on raw bytes.
* Endpoints, claims and edges live in a temporary on-disk SQLite database
  (synchronous off, memory journal, modest page cache, memory mapping off).
* The only Python-resident index is the disjoint-set union structure stored
  in compact ``array('i')`` integer arrays (parent + size), so connectivity,
  forward references, duplicate edges, self loops and cycles cost O(n) bytes
  rather than O(n) Python objects.
* Entity remapping, consensus intersection and the deletion core are all
  computed in streaming group passes over clustered tables; per group only
  the current candidate sets (at most 256 candidates per expression) are
  held.  The core uses candidate occurrence counts over the *currently kept*
  members, which reproduces the literal trial-deletion rule in linear time.
* The report is serialized group by group straight to the temporary report
  file; no whole-report structure is ever assembled in memory.

Every temporary artefact is removed before the process exits and a report is
published only by a single ``os.replace`` after all validation has passed.
"""
from array import array
from pathlib import Path, PurePath
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from .common import ident, read
from .expression import alternatives, parse

MAX_SHARDS = 64
MAX_SHARD_BYTES = 2 * 1024 ** 3
MAX_LINE_BYTES = 65536
MAX_CLAIMS = 1_000_000
MAX_ALIASES = 2_000_000
SCHEMA_VERSION = 1
_READ_BLOCK = 1 << 20
_COMMIT_EVERY = 50_000

# Test hook: invoked after the temporary report has been fully written and
# fsynced but before the atomic os.replace.  Production leaves it unset;
# tests raise here to prove the old report survives byte for byte and that
# the temporary workspace is released.
_FAULT_HOOK = None


def set_fault_hook(hook):
    global _FAULT_HOOK
    _FAULT_HOOK = hook


class InventoryError(ValueError):
    """Contract violation; the CLI maps it to exit code 2."""


# ---------------------------------------------------------------------------
# Strict line / shape parsing
# ---------------------------------------------------------------------------

def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError('duplicate JSON key: ' + str(key))
        result[key] = value
    return result


def _reject_constant(value):
    raise InventoryError('non-finite JSON number: ' + value)


def _endpoint(value):
    if not isinstance(value, dict) or set(value) != {'source', 'id'}:
        raise InventoryError('endpoint must be an object with exactly '
                             'source and id')
    return ident(value['source']), ident(value['id'])


def _parse_line(raw_line):
    """Parse one non-empty NDJSON line into a normalized claim or alias.

    Identity is always the ``(source, id)`` tuple; strings are never joined
    into a collision-prone composite key.
    """
    try:
        text = raw_line.decode('utf-8')
    except UnicodeDecodeError as error:
        raise InventoryError('shard line is not valid UTF-8: %s' % error)
    try:
        obj = json.loads(text, object_pairs_hook=_unique_json_pairs,
                         parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise InventoryError('invalid NDJSON line: %s' % error)
    if not isinstance(obj, dict):
        raise InventoryError('every non-empty NDJSON line must be an object')
    kind = obj.get('kind')
    if kind == 'claim':
        if set(obj) != {'kind', 'source', 'id', 'expression'}:
            raise InventoryError('claim must contain exactly kind, source, '
                                 'id and expression')
        try:
            source, ref_id = _endpoint(
                {'source': obj['source'], 'id': obj['id']})
            expression = obj['expression']
        except KeyError as error:
            raise InventoryError('missing claim field: %s' % error)
        if not isinstance(expression, str):
            raise InventoryError('claim expression must be a string')
        # Baseline grammar, token and alternative limits apply unchanged.
        family = alternatives(parse(expression))
        candidates = sorted(tuple(sorted(choice)) for choice in family)
        # Candidates and the sets inside them are deduplicated; keys keep the
        # full "LICENSE WITH exception" spelling.
        unique = list(dict.fromkeys(candidates))
        return ('claim', source, ref_id, expression,
                json.dumps(unique, separators=(',', ':'), ensure_ascii=False),
                len(unique))
    if kind == 'alias':
        if set(obj) != {'kind', 'left', 'right'}:
            raise InventoryError('alias must contain exactly kind, left '
                                 'and right')
        try:
            left = _endpoint(obj['left'])
            right = _endpoint(obj['right'])
        except KeyError as error:
            raise InventoryError('missing alias field: %s' % error)
        return ('alias', left, right)
    raise InventoryError('line kind must be "claim" or "alias"')


def _iter_shard_lines(path):
    """Yield ``(raw_line_without_newline, delimiter_byte_length)``.

    Blank lines (whitespace only) are skipped.  The byte limit counts the
    line ending when one is present.  Reads are bounded blocks so a hostile
    multi-gigabyte unterminated line cannot be pulled into memory.
    """
    carried = b''
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(_READ_BLOCK)
            if not block:
                break
            pieces = block.split(b'\n')
            for piece in pieces[:-1]:
                line = carried + piece
                carried = b''
                if line.strip():
                    if len(line) + 1 > MAX_LINE_BYTES:
                        raise InventoryError(
                            'NDJSON line exceeds %d bytes including the line '
                            'ending' % MAX_LINE_BYTES)
                    yield line, 1
                else:
                    # Blank lines carry no row but their bytes still count.
                    yield b'', len(line) + 1
            carried += pieces[-1]
            if len(carried) >= MAX_LINE_BYTES:
                raise InventoryError(
                    'NDJSON line exceeds %d bytes' % MAX_LINE_BYTES)
        if carried.strip():
            if len(carried) >= MAX_LINE_BYTES:
                raise InventoryError(
                    'NDJSON line exceeds %d bytes' % MAX_LINE_BYTES)
            yield carried, 0
        elif carried:
            # A trailing blank fragment without a newline.
            yield b'', len(carried)


# ---------------------------------------------------------------------------
# Manifest and path contract
# ---------------------------------------------------------------------------

def _normalize_relative(rel):
    if not isinstance(rel, str) or not rel:
        raise InventoryError('shard path must be a non-empty string')
    pure = PurePath(rel)
    if pure.is_absolute() or pure.is_reserved() or rel.endswith(
            ('/', os.sep)):
        raise InventoryError('shard path must be relative: ' + rel)
    depth = 0
    for part in pure.parts:
        if part in ('', '.'):
            continue
        if part == '..':
            depth -= 1
            if depth < 0:
                raise InventoryError(
                    'shard path escapes the manifest directory: ' + rel)
        else:
            depth += 1
    if not pure.parts or pure.parts == ('.',):
        raise InventoryError('shard path must name a file: ' + rel)
    normalized = '/'.join(part for part in pure.parts if part != '.')
    if not normalized:
        raise InventoryError('shard path must name a file: ' + rel)
    return normalized


def _load_manifest(manifest_path, output_path):
    # The manifest follows the existing strict JSON contract exactly,
    # including the raw-byte 8 MB ceiling and digest.
    manifest, digest = read(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {'policy', 'files'}:
        raise InventoryError(
            'manifest must contain exactly policy and files')

    policy = {}
    policy_rows = manifest['policy']
    if not isinstance(policy_rows, list) or len(policy_rows) > 200:
        raise InventoryError('policy must be an array of at most 200 entries')
    for entry in policy_rows:
        if not isinstance(entry, dict) \
                or set(entry) != {'license', 'allowed', 'obligations'}:
            raise InventoryError(
                'policy entry must contain exactly license, allowed, '
                'obligations')
        key = ident(entry['license'])
        tree = parse(key)
        if tree != ('license', key) or key in policy \
                or type(entry['allowed']) is not bool:
            raise InventoryError('invalid policy key/allow flag')
        obligations = entry['obligations']
        if not isinstance(obligations, list) or len(obligations) > 50:
            raise InventoryError(
                'obligations must be an array of at most 50')
        obligations = [ident(item) for item in obligations]
        if len(set(obligations)) != len(obligations):
            raise InventoryError('duplicate policy obligation')
        policy[key] = (entry['allowed'], frozenset(obligations))

    file_rows = manifest['files']
    if not isinstance(file_rows, list) or len(file_rows) > MAX_SHARDS:
        raise InventoryError(
            'files must be an array of at most %d shard paths' % MAX_SHARDS)
    normalized = [_normalize_relative(item) for item in file_rows]
    if len(set(normalized)) != len(normalized):
        raise InventoryError('manifest shard paths must be distinct')
    base = Path(manifest_path).resolve().parent
    resolved = {}
    for rel in normalized:
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            raise InventoryError(
                'shard path resolves outside the manifest directory: ' + rel)
        if not target.is_file():
            raise InventoryError(
                'shard must be an existing regular file: ' + rel)
        resolved[rel] = target
    manifest_resolved = Path(manifest_path).resolve()
    output_resolved = Path(output_path).resolve()
    all_paths = list(resolved.values()) + [manifest_resolved, output_resolved]
    if len(set(all_paths)) != len(all_paths):
        raise InventoryError('manifest, shard and report paths must all '
                             'be distinct')
    return digest, policy, sorted(resolved.items()), output_resolved


# ---------------------------------------------------------------------------
# On-disk workspace
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE refs(
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    UNIQUE(source, ref)
);
CREATE TABLE claims(
    ref_id INTEGER PRIMARY KEY REFERENCES refs(id),
    expression TEXT NOT NULL,
    fam TEXT NOT NULL,
    cand_count INTEGER NOT NULL
);
CREATE TABLE edges(a INTEGER NOT NULL, b INTEGER NOT NULL);
"""


class _Workspace:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='license-planner-inventory-')
        self.db_path = os.path.join(self.dir, 'work.sqlite')
        self.conn = sqlite3.connect(self.db_path)
        self.conn.isolation_level = None
        # Bounded page cache and no memory mapping keep RSS independent of
        # database size; external sorts and big hash joins spill into the
        # temporary directory instead of growing resident memory.
        self.conn.execute('PRAGMA page_size=4096')
        self.conn.execute('PRAGMA journal_mode=MEMORY')
        self.conn.execute('PRAGMA synchronous=OFF')
        self.conn.execute('PRAGMA temp_store=1')
        self.conn.execute('PRAGMA cache_size=-16384')
        self.conn.execute('PRAGMA mmap_size=0')
        self.conn.execute('BEGIN')
        for statement in (s.strip() for s in _SCHEMA.split(';') if s.strip()):
            self.conn.execute(statement)
        # Compact integer DSU; index 0 is unused and SQLite ids are dense.
        self.parent = array('i', [0])
        self.size = array('i', [0])
        self.pending = 0
        self.claim_count = 0
        self.alias_count = 0

    def _commit_window(self):
        if self.pending >= _COMMIT_EVERY:
            self.conn.execute('COMMIT')
            self.conn.execute('BEGIN')
            self.pending = 0

    def ref_id(self, source, ref_id):
        # One round trip; the DSU array grows only when the id is genuinely
        # new, so Python never stores the endpoint strings twice.
        row = self.conn.execute(
            'INSERT INTO refs(source,ref) VALUES(?,?) '
            'ON CONFLICT(source,ref) DO UPDATE SET source=excluded.source '
            'RETURNING id', (source, ref_id)).fetchone()
        new_id = row[0]
        while len(self.parent) <= new_id:
            if len(self.parent) >= 2 ** 31 - 1:
                raise InventoryError('endpoint count exceeds the bounded '
                                     'integer workspace')
            self.parent.append(len(self.parent))
            self.size.append(1)
        self.pending += 1
        return new_id

    def find(self, node):
        parent = self.parent
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            nxt = parent[node]
            parent[node] = root
            node = nxt
        return root

    def union(self, left, right):
        roots = self.find(left), self.find(right)
        if roots[0] == roots[1]:
            return
        first, second = roots
        if self.size[first] < self.size[second]:
            first, second = second, first
        self.parent[second] = first
        self.size[first] += self.size[second]

    def add_claim(self, ref_id, expression, family, cand_count):
        try:
            self.conn.execute(
                'INSERT INTO claims(ref_id,expression,fam,cand_count) '
                'VALUES(?,?,?,?)',
                (ref_id, expression, family, cand_count))
        except sqlite3.IntegrityError as error:
            raise InventoryError(
                'duplicate claim for the same (source,id) endpoint') from error
        self.pending += 1
        self.claim_count += 1
        if self.claim_count > MAX_CLAIMS:
            raise InventoryError('more than %d claims' % MAX_CLAIMS)
        self._commit_window()

    def add_edge(self, left, right):
        self.conn.execute('INSERT INTO edges(a,b) VALUES(?,?)',
                          (left, right))
        self.pending += 1
        self.alias_count += 1
        if self.alias_count > MAX_ALIASES:
            raise InventoryError('more than %d aliases' % MAX_ALIASES)
        self._commit_window()

    def close(self):
        try:
            self.conn.close()
        finally:
            # Temporary workspace: never reused across calls.
            def _on_error(func, path, exc):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
                func(path)
            shutil.rmtree(self.dir, onerror=_on_error)


# ---------------------------------------------------------------------------
# Entity construction (final, after every shard has been read)
# ---------------------------------------------------------------------------

def _build_entities(ws):
    conn = ws.conn
    conn.execute('COMMIT')
    conn.execute('BEGIN')
    # Every alias endpoint must ultimately carry a claim.  Forward references
    # are legal, so this check is meaningful only after the full read.
    dangling = conn.execute(
        'SELECT 1 FROM edges e WHERE NOT EXISTS '
        '(SELECT 1 FROM claims c WHERE c.ref_id=e.a) '
        'OR NOT EXISTS (SELECT 1 FROM claims c WHERE c.ref_id=e.b) LIMIT 1'
    ).fetchone()
    if dangling is not None:
        raise InventoryError('alias endpoint without a matching claim')

    # DSU roots are the final entities; remap every ref to its root in one
    # compact streaming pass (integer arrays only).
    conn.execute('CREATE TABLE membership(ref_id INTEGER PRIMARY KEY, '
                 'entity INTEGER NOT NULL)')
    batch = []
    for (ref_id,) in conn.execute('SELECT id FROM refs ORDER BY id'):
        batch.append((ref_id, ws.find(ref_id)))
        if len(batch) >= _COMMIT_EVERY:
            conn.executemany('INSERT INTO membership VALUES(?,?)', batch)
            batch.clear()
    if batch:
        conn.executemany('INSERT INTO membership VALUES(?,?)', batch)

    # Entity key = smallest endpoint.  Scan in endpoint order; the first row
    # seen for each root is therefore its key.  No endpoint array is built.
    conn.execute('CREATE TABLE entmap(root_id INTEGER PRIMARY KEY, '
                 'key_id INTEGER NOT NULL)')
    seen_roots = set()
    batch = []
    rows_q = conn.execute(
        'SELECT m.entity, m.ref_id FROM membership m '
        'JOIN refs r ON r.id=m.ref_id '
        'ORDER BY r.source, r.ref')
    for root_id, ref_id in rows_q:
        if root_id not in seen_roots:
            seen_roots.add(root_id)
            batch.append((ref_id, root_id))
    conn.executemany('INSERT INTO entmap(key_id,root_id) VALUES(?,?)', batch)
    del seen_roots, batch

    conn.execute('CREATE TABLE membership2(ref_id INTEGER PRIMARY KEY, '
                 'entity INTEGER NOT NULL) WITHOUT ROWID')
    conn.execute('INSERT INTO membership2 '
                 'SELECT m.ref_id, e.key_id FROM membership m '
                 'JOIN entmap e ON e.root_id=m.entity')
    conn.execute('DROP TABLE membership')
    conn.execute('ALTER TABLE membership2 RENAME TO membership')
    conn.execute('CREATE INDEX idx_membership_entity ON membership(entity)')

    # Clustered per-entity claim table (every validated ref holds a claim).
    conn.execute('CREATE TABLE ecc(entity INTEGER NOT NULL, '
                 'ref_id INTEGER NOT NULL, fam TEXT NOT NULL, '
                 'expression TEXT NOT NULL, '
                 'PRIMARY KEY(entity, ref_id)) WITHOUT ROWID')
    conn.execute('INSERT INTO ecc(entity,ref_id,fam,expression) '
                 'SELECT m.entity, c.ref_id, c.fam, c.expression '
                 'FROM membership m JOIN claims c ON c.ref_id=m.ref_id')
    conn.execute('COMMIT')
    conn.execute('BEGIN')


# ---------------------------------------------------------------------------
# Consensus and deletion core
# ---------------------------------------------------------------------------

def _load_family(raw):
    return [tuple(candidate) for candidate in json.loads(raw)]


def _entity_consensus(ws, entity):
    """Stream one entity's families and intersect them exactly."""
    consensus = None
    for (raw,) in ws.conn.execute(
            'SELECT c.fam FROM ecc c WHERE c.entity=?', (entity,)):
        family = _load_family(raw)
        if not family:
            # Defensive: the baseline grammar cannot produce an empty
            # candidate family, but an empty family means no consensus.
            return set()
        if consensus is None:
            consensus = set(family)
        else:
            consensus.intersection_update(family)
    return consensus if consensus is not None else set()


def _conflict_core(ws, entity):
    """Irreducible evidence kernel per the mandated deletion procedure.

    Members are tried in descending endpoint order; a member is removed
    permanently only when the intersection of the *currently kept* members
    stays empty.  Candidate occurrence counts make each trial linear: with
    the kept intersection already empty, removing member ``x`` leaves a
    non-empty intersection iff some candidate is present in every other kept
    member, i.e. its kept count is ``|kept|-1`` and ``x`` lacks it.

    Both passes stream the clustered table; the only resident structures are
    the candidate counter (at most 256 distinct candidates per expression)
    and the survivor id list, never a parallel array of all members.
    """
    rows_q = ws.conn.execute(
        'SELECT c.ref_id, c.fam FROM ecc c JOIN refs r ON r.id=c.ref_id '
        'WHERE c.entity=? ORDER BY r.source DESC, r.ref DESC', (entity,))
    counts = {}
    kept_count = 0
    for _ref_id, raw in rows_q:
        kept_count += 1
        for candidate in _load_family(raw):
            counts[candidate] = counts.get(candidate, 0) + 1

    def kept_intersection_nonempty_after_removing(family_set):
        target = kept_count - 1
        for candidate, count in counts.items():
            if count == target and candidate not in family_set:
                return True
        return False

    survivors = []
    rows_q = ws.conn.execute(
        'SELECT c.ref_id, c.fam FROM ecc c JOIN refs r ON r.id=c.ref_id '
        'WHERE c.entity=? ORDER BY r.source DESC, r.ref DESC', (entity,))
    for ref_id, raw in rows_q:
        family = _load_family(raw)
        family_set = set(family)
        if kept_intersection_nonempty_after_removing(family_set):
            # Deleting this member would leave a shared candidate: keep it.
            survivors.append(ref_id)
        else:
            for candidate in family:
                counts[candidate] -= 1
            kept_count -= 1
    # Traversal was descending endpoint order; reversing yields ascending.
    survivors.reverse()
    return survivors


def _policy_verdict(policy, consensus):
    permitted, rejected = [], []
    for choice in sorted(consensus):
        blocked = sorted(key for key in choice
                         if key not in policy or not policy[key][0])
        if blocked:
            rejected.append({'licenses': list(choice),
                             'blocked_or_unknown': blocked})
            continue
        duties = set()
        for key in choice:
            duties |= policy[key][1]
        permitted.append({'licenses': list(choice),
                          'obligations': sorted(duties)})
    permitted.sort(key=lambda item: (len(item['obligations']),
                                     len(item['licenses']),
                                     item['licenses']))
    selected = permitted[0] if permitted else None
    return selected, permitted, rejected


# ---------------------------------------------------------------------------
# Report publication
# ---------------------------------------------------------------------------

def _endpoint_object(source, ref_id):
    return {'source': source, 'id': ref_id}


def _line(obj):
    return (json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       allow_nan=False, separators=(',', ':')) + '\n') \
        .encode('utf-8')


def _write_report(ws, policy, output_path, manifest_digest, shard_info,
                  totals):
    conn = ws.conn
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix='.report-', dir=output_path.parent)
        with os.fdopen(fd, 'wb') as stream:
            header = {
                'kind': 'header',
                'schema_version': SCHEMA_VERSION,
                'source_sha256': manifest_digest,
                'files': [{'path': rel, 'sha256': digest}
                          for rel, digest in sorted(shard_info)],
            }
            stream.write(_line(header))
            group_total = 0
            entities = conn.execute(
                'SELECT e.key_id, r.source, r.ref FROM entmap e '
                'JOIN refs r ON r.id=e.key_id ORDER BY r.source, r.ref')
            # Lazy cursor: entity rows are consumed one at a time, never
            # accumulated into a report-sized structure.
            for entity, source, ref_id in entities:
                group_total += 1
                consensus = _entity_consensus(ws, entity)
                member_count = conn.execute(
                    'SELECT COUNT(*) FROM ecc WHERE entity=?',
                    (entity,)).fetchone()[0]
                endpoint = _endpoint_object(source, ref_id)
                if not consensus:
                    core = []
                    for core_id in _conflict_core(ws, entity):
                        cs, cr = conn.execute(
                            'SELECT source,ref FROM refs WHERE id=?',
                            (core_id,)).fetchone()
                        core.append(_endpoint_object(cs, cr))
                    group = {'kind': 'group', 'entity': endpoint,
                             'member_count': member_count,
                             'status': 'conflict', 'selected': None,
                             'alternatives': [], 'rejected': [],
                             'core': core}
                else:
                    selected, alternatives, rejected = _policy_verdict(
                        policy, consensus)
                    status = 'allowed' if alternatives else 'blocked'
                    group = {'kind': 'group', 'entity': endpoint,
                             'member_count': member_count,
                             'status': status, 'selected': selected,
                             'alternatives': alternatives,
                             'rejected': rejected, 'core': []}
                stream.write(_line(group))
                member_rows = conn.execute(
                    'SELECT r.source, r.ref, c.expression FROM ecc c '
                    'JOIN refs r ON r.id=c.ref_id WHERE c.entity=? '
                    'ORDER BY r.source, r.ref', (entity,))
                for member_source, member_ref, expression in member_rows:
                    stream.write(_line({
                        'kind': 'member',
                        'entity': endpoint,
                        'ref': _endpoint_object(member_source, member_ref),
                        'expression': expression,
                    }))
            stream.flush()
            os.fsync(stream.fileno())
        if _FAULT_HOOK is not None:
            _FAULT_HOOK(tmp_name)
        os.replace(tmp_name, output_path)
        tmp_name = None
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)
    receipt = {'groups': group_total, 'claims': totals['claims'],
               'aliases': totals['aliases'], 'shards': len(shard_info),
               'shard_bytes': totals['bytes']}
    return receipt


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_pipeline(manifest_path, output_path):
    manifest_path = Path(manifest_path)
    output_path = Path(output_path)
    if output_path.is_symlink():
        raise InventoryError('report output cannot be a symlink')
    manifest_digest, policy, shards, output_resolved = _load_manifest(
        manifest_path, output_path)
    # Raw-byte accounting uses the full shard bytes (blank lines included);
    # the cap is enforced before each shard is parsed, so an oversized input
    # never forces a full read.
    total_bytes = 0
    shard_sizes = []
    for rel, target in shards:
        size = os.path.getsize(target)
        shard_sizes.append((rel, target, size))
        total_bytes += size
        if total_bytes > MAX_SHARD_BYTES:
            raise InventoryError('shard input exceeds 2 GiB in total')

    ws = _Workspace()
    try:
        shard_info = []
        for rel, target, size in shard_sizes:
            digest = hashlib.sha256()
            line_bytes = 0
            for raw_line, delimiter in _iter_shard_lines(target):
                line_bytes += len(raw_line) + delimiter
                if not raw_line:
                    continue
                parsed = _parse_line(raw_line)
                if parsed[0] == 'claim':
                    _, source, ref_id, expression, family, count = parsed
                    endpoint = ws.ref_id(source, ref_id)
                    ws.add_claim(endpoint, expression, family, count)
                else:
                    _, left, right = parsed
                    left_id = ws.ref_id(*left)
                    right_id = ws.ref_id(*right)
                    ws.union(left_id, right_id)
                    ws.add_edge(left_id, right_id)
            if line_bytes != size:
                # The scanner must account for every raw byte (including
                # skipped blank lines and a missing final newline).
                raise InventoryError('internal shard byte accounting error')
            # Hash covers every raw byte of the shard, blanks and endings
            # included.
            with open(target, 'rb') as stream:
                for block in iter(lambda: stream.read(_READ_BLOCK), b''):
                    digest.update(block)
            shard_info.append((rel, digest.hexdigest()))
        _build_entities(ws)
        totals = {'claims': ws.claim_count, 'aliases': ws.alias_count,
                  'bytes': total_bytes}
        return _write_report(ws, policy, output_resolved, manifest_digest,
                             shard_info, totals)
    finally:
        ws.close()


def run_inventory(manifest_path, output_path):
    return _run_pipeline(manifest_path, output_path)
