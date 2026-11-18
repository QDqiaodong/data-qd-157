"""Cross-version lineage reconciliation tests with an independent oracle.

The oracle enumerates *every* partial one-to-one matching for small inputs,
selects the exact two-level optimum (maximum cardinality, then minimum
penalty) and derives all three edge classes from the whole optimal set --
it never reuses the flow/SCC implementation under test.
"""
import copy
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from license_planner.model import analyze
from license_planner.expression import alternatives, parse
from license_planner.cli import run

ROOT = Path(__file__).resolve().parents[1]
AUDIT_KEYS = {'matched_count', 'minimum_penalty', 'links',
              'always_unmatched_baseline', 'always_unmatched_current',
              'transitions'}
LINK_KEYS = {'baseline_id', 'current_id', 'penalty', 'classification'}
TRANSITION_KEYS = {'baseline_id', 'current_id',
                   'baseline_status', 'current_status'}


def policy(entries):
    return [{'license': lic, 'allowed': ok, 'obligations': list(obs)}
            for lic, ok, obs in entries]


def basic_policy():
    return policy([('MIT', True, ['notice']),
                   ('Apache-2.0', True, ['notice']),
                   ('GPL-3.0-only', False, []),
                   ('GPL-2.0-only WITH Classpath-exception-2.0', False, [])])


def source(baseline_components, links, current_components=None,
           baseline_policy=None, current_policy=None, extra=None):
    data = {
        'policy': current_policy if current_policy is not None else basic_policy(),
        'components': current_components if current_components is not None else [
            {'id': 'x', 'expression': 'MIT'},
            {'id': 'y', 'expression': 'GPL-3.0-only'},
        ],
        'reconciliation': {
            'baseline': {
                'policy': baseline_policy if baseline_policy is not None
                else basic_policy(),
                'components': baseline_components,
            },
            'links': [{'baseline_id': b, 'current_id': c, 'penalty': p}
                      for b, c, p in links],
        },
    }
    if extra:
        data.update(extra)
    return data


def components(pairs):
    return [{'id': cid, 'expression': expression} for cid, expression in pairs]


def optimal_matchings(links, baseline_ids, current_ids):
    """Exhaustively enumerate all one-to-one matchings and the optimum."""
    adjacency = {}
    for baseline_id, current_id, penalty in links:
        adjacency.setdefault(baseline_id, []).append((current_id, penalty))
    ordered = sorted(baseline_ids)
    found = []

    def visit(index, used, chosen, penalty):
        if index == len(ordered):
            found.append((len(chosen), penalty, frozenset(chosen)))
            return
        baseline_id = ordered[index]
        visit(index + 1, used, chosen, penalty)
        for current_id, edge_penalty in adjacency.get(baseline_id, ()):
            if current_id in used:
                continue
            used.add(current_id)
            chosen.append((baseline_id, current_id))
            visit(index + 1, used, chosen, penalty + edge_penalty)
            chosen.pop()
            used.remove(current_id)

    visit(0, set(), [], 0)
    best_count = max(item[0] for item in found)
    best_penalty = min(item[1] for item in found if item[0] == best_count)
    return [item[2] for item in found
            if item[0] == best_count and item[1] == best_penalty]


def oracle_audit(links, baseline_status, current_status):
    """Reference audit derived from the full set of optimal matchings."""
    baseline_ids = sorted(baseline_status)
    current_ids = sorted(current_status)
    optimal = optimal_matchings(links, baseline_ids,
                                current_ids) if links else [frozenset()]
    used_pairs = set().union(*optimal) if optimal else set()
    matched_baseline = {b for b, _ in used_pairs}
    matched_current = {c for _, c in used_pairs}
    cardinality = len(optimal[0]) if optimal else 0
    minimum_penalty = (sum(penalty for b, c, penalty in links
                           if (b, c) in optimal[0]) if optimal else 0)

    rows = []
    for baseline_id, current_id, penalty in sorted(
            links, key=lambda item: (item[0], item[1])):
        pair = (baseline_id, current_id)
        occurrences = sum(1 for matching in optimal if pair in matching)
        if occurrences == len(optimal) and occurrences:
            classification = 'forced'
        elif occurrences:
            classification = 'possible'
        else:
            classification = 'excluded'
        rows.append({'baseline_id': baseline_id, 'current_id': current_id,
                     'penalty': penalty,
                     'classification': classification})
    transitions = [{
        'baseline_id': row['baseline_id'],
        'current_id': row['current_id'],
        'baseline_status': baseline_status[row['baseline_id']],
        'current_status': current_status[row['current_id']],
    } for row in rows if row['classification'] == 'forced']
    return {
        'matched_count': cardinality,
        'minimum_penalty': minimum_penalty,
        'links': rows,
        'always_unmatched_baseline': [b for b in baseline_ids
                                      if b not in matched_baseline],
        'always_unmatched_current': [c for c in current_ids
                                     if c not in matched_current],
        'transitions': transitions,
    }


