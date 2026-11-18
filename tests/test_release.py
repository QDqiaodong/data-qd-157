"""Release batch shared-obligation planning tests."""
import hashlib, itertools, json, random, subprocess, sys, tempfile, unittest
from pathlib import Path
from license_planner.model import analyze
from license_planner.expression import parse, alternatives
from license_planner.cli import run

ROOT = Path(__file__).resolve().parents[1]
PLAN_KEYS = {'status', 'blocked_components', 'total_cost', 'obligation_count',
             'license_occurrences', 'selections', 'obligations'}
SELECTION_KEYS = {'id', 'licenses', 'obligations'}
OBLIGATION_KEYS = {'obligation', 'cost', 'components'}


def policy(*entries):
    return [{'license': lic, 'allowed': ok, 'obligations': list(obs)}
            for lic, ok, obs in entries]


def costs(*pairs):
    return {'obligation_costs': [{'obligation': name, 'cost': value}
                                 for name, value in pairs]}


def source(pol, comps, release=None):
    data = {'policy': pol, 'components': comps}
    if release is not None:
        data['release'] = release
    return data


def oracle_plan(data):
    """Independent exhaustive enumeration of the complete release plan."""
    pol = {r['license']: (r['allowed'], tuple(sorted(r['obligations'])))
           for r in data['policy']}
    options = []
    blocked = []
    for component in data['components']:
        choices = []
        for choice in alternatives(parse(component['expression'])):
            if all(key in pol and pol[key][0] for key in choice):
                obligations = set().union(*(set(pol[key][1]) for key in choice))
                choices.append((tuple(sorted(choice)), tuple(sorted(obligations))))
        if not choices:
            blocked.append(component['id'])
        else:
            options.append((component['id'], choices))
    if blocked:
        return {'status': 'blocked', 'blocked_components': sorted(blocked),
                'total_cost': None, 'obligation_count': None,
                'license_occurrences': None, 'selections': [],
                'obligations': []}
    cost = {r['obligation']: r['cost'] for r in data['release']['obligation_costs']}
    best = None
    for combo in itertools.product(*(choices for _, choices in options)):
        union = set()
        occurrences = 0
        sequence = []
        for licences, obligations in combo:
            union |= set(obligations)
            occurrences += len(licences)
            sequence.append(list(licences))
        key = (sum(cost[name] for name in union), len(union),
               occurrences, sequence)
        if best is None or key < best[0]:
            best = (key, combo)
    key, combo = best
    selections = []
    evidence = {}
    for (component_id, _choices), (licences, obligations) in zip(options, combo):
        selections.append({'id': component_id, 'licenses': list(licences),
                           'obligations': list(obligations)})
        for obligation in obligations:
            evidence.setdefault(obligation, set()).add(component_id)
    selections.sort(key=lambda item: item['id'])
    return {'status': 'allowed', 'blocked_components': [],
            'total_cost': key[0], 'obligation_count': key[1],
            'license_occurrences': key[2], 'selections': selections,
            'obligations': [{'obligation': name, 'cost': cost[name],
                             'components': sorted(evidence[name])}
                            for name in sorted(evidence)]}


