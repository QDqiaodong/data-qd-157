"""Independent small-tree oracle and differential tests for layered delivery.

The oracle is deliberately naive and shares no production directory code.  It
keeps a plain ``dict`` copy of the whole visible tree per layer, applies the
deletion markers from the pre-layer copy, installs normal writes by depth and
then re-binds hardlinks by following the layer's own small target map.  Path
subtree removal is a literal string-prefix scan of the copied dict.  It never
imports ``license_planner.layers`` and never calls its transforms, resolver or
audit entry point.

Every random case compares the *complete* business result (all four keys and
all nested fields) with the oracle.  Cases the oracle deems invalid must also
be rejected by production.
"""
import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from license_planner.layers import audit  # noqa: E402

GOOD = 'a' * 64
OTHER = 'b' * 64
BAD = 'c' * 64
D = {'x': '1' * 64, 'y': '2' * 64, 'z': '3' * 64}

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
    {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
    {'license': 'MIT WITH Exception', 'allowed': True, 'obligations': ['notice']},
]


# ---------------------------------------------------------------------------
# Independent oracle: tiny SPDX alternative enumeration
# ---------------------------------------------------------------------------

def oracle_alternatives(text):
    tokens = text.replace('(', ' ( ').replace(')', ' ) ').split()
    pos = [0]

    def atom():
        token = tokens[pos[0]]
        pos[0] += 1
        if token == '(':
            result = disjunction()
            assert tokens[pos[0]] == ')'
            pos[0] += 1
            return result
        if pos[0] < len(tokens) and tokens[pos[0]] == 'WITH':
            pos[0] += 1
            token = token + ' WITH ' + tokens[pos[0]]
            pos[0] += 1
        return {frozenset([token])}

    def conjunction():
        result = atom()
        while pos[0] < len(tokens) and tokens[pos[0]] == 'AND':
            pos[0] += 1
            right = atom()
            result = {left | other for left in result for other in right}
        return result

    def disjunction():
        result = conjunction()
        while pos[0] < len(tokens) and tokens[pos[0]] == 'OR':
            pos[0] += 1
            result = result | conjunction()
        return result

    result = disjunction()
    assert pos[0] == len(tokens), text
    return result


def oracle_license(policy, expression):
    permitted = []
    for choice in sorted(oracle_alternatives(expression),
                         key=lambda s: tuple(sorted(s))):
        blocked = sorted(k for k in choice
                         if k not in policy or not policy[k][0])
        if blocked:
            continue
        duties = set()
        for key in choice:
            duties |= policy[key][1]
        permitted.append({'licenses': sorted(choice),
                          'obligations': sorted(duties)})
    permitted.sort(key=lambda row: (len(row['obligations']),
                                    len(row['licenses']), row['licenses']))
    if not permitted:
        return 'blocked', None
    return 'allowed', permitted[0]


# ---------------------------------------------------------------------------
# Independent oracle: literal copied tree
# ---------------------------------------------------------------------------

def _parts(path):
    return path.split('/')


def _remove_subtree(tree, path):
    doomed = [key for key in tree
              if key == path or key.startswith(path + '/')]
    for key in doomed:
        del tree[key]


def _ensure_parents(tree, parts):
    prefix = ''
    for segment in parts[:-1]:
        prefix = segment if not prefix else prefix + '/' + segment
        node = tree.get(prefix)
        if node is None:
            tree[prefix] = ('d',)
        elif node[0] != 'd':
            raise ValueError('regular-file parent')


def _lookup(tree, parts):
    return tree.get('/'.join(parts))


def _validate_layer(entries):
    writes = {}
    deletes = {}
    for entry in entries:
        kind = entry['kind']
        path = entry['path']
        if kind in ('dir', 'file', 'hardlink'):
            if path in writes:
                raise ValueError('duplicate write path')
            writes[path] = kind
        else:
            if path in deletes:
                raise ValueError('duplicate delete path')
            deletes[path] = kind
    filelike = {path for path, kind in writes.items() if kind != 'dir'}
    for path in writes:
        parts = _parts(path)
        for index in range(1, len(parts)):
            if '/'.join(parts[:index]) in filelike:
                raise ValueError('file ancestor')
    return writes, deletes


