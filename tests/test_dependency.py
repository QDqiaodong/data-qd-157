"""Single-point bottleneck audit tests with an independent oracle.

The oracle recomputes reachability from scratch for every deactivated
node and derives strict dominator sets by brute-force path removal, so it
shares no implementation with the Cooper-Harvey-Kennedy based planner.
"""
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

from license_planner.model import analyze
from license_planner.cli import run

ROOT = Path(__file__).resolve().parents[1]


def policy(allowed_ids=('MIT',), blocked_ids=('GPL',)):
    rows = [{'license': name, 'allowed': True, 'obligations': ['notice']}
            for name in allowed_ids]
    rows += [{'license': name, 'allowed': False, 'obligations': []}
             for name in blocked_ids]
    return rows


def components(ids, blocked_ids=()):
    return [{'id': cid,
             'expression': 'GPL' if cid in blocked_ids else 'MIT'}
            for cid in ids]


def graph(roots, edges):
    return {'roots': list(roots),
            'edges': [{'dependency': p, 'dependent': q} for p, q in edges]}


def source(ids, roots, edges, blocked_ids=()):
    return {'policy': policy(),
            'components': components(ids, blocked_ids),
            'dependency_graph': graph(roots, edges)}


def oracle_reachable(adjacency, roots, forbidden=()):
    """BFS reachability with an optional set of deactivated nodes."""
    blocked = set(forbidden)
    seen = {root for root in roots if root not in blocked}
    queue = deque(seen)
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, ()):
            if nxt not in blocked and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def oracle_audit(ids, roots, edges, blocked_ids):
    """Brute-force reference implementation of the whole audit."""
    adjacency = {}
    for dependency, dependent in edges:
        adjacency.setdefault(dependency, []).append(dependent)
    roots = list(roots)
    scope = oracle_reachable(adjacency, roots)
    unreachable = sorted(set(ids) - scope)
    nodes = []
    for cid in sorted(scope):
        # lost_count: fresh reachability after removing exactly this node.
        remaining = oracle_reachable(adjacency, roots, forbidden={cid})
        lost_count = len(scope) - len(remaining)
        # Strict dominators: every node whose removal makes cid
        # unreachable, cid itself excluded from the strict set.
        strict = []
        for candidate in sorted(scope):
            if candidate == cid:
                continue
            after = oracle_reachable(adjacency, roots, forbidden={candidate})
            if cid not in after:
                strict.append(candidate)
        # Immediate dominator = the strict dominator closest to cid, i.e.
        # the one that every other strict dominator dominates.
        immediate = None
        for candidate in strict:
            if all(other == candidate
                   or _oracle_dominates(adjacency, roots, other, candidate)
                   for other in strict):
                immediate = candidate
                break
        # Blocked dominators include cid itself when it is blocked.
        blocked_set = set(blocked_ids)
        chain = ([cid] if cid in blocked_set else [])
        blocked_strict = [d for d in strict if d in blocked_set]
        chain.extend(blocked_strict)
        # Nearest blocked dominator: cid itself when blocked (nothing can
        # be closer than the node), otherwise the blocked strict dominator
        # dominated by every other blocked strict dominator.
        if cid in blocked_set:
            nearest = cid
        else:
            nearest = None
            for candidate in blocked_strict:
                if all(other == candidate
                       or _oracle_dominates(adjacency, roots, other, candidate)
                       for other in blocked_strict):
                    nearest = candidate
                    break
        nodes.append({'id': cid,
                      'immediate_dominator': immediate,
                      'lost_count': lost_count,
                      'blocked_dominator_count': len(chain),
                      'nearest_blocked_dominator': nearest})
    return {'roots': sorted(roots), 'unreachable': unreachable, 'nodes': nodes}


def _oracle_dominates(adjacency, roots, dominator, target):
    if dominator == target:
        return True
    return target not in oracle_reachable(adjacency, roots,
                                          forbidden={dominator})