def statuses_from(data):
    """Per-side allowed/blocked from the existing SPDX judgement."""
    baseline_policy = {r['license']: r['allowed']
                       for r in data['reconciliation']['baseline']['policy']}
    current_policy = {r['license']: r['allowed'] for r in data['policy']}

    def status(expression, policy_table):
        for choice in alternatives(parse(expression)):
            if all(policy_table.get(key) is True for key in choice):
                return 'allowed'
        return 'blocked'

    baseline = {row['id']: status(row['expression'], baseline_policy)
                for row in data['reconciliation']['baseline']['components']}
    current = {row['id']: status(row['expression'], current_policy)
               for row in data['components']}
    return baseline, current


def links_of(data):
    return [(row['baseline_id'], row['current_id'], row['penalty'])
            for row in data['reconciliation']['links']]


class OracleAgreement(unittest.TestCase):
    def check(self, data):
        expected_baseline, expected_current = statuses_from(data)
        expected = oracle_audit(links_of(data), expected_baseline,
                                expected_current)
        actual = analyze(data)['reconciliation_audit']
        self.assertEqual(set(actual), AUDIT_KEYS)
        self.assertEqual(actual, expected)
        for row in actual['links']:
            self.assertEqual(set(row), LINK_KEYS)
        for row in actual['transitions']:
            self.assertEqual(set(row), TRANSITION_KEYS)
        return actual

    def test_fixed_counterexample(self):
        data = source(components([('a', 'MIT'), ('b', 'GPL-3.0-only')]),
                      [('a', 'x', 0), ('a', 'y', 1), ('b', 'x', 1)])
        audit = self.check(data)
        self.assertEqual(audit['matched_count'], 2)
        self.assertEqual(audit['minimum_penalty'], 2)
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        self.assertEqual(classes, {('a', 'x'): 'excluded',
                                   ('a', 'y'): 'forced',
                                   ('b', 'x'): 'forced'})
        self.assertEqual(audit['transitions'], [
            {'baseline_id': 'a', 'current_id': 'y',
             'baseline_status': 'allowed', 'current_status': 'blocked'},
            {'baseline_id': 'b', 'current_id': 'x',
             'baseline_status': 'blocked', 'current_status': 'allowed'},
        ])

    def test_equal_cost_block_all_possible_without_transitions(self):
        links = [(b, c, 0) for b in ('a', 'b') for c in ('x', 'y')]
        audit = self.check(source(
            components([('a', 'MIT'), ('b', 'GPL-3.0-only')]), links))
        self.assertEqual(audit['matched_count'], 2)
        self.assertEqual(audit['minimum_penalty'], 0)
        self.assertEqual([row['classification'] for row in audit['links']],
                         ['possible'] * 4)
        self.assertEqual(audit['transitions'], [])

    def test_forced_edge_inside_minimum_cost_tie(self):
        links = [('a', 'x', 0), ('a', 'y', 0),
                 ('b', 'x', 0), ('b', 'y', 0),
                 ('c', 'z', 0), ('c', 'x', 5)]
        audit = self.check(source(
            components([('a', 'MIT'), ('b', 'MIT'), ('c', 'MIT')]),
            links, current_components=components(
                [('x', 'MIT'), ('y', 'MIT'), ('z', 'MIT')])))
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        self.assertEqual(classes[('c', 'z')], 'forced')
        self.assertEqual(classes[('c', 'x')], 'excluded')
        for pair in [('a', 'x'), ('a', 'y'), ('b', 'x'), ('b', 'y')]:
            self.assertEqual(classes[pair], 'possible')
        self.assertEqual([(row['baseline_id'], row['current_id'])
                          for row in audit['transitions']], [('c', 'z')])

    def test_necessarily_unmatched_versus_sometimes_unmatched(self):
        # One current record shared by two baselines: maximum matching size
        # is one, y is unmatched in every optimum while a and b are each
        # unmatched in some optimum.
        links = [('a', 'x', 0), ('b', 'x', 0)]
        audit = self.check(source(
            components([('a', 'MIT'), ('b', 'MIT')]), links,
            current_components=components([('x', 'MIT'),
                                           ('y', 'GPL-3.0-only')])))
        self.assertEqual(audit['matched_count'], 1)
        self.assertEqual(audit['minimum_penalty'], 0)
        self.assertEqual(audit['always_unmatched_baseline'], [])
        self.assertEqual(audit['always_unmatched_current'], ['y'])
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        self.assertEqual(classes, {('a', 'x'): 'possible',
                                   ('b', 'x'): 'possible'})

    def test_zero_penalties(self):
        links = [('a', 'x', 0), ('a', 'y', 0), ('b', 'y', 0)]
        audit = self.check(source(
            components([('a', 'MIT'), ('b', 'MIT')]), links))
        self.assertEqual(audit['minimum_penalty'], 0)
        self.assertEqual(audit['matched_count'], 2)
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        # b has a single edge, so the maximum matching a-x/b-y is unique.
        self.assertEqual(classes, {('a', 'x'): 'forced',
                                   ('a', 'y'): 'excluded',
                                   ('b', 'y'): 'forced'})

    def test_isolated_records_and_empty_links(self):
        data = source(components([('a', 'MIT'), ('lonely', 'MIT')]),
                      [], current_components=components(
                          [('x', 'MIT'), ('orphan', 'GPL-3.0-only')]))
        audit = self.check(data)
        self.assertEqual(audit['matched_count'], 0)
        self.assertEqual(audit['minimum_penalty'], 0)
        self.assertEqual(audit['links'], [])
        self.assertEqual(audit['always_unmatched_baseline'],
                         ['a', 'lonely'])
        self.assertEqual(audit['always_unmatched_current'], ['orphan', 'x'])
        self.assertEqual(audit['transitions'], [])

    def test_empty_side(self):
        for baseline_components, current_components, links in [
                ([], [('x', 'MIT')], []),
                ([('a', 'MIT')], [], [])]:
            with self.subTest(baseline=baseline_components,
                              current=current_components):
                data = source(components(baseline_components), links,
                              current_components=components(
                                  current_components))
                audit = self.check(data)
                self.assertEqual(audit['matched_count'], 0)
                self.assertEqual(audit['minimum_penalty'], 0)
                self.assertEqual(audit['links'], [])
                self.assertEqual(audit['transitions'], [])

    def test_with_and_unknown_license_statuses(self):
        links = [('a', 'x', 0)]
        data = source(
            components([('a', 'GPL-2.0-only WITH Classpath-exception-2.0'),
                        ('b', 'MIT WITH Unknown-exception')]),
            links,
            current_components=components([('x', 'MIT WITH Missing-Exception'),
                                           ('y', 'MIT')]))
        audit = self.check(data)
        self.assertEqual(audit['transitions'], [{
            'baseline_id': 'a', 'current_id': 'x',
            'baseline_status': 'blocked', 'current_status': 'blocked'}])
        self.assertEqual(audit['always_unmatched_baseline'], ['b'])
        self.assertEqual(audit['always_unmatched_current'], ['y'])

    def test_baseline_status_uses_existing_candidate_rule(self):
        data = source(
            components([('old-or', 'MIT OR GPL-3.0-only'),
                        ('old-and', 'MIT AND GPL-3.0-only'),
                        ('old-unknown', 'NoSuchLicense')]),
            [('old-or', 'x', 0), ('old-and', 'x', 1),
             ('old-unknown', 'x', 2)])
        audit = self.check(data)
        self.assertEqual(audit['matched_count'], 1)
        # Only old-or is allowed; it wins the unique maximum matching with
        # the cheapest allowed-side edge (old-or->x, penalty 0).
        self.assertEqual(audit['transitions'], [{
            'baseline_id': 'old-or', 'current_id': 'x',
            'baseline_status': 'allowed', 'current_status': 'allowed'}])
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        self.assertEqual(classes[('old-or', 'x')], 'forced')
        self.assertEqual(classes[('old-and', 'x')], 'excluded')
        self.assertEqual(classes[('old-unknown', 'x')], 'excluded')
        self.assertEqual(audit['always_unmatched_baseline'],
                         ['old-and', 'old-unknown'])

    def test_forced_transition_present_when_statuses_equal(self):
        audit = self.check(source(
            components([('a', 'MIT')]), [('a', 'x', 0)],
            current_components=components([('x', 'MIT')])))
        self.assertEqual(audit['transitions'], [{
            'baseline_id': 'a', 'current_id': 'x',
            'baseline_status': 'allowed', 'current_status': 'allowed'}])

    def test_randomized_oracle_agreement(self):
        rng = random.Random(20261118)
        for trial in range(60):
            baseline_ids = [f'b{i}' for i in range(rng.randint(0, 4))]
            current_ids = [f'c{i}' for i in range(rng.randint(0, 4))]
            pool = [(b, c) for b in baseline_ids for c in current_ids]
            rng.shuffle(pool)
            chosen = pool[:rng.randint(0, min(len(pool), 7))]
            links = [(b, c, rng.choice([0, 0, 1, 2, 5]))
                     for b, c in chosen]
            baseline_components = components(
                [(b, rng.choice(['MIT', 'GPL-3.0-only'])) for b in baseline_ids])
            current_components = components(
                [(c, rng.choice(['MIT', 'Apache-2.0', 'GPL-3.0-only']))
                 for c in current_ids])
            data = source(baseline_components, links, current_components)
            with self.subTest(trial=trial):
                self.check(data)

    def test_matching_objective_independent_of_license_status(self):
        baseline = components([('a', 'MIT'), ('b', 'GPL-3.0-only')])
        links = [('a', 'x', 0), ('a', 'y', 1), ('b', 'x', 1)]
        allowed_policy = basic_policy()
        flipped_policy = policy([('MIT', False, ['notice']),
                                 ('Apache-2.0', False, ['notice']),
                                 ('GPL-3.0-only', True, []),
                                 ('GPL-2.0-only WITH '
                                  'Classpath-exception-2.0', True, [])])
        first = self.check(source(baseline, links,
                                  baseline_policy=allowed_policy,
                                  current_policy=allowed_policy))
        second = self.check(source(baseline, links,
                                   baseline_policy=flipped_policy,
                                   current_policy=flipped_policy))
        for field in ['matched_count', 'minimum_penalty', 'links',
                      'always_unmatched_baseline',
                      'always_unmatched_current']:
            self.assertEqual(first[field], second[field])
        self.assertEqual(
            [(row['baseline_id'], row['current_id'])
             for row in first['transitions']],
            [(row['baseline_id'], row['current_id'])
             for row in second['transitions']])
        self.assertNotEqual(
            [(row['baseline_status'], row['current_status'])
             for row in first['transitions']],
            [(row['baseline_status'], row['current_status'])
             for row in second['transitions']])

    def test_explicit_same_name_link_is_honoured(self):
        data = source(
            components([('same', 'MIT')]), [('same', 'same', 7)],
            current_components=components([('same', 'GPL-3.0-only')]))
        audit = self.check(data)
        self.assertEqual(audit['matched_count'], 1)
        self.assertEqual(audit['minimum_penalty'], 7)
        self.assertEqual(audit['links'][0]['classification'], 'forced')
        self.assertEqual(audit['transitions'], [{
            'baseline_id': 'same', 'current_id': 'same',
            'baseline_status': 'allowed', 'current_status': 'blocked'}])

    def test_input_order_permutation_invariance(self):
        rng = random.Random(7)
        baseline_ids = [f'b{i}' for i in range(4)]
        current_ids = [f'c{i}' for i in range(4)]
        links = [(b, c, rng.choice([0, 1, 2]))
                 for b in baseline_ids for c in current_ids
                 if rng.random() < 0.7]
        data = source(components([(b, 'MIT') for b in baseline_ids]), links,
                      current_components=components(
                          [(c, 'GPL-3.0-only' if i % 2 else 'MIT')
                           for i, c in enumerate(current_ids)]))
        first = analyze(data)
        for _ in range(5):
            shuffled = copy.deepcopy(data)
            rng.shuffle(shuffled['reconciliation']['links'])
            rng.shuffle(shuffled['reconciliation']['baseline']['components'])
            rng.shuffle(shuffled['reconciliation']['baseline']['policy'])
            rng.shuffle(shuffled['components'])
            rng.shuffle(shuffled['policy'])
            self.assertEqual(analyze(shuffled), first)