class ReleaseShape(unittest.TestCase):
    def test_acceptance_shared_obligation_costs_once(self):
        # Legacy ties break on licence sort order, so name the unique
        # licences below "Shared" to make the per-component rule pick them.
        pol = policy(('A-Unique', True, ['unique-a']),
                     ('B-Unique', True, ['unique-b']),
                     ('Shared', True, ['shared']))
        comps = [{'id': 'alpha', 'expression': 'A-Unique OR Shared'},
                 {'id': 'beta', 'expression': 'B-Unique OR Shared'}]
        rel = costs(('unique-a', 1), ('unique-b', 1), ('shared', 1))
        result = analyze(source(pol, comps, rel))
        plan = result['release_plan']
        self.assertEqual(set(plan), PLAN_KEYS)
        self.assertEqual(plan['status'], 'allowed')
        self.assertEqual(plan['blocked_components'], [])
        self.assertEqual(plan['total_cost'], 1)
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 2)
        self.assertEqual(plan['selections'],
                         [{'id': 'alpha', 'licenses': ['Shared'],
                           'obligations': ['shared']},
                          {'id': 'beta', 'licenses': ['Shared'],
                           'obligations': ['shared']}])
        self.assertEqual(plan['obligations'],
                         [{'obligation': 'shared', 'cost': 1,
                           'components': ['alpha', 'beta']}])
        # The legacy per-component rule still selects each unique obligation,
        # proving the batch result is not the sum of per-component choices.
        self.assertEqual(result['components'][0]['selected'],
                         {'licenses': ['A-Unique'], 'obligations': ['unique-a']})
        self.assertEqual(result['components'][1]['selected'],
                         {'licenses': ['B-Unique'], 'obligations': ['unique-b']})

    def test_zero_cost_superset_must_not_be_discarded(self):
        # The per-component optimum picks the unique (zero-cost) obligation,
        # giving two obligations. Only the shared obligation yields count 1,
        # so a greedy "drop zero cost/current fewer obligations" pass fails;
        # the shared option costs two licences per component but wins level 2.
        pol = policy(('UNIQ1', True, ['u1']), ('UNIQ2', True, ['u2']),
                     ('S1', True, ['shared']), ('S2', True, ['shared']))
        comps = [{'id': 'c1', 'expression': 'UNIQ1 OR (S1 AND S2)'},
                 {'id': 'c2', 'expression': 'UNIQ2 OR (S1 AND S2)'}]
        rel = costs(('u1', 0), ('u2', 0), ('shared', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['total_cost'], 0)
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 4)
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['S1', 'S2'], ['S1', 'S2']])
        self.assertEqual(plan['obligations'],
                         [{'obligation': 'shared', 'cost': 0,
                           'components': ['c1', 'c2']}])

    def test_level_two_before_level_three(self):
        # {a} needs two licences but one obligation; {a,b} needs one licence
        # but two obligations. Obligation count is optimised first.
        pol = policy(('P1', True, ['a']), ('P2', True, ['a']),
                     ('One', True, ['a', 'b']))
        comps = [{'id': 'x', 'expression': '(P1 AND P2) OR One'}]
        rel = costs(('a', 0), ('b', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 2)
        self.assertEqual(plan['selections'][0]['licenses'], ['P1', 'P2'])

    def test_level_three_before_level_four(self):
        # Both unions cost 0 and have obligation count 1. Level 3 must pick
        # the option needing one licence (A), not the two-licence (B AND C),
        # even though the two-licence option would sort earlier.
        pol = policy(('A', True, ['x']), ('B', True, ['x']),
                     ('C', True, ['x']))
        comps = [{'id': 'c1', 'expression': 'A OR (B AND C)'},
                 {'id': 'c2', 'expression': 'A'}]
        rel = costs(('x', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 2)
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['A'], ['A']])

    def test_level_four_lexicographic(self):
        pol = policy(('A', True, ['x']), ('B', True, ['x']))
        comps = [{'id': 'b', 'expression': 'A OR B'},
                 {'id': 'a', 'expression': 'A OR B'}]
        rel = costs(('x', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['license_occurrences'], 2)
        self.assertEqual([s['id'] for s in plan['selections']], ['a', 'b'])
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['A'], ['A']])

    def test_level_four_across_masks(self):
        pol = policy(('A', True, ['x']), ('B', True, ['y']),
                     ('C', True, ['x']), ('D', True, ['y']))
        comps = [{'id': 'c1', 'expression': 'A OR B'},
                 {'id': 'c2', 'expression': 'C OR D'}]
        rel = costs(('x', 0), ('y', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['A'], ['C']])
        self.assertEqual([o['obligation'] for o in plan['obligations']], ['x'])

    def test_level_four_selects_over_tied_masks(self):
        # Masks {x} and {y} tie on cost, count and total licences. Level 4
        # must compare the actual licence sequence, not mask order, so the
        # union giving licence "A" wins over the one giving "B".
        pol = policy(('A', True, ['x']), ('B', True, ['y']))
        comps = [{'id': 'c1', 'expression': 'A OR B'},
                 {'id': 'c2', 'expression': 'A OR B'}]
        rel = costs(('x', 0), ('y', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual([o['obligation'] for o in plan['obligations']], ['x'])
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['A'], ['A']])
        # Swapping the licence names swaps the winner.
        swapped = policy(('Z', True, ['x']), ('A', True, ['y']))
        comps = [{'id': 'c1', 'expression': 'Z OR A'},
                 {'id': 'c2', 'expression': 'A OR Z'}]
        plan = analyze(source(swapped, comps, rel))['release_plan']
        self.assertEqual([o['obligation'] for o in plan['obligations']], ['y'])

    def test_cost_precedes_obligation_count(self):
        pol = policy(('Free', True, []), ('Paid', True, ['a']))
        comps = [{'id': 'c', 'expression': 'Free OR Paid'}]
        rel = costs(('a', 5))
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual((plan['total_cost'], plan['obligation_count'],
                          plan['license_occurrences']), (0, 0, 1))
        self.assertEqual(plan['obligations'], [])

    def test_empty_batch(self):
        rel = costs()
        result = analyze(source([], [], rel))
        self.assertTrue(result['compliant'])
        self.assertEqual(result['components'], [])
        plan = result['release_plan']
        self.assertEqual(plan, {'status': 'allowed', 'blocked_components': [],
                                'total_cost': 0, 'obligation_count': 0,
                                'license_occurrences': 0, 'selections': [],
                                'obligations': []})

    def test_empty_obligations(self):
        # Ties on every level fall to the lexicographically smallest licence
        # list sequence: id "a" takes ISC, id "b" takes MIT.
        pol = policy(('MIT', True, []), ('ISC', True, []))
        comps = [{'id': 'a', 'expression': 'MIT OR ISC'},
                 {'id': 'b', 'expression': 'MIT'}]
        result = analyze(source(pol, comps, costs()))
        plan = result['release_plan']
        self.assertEqual((plan['total_cost'], plan['obligation_count'],
                          plan['license_occurrences']), (0, 0, 2))
        self.assertEqual(plan['obligations'], [])
        self.assertEqual([s['licenses'] for s in plan['selections']],
                         [['ISC'], ['MIT']])

    def test_with_is_full_key(self):
        pol = policy(('GPL-2.0-only', True, ['strong']),
                     ('GPL-2.0-only WITH Classpath-exception-2.0', True, ['weak']))
        comps = [{'id': 'tools',
                  'expression': 'GPL-2.0-only WITH Classpath-exception-2.0'}]
        rel = costs(('strong', 0), ('weak', 1))
        result = analyze(source(pol, comps, rel))
        self.assertEqual(result['components'][0]['selected'],
                         {'licenses': ['GPL-2.0-only WITH Classpath-exception-2.0'],
                          'obligations': ['weak']})
        self.assertEqual(result['release_plan']['obligations'],
                         [{'obligation': 'weak', 'cost': 1, 'components': ['tools']}])

    def test_with_key_not_inherited(self):
        pol = policy(('GPL-2.0-only', True, ['strong']))
        comps = [{'id': 'tools', 'expression': 'GPL-2.0-only WITH Classpath-exception-2.0'}]
        rel = costs(('strong', 0))
        result = analyze(source(pol, comps, rel))
        self.assertEqual(result['components'][0]['status'], 'blocked')
        self.assertEqual(result['release_plan'],
                         {'status': 'blocked', 'blocked_components': ['tools'],
                          'total_cost': None, 'obligation_count': None,
                          'license_occurrences': None, 'selections': [],
                          'obligations': []})

    def test_disallowed_path(self):
        pol = policy(('MIT', True, ['notice']), ('GPL-3.0-only', False, []))
        comps = [{'id': 'a', 'expression': 'MIT OR GPL-3.0-only'},
                 {'id': 'b', 'expression': 'MIT'}]
        rel = costs(('notice', 2))
        result = analyze(source(pol, comps, rel))
        self.assertEqual(result['components'][0]['rejected'],
                         [{'licenses': ['GPL-3.0-only'],
                           'blocked_or_unknown': ['GPL-3.0-only']}])
        self.assertEqual(result['release_plan']['total_cost'], 2)
        self.assertEqual(result['release_plan']['license_occurrences'], 2)

    def test_blocked_batch_no_partial_plan(self):
        pol = policy(('MIT', True, ['notice']), ('GPL', False, []))
        comps = [{'id': 'z', 'expression': 'GPL'},
                 {'id': 'a', 'expression': 'MIT OR GPL'},
                 {'id': 'y', 'expression': 'Unknown'}]
        rel = costs(('notice', 3))
        result = analyze(source(pol, comps, rel))
        plan = result['release_plan']
        self.assertEqual(plan, {'status': 'blocked',
                                'blocked_components': ['y', 'z'],
                                'total_cost': None, 'obligation_count': None,
                                'license_occurrences': None,
                                'selections': [], 'obligations': []})
        self.assertFalse(result['compliant'])
        self.assertEqual(result['components'][0]['selected'],
                         {'licenses': ['MIT'], 'obligations': ['notice']})
        self.assertIsNone(result['components'][1]['selected'])

    def test_evidence_matches_selection(self):
        # "one" must take A (obligations x,y); "two" must take C (z). Each
        # obligation is listed once and bound to exactly the components whose
        # chosen licences require it.
        pol = policy(('A', True, ['x', 'y']), ('B', True, ['y']),
                     ('C', True, ['z']))
        comps = [{'id': 'one', 'expression': 'A'},
                 {'id': 'two', 'expression': 'C'}]
        rel = costs(('x', 1), ('y', 1), ('z', 0))
        plan = analyze(source(pol, comps, rel))['release_plan']
        by_licence = {'A': {'x', 'y'}, 'B': {'y'}, 'C': {'z'}}
        evidence = {}
        for selection in plan['selections']:
            expected = set().union(*(by_licence[k] for k in selection['licenses']))
            self.assertEqual(selection['obligations'], sorted(expected))
            for obligation in selection['obligations']:
                evidence.setdefault(obligation, set()).add(selection['id'])
        self.assertEqual(plan['obligations'],
                         [{'obligation': o, 'cost': c,
                           'components': sorted(evidence[o])}
                          for o, c in [('x', 1), ('y', 1), ('z', 0)]])
        self.assertEqual(plan['total_cost'], 2)
        self.assertEqual(plan['obligation_count'], 3)

    def test_release_absent_unchanged(self):
        data = json.loads((ROOT / 'fixtures/input.json').read_text())
        before = analyze(data)
        self.assertEqual(set(before), {'compliant', 'components'})
        self.assertNotIn('release_plan', before)
        self.assertFalse(before['compliant'])
        self.assertEqual(before['components'][0]['selected'],
                         {'licenses': ['MIT'], 'obligations': ['notice']})

    def test_legacy_selected_unchanged_with_release(self):
        base = json.loads((ROOT / 'fixtures/release.json').read_text())
        with_release = analyze(base)
        without = dict(base)
        del without['release']
        self.assertEqual(with_release['components'], analyze(without)['components'])

    def test_order_permutation(self):
        base = json.loads((ROOT / 'fixtures/release.json').read_text())
        reference = analyze(base)
        for seed in range(5):
            shuffled = json.loads(json.dumps(base))
            random.Random(seed).shuffle(shuffled['policy'])
            random.Random(seed + 1).shuffle(shuffled['components'])
            random.Random(seed + 2).shuffle(
                shuffled['release']['obligation_costs'])
            self.assertEqual(analyze(shuffled), reference)

    def test_fixture_plans(self):
        allowed = analyze(json.loads((ROOT / 'fixtures/release.json').read_text()))
        self.assertEqual(allowed['release_plan']['total_cost'], 3)
        self.assertEqual(allowed['release_plan']['obligation_count'], 2)
        self.assertEqual(allowed['release_plan']['license_occurrences'], 3)
        blocked = analyze(json.loads(
            (ROOT / 'fixtures/release-blocked.json').read_text()))
        self.assertEqual(blocked['release_plan']['status'], 'blocked')
        self.assertEqual(blocked['release_plan']['blocked_components'],
                         ['plugin', 'vendor'])
        self.assertTrue(blocked['compliant'] is False)

    def test_oracle_small_deterministic(self):
        cases = [
            source(policy(('A', True, ['x']), ('B', True, ['y']),
                          ('C', True, ['x', 'y'])),
                   [{'id': 'p', 'expression': 'A OR B'},
                    {'id': 'q', 'expression': 'B OR C'}],
                   costs(('x', 1), ('y', 1))),
            source(policy(('A', True, ['x']), ('B', True, ['y']),
                          ('C', True, ['x', 'y'])),
                   [{'id': 'p', 'expression': 'A AND B'},
                    {'id': 'q', 'expression': 'C'}],
                   costs(('x', 0), ('y', 2))),
            source(policy(('A', True, ['x']), ('B', True, ['x', 'y']),
                          ('C', True, ['y']), ('D', False, ['x'])),
                   [{'id': 'p', 'expression': 'A OR B'},
                    {'id': 'q', 'expression': '(A OR C) AND B'}],
                   costs(('x', 3), ('y', 3))),
        ]
        for data in cases:
            self.assertEqual(analyze(data)['release_plan'], oracle_plan(data))

    def test_oracle_randomized(self):
        rng = random.Random(20261118)
        obligations = ['o%d' % i for i in range(4)]
        for _ in range(250):
            pol = []
            for index in range(rng.randint(1, 5)):
                pol.append({'license': 'L%d' % index,
                            'allowed': rng.random() < 0.85,
                            'obligations': rng.sample(
                                obligations, rng.randint(0, 3))})
            comps = []
            for index in range(rng.randint(0, 4)):
                terms = []
                for _ in range(rng.randint(1, 4)):
                    keys = rng.sample([entry['license'] for entry in pol],
                                      rng.randint(1, min(2, len(pol))))
                    terms.append(' AND '.join(keys))
                expression = ' OR '.join('(%s)' % term if ' AND ' in term else term
                                         for term in terms)
                comps.append({'id': 'c%d' % index, 'expression': expression})
            allowed = sorted(set().union(
                *(set(entry['obligations']) for entry in pol if entry['allowed'])))
            rel = costs(*[(name, rng.randint(0, 5)) for name in allowed])
            data = source(pol, comps, rel)
            self.assertEqual(analyze(data)['release_plan'], oracle_plan(data),
                             msg=json.dumps(data))