def oracle(data):
    policy = {}
    for row in data['policy']:
        policy[row['license']] = (row['allowed'], set(row['obligations']))
    expressions = {}
    for row in data['components']:
        if row['id'] in expressions:
            raise ValueError('duplicate component')
        expressions[row['id']] = row['expression']
    declared = {}
    for row in data['materials']:
        declared.setdefault(row['obligation'], set()).add(row['sha256'])

    tree = {}
    for layer in data['layers']:
        entries = layer['entries']
        writes, deletes = _validate_layer(entries)
        pre = copy.copy(tree)

        # Deletion markers: every target and its deletion set is decided on
        # the pre-layer copy; the result is then applied to the live copy.
        for path, kind in deletes.items():
            node = pre.get(path)
            if kind == 'opaque':
                if node is not None and node[0] != 'd':
                    raise ValueError('opaque target is a file')
                if node is not None:
                    for key in [key for key in tree
                                if key.startswith(path + '/')]:
                        del tree[key]
            elif node is not None:
                _remove_subtree(tree, path)

        # Directory and regular-file writes, shallow paths first.
        normal = [entry for entry in entries
                  if entry['kind'] in ('dir', 'file')]
        normal.sort(key=lambda entry: (len(_parts(entry['path'])),
                                       entry['path']))
        for entry in normal:
            parts = _parts(entry['path'])
            _ensure_parents(tree, parts)
            if entry['kind'] == 'dir':
                if tree.get(entry['path'], ('d',))[0] != 'd':
                    tree[entry['path']] = ('d',)
                else:
                    tree[entry['path']] = ('d',)
            else:
                _remove_subtree(tree, entry['path'])
                tree[entry['path']] = (
                    'f', entry['sha256'], entry['component'],
                    layer['id'], entry['path'])

        # Hardlinks: old bindings at link paths are detached first, then each
        # target is followed through the layer's own map to a regular file.
        links = {entry['path']: entry['target'] for entry in entries
                 if entry['kind'] == 'hardlink'}
        for path in links:
            _remove_subtree(tree, path)
        for path in sorted(links):
            seen = set()
            cursor = path
            while cursor in links:
                if cursor in seen:
                    raise ValueError('hardlink cycle')
                seen.add(cursor)
                cursor = links[cursor]
            node = tree.get(cursor)
            if node is None:
                raise ValueError('missing target')
            if node[0] != 'f':
                raise ValueError('directory target')
            obj = node[1:]
            parts = _parts(path)
            _ensure_parents(tree, parts)
            _remove_subtree(tree, path)
            tree[path] = ('f',) + obj

    visible = sorted((path, node) for path, node in tree.items()
                     if node[0] == 'f')
    files = [{'path': path, 'sha256': node[1], 'component': node[2],
              'origin': {'layer': node[3], 'path': node[4]}}
             for path, node in visible]
    exposed = {}
    for path, node in visible:
        if node[2] is not None:
            exposed.setdefault(node[2], []).append(path)
    components = []
    required = {}
    for cid in sorted(exposed):
        status, selected = oracle_license(policy, expressions[cid])
        components.append({'id': cid, 'status': status,
                           'selected': selected,
                           'paths': sorted(exposed[cid])})
        if selected is not None:
            for obligation in selected['obligations']:
                required.setdefault(obligation, set()).add(cid)
    obligations = []
    for obligation in sorted(required):
        digests = declared.get(obligation, set())
        paths = sorted(path for path, node in visible
                       if node[1] in digests)
        obligations.append({'obligation': obligation,
                            'components': sorted(required[obligation]),
                            'satisfied': bool(paths), 'paths': paths})
    compliant = (all(row['status'] == 'allowed' for row in components)
                 and all(row['satisfied'] for row in obligations))
    return {'compliant': compliant, 'files': files,
            'components': components, 'obligations': obligations}


# ---------------------------------------------------------------------------
# Random small case generator
# ---------------------------------------------------------------------------

def _file(path, digest, component=None):
    return {'kind': 'file', 'path': path, 'sha256': digest,
            'component': component}