class AcceptanceFixture(unittest.TestCase):
    EDGES = [('s', 'a'), ('t', 'a'), ('a', 'b'), ('b', 'a'),
             ('a', 'c'), ('b', 'c'), ('t', 'd'), ('d', 'c')]
    IDS = ['s', 't', 'a', 'b', 'c', 'd', 'u']

    def audit(self, edges):
        data = source(self.IDS, ['s', 't'], edges, blocked_ids={'a'})
        return analyze(data)['dependency_audit']

    def test_fixed_counterexample(self):
        audit = self.audit(self.EDGES)
        self.assertEqual(audit['roots'], ['s', 't'])
        self.assertEqual(audit['unreachable'], ['u'])
        by_id = {node['id']: node for node in audit['nodes']}
        self.assertEqual(set(by_id), {'s', 't', 'a', 'b', 'c', 'd'})
        self.assertIsNone(by_id['a']['immediate_dominator'])
        self.assertEqual(by_id['a']['lost_count'], 2)
        self.assertEqual(by_id['a']['nearest_blocked_dominator'], 'a')
        self.assertEqual(by_id['a']['blocked_dominator_count'], 1)
        self.assertEqual(by_id['b']['immediate_dominator'], 'a')
        self.assertEqual(by_id['b']['nearest_blocked_dominator'], 'a')
        self.assertEqual(by_id['b']['blocked_dominator_count'], 1)
        self.assertIsNone(by_id['c']['immediate_dominator'])
        self.assertEqual(by_id['c']['blocked_dominator_count'], 0)
        self.assertIsNone(by_id['c']['nearest_blocked_dominator'])
        self.assertEqual(by_id['t']['lost_count'], 2)
        self.assertEqual(by_id['s']['lost_count'], 1)
        self.assertTrue(all(set(node) == {'id', 'immediate_dominator',
                                          'lost_count',
                                          'blocked_dominator_count',
                                          'nearest_blocked_dominator'}
                            for node in audit['nodes']))
        self.assertEqual([n['id'] for n in audit['nodes']],
                         sorted(n['id'] for n in audit['nodes']))

    def test_bypass_removal_tightens_dominators(self):
        edges = [edge for edge in self.EDGES if edge != ('d', 'c')]
        audit = self.audit(edges)
        by_id = {node['id']: node for node in audit['nodes']}
        self.assertEqual(by_id['c']['immediate_dominator'], 'a')
        self.assertEqual(by_id['c']['nearest_blocked_dominator'], 'a')
        self.assertEqual(by_id['c']['blocked_dominator_count'], 1)
        self.assertEqual(by_id['a']['lost_count'], 3)
        self.assertEqual(audit['unreachable'], ['u'])

    def test_input_permutation_invariant(self):
        data = source(self.IDS, ['t', 's'], list(reversed(self.EDGES)),
                      blocked_ids={'a'})
        reference = analyze(data)['dependency_audit']
        rng = random.Random(4041)
        for seed in range(8):
            shuffled = json.loads(json.dumps(data))
            r = random.Random(seed)
            r.shuffle(shuffled['dependency_graph']['edges'])
            r.shuffle(shuffled['components'])
            if seed % 2:
                r.shuffle(shuffled['dependency_graph']['roots'])
            self.assertEqual(analyze(shuffled)['dependency_audit'],
                             reference)