class ReleaseValidation(unittest.TestCase):
    def payload(self):
        return source(policy(('A', True, ['x']), ('B', False, ['y'])),
                      [{'id': 'c', 'expression': 'A OR B'}],
                      costs(('x', 1)))

    def test_release_only_obligation_costs(self):
        data = self.payload()
        data['release']['extra'] = 1
        with self.assertRaises(ValueError):
            analyze(data)
        data = self.payload()
        data['release'] = {}
        with self.assertRaises(ValueError):
            analyze(data)
        data = self.payload()
        data['release'] = None
        with self.assertRaises(ValueError):
            analyze(data)
        data = self.payload()
        data['release'] = {'obligation_costs': [], 'other': []}
        with self.assertRaises(ValueError):
            analyze(data)

    def test_cost_entries_exact_fields(self):
        for entry in [{'obligation': 'x', 'cost': 1, 'extra': 0},
                      {'obligation': 'x'},
                      {'cost': 1}]:
            data = self.payload()
            data['release'] = {'obligation_costs': [entry]}
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                analyze(data)

    def test_cost_value_validation(self):
        for bad in [True, False, 1.0, '1', None, -1, 1000000001,
                    10 ** 12]:
            data = self.payload()
            data['release'] = {'obligation_costs': [
                {'obligation': 'x', 'cost': bad}]}
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                analyze(data)

    def test_cost_coverage(self):
        cases = [
            [{'obligation': 'x', 'cost': 1},
             {'obligation': 'x', 'cost': 2}],                       # duplicate
            [{'obligation': 'x', 'cost': 1},
             {'obligation': 'z', 'cost': 1}],                       # unknown
            [{'obligation': 'z', 'cost': 1}],                       # missing x
            [],                                                    # missing x
            [{'obligation': 'x', 'cost': 1},
             {'obligation': 'y', 'cost': 1}],                       # disallowed
        ]
        for entries in cases:
            data = self.payload()
            data['release'] = {'obligation_costs': entries}
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                analyze(data)

    def test_cost_object_required(self):
        data = self.payload()
        data['release'] = []
        with self.assertRaises(ValueError):
            analyze(data)

    def test_obligations_deduplicated_across_policy(self):
        # The same obligation on several allowed policies counts once.
        pol = policy(('A', True, ['x']), ('B', True, ['x']))
        data = source(pol, [{'id': 'c', 'expression': 'A OR B'}],
                      costs(('x', 4)))
        plan = analyze(data)['release_plan']
        self.assertEqual(plan['total_cost'], 4)
        self.assertEqual(plan['obligations'],
                         [{'obligation': 'x', 'cost': 4, 'components': ['c']}])

    def test_obligation_variety_limit(self):
        names = ['o%d' % i for i in range(17)]
        pol = policy(*[(name, True, [name]) for name in names])
        data = source(pol, [{'id': 'c', 'expression': 'OR'.join(names)}],
                      costs(*[(name, 0) for name in names]))
        with self.assertRaisesRegex(ValueError, 'obligations'):
            analyze(data)

    def test_component_limit_release_only(self):
        names = ['o%d' % i for i in range(16)]
        pol = policy(*[(name, True, [name]) for name in names])
        comps = [{'id': 'c%03d' % i, 'expression': names[0]}
                 for i in range(121)]
        rel = costs(*[(name, 0) for name in names])
        with self.assertRaises(ValueError):
            analyze(source(pol, comps, rel))
        # Without release the legacy 1000-component limit still applies.
        self.assertEqual(len(analyze(source(pol, comps))['components']), 121)