def _ok(case):
    try:
        oracle(case)
    except (ValueError, AssertionError):
        return False
    return True


def random_case(rng):
    components = []
    for index in range(rng.randint(0, 4)):
        components.append({'id': 'c%d' % index,
                           'expression': rng.choice([
                               'MIT', 'Apache-2.0', 'MIT OR Apache-2.0',
                               'MIT AND Apache-2.0', 'GPL-3.0-only',
                               'MIT OR GPL-3.0-only', 'MIT WITH Exception',
                               'MIT OR MIT WITH Exception'])})
    ids = [row['id'] for row in components]
    materials = []
    if rng.random() < 0.8:
        pool = list(D.values()) + [GOOD, OTHER, BAD]
        for obligation in ('notice', 'patent'):
            for digest in rng.sample(pool, rng.randint(0, 2)):
                materials.append({'obligation': obligation,
                                  'sha256': digest})
    anchors = ['anchor/%d' % i for i in range(3)]
    layers = [{'id': 'L0', 'entries': [
        _file(path, D['x'], rng.choice(ids) if ids and rng.random() < .5
              else None) for path in anchors]}]
    known = list(anchors)
    touched = set(anchors)
    neutral = 0
    for number in range(1, rng.randint(1, 4)):
        entries = []
        used_write = set()
        used_delete = set()
        fresh = []

        def new_path(prefix):
            nonlocal neutral
            neutral += 1
            return '%s/%d' % (prefix, neutral)

        for _ in range(rng.randint(0, 5)):
            choice = rng.random()
            if choice < 0.4:
                path = new_path('f')
                comp = rng.choice(ids) if ids and rng.random() < 0.6 else None
                entries.append(_file(path, rng.choice(list(D.values())), comp))
                fresh.append(path)
            elif choice < 0.55:
                path = new_path('h')
                target = rng.choice(anchors + fresh) if (anchors + fresh) \
                    else anchors[0]
                entries.append({'kind': 'hardlink', 'path': path,
                                'target': target})
                fresh.append(path)
            elif choice < 0.7:
                path = new_path('dir')
                entries.append({'kind': 'dir', 'path': path})
                entries.append(_file(path + '/inside', D['z'],
                                     rng.choice(ids) if ids else None))
            elif choice < 0.85:
                # whiteout: an anchor (which we later re-create) or a fresh
                # no-op path.
                if rng.random() < 0.4:
                    path = 'ghost/%d' % neutral
                else:
                    path = rng.choice(anchors)
                entries.append({'kind': 'whiteout', 'path': path})
            else:
                entries.append({'kind': 'opaque',
                                'path': rng.choice(['ghost', 'anchor'])})
            used_write = {e['path'] for e in entries if e['kind'] != 'opaque'
                          and e['kind'] != 'whiteout'}
            used_delete = {e['path'] for e in entries if e['kind'] in
                           ('opaque', 'whiteout')}
            if used_write & used_delete:
                entries = [e for e in entries
                           if not (e['path'] in used_write & used_delete)]
        # Re-create any whiteouted anchor so anchors stay valid link targets.
        written = {e['path'] for e in entries
                   if e['kind'] in ('dir', 'file', 'hardlink')}
        for path in anchors:
            if path not in written and rng.random() < 0.8:
                entries.append(_file(path, rng.choice(list(D.values())),
                                     rng.choice(ids) if ids else None))
        # A same-layer forward hardlink chain is a pure dependency chain.
        if rng.random() < 0.5:
            length = rng.randint(1, 3)
            chain = ['chain/%d/%d' % (number, i) for i in range(length)]
            for index, path in enumerate(chain):
                target = chain[index + 1] if index + 1 < length \
                    else rng.choice(anchors)
                entries.append({'kind': 'hardlink', 'path': path,
                                'target': target})
        rng.shuffle(entries)
        layers.append({'id': 'L%d' % number, 'entries': entries})
        known.extend(fresh)
    return {'policy': POLICY, 'components': components,
            'materials': materials, 'layers': layers}