class ValidationBoundaries(unittest.TestCase):
    def base(self):
        return source(components([('a', 'MIT')]), [('a', 'x', 0)])

    def test_root_reconciliation_exact_fields(self):
        for mutate in [
                lambda d: d['reconciliation'].update(extra=1),
                lambda d: d['reconciliation'].pop('links'),
                lambda d: d['reconciliation'].pop('baseline'),
        ]:
            data = self.base()
            mutate(data)
            with self.assertRaises(ValueError):
                analyze(data)

    def test_baseline_exact_fields_and_no_nesting(self):
        for mutate in [
                lambda b: b.update(release={}),
                lambda b: b.update(reconciliation={}),
                lambda b: b.pop('policy'),
                lambda b: b.update(extra=[]),
                lambda b: b['components'][0].update(selected={}),
        ]:
            data = self.base()
            mutate(data['reconciliation']['baseline'])
            with self.assertRaises(ValueError):
                analyze(data)

    def test_link_exact_fields_and_penalty_bounds(self):
        bad_links = [
            {'baseline_id': 'a', 'current_id': 'x'},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': 0, 'extra': 1},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': True},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': -1},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': 1000000001},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': 1.0},
            {'baseline_id': 'a', 'current_id': 'x', 'penalty': '1'},
            {'baseline_id': 'nope', 'current_id': 'x', 'penalty': 0},
            {'baseline_id': 'a', 'current_id': 'nope', 'penalty': 0},
            {'baseline_id': '', 'current_id': 'x', 'penalty': 0},
        ]
        for link in bad_links:
            data = self.base()
            data['reconciliation']['links'] = [link]
            with self.subTest(link=link), self.assertRaises(ValueError):
                analyze(data)

    def test_penalty_upper_bound_accepted(self):
        data = self.base()
        data['reconciliation']['links'][0]['penalty'] = 1000000000
        self.assertEqual(
            analyze(data)['reconciliation_audit']['minimum_penalty'],
            1000000000)

    def test_duplicate_pair_rejected(self):
        data = self.base()
        data['reconciliation']['links'].append(dict(
            data['reconciliation']['links'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            analyze(data)

    def test_same_name_is_not_auto_paired(self):
        data = source(
            components([('same', 'MIT')]), [],
            current_components=components([('same', 'MIT')]))
        audit = analyze(data)['reconciliation_audit']
        self.assertEqual(audit['matched_count'], 0)
        self.assertEqual(audit['always_unmatched_baseline'], ['same'])
        self.assertEqual(audit['always_unmatched_current'], ['same'])

    def test_baseline_component_limit(self):
        baseline = components([(f'b{i}', 'MIT') for i in range(1000)])
        analyze(source(baseline, []))
        baseline.append({'id': 'overflow', 'expression': 'MIT'})
        with self.assertRaises(ValueError):
            analyze(source(baseline, []))

    def test_current_component_limit_with_reconciliation(self):
        current = components([(f'c{i}', 'MIT') for i in range(1000)])
        analyze(source(components([('a', 'MIT')]), [], current))
        current.append({'id': 'overflow', 'expression': 'MIT'})
        with self.assertRaises(ValueError):
            analyze(source(components([('a', 'MIT')]), [], current))

    def test_existing_smaller_limits_still_apply(self):
        # A release caps current components at 120 even with reconciliation.
        current = components([(f'c{i}', 'MIT') for i in range(121)])
        data = source(components([('a', 'MIT')]), [], current,
                      extra={'release': {'obligation_costs': [
                          {'obligation': 'notice', 'cost': 1}]}})
        with self.assertRaises(ValueError):
            analyze(data)

    def test_link_limit(self):
        baseline = components([(f'b{i}', 'MIT') for i in range(1000)])
        current = components([(f'c{i}', 'MIT') for i in range(1000)])
        links = [(f'b{i}', f'c{j}', 0)
                 for i in range(1000) for j in range(12)]
        self.assertEqual(len(links), 12000)
        audit = analyze(source(baseline, links, current)
                        )['reconciliation_audit']
        self.assertEqual(len(audit['links']), 12000)
        links.append(('b0', 'c12', 0))
        with self.assertRaises(ValueError):
            analyze(source(baseline, links, current))

    def test_existing_analyses_unchanged_with_reconciliation(self):
        data = {
            'policy': basic_policy(),
            'components': [
                {'id': 'core', 'expression': 'MIT'},
                {'id': 'lib', 'expression': 'Apache-2.0'},
                {'id': 'plugin', 'expression': 'MIT AND GPL-3.0-only'},
            ],
            'release': {'obligation_costs': [
                {'obligation': 'notice', 'cost': 2}]},
            'dependency_graph': {
                'roots': ['core'],
                'edges': [{'dependency': 'core', 'dependent': 'lib'}],
            },
        }
        legacy = analyze(data)
        with_reconciliation = copy.deepcopy(data)
        with_reconciliation['reconciliation'] = self.base()[
            'reconciliation']
        with_reconciliation['reconciliation']['baseline']['components'] = [
            {'id': 'old', 'expression': 'MIT'}]
        with_reconciliation['reconciliation']['links'] = [
            {'baseline_id': 'old', 'current_id': 'lib', 'penalty': 3}]
        current = analyze(with_reconciliation)
        self.assertEqual(current['components'], legacy['components'])
        self.assertEqual(current['release_plan'], legacy['release_plan'])
        self.assertEqual(current['dependency_audit'],
                         legacy['dependency_audit'])
        self.assertEqual(set(current) - {'reconciliation_audit'},
                         set(legacy))

    def test_dependency_graph_limit_narrowed_by_reconciliation(self):
        current = components([(f'c{i}', 'MIT') for i in range(1500)])
        data = source(components([('a', 'MIT')]), [], current,
                      extra={'dependency_graph': {'roots': [],
                                                  'edges': []}})
        with self.assertRaises(ValueError):
            analyze(data)
        data['components'] = data['components'][:1000]
        self.assertIn('reconciliation_audit', analyze(data))

    def test_reconciliation_does_not_change_old_result(self):
        base = {'policy': basic_policy(), 'components': [
            {'id': 'core', 'expression': 'MIT'},
            {'id': 'plugin', 'expression': 'MIT AND GPL-3.0-only'},
            {'id': 'tool', 'expression': 'GPL-2.0-only WITH '
             'Classpath-exception-2.0'},
        ]}
        legacy = analyze(base)
        self.assertNotIn('reconciliation_audit', legacy)
        data = copy.deepcopy(base)
        data['reconciliation'] = self.base()['reconciliation']
        data['reconciliation']['links'] = []
        data['reconciliation']['baseline']['components'] = [
            {'id': 'old', 'expression': 'MIT'}]
        current = analyze(data)
        self.assertEqual(current['components'], legacy['components'])
        self.assertEqual(current['compliant'], legacy['compliant'])
        self.assertEqual(set(current) - {'reconciliation_audit'},
                         set(legacy))


class ScaleCase(unittest.TestCase):
    def build(self):
        """Reproducible 800/800 mix of unique, ambiguous and isolated."""
        baseline_components = []
        current_components = []
        links = []
        expected_classes = {}
        expected_transitions = []
        # 200 unique forced edges, penalty 2 each; odd currents blocked.
        for index in range(200):
            bid, cid = f'b{index:03d}', f'c{index:03d}'
            baseline_components.append({'id': bid, 'expression': 'MIT'})
            current_components.append({
                'id': cid,
                'expression': 'GPL-3.0-only' if index % 2 else 'MIT'})
            links.append((bid, cid, 2))
            expected_classes[(bid, cid)] = 'forced'
            expected_transitions.append({
                'baseline_id': bid, 'current_id': cid,
                'baseline_status': 'allowed',
                'current_status': 'blocked' if index % 2 else 'allowed'})
        # 250 equal-cost 2x2 blocks: all four edges possible.
        for block in range(250):
            base = 200 + block * 2
            for offset in range(2):
                bid = f'b{base + offset:03d}'
                baseline_components.append({'id': bid, 'expression': 'MIT'})
            for offset in range(2):
                cid = f'c{base + offset:03d}'
                current_components.append({'id': cid, 'expression': 'MIT'})
            for offset_b in range(2):
                for offset_c in range(2):
                    pair = (f'b{base + offset_b:03d}',
                            f'c{base + offset_c:03d}')
                    links.append((pair[0], pair[1], 1))
                    expected_classes[pair] = 'possible'
        # One dense equal-cost block: 70x70, all possible, penalty 0.
        for index in range(70):
            baseline_components.append(
                {'id': f'b{700 + index:03d}', 'expression': 'MIT'})
            current_components.append(
                {'id': f'c{700 + index:03d}', 'expression': 'MIT'})
        for index_b in range(70):
            for index_c in range(70):
                pair = (f'b{700 + index_b:03d}', f'c{700 + index_c:03d}')
                links.append((pair[0], pair[1], 0))
                expected_classes[pair] = 'possible'
        # 30 isolated records on each side.
        for index in range(770, 800):
            baseline_components.append(
                {'id': f'b{index:03d}', 'expression': 'MIT'})
            current_components.append(
                {'id': f'c{index:03d}', 'expression': 'GPL-3.0-only'})
        return baseline_components, current_components, links, \
            expected_classes, expected_transitions

    def test_800_800_6100_edges_exact(self):
        baseline, current, links, expected_classes, expected_transitions = \
            self.build()
        self.assertEqual(len(baseline), 800)
        self.assertEqual(len(current), 800)
        self.assertEqual(len(links), 200 + 1000 + 4900)
        data = source(baseline, links, current)
        started = time.perf_counter()
        audit = analyze(data)['reconciliation_audit']
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 90.0)
        self.assertEqual(audit['matched_count'], 770)
        self.assertEqual(audit['minimum_penalty'], 900)
        self.assertEqual(len(audit['links']), 6100)
        actual_classes = {(row['baseline_id'], row['current_id']):
                          row['classification'] for row in audit['links']}
        self.assertEqual(actual_classes, expected_classes)
        self.assertEqual(audit['always_unmatched_baseline'],
                         [f'b{index:03d}' for index in range(770, 800)])
        self.assertEqual(audit['always_unmatched_current'],
                         [f'c{index:03d}' for index in range(770, 800)])
        self.assertEqual(audit['transitions'], expected_transitions)
        self.assertEqual([(row['baseline_id'], row['current_id'])
                          for row in audit['links']],
                         sorted((b, c) for b, c, _ in links))

    def test_scale_permutation_invariance(self):
        baseline, current, links, _, _ = self.build()
        data = source(baseline, links, current)
        first = analyze(data)
        rng = random.Random(99)
        for _ in range(2):
            shuffled = copy.deepcopy(data)
            rng.shuffle(shuffled['reconciliation']['links'])
            rng.shuffle(shuffled['reconciliation']['baseline']['components'])
            rng.shuffle(shuffled['components'])
            self.assertEqual(analyze(shuffled), first)


class CliIntegration(unittest.TestCase):
    def invoke(self, source_path, output_path):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli',
             '--source', str(source_path), '--output', str(output_path)],
            capture_output=True, text=True, cwd=str(ROOT),
            env={'PYTHONPATH': str(ROOT / 'src')})

    def test_fixture_run_retains_source_hash(self):
        report = run(ROOT / 'fixtures/reconciliation.json')
        audit = report['result']['reconciliation_audit']
        self.assertEqual(len(report['source_sha256']), 64)
        self.assertEqual(audit['matched_count'], 5)
        self.assertEqual(audit['minimum_penalty'], 6)
        classes = {(row['baseline_id'], row['current_id']):
                   row['classification'] for row in audit['links']}
        self.assertEqual(classes[('legacy-old', 'legacy-a')], 'possible')
        self.assertEqual(classes[('legacy-old', 'legacy-b')], 'possible')
        self.assertEqual(classes[('core-old', 'core')], 'forced')
        self.assertNotIn(('legacy-old', 'legacy-a'),
                         {(row['baseline_id'], row['current_id'])
                          for row in audit['transitions']})

    def test_cli_success_and_business_blocking(self):
        data = source(components([('a', 'GPL-3.0-only')]),
                      [('a', 'x', 0)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            result = self.invoke(path, Path(tmp) / 'out.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads((Path(tmp) / 'out.json').read_text())
            self.assertFalse(output['result']['compliant'])
            self.assertEqual(output['result']['reconciliation_audit']
                             ['transitions'][0]['baseline_status'],
                             'blocked')

    def test_cli_reconciliation_error_preserves_old_bytes(self):
        data = source(components([('a', 'MIT')]), [('a', 'x', True)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            output = Path(tmp) / 'out.json'
            output.write_bytes(b'old report bytes')
            result = self.invoke(path, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), b'old report bytes')

    def test_cli_unknown_link_reference_exit_two(self):
        data = source(components([('a', 'MIT')]), [('a', 'missing', 0)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            output = Path(tmp) / 'out.json'
            output.write_bytes(b'previous')
            result = self.invoke(path, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), b'previous')

    def test_cli_duplicate_pair_exit_two(self):
        data = source(components([('a', 'MIT')]),
                      [('a', 'x', 0), ('a', 'x', 1)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            output = Path(tmp) / 'out.json'
            output.write_bytes(b'previous')
            result = self.invoke(path, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), b'previous')

    def test_cli_nested_reconciliation_in_baseline_exit_two(self):
        data = source(components([('a', 'MIT')]), [])
        data['reconciliation']['baseline']['reconciliation'] = {
            'baseline': {'policy': basic_policy(),
                         'components': [{'id': 'old',
                                         'expression': 'MIT'}]},
            'links': []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            output = Path(tmp) / 'out.json'
            output.write_bytes(b'previous')
            result = self.invoke(path, output)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), b'previous')

    def test_cli_reports_sorted_and_json_strict(self):
        data = source(
            components([('b', 'MIT'), ('a', 'MIT')]),
            [('b', 'y', 3), ('a', 'y', 1), ('b', 'x', 1), ('a', 'x', 2)],
            current_components=components([('y', 'MIT'), ('x', 'MIT')]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            output = Path(tmp) / 'out.json'
            result = self.invoke(path, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = output.read_text()
            self.assertTrue(text.endswith('\n'))
            self.assertEqual(json.loads(text), json.loads(result.stdout))
            audit = json.loads(text)['result']['reconciliation_audit']
            self.assertEqual(
                [(row['baseline_id'], row['current_id'])
                 for row in audit['links']],
                [('a', 'x'), ('a', 'y'), ('b', 'x'), ('b', 'y')])
            self.assertEqual(
                [(row['baseline_id'], row['current_id'])
                 for row in audit['transitions']],
                sorted((row['baseline_id'], row['current_id'])
                       for row in audit['transitions']))
            self.assertEqual(audit['always_unmatched_baseline'], [])
            self.assertEqual(audit['always_unmatched_current'], [])

    def test_cli_empty_links_side_effects(self):
        data = source(components([('b', 'MIT'), ('a', 'MIT')]), [],
                      current_components=components([('z', 'MIT'),
                                                     ('y', 'MIT')]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'in.json'
            path.write_text(json.dumps(data))
            result = self.invoke(path, Path(tmp) / 'out.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            audit = json.loads(result.stdout)['result']['reconciliation_audit']
            self.assertEqual(audit['matched_count'], 0)
            self.assertEqual(audit['minimum_penalty'], 0)
            self.assertEqual(audit['links'], [])
            self.assertEqual(audit['transitions'], [])
            self.assertEqual(audit['always_unmatched_baseline'], ['a', 'b'])
            self.assertEqual(audit['always_unmatched_current'], ['y', 'z'])


if __name__ == '__main__':
    unittest.main()