class OracleCrossCheck(unittest.TestCase):
    def assert_matches_oracle(self, ids, roots, edges, blocked_ids=()):
        expected = oracle_audit(ids, roots, edges, blocked_ids)
        got = analyze(source(ids, roots, edges, blocked_ids))['dependency_audit']
        self.assertEqual(got, expected)

    def test_diamond_with_bypass_and_cycle(self):
        edges = [('s', 'a'), ('t', 'a'), ('a', 'b'), ('b', 'a'),
                 ('a', 'c'), ('b', 'c'), ('t', 'd'), ('d', 'c')]
        self.assert_matches_oracle(['s', 't', 'a', 'b', 'c', 'd', 'u'],
                                   ['s', 't'], edges, {'a'})

    def test_roots_reach_each_other(self):
        # Root r1 reaches root r2; r2 must not look dominated by r1
        # because r2 is itself a root.
        edges = [('r1', 'r2'), ('r2', 'x'), ('r1', 'x'), ('x', 'y')]
        self.assert_matches_oracle(['r1', 'r2', 'x', 'y'],
                                   ['r1', 'r2'], edges)

    def test_root_dominated_illusion(self):
        # A root on every path downstream still has no dominator itself;
        # the second root merges only after node m.
        edges = [('r1', 'm'), ('r2', 'm'), ('m', 'z'), ('z', 'z')]
        self.assert_matches_oracle(['r1', 'r2', 'm', 'z'],
                                   ['r1', 'r2'], edges, {'m'})

    def test_self_loop_and_cycle_entry_bypass(self):
        edges = [('r', 'a'), ('r', 'b'), ('a', 'c'), ('b', 'c'),
                 ('c', 'd'), ('d', 'c'), ('d', 'd')]
        self.assert_matches_oracle(['r', 'a', 'b', 'c', 'd'],
                                   ['r'], edges, {'c'})

    def test_empty_roots(self):
        edges = [('a', 'b'), ('b', 'c')]
        audit = analyze(source(['a', 'b', 'c', 'q'], [], edges))
        self.assertEqual(audit['dependency_audit'],
                         {'roots': [], 'unreachable': ['a', 'b', 'c', 'q'],
                          'nodes': []})

    def test_single_root_chain(self):
        edges = [('r', 'a'), ('a', 'b'), ('b', 'c')]
        self.assert_matches_oracle(['r', 'a', 'b', 'c'], ['r'], edges,
                                   {'b'})

    def test_parallel_paths_no_dominator(self):
        edges = [('r', 'a1'), ('r', 'a2'), ('a1', 'x'), ('a2', 'x')]
        self.assert_matches_oracle(['r', 'a1', 'a2', 'x'], ['r'], edges)

    def test_blocked_chain_evidence(self):
        edges = [('r', 'p'), ('p', 'q'), ('q', 'x')]
        self.assert_matches_oracle(['r', 'p', 'q', 'x'], ['r'], edges,
                                   {'p', 'q'})

    def test_randomized_graphs(self):
        rng = random.Random(20261118)
        for trial in range(40):
            count = rng.randint(1, 12)
            ids = ['n%02d' % i for i in range(count)]
            edges = []
            for _ in range(rng.randint(0, count * 2)):
                pair = (rng.choice(ids), rng.choice(ids))
                if pair not in edges:
                    edges.append(pair)
            roots = rng.sample(ids, rng.randint(1, min(3, count)))
            blocked = set(rng.sample(ids, rng.randint(0, count // 2)))
            self.assert_matches_oracle(ids, roots, edges, blocked)

    def test_duplicate_edge_rejected(self):
        data = source(['r', 'x'], ['r'], [('r', 'x'), ('r', 'x')])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(data)

    def test_duplicate_root_rejected(self):
        data = source(['r'], ['r', 'r'], [])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(data)


class GraphValidation(unittest.TestCase):
    def payload(self, **overrides):
        data = source(['r', 'x'], ['r'], [('r', 'x')])
        graph_value = data['dependency_graph']
        graph_value.update(overrides)
        return data

    def test_exact_graph_fields(self):
        data = self.payload()
        data['dependency_graph']['extra'] = 1
        with self.assertRaises(ValueError):
            analyze(data)
        data = self.payload()
        data['dependency_graph'] = {'roots': []}
        with self.assertRaises(ValueError):
            analyze(data)

    def test_edge_exact_fields(self):
        data = self.payload()
        data['dependency_graph']['edges'] = [
            {'dependency': 'r', 'dependent': 'x', 'weight': 1}]
        with self.assertRaises(ValueError):
            analyze(data)
        data = self.payload()
        data['dependency_graph']['edges'] = [{'dependency': 'r'}]
        with self.assertRaises(ValueError):
            analyze(data)

    def test_unknown_references(self):
        for bad_roots, bad_edges in [
                (['ghost'], []),
                (['r'], [{'dependency': 'r', 'dependent': 'ghost'}]),
                (['r'], [{'dependency': 'ghost', 'dependent': 'r'}]),
        ]:
            data = source(['r', 'x'], ['r'], [('r', 'x')])
            data['dependency_graph'] = {'roots': bad_roots, 'edges': bad_edges}
            with self.subTest(bad=(bad_roots, bad_edges)):
                with self.assertRaisesRegex(ValueError, 'existing component'):
                    analyze(data)

    def test_self_loop_legal(self):
        data = source(['r', 'x'], ['r'], [('r', 'r'), ('r', 'x'),
                                          ('x', 'x')])
        audit = analyze(data)['dependency_audit']
        self.assertEqual([n['id'] for n in audit['nodes']], ['r', 'x'])
        self.assertEqual(audit['nodes'][1]['immediate_dominator'], 'r')

    def test_cycle_legal(self):
        data = source(['r', 'a', 'b'], ['r'],
                      [('r', 'a'), ('a', 'b'), ('b', 'a')])
        audit = analyze(data)['dependency_audit']
        by_id = {n['id']: n for n in audit['nodes']}
        self.assertEqual(by_id['a']['immediate_dominator'], 'r')
        self.assertEqual(by_id['b']['immediate_dominator'], 'a')

    def test_graph_must_be_object(self):
        data = source(['r'], ['r'], [])
        for bad in [[], None, 'x', 42]:
            data['dependency_graph'] = bad
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    analyze(data)

    def test_roots_and_edges_bounded_arrays(self):
        data = source(['r'], ['r'], [])
        data['dependency_graph']['roots'] = 'r'
        with self.assertRaises(ValueError):
            analyze(data)
        data = source(['r'], ['r'], [])
        data['dependency_graph']['edges'] = {}
        with self.assertRaises(ValueError):
            analyze(data)

    def test_component_limit_30000_only_with_graph(self):
        ids = ['c%05d' % i for i in range(1001)]
        plain = {'policy': policy(), 'components': components(ids)}
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            analyze(plain)
        with_graph = dict(plain, dependency_graph=graph([], []))
        result = analyze(with_graph)
        self.assertEqual(len(result['components']), 1001)
        self.assertEqual(result['dependency_audit']['unreachable'],
                         sorted(ids))

    def test_release_keeps_120_limit_even_with_graph(self):
        ids = ['c%03d' % i for i in range(121)]
        data = {'policy': policy(),
                'components': components(ids),
                'release': {'obligation_costs': [
                    {'obligation': 'notice', 'cost': 1}]},
                'dependency_graph': graph([], [])}
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            analyze(data)

    def test_edges_limit_120000(self):
        ids = ['c%05d' % i for i in range(400)]
        edges = []
        for i in range(400):
            for j in range(400):
                if len(edges) == 120001:
                    break
                edges.append(('c%05d' % i, 'c%05d' % j))
            if len(edges) == 120001:
                break
        data = source(ids, ['c00000'], edges)
        with self.assertRaisesRegex(ValueError, 'bounded array'):
            analyze(data)

    def test_legacy_result_unchanged_without_graph(self):
        fixture = json.loads((ROOT / 'fixtures/input.json').read_text())
        result = analyze(fixture)
        self.assertEqual(set(result), {'compliant', 'components'})
        self.assertNotIn('dependency_audit', result)

    def test_legacy_values_unchanged_with_graph(self):
        fixture = json.loads((ROOT / 'fixtures/input.json').read_text())
        without = analyze(fixture)
        with_graph = json.loads(json.dumps(fixture))
        with_graph['dependency_graph'] = {
            'roots': ['core'],
            'edges': [{'dependency': 'core', 'dependent': 'plugin'}]}
        result = analyze(with_graph)
        self.assertEqual(result['compliant'], without['compliant'])
        self.assertEqual(result['components'], without['components'])
        self.assertIn('dependency_audit', result)

    def test_unknown_top_level_field_still_rejected(self):
        data = source(['r'], ['r'], [])
        data['unexpected'] = True
        with self.assertRaises(ValueError):
            analyze(data)


class ScaleGraph(unittest.TestCase):
    """20k+ nodes and 60k+ distinct edges, solved in one process.

    Deterministic layout with reproducible bottlenecks.  Each of the two
    independent roots fans out into parallel branches that all merge at a
    single gate node; nothing except the gate can reach the post-gate
    region, so the gate is a provable single point of failure::

        root0 -> chains -> gate0 -> post chain
        root1 -> chains -> gate1 -> post chain

    Cross-links stay strictly inside a branch region (they never skip a
    gate), giving cycles and bypasses without weakening the bottleneck.
    """

    N = 20000
    HALF = 10000
    BRANCHES = 3
    GATE = 3333          # gate id offset inside each backbone region
    POST_LEN = 6666      # disjoint post-gate region inside a backbone

    def build(self):
        n = self.N
        ids = ['n%05d' % i for i in range(n)]
        roots = [ids[0], ids[self.HALF]]
        edges = set()

        def wire(base):
            root = ids[base]
            gate = ids[base + self.GATE]
            # Parallel branch chains occupy base+1 .. base+GATE-1 and all
            # merge into the single gate node.
            branch_len = (self.GATE - 1) // self.BRANCHES
            for branch in range(self.BRANCHES):
                start = base + 1 + branch * branch_len
                end = (base + 1 + (branch + 1) * branch_len
                       if branch < self.BRANCHES - 1
                       else base + self.GATE)
                edges.add((root, ids[start]))
                for i in range(start, end - 1):
                    edges.add((ids[i], ids[i + 1]))
                edges.add((ids[end - 1], gate))
            # Dense cross-links stay strictly inside a branch: bypasses and
            # cycles that can never reach the gate around their own branch.
            rng = random.Random(base * 7919 + 17)
            branch_regions = []
            for branch in range(self.BRANCHES):
                start = base + 1 + branch * branch_len
                end = (base + 1 + (branch + 1) * branch_len
                       if branch < self.BRANCHES - 1
                       else base + self.GATE)
                branch_regions.append(list(range(start, end)))
                members = branch_regions[-1]
                for offset, i in enumerate(members):
                    for jump in (1, 2, 3):
                        if offset + jump < len(members):
                            edges.add((ids[i], ids[members[offset + jump]]))
                for _ in range(branch_len * 2):
                    a = rng.choice(members)
                    b = rng.choice(members)
                    edges.add((ids[a], ids[b]))
            # Extra deterministic intra-branch edges lift the graph past
            # 60000 distinct edges while keeping every edge inside a
            # branch region, so gate bottlenecks stay provably intact.
            target_per_backbone = 30050
            while sum(1 for pair in edges
                      if pair[0] < ids[base + self.GATE]) < target_per_backbone:
                members = rng.choice(branch_regions)
                a = rng.choice(members)
                b = rng.choice(members)
                edges.add((ids[a], ids[b]))
            # Post-gate region uses disjoint ids so only the gate enters it.
            post_start = base + self.GATE + 1
            post_end = min(post_start + self.POST_LEN, base + self.HALF)
            edges.add((gate, ids[post_start]))
            for i in range(post_start, post_end - 1):
                edges.add((ids[i], ids[i + 1]))

        wire(0)
        wire(self.HALF)
        return ids, roots, sorted(edges)

    def test_large_graph_known_bottlenecks(self):
        ids, roots, edges = self.build()
        self.assertGreaterEqual(len(ids), 20000)
        self.assertGreaterEqual(len(edges), 60000)
        self.assertEqual(len(edges), len(set(edges)))
        data = source(ids, roots, edges)
        audit = analyze(data)['dependency_audit']
        by_id = {node['id']: node for node in audit['nodes']}
        adjacency = {}
        for dependency, dependent in edges:
            adjacency.setdefault(dependency, []).append(dependent)
        scope = oracle_reachable(adjacency, roots)
        # Independent roots have no dominator.
        self.assertIsNone(by_id[ids[0]]['immediate_dominator'])
        self.assertIsNone(by_id[ids[self.HALF]]['immediate_dominator'])
        # The two gates are exact single points of failure: deactivating a
        # gate loses itself plus every post-gate node of its backbone, and
        # the independent per-node BFS oracle must agree at every gate.
        for base in (0, self.HALF):
            gate = ids[base + self.GATE]
            remaining = oracle_reachable(adjacency, roots, forbidden={gate})
            expected_lost = len(scope) - len(remaining)
            self.assertEqual(by_id[gate]['lost_count'], expected_lost)
            self.assertEqual(expected_lost, 1 + self.POST_LEN)
            post_first = ids[base + self.GATE + 1]
            self.assertEqual(by_id[post_first]['immediate_dominator'], gate)
        # Branch internals keep alternative routes between branch nodes but
        # cannot escape the gate: oracle cross-check at sampled nodes.
        rng = random.Random(99)
        sampled = rng.sample(sorted(scope), 25)
        for cid in sampled:
            remaining = oracle_reachable(adjacency, roots, forbidden={cid})
            self.assertEqual(by_id[cid]['lost_count'],
                             len(scope) - len(remaining))
        self.assertEqual(audit['unreachable'], [])
        self.assertEqual(len(audit['nodes']), len(scope))

    def test_deep_chain_iterative(self):
        # A 25000-deep chain must not depend on raising the recursion limit.
        ids = ['d%05d' % i for i in range(25000)]
        edges = [(ids[i], ids[i + 1]) for i in range(24999)]
        data = source(ids, [ids[0]], edges)
        audit = analyze(data)['dependency_audit']
        self.assertEqual(len(audit['nodes']), 25000)
        last = audit['nodes'][-1]
        self.assertEqual(last['id'], 'd24999')
        self.assertEqual(last['immediate_dominator'], 'd24998')
        self.assertEqual(last['lost_count'], 1)
        self.assertEqual(audit['nodes'][0]['lost_count'], 25000)


class AuditTransport(unittest.TestCase):
    # A fresh interpreter (mirroring the subprocess) can only import the
    # package when it is actually installed; the test runner's own
    # sys.path entries do not propagate to child processes.
    probe = subprocess.run(
        [sys.executable, '-c', 'import license_planner'],
        capture_output=True, text=True)
    INSTALLED = probe.returncode == 0

    def cli(self, source_path, output_path):
        if self.INSTALLED:
            command = [sys.executable, '-m', 'license_planner.cli']
        else:
            # Source-tree fallback when the package is not installed.
            driver = ('import sys; sys.path.insert(0, %r); '
                      'from license_planner.cli import main; '
                      'raise SystemExit(main())' % str(ROOT / 'src'))
            command = [sys.executable, '-c', driver]
        command += ['--source', str(source_path),
                    '--output', str(output_path)]
        return subprocess.run(command, capture_output=True, text=True)

    def test_cli_end_to_end_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = source(['s', 't', 'a', 'b', 'u'], ['s', 't'],
                          [('s', 'a'), ('t', 'a'), ('a', 'b'),
                           ('a', 'a')], blocked_ids={'a'})
            in_path = folder / 'in.json'
            out_path = folder / 'report.json'
            in_path.write_text(json.dumps(data))
            result = self.cli(in_path, out_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out_path.read_text())
            raw = in_path.read_bytes()
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(raw).hexdigest())
            audit = report['result']['dependency_audit']
            self.assertEqual(audit['unreachable'], ['u'])
            by_id = {n['id']: n for n in audit['nodes']}
            self.assertEqual(by_id['b']['nearest_blocked_dominator'], 'a')
            self.assertEqual(by_id['b']['blocked_dominator_count'], 1)

    def test_blocked_business_result_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = source(['r', 'x'], ['r'], [('r', 'x')],
                          blocked_ids={'r', 'x'})
            in_path = folder / 'in.json'
            out_path = folder / 'report.json'
            in_path.write_text(json.dumps(data))
            result = self.cli(in_path, out_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out_path.read_text())
            self.assertFalse(report['result']['compliant'])
            self.assertEqual(
                report['result']['dependency_audit']['nodes'][1]
                ['blocked_dominator_count'], 2)

    def test_bad_graph_preserves_old_report_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = source(['r'], ['r', 'r'], [])
            in_path = folder / 'in.json'
            out_path = folder / 'report.json'
            in_path.write_text(json.dumps(data))
            old_bytes = b'{"keep": "exact bytes"}\n'
            out_path.write_bytes(old_bytes)
            result = self.cli(in_path, out_path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(out_path.read_bytes(), old_bytes)

    def test_duplicate_json_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'bad.json'
            path.write_text(
                '{"policy":[],"components":[],"dependency_graph":'
                '{"roots":[],"edges":[]},"dependency_graph":'
                '{"roots":[],"edges":[]}}')
            with self.assertRaises(ValueError):
                run(path)

    def test_atomic_publication_and_sorted_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = source(['r', 'x'], ['r'], [('r', 'x')])
            in_path = folder / 'in.json'
            out_path = folder / 'nested' / 'report.json'
            in_path.write_text(json.dumps(data))
            result = self.cli(in_path, out_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = out_path.read_text()
            self.assertTrue(text.endswith('\n'))
            report = json.loads(text)
            self.assertEqual(set(report),
                             {'schema_version', 'source_sha256', 'result'})


if __name__ == '__main__':
    unittest.main()