class ReleaseScale(unittest.TestCase):
    OBLIGATIONS = ['o%02d' % i for i in range(16)]

    def scale_policy(self):
        return policy(*[(name, True, [name]) for name in self.OBLIGATIONS])

    def test_three_levels_provable_optimum(self):
        # Every component can pick O00 (cost 1); every other single
        # obligation costs >= 2, so (cost, count) >= (1, 1). 120 components
        # each expose 8 distinct allowed candidates drawn from 16 duties.
        pol = self.scale_policy()
        comps = []
        for j in range(120):
            chosen = ['o00'] + [self.OBLIGATIONS[1 + (j + k) % 15]
                                for k in range(7)]
            comps.append({'id': 'c%03d' % j,
                          'expression': ' OR '.join(chosen)})
        rel = costs(*[('o00', 1)] + [(name, 2 + i)
                                     for i, name in enumerate(self.OBLIGATIONS[1:])])
        data = source(pol, comps, rel)
        # Each component indeed exposes eight distinct allowed candidates.
        for component in data['components']:
            self.assertEqual(len(set(component['expression'].split(' OR '))), 8)
        plan = analyze(data)['release_plan']
        self.assertEqual(plan['status'], 'allowed')
        self.assertEqual(plan['total_cost'], 1)
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 120)
        self.assertEqual(plan['obligations'],
                         [{'obligation': 'o00', 'cost': 1,
                           'components': ['c%03d' % j for j in range(120)]}])
        # All 16 obligations are reachable somewhere in the batch.
        reachable = set()
        for component in data['components']:
            reachable.update(component['expression'].split(' OR '))
        self.assertEqual(reachable, set(self.OBLIGATIONS))
        # Proof of optimality: at least one obligation must be paid (every
        # candidate carries exactly one), so cost >= 1 and count >= 1; each
        # component needs at least one licence, so occurrences >= 120. O00 is
        # the unique cost-1 obligation, so the union is {o00} and every
        # component must select the single licence "o00" (unique level 4).

    def test_120_components_8_candidates_level_three(self):
        # All 120 components can take A (one licence) or B1 AND B2 (two
        # licences); both unions cost 1 and have one obligation, so level 3
        # must pick the single-licence candidate everywhere.
        pol = self.scale_policy() + policy(
            ('A', True, ['o00']), ('B1', True, ['o01']),
            ('B2', True, ['o01']))
        comps = [{'id': 'c%03d' % j,
                  'expression': 'A OR (B1 AND B2) OR o%02d OR o%02d'
                                % (2 + j % 14, 2 + (j + 1) % 14)}
                 for j in range(120)]
        rel = costs(*([('o00', 1), ('o01', 1)]
                      + [(name, 3 + i) for i, name in enumerate(self.OBLIGATIONS[2:])]))
        data = source(pol, comps, rel)
        plan = analyze(data)['release_plan']
        self.assertEqual(plan['total_cost'], 1)
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 120)
        self.assertEqual({tuple(s['licenses']) for s in plan['selections']},
                         {('A',)})
        self.assertEqual(plan['obligations'][0]['obligation'], 'o00')

    def test_120_components_adversarial_all_levels_tie(self):
        # Sixteen zero-cost obligations; every component may pick any single
        # one. Cost, obligation count and licence count all tie across the 16
        # singleton unions (cost 0, count 1, occurrences 120), so only level
        # 4 decides. It must choose the lexicographically smallest licence
        # sequence, i.e. "o00" for every component. This is a deterministic,
        # reproducible 120-component / 16-obligation case with 16 candidate
        # licences (>= 8), solved exactly in one process.
        pol = self.scale_policy()
        comps = [{'id': 'c%03d' % j,
                  'expression': ' OR '.join(self.OBLIGATIONS)}
                 for j in range(120)]
        rel = costs(*[(name, 0) for name in self.OBLIGATIONS])
        plan = analyze(source(pol, comps, rel))['release_plan']
        self.assertEqual(plan['total_cost'], 0)
        self.assertEqual(plan['obligation_count'], 1)
        self.assertEqual(plan['license_occurrences'], 120)
        self.assertEqual(plan['selections'],
                         [{'id': 'c%03d' % j, 'licenses': ['o00'],
                           'obligations': ['o00']} for j in range(120)])
        self.assertEqual(plan['obligations'],
                         [{'obligation': 'o00', 'cost': 0,
                           'components': ['c%03d' % j for j in range(120)]}])

    def test_scale_matches_oracle_bounds(self):
        # Cross-check the scale fixture against the exhaustive oracle.
        data = json.loads((ROOT / 'fixtures/release.json').read_text())
        plan = analyze(data)['release_plan']
        self.assertEqual(plan, oracle_plan(data))