def universe_case(rng):
    """Dense random case over a small path universe.

    Paths repeat across layers, so directory/file swaps, same-path write then
    delete, overlapping deletes, opaque on missing/directory/file targets,
    missing and directory link targets, forward chains and cycles all occur;
    whether a case is legal is decided solely by the oracle.
    """
    components = [{'id': 'c%d' % i, 'expression': rng.choice(
        ['MIT', 'Apache-2.0', 'MIT AND Apache-2.0', 'GPL-3.0-only'])}
        for i in range(rng.randint(1, 3))]
    ids = [row['id'] for row in components]
    materials = []
    for obligation in ('notice', 'patent'):
        if rng.random() < 0.7:
            for digest in rng.sample(list(D.values()) + [GOOD, OTHER],
                                     rng.randint(1, 3)):
                materials.append({'obligation': obligation,
                                  'sha256': digest})
    universe = ['a', 'b', 'a/x', 'a/y', 'b/x', 'c', 'c/d', 'c/d/e']
    layers = []
    for number in range(rng.randint(1, 4)):
        entries = []
        for _ in range(rng.randint(0, 6)):
            kind = rng.choice(['dir', 'file', 'hardlink', 'whiteout',
                               'whiteout', 'opaque'])
            path = rng.choice(universe)
            if kind == 'dir':
                entries.append({'kind': 'dir', 'path': path})
            elif kind == 'file':
                entries.append(_file(path, rng.choice(list(D.values())),
                                     rng.choice(ids)
                                     if rng.random() < 0.5 else None))
            elif kind == 'hardlink':
                target = rng.choice(universe + ['missing'])
                entries.append({'kind': 'hardlink', 'path': path,
                                'target': target})
            else:
                entries.append({'kind': kind, 'path': path})
        rng.shuffle(entries)
        layers.append({'id': 'U%d' % number, 'entries': entries})
    return {'policy': POLICY, 'components': components,
            'materials': materials, 'layers': layers}


class OracleDifferential(unittest.TestCase):
    def test_dense_universe_cases_match(self):
        rng = random.Random(2026111804)
        valid = rejected = 0
        for _ in range(700):
            case = universe_case(rng)
            try:
                expected = oracle(case)
            except (ValueError, AssertionError):
                rejected += 1
                with self.assertRaises(ValueError):
                    audit(copy.deepcopy(case))
                continue
            valid += 1
            self.assertEqual(audit(copy.deepcopy(case)), expected)
        self.assertGreaterEqual(valid, 80)
        self.assertGreater(rejected, 50)

    def test_random_small_trees_match_full_business_report(self):
        rng = random.Random(2026111803)
        valid = rejected = 0
        for _ in range(400):
            case = random_case(rng)
            try:
                expected = oracle(case)
            except (ValueError, AssertionError):
                rejected += 1
                with self.assertRaises(ValueError):
                    audit(copy.deepcopy(case))
                continue
            valid += 1
            actual = audit(copy.deepcopy(case))
            self.assertEqual(set(actual), {'compliant', 'files',
                                           'components', 'obligations'})
            self.assertEqual(actual, expected)
        self.assertGreaterEqual(valid, 120)
        self.assertGreater(rejected, 0)

    def test_entry_permutation_is_irrelevant(self):
        rng = random.Random(77)
        for _ in range(60):
            case = random_case(rng)
            if not _ok(case):
                continue
            baseline = audit(copy.deepcopy(case))
            for layer in case['layers']:
                rng.shuffle(layer['entries'])
            self.assertEqual(audit(copy.deepcopy(case)), baseline)