class ReleaseTransport(unittest.TestCase):
    def cli(self, source_path, output_path):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli',
             '--source', str(source_path), '--output', str(output_path)],
            capture_output=True, text=True)

    def test_cli_allowed_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'report.json'
            result = self.cli(ROOT / 'fixtures/release.json', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            raw = (ROOT / 'fixtures/release.json').read_bytes()
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(raw).hexdigest())
            self.assertEqual(report['result']['release_plan']['total_cost'], 3)

    def test_cli_blocked_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'report.json'
            result = self.cli(ROOT / 'fixtures/release-blocked.json', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['result']['release_plan']['status'], 'blocked')
            self.assertFalse(report['result']['compliant'])

    def test_cli_cost_error_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            data = json.loads((ROOT / 'fixtures/release.json').read_text())
            data['release']['obligation_costs'][0]['cost'] = True
            (folder / 'in.json').write_text(json.dumps(data))
            (folder / 'out.json').write_text('old report bytes')
            result = self.cli(folder / 'in.json', folder / 'out.json')
            self.assertEqual(result.returncode, 2)
            self.assertEqual((folder / 'out.json').read_text(),
                             'old report bytes')

    def test_cli_limit_error_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ['o%02d' % i for i in range(17)]
            data = {'policy': [{'license': name, 'allowed': True,
                                'obligations': [name]} for name in names],
                    'components': [{'id': 'c', 'expression': ' OR '.join(names)}],
                    'release': {'obligation_costs': [
                        {'obligation': name, 'cost': 0} for name in names]}}
            (folder / 'in.json').write_text(json.dumps(data))
            (folder / 'out.json').write_text('keep me')
            result = self.cli(folder / 'in.json', folder / 'out.json')
            self.assertEqual(result.returncode, 2)
            self.assertEqual((folder / 'out.json').read_text(), 'keep me')

    def test_cli_duplicate_json_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / 'bad.json'
            path.write_text('{"policy":[],"policy":[],"components":[]}')
            (folder / 'out.json').write_text('old')
            with self.assertRaises(ValueError):
                run(path)
            self.assertEqual((folder / 'out.json').read_text(), 'old')


if __name__ == '__main__':
    unittest.main()