class FixedCounterexamples(unittest.TestCase):
    def base(self, layers, components=None, materials=None):
        return {'policy': POLICY,
                'components': components or [
                    {'id': 'a', 'expression': 'MIT'}],
                'materials': materials or [],
                'layers': layers}

    def test_directory_file_swap_and_parent_conflict(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('x', D['x'])]},
            {'id': 'L1', 'entries': [
                _file('x/y', D['y']),  # parent x is an inherited file
            ]}])
        with self.assertRaises(ValueError):
            audit(data)
        data['layers'][1]['entries'].insert(0, {'kind': 'dir', 'path': 'x'})
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']], ['x/y'])
        # A file write replaces an inherited directory and its subtree.
        data['layers'].append({'id': 'L2', 'entries': [
            _file('x', D['z'], 'a')]})
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']], ['x'])
        self.assertEqual(result['files'][0]['sha256'], D['z'])

    def test_duplicate_and_delete_paths(self):
        duplicate_write = self.base([{'id': 'L0', 'entries': [
            _file('x', D['x']), {'kind': 'dir', 'path': 'x'}]}])
        with self.assertRaises(ValueError):
            audit(duplicate_write)
        duplicate_delete = self.base([{'id': 'L0', 'entries': [
            {'kind': 'whiteout', 'path': 'x'},
            {'kind': 'opaque', 'path': 'x'}]}])
        with self.assertRaises(ValueError):
            audit(duplicate_delete)
        # A directory and a delete marker may share one path in one layer.
        allowed = self.base([{'id': 'L0', 'entries': [
            {'kind': 'whiteout', 'path': 'x'},
            {'kind': 'file', 'path': 'x', 'sha256': D['x'],
             'component': None}]}])
        self.assertEqual([row['path'] for row in audit(allowed)['files']],
                         ['x'])

    def test_overlapping_deletes_are_idempotent(self):
        data = self.base([
            {'id': 'L0', 'entries': [
                {'kind': 'dir', 'path': 'd'},
                _file('d/a', D['x']), _file('d/b', D['y'])]},
            {'id': 'L1', 'entries': [
                {'kind': 'whiteout', 'path': 'd'},
                {'kind': 'whiteout', 'path': 'd/a'}]}])
        self.assertEqual(audit(data)['files'], [])
        # A whiteout of an ancestor cannot make a later opaque target
        # illegal: both are judged on the pre-layer tree.  Here the opaque
        # target is still the pre-layer directory, never a file.
        data['layers'][1]['entries'] = [
            {'kind': 'whiteout', 'path': 'd/a'},
            {'kind': 'opaque', 'path': 'd'}]
        self.assertEqual(audit(data)['files'], [])

    def test_opaque_missing_is_noop_and_file_is_error(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('x', D['x'])]},
            {'id': 'L1', 'entries': [{'kind': 'opaque', 'path': 'nope'}]}])
        self.assertTrue(audit(data)['files'])
        data['layers'][1]['entries'] = [
            {'kind': 'opaque', 'path': 'x'}]
        with self.assertRaises(ValueError):
            audit(data)
        # A hardlink binding is also a regular file for opaque purposes.
        data['layers'][0]['entries'].append(
            {'kind': 'hardlink', 'path': 'x2', 'target': 'x'})
        data['layers'][1]['entries'] = [
            {'kind': 'opaque', 'path': 'x2'}]
        with self.assertRaises(ValueError):
            audit(data)

    def test_delete_legality_always_uses_pre_layer_tree(self):
        # A same-layer whiteout does not make an opaque-over-file legal.
        data = self.base([
            {'id': 'L0', 'entries': [_file('x', D['x'])]},
            {'id': 'L1', 'entries': [
                {'kind': 'whiteout', 'path': 'x'},
                {'kind': 'opaque', 'path': 'x'}]}])
        with self.assertRaises(ValueError):
            audit(data)
        # Conversely, whiteouting an inherited directory does not make a
        # nested opaque target legal when that path itself held a regular
        # file before the layer: legality is always judged on the pre-layer
        # tree, so the opaque-over-file error still fires.
        data = self.base([
            {'id': 'L0', 'entries': [
                {'kind': 'dir', 'path': 'd'}, _file('d/a', D['x'])]},
            {'id': 'L1', 'entries': [
                {'kind': 'whiteout', 'path': 'd'},
                {'kind': 'opaque', 'path': 'd/a'}]}])
        with self.assertRaises(ValueError):
            audit(data)
        # An opaque target that was a directory before the layer is legal,
        # even when an overlapping whiteout detaches it first.
        data['layers'][0]['entries'] = [
            {'kind': 'dir', 'path': 'd'}, {'kind': 'dir', 'path': 'd/a'},
            _file('d/a/f', D['x'])]
        self.assertEqual(audit(data)['files'], [])

    def test_link_to_inherited_link_keeps_original_origin(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('base', D['x'], 'a')]},
            {'id': 'L1', 'entries': [
                {'kind': 'hardlink', 'path': 'h1', 'target': 'base'}]},
            {'id': 'L2', 'entries': [
                {'kind': 'hardlink', 'path': 'h2', 'target': 'h1'}]}])
        rows = {row['path']: row for row in audit(data)['files']}
        for path in ('h1', 'h2'):
            self.assertEqual(rows[path]['origin'],
                             {'layer': 'L0', 'path': 'base'})
            self.assertEqual(rows[path]['sha256'], D['x'])

    def test_same_layer_forward_chain_and_cycles(self):
        data = self.base([{'id': 'L0', 'entries': [
            _file('base', D['x']),
            {'kind': 'hardlink', 'path': 'p0', 'target': 'p1'},
            {'kind': 'hardlink', 'path': 'p1', 'target': 'p2'},
            {'kind': 'hardlink', 'path': 'p2', 'target': 'base'}]}])
        result = audit(data)
        self.assertEqual({row['path'] for row in result['files']},
                         {'base', 'p0', 'p1', 'p2'})
        for row in result['files']:
            self.assertEqual(row['origin'], {'layer': 'L0', 'path': 'base'})
        for entries in (
                [{'kind': 'hardlink', 'path': 'a', 'target': 'a'}],
                [{'kind': 'hardlink', 'path': 'a', 'target': 'b'},
                 {'kind': 'hardlink', 'path': 'b', 'target': 'a'}],
                [{'kind': 'hardlink', 'path': 'a', 'target': 'missing'}],
                [{'kind': 'hardlink', 'path': 'a', 'target': 'd'},
                 {'kind': 'dir', 'path': 'd'}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                audit(self.base([{'id': 'L0', 'entries': entries}]))

    def test_hardlink_does_not_follow_later_overwrites(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('x', D['x'], 'a')]},
            {'id': 'L1', 'entries': [
                {'kind': 'hardlink', 'path': 'y', 'target': 'x'}]},
            {'id': 'L2', 'entries': [
                _file('x', D['y'], 'a'),  # overwrite the target path
                {'kind': 'whiteout', 'path': 'x'}]}])
        result = audit(data)
        rows = {row['path']: row for row in result['files']}
        self.assertEqual(rows['y']['sha256'], D['x'])
        self.assertEqual(rows['y']['origin'], {'layer': 'L0', 'path': 'x'})
        # The path x itself was re-created by this layer with new content.
        self.assertEqual(rows['x']['sha256'], D['y'])
        self.assertEqual(rows['x']['origin'], {'layer': 'L2', 'path': 'x'})

    def test_hardlink_old_binding_is_removed_before_resolution(self):
        # The inherited object at a link path never serves as its own target
        # namespace: the previous binding is detached before the layer's
        # links resolve.  Here the old directory at "p" would otherwise
        # contain the target "p/x".
        data = self.base([
            {'id': 'L0', 'entries': [
                {'kind': 'dir', 'path': 'p'}, _file('p/x', D['x'])]},
            {'id': 'L1', 'entries': [
                {'kind': 'hardlink', 'path': 'p', 'target': 'p/x'}]}])
        with self.assertRaises(ValueError):
            audit(data)
        # A target outside the detached subtree still resolves normally.
        data['layers'][1]['entries'] = [
            {'kind': 'hardlink', 'path': 'p', 'target': 'elsewhere'}]
        data['layers'][1]['entries'].append(_file('elsewhere', D['y'], 'a'))
        result = audit(data)
        rows = {row['path']: row for row in result['files']}
        self.assertEqual(rows['p']['sha256'], D['y'])
        self.assertEqual(rows['p']['origin'],
                         {'layer': 'L1', 'path': 'elsewhere'})

    def test_same_digest_different_components_are_distinct_objects(self):
        data = self.base([
            {'id': 'L0', 'entries': [
                _file('a', D['x'], 'a'),
                _file('b', D['x'], 'b')]}],
            components=[{'id': 'a', 'expression': 'MIT'},
                        {'id': 'b', 'expression': 'MIT'}])
        result = audit(data)
        rows = {row['path']: row for row in result['files']}
        self.assertEqual(rows['a']['origin'], {'layer': 'L0', 'path': 'a'})
        self.assertEqual(rows['b']['origin'], {'layer': 'L0', 'path': 'b'})
        self.assertEqual([row['id'] for row in result['components']],
                         ['a', 'b'])

    def test_with_key_unknown_license_and_unexposed_blocked(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('a', D['x'], 'a')]}],
            components=[{'id': 'a', 'expression': 'MIT WITH Exception'},
                        {'id': 'b', 'expression': 'GPL-3.0-only'},
                        {'id': 'c', 'expression': 'BSD-9.0'}])
        result = audit(data)
        self.assertEqual([row['id'] for row in result['components']], ['a'])
        self.assertEqual(result['components'][0]['status'], 'allowed')
        self.assertEqual(result['components'][0]['selected'],
                         {'licenses': ['MIT WITH Exception'],
                          'obligations': ['notice']})
        # A component with an unknown licence that is never exposed is still
        # valid input: blocked is a business state, not a parse error.  Its
        # absence leaves only "a", whose notice has no declared material.
        self.assertEqual([row['id'] for row in result['components']], ['a'])
        self.assertFalse(result['compliant'])
        self.assertEqual(result['obligations'],
                         [{'obligation': 'notice', 'components': ['a'],
                           'satisfied': False, 'paths': []}])

    def test_no_materials_means_unsatisfied(self):
        data = self.base([{'id': 'L0', 'entries': [
            _file('a', D['x'], 'a')]}])
        result = audit(data)
        self.assertEqual(result['obligations'],
                         [{'obligation': 'notice', 'components': ['a'],
                           'satisfied': False, 'paths': []}])
        self.assertFalse(result['compliant'])

    def test_empty_exposure_is_compliant_and_lists_visible_files(self):
        data = self.base([
            {'id': 'L0', 'entries': [_file('m', GOOD, None)]},
            {'id': 'L1', 'entries': []}])
        result = audit(data)
        self.assertTrue(result['compliant'])
        self.assertEqual(result['components'], [])
        self.assertEqual(result['obligations'], [])
        self.assertEqual([row['path'] for row in result['files']], ['m'])

    def test_invalid_expression_rejected_even_when_unexposed(self):
        data = self.base([{'id': 'L0', 'entries': []}],
                         components=[{'id': 'ghost', 'expression': 'MIT OR'}])
        with self.assertRaises(ValueError):
            audit(data)


class CliFailureProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', *map(str, arguments)],
            capture_output=True, text=True,
            env={'PYTHONPATH': str(ROOT / 'src')})

    def write(self, name, obj, raw=None):
        target = self.dir / name
        if raw is not None:
            target.write_bytes(raw)
        else:
            target.write_text(json.dumps(obj))
        return target

    def test_error_exit_2_and_byte_identical_old_report(self):
        report = self.dir / 'report.json'
        report.write_text('old report bytes\n')
        original = report.read_bytes()
        cases = [
            (b'{"policy": []}', None),
            (b'{"policy":[],"policy":[],"components":[],"materials":[],'
             b'"layers":[]}', None),
            (b'{"policy":[],"components":[],"materials":[],"layers":[],'
             b'"extra":1}', None),
            (None, {'policy': [], 'components': [
                {'id': 'g', 'expression': 'MIT OR'}],
                'materials': [], 'layers': []}),
            (None, {'policy': [], 'components': [], 'materials': [],
                    'layers': [{'id': 'L', 'entries': [
                        {'kind': 'hardlink', 'path': 'a',
                         'target': 'a'}]}]}),
        ]
        for index, (raw, obj) in enumerate(cases):
            with self.subTest(index=index):
                if raw is not None:
                    source = self.write('bad%d.json' % index, None, raw)
                else:
                    source = self.write('bad%d.json' % index, obj)
                result = self.cli('--layers', source, '--output', report)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(report.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
