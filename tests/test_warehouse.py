"""Warehouse functional tests with an independent oracle.

The oracle reads only the raw ``observations`` fact table over a separate
sqlite3 connection and re-derives winners and day/second integrals from
scratch.  It never imports the production interval maintenance or aggregation
code (``warehouse._rebuild_intervals``, ``window_report``, ``_day_bounds``),
so agreement is an independent cross-check rather than a self-fulfilling one.
"""
import json
import random
import sqlite3
import tempfile
import unittest
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

from license_planner.warehouse import (
    OBSERVATION_LIMIT, WarehouseError, import_observations, open_warehouse,
    set_fault_hook, window_report)

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
    {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
    {'license': 'GPL-2.0-only WITH Classpath-exception-2.0',
     'allowed': False, 'obligations': []},
]
ALLOWED_EXPR = 'MIT OR Apache-2.0'
BLOCKED_EXPR = 'GPL-3.0-only'
WITH_EXPR = 'GPL-2.0-only WITH Classpath-exception-2.0'


def put(event_id, revision, component_id, effective_at, expression,
        policy=None):
    return {'event_id': event_id, 'revision': revision,
            'component_id': component_id, 'action': 'put',
            'effective_at': effective_at,
            'policy': POLICY if policy is None else policy,
            'expression': expression}


def retract(event_id, revision, component_id):
    return {'event_id': event_id, 'revision': revision,
            'component_id': component_id, 'action': 'retract'}


def _utc_day_start(ts):
    d = datetime.fromtimestamp(ts, timezone.utc).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
               .timestamp())


def oracle_window(db_path, components, start, end):
    """Independent recomputation from accepted version facts."""
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    result = {}
    day_totals = {}

    def add_day(day_ts, seconds, status_idx):
        bucket = day_totals.setdefault(day_ts, [0, 0, 0])
        bucket[status_idx] += seconds

    for component_id in components:
        facts = conn.execute(
            'SELECT event_id, revision, action, effective_at, status '
            'FROM observations WHERE component_id=?',
            (component_id,)).fetchall()
        winners = {}
        for event_id, revision, action, effective_at, status in facts:
            old = winners.get(event_id)
            if old is None or revision > old[0]:
                winners[event_id] = (revision, action, effective_at, status)
        # Winner per business instant: greatest event_id among effective puts.
        instant_winners = {}
        for event_id, (_rev, action, effective_at, status) in winners.items():
            if action != 'put':
                continue
            current = instant_winners.get(effective_at)
            if current is None or event_id > current[0]:
                instant_winners[effective_at] = (event_id, status)
        instants = sorted(instant_winners)

        def status_at(instant):
            # The last observation at or before the instant carries forward;
            # nothing precedes the earliest observation (unknown).
            index = bisect_right(instants, instant) - 1
            return instant_winners[instants[index]][1] if index >= 0 \
                else 'unknown'

        boundaries = sorted(set(instant_winners) | {start, end})
        totals = [0, 0, 0]
        for left, right in zip(boundaries, boundaries[1:]):
            lo, hi = max(start, left), min(end, right)
            if lo >= hi:
                continue
            status = status_at(left)
            idx = ('allowed', 'blocked', 'unknown').index(status)
            totals[idx] += hi - lo
            # Split the piece on UTC calendar day boundaries with arithmetic
            # local to the oracle.
            cursor = lo
            while cursor < hi:
                day_start = _utc_day_start(cursor)
                next_day = day_start + 86400
                piece = min(hi, next_day)
                add_day(day_start, piece - cursor, idx)
                cursor = piece
        result[component_id] = totals
    conn.close()
    days = []
    for day_ts in sorted(day_totals):
        days.append({
            'day': datetime.fromtimestamp(day_ts, timezone.utc).date()
            .isoformat(),
            'allowed_seconds': day_totals[day_ts][0],
            'blocked_seconds': day_totals[day_ts][1],
            'unknown_seconds': day_totals[day_ts][2],
        })
    return result, days


class WarehouseFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'warehouse.db'
        self.conn = open_warehouse(self.db, create=True)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def query(self, components, start, end, digest='w'):
        return window_report(
            self.conn,
            {'components': list(components), 'start': start, 'end': end},
            digest)

    def assertOracleAgrees(self, components, start, end):
        report = self.query(components, start, end)
        expected_totals, expected_days = oracle_window(
            self.db, sorted(components), start, end)
        for entry in report['result']['components']:
            self.assertEqual(
                (entry['allowed_seconds'], entry['blocked_seconds'],
                 entry['unknown_seconds']),
                tuple(expected_totals[entry['id']]))
            self.assertEqual(
                sum(expected_totals[entry['id']]), end - start)
        self.assertEqual(report['result']['days'], expected_days)
        component_count = len(set(components))
        for entry in report['result']['days']:
            day_start = int(datetime.strptime(entry['day'], '%Y-%m-%d')
                            .replace(tzinfo=timezone.utc).timestamp())
            intersection = min(end, day_start + 86400) - max(start, day_start)
            self.assertEqual(
                entry['allowed_seconds'] + entry['blocked_seconds']
                + entry['unknown_seconds'],
                component_count * intersection)
        return report


class AcceptanceTimeline(WarehouseFixture):
    def test_fixed_acceptance_example(self):
        # Times are Unix seconds; statuses come from policy/expression only.
        receipt = import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, BLOCKED_EXPR),
            put('e2', 1, 'x', 20, ALLOWED_EXPR)]}, 'd1')
        self.assertEqual((receipt['generation'], receipt['inserted'],
                          receipt['duplicates']), (1, 2, 0))
        self.assertEqual(
            self.query(['x'], 0, 40)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 20, 'blocked_seconds': 10,
              'unknown_seconds': 10}])
        # Correct e1 from t=10 to t=30.
        import_observations(self.conn, {'observations': [
            put('e1', 2, 'x', 30, BLOCKED_EXPR)]}, 'd2')
        self.assertEqual(
            self.query(['x'], 0, 40)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 10, 'blocked_seconds': 10,
              'unknown_seconds': 20}])
        # Retract e2.
        import_observations(self.conn, {'observations': [
            retract('e2', 2, 'x')]}, 'd3')
        self.assertEqual(
            self.query(['x'], 0, 40)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 0, 'blocked_seconds': 10,
              'unknown_seconds': 30}])
        # A lexicographically greater same-instant allowed event wins, and
        # retracting it must reveal e1's blocked state again.
        import_observations(self.conn, {'observations': [
            put('e3', 1, 'x', 30, ALLOWED_EXPR)]}, 'd4')
        self.assertEqual(
            self.query(['x'], 0, 40)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 10, 'blocked_seconds': 0,
              'unknown_seconds': 30}])
        import_observations(self.conn, {'observations': [
            retract('e3', 2, 'x')]}, 'd5')
        self.assertEqual(
            self.query(['x'], 0, 40)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 0, 'blocked_seconds': 10,
              'unknown_seconds': 30}])

    def test_all_retracted_is_unknown_but_component_persists(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 5, BLOCKED_EXPR),
            put('e2', 1, 'x', 8, ALLOWED_EXPR)]}, 'd1')
        import_observations(self.conn, {'observations': [
            retract('e1', 2, 'x'), retract('e2', 2, 'x')]}, 'd2')
        report = self.query(['x'], 0, 100)
        self.assertEqual(report['result']['components'],
                         [{'id': 'x', 'allowed_seconds': 0,
                           'blocked_seconds': 0, 'unknown_seconds': 100}])
        known = {row[0] for row in self.conn.execute(
            'SELECT component_id FROM components')}
        self.assertEqual(known, {'x'})
        intervals = self.conn.execute(
            'SELECT COUNT(*) FROM intervals WHERE component_id=?',
            ('x',)).fetchone()[0]
        self.assertEqual(intervals, 0)

    def test_first_record_may_be_retract_then_later_put_revives(self):
        import_observations(self.conn, {'observations': [
            retract('e1', 1, 'x')]}, 'd1')
        self.assertEqual(
            self.query(['x'], 0, 10)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 0, 'blocked_seconds': 0,
              'unknown_seconds': 10}])
        import_observations(self.conn, {'observations': [
            put('e1', 2, 'x', 3, ALLOWED_EXPR)]}, 'd2')
        self.assertEqual(
            self.query(['x'], 0, 10)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 7, 'blocked_seconds': 0,
              'unknown_seconds': 3}])

    def test_same_instant_lexicographic_fallback_on_retract(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, BLOCKED_EXPR),
            put('e2', 1, 'x', 10, ALLOWED_EXPR),
            put('e3', 1, 'x', 10, ALLOWED_EXPR)]}, 'd1')
        # e3 is lexicographically greatest at t=10 and allowed.
        self.assertEqual(
            self.query(['x'], 0, 20)['result']['components'][0]
            ['allowed_seconds'], 10)
        import_observations(self.conn, {'observations': [
            retract('e3', 2, 'x')]}, 'd2')
        # e2 (the next-greatest event, also allowed) surfaces.
        self.assertEqual(
            self.query(['x'], 0, 20)['result']['components'][0]
            ['allowed_seconds'], 10)
        import_observations(self.conn, {'observations': [
            retract('e2', 2, 'x')]}, 'd3')
        # e1 (blocked) is now the sole fallback winner.
        self.assertEqual(
            self.query(['x'], 0, 20)['result']['components'],
            [{'id': 'x', 'allowed_seconds': 0, 'blocked_seconds': 10,
              'unknown_seconds': 10}])

    def test_zero_length_adjacent_and_coalesced_segments(self):
        # Same-status observations adjacent to each other collapse into one
        # segment; an instant whose winner equals the running state has zero
        # duration and must not create a stored row.
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, ALLOWED_EXPR),
            put('e2', 1, 'x', 20, ALLOWED_EXPR),
            put('e3', 1, 'x', 20, BLOCKED_EXPR),
            put('e4', 1, 'x', 30, ALLOWED_EXPR)]}, 'd1')
        rows = self.conn.execute(
            'SELECT start_ts,end_ts,status FROM intervals WHERE '
            "component_id='x' ORDER BY start_ts").fetchall()
        self.assertEqual(rows, [(10, 20, 'allowed'), (20, 30, 'blocked'),
                                (30, None, 'allowed')])
        self.assertOracleAgrees(['x'], 0, 100)

    def test_pre_window_observation_carries_into_window(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, BLOCKED_EXPR)]}, 'd1')
        report = self.query(['x'], 1000, 1100)
        self.assertEqual(report['result']['components'],
                         [{'id': 'x', 'allowed_seconds': 0,
                           'blocked_seconds': 100, 'unknown_seconds': 0}])

    def test_half_open_right_end(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, ALLOWED_EXPR),
            put('e2', 1, 'x', 20, BLOCKED_EXPR)]}, 'd1')
        # Window ending exactly on an observation boundary excludes the right
        # endpoint from the earlier state.
        self.assertOracleAgrees(['x'], 0, 20)
        self.assertOracleAgrees(['x'], 10, 20)

    def test_daily_totals_aggregate_all_selected_components(self):
        day = 86400
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'a', day - 4, ALLOWED_EXPR),
            put('e2', 1, 'b', day - 2, BLOCKED_EXPR)]}, 'd1')
        report = self.query(['a', 'b'], day - 10, day + 10)
        totals = {entry['day']: entry for entry in report['result']['days']}
        # Each day intersects the window for 10s per component, so each day's
        # three figures sum to 2 components x 10s = 20s.
        # Day 1: a is unknown 6s then allowed 4s; b is unknown 8s then
        # blocked 2s -> allowed 4, blocked 2, unknown 14.
        self.assertEqual(
            (totals['1970-01-01']['allowed_seconds'],
             totals['1970-01-01']['blocked_seconds'],
             totals['1970-01-01']['unknown_seconds']),
            (4, 2, 14))
        # Day 2: a stays allowed for 10s, b stays blocked for 10s.
        self.assertEqual(
            (totals['1970-01-02']['allowed_seconds'],
             totals['1970-01-02']['blocked_seconds'],
             totals['1970-01-02']['unknown_seconds']),
            (10, 10, 0))

    def test_cross_day_correction_moves_both_neighbourhoods(self):
        # Observation straddling a UTC day boundary, then moved across days.
        day = 86400
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', day - 2, BLOCKED_EXPR)]}, 'd1')
        self.assertOracleAgrees(['x'], day - 5, day + 5)
        import_observations(self.conn, {'observations': [
            put('e1', 2, 'x', day + 1, BLOCKED_EXPR)]}, 'd2')
        self.assertOracleAgrees(['x'], day - 5, day + 5)
        self.assertOracleAgrees(['x'], 0, 3 * day)


class VersionSemantics(WarehouseFixture):
    def test_idempotent_same_version_payload(self):
        row = put('e1', 1, 'x', 10, ALLOWED_EXPR)
        first = import_observations(
            self.conn, {'observations': [row, dict(row)]}, 'd1')
        self.assertEqual((first['inserted'], first['duplicates'],
                          first['generation']), (1, 1, 1))
        # Key order and whitespace differences are irrelevant for JSON value
        # equality; the arrays inside keep their order semantics.
        re_text = json.dumps({'observations': [row]})
        re_loaded = json.loads(re_text)
        second = import_observations(self.conn, re_loaded, 'd2')
        self.assertEqual((second['inserted'], second['duplicates'],
                          second['generation']), (0, 1, 1))

    def test_empty_batch_does_not_bump_generation(self):
        receipt = import_observations(
            self.conn, {'observations': []}, 'd0')
        self.assertEqual((receipt['generation'], receipt['inserted'],
                          receipt['duplicates']), (0, 0, 0))

    def test_pure_duplicate_batch_rewrites_no_derived_rows(self):
        row = put('e1', 1, 'x', 10, ALLOWED_EXPR)
        import_observations(self.conn, {'observations': [row]}, 'd1')
        intervals_before = self.conn.execute(
            'SELECT * FROM intervals ORDER BY component_id, start_ts'
        ).fetchall()
        # The fault hook must not even fire when nothing new is inserted.
        def hook(_conn, _affected):
            raise AssertionError('derived maintenance ran for a duplicate')

        set_fault_hook(hook)
        try:
            receipt = import_observations(
                self.conn, {'observations': [row, dict(row)]}, 'd2')
        finally:
            set_fault_hook(None)
        self.assertEqual((receipt['inserted'], receipt['generation']),
                         (0, 1))
        self.assertEqual(self.conn.execute(
            'SELECT * FROM intervals ORDER BY component_id, start_ts'
        ).fetchall(), intervals_before)

    def test_late_smaller_revision_stored_but_ineffective(self):
        import_observations(self.conn, {'observations': [
            put('e1', 3, 'x', 100, ALLOWED_EXPR)]}, 'd1')
        receipt = import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 0, BLOCKED_EXPR)]}, 'd2')
        self.assertEqual((receipt['inserted'], receipt['generation']), (1, 2))
        report = self.query(['x'], 0, 200)
        # Revision 3 stays the winner: allowed from t=100, unknown before.
        self.assertEqual(report['result']['components'],
                         [{'id': 'x', 'allowed_seconds': 100,
                           'blocked_seconds': 0, 'unknown_seconds': 100}])
        stored = self.conn.execute(
            'SELECT COUNT(*) FROM observations').fetchone()[0]
        self.assertEqual(stored, 2)

    def test_same_version_payload_conflict_rejected_even_when_superseded(self):
        import_observations(self.conn, {'observations': [
            put('e1', 2, 'x', 10, ALLOWED_EXPR),
            put('e1', 1, 'x', 5, ALLOWED_EXPR)]}, 'd1')
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                put('e1', 1, 'x', 5, BLOCKED_EXPR)]}, 'd2')
        # State and generation are untouched.
        self.assertEqual(int(self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0]), 1)

    def test_retract_payload_conflict(self):
        import_observations(self.conn, {'observations': [
            retract('e1', 1, 'x')]}, 'd1')
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                put('e1', 1, 'x', 1, ALLOWED_EXPR)]}, 'd2')

    def test_component_ownership_fixed_per_event(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, ALLOWED_EXPR)]}, 'd1')
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                put('e1', 2, 'y', 20, ALLOWED_EXPR)]}, 'd2')
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                retract('e1', 2, 'y')]}, 'd3')

    def test_ownership_conflict_inside_one_batch(self):
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                put('e1', 1, 'x', 10, ALLOWED_EXPR),
                put('e1', 2, 'y', 20, ALLOWED_EXPR)]}, 'd1')

    def test_duplicate_version_repeated_in_batch_conflicts(self):
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': [
                put('e1', 1, 'x', 10, ALLOWED_EXPR),
                put('e1', 1, 'x', 11, ALLOWED_EXPR)]}, 'd1')

    def test_batch_rolls_back_on_mid_batch_conflict(self):
        good = [put('e%d' % i, 1, 'x', i, ALLOWED_EXPR) for i in range(5)]
        good.append(put('e1', 1, 'x', 99, BLOCKED_EXPR))  # conflicts in-batch
        with self.assertRaises(WarehouseError):
            import_observations(self.conn, {'observations': good}, 'd1')
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM observations').fetchone()[0], 0)
        self.assertEqual(int(self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0]), 0)

    def test_multi_version_batch_arbitrary_order(self):
        rows = [
            retract('e2', 3, 'x'),
            put('e1', 2, 'x', 30, BLOCKED_EXPR),
            put('e2', 1, 'x', 20, ALLOWED_EXPR),
            put('e1', 1, 'x', 10, ALLOWED_EXPR),
            put('e2', 2, 'x', 25, BLOCKED_EXPR),
        ]
        for seed in range(6):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'w.db'
                conn = open_warehouse(path, create=True)
                shuffled = list(rows)
                random.Random(seed).shuffle(shuffled)
                import_observations(conn, {'observations': shuffled}, 'd')
                report = window_report(
                    conn, {'components': ['x'], 'start': 0, 'end': 40}, 'w')
                # e1 r2 is blocked@30; e2 r3 is a retract so e2 contributes
                # nothing. Unknown [0,30), blocked [30,40).
                self.assertEqual(report['result']['components'],
                                 [{'id': 'x', 'allowed_seconds': 0,
                                   'blocked_seconds': 10,
                                   'unknown_seconds': 30}])
                conn.close()


class ObservationValidation(WarehouseFixture):
    def _reject(self, data):
        # Shared helpers (ident/analyze) raise plain ValueErrors; both kinds
        # are input errors and must surface the same way.
        with self.assertRaises(ValueError):
            import_observations(self.conn, data, 'd')

    def test_root_shape(self):
        self._reject({})
        self._reject({'observations': [], 'extra': 1})
        self._reject([])
        self._reject({'observations': {}})

    def test_unknown_action_and_fields(self):
        self._reject({'observations': [
            {'event_id': 'e1', 'revision': 1, 'component_id': 'x',
             'action': 'delete'}]})
        self._reject({'observations': [
            dict(put('e1', 1, 'x', 1, ALLOWED_EXPR), status='allowed')]})
        self._reject({'observations': [
            dict(retract('e1', 1, 'x'), effective_at=1)]})
        self._reject({'observations': [
            dict(retract('e1', 1, 'x'), policy=POLICY)]})

    def test_revision_bounds_and_bool_rejection(self):
        for bad in (0, 1_000_000_001, True, False, 1.0, '1', None):
            self._reject({'observations': [
                put('e1', bad, 'x', 1, ALLOWED_EXPR)]})

    def test_time_bounds_and_bool_rejection(self):
        for bad in (-1, 4102444801, True, False, 1.5, '10', None):
            self._reject({'observations': [
                put('e1', 1, 'x', bad, ALLOWED_EXPR)]})
        # Boundary values are legal.
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 0, ALLOWED_EXPR),
            put('e2', 1, 'y', 4102444800, ALLOWED_EXPR)]}, 'd')

    def test_ident_rules(self):
        for bad in ('', ' ', ' x', 'x ', 'x\ny', 1, None):
            self._reject({'observations': [
                put('e1', 1, bad, 1, ALLOWED_EXPR)]})
            self._reject({'observations': [
                put(bad, 1, 'x', 1, ALLOWED_EXPR)]})

    def test_too_many_observations(self):
        rows = [put('e%d' % i, 1, 'x', i, ALLOWED_EXPR)
                for i in range(OBSERVATION_LIMIT + 1)]
        self._reject({'observations': rows})

    def test_expression_and_policy_reuse_existing_rules(self):
        # Unknown license blocks, WITH is a full key, bad SPDX syntax rejects.
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 1, 'NO-SUCH-LICENSE')]}, 'd1')
        report = self.query(['x'], 0, 5)
        # Unknown [0,1), blocked [1,5).
        self.assertEqual(report['result']['components'][0]
                         ['blocked_seconds'], 4)
        import_observations(self.conn, {'observations': [
            put('e2', 1, 'w', 1, WITH_EXPR)]}, 'd2')
        self.assertEqual(self.query(['w'], 0, 5)['result']
                         ['components'][0]['blocked_seconds'], 4)
        self._reject({'observations': [
            put('e3', 1, 'x', 1, 'MIT AND')]})
        self._reject({'observations': [
            put('e3', 1, 'x', 1, ALLOWED_EXPR,
                policy=[{'license': 'MIT', 'allowed': 'yes',
                         'obligations': []}])]})
        self._reject({'observations': [
            put('e3', 1, 'x', 1, ALLOWED_EXPR,
                policy=[{'license': 'MIT', 'allowed': True}])]})

    def test_caller_supplied_status_is_unknown_field(self):
        row = put('e1', 1, 'x', 1, ALLOWED_EXPR)
        row['status'] = 'blocked'
        self._reject({'observations': [row]})

    def test_with_is_a_full_policy_key(self):
        with_key = 'GPL-2.0-only WITH Classpath-exception-2.0'
        unknown_policy = [
            {'license': 'MIT', 'allowed': True, 'obligations': []}]
        allowed_policy = [
            {'license': with_key, 'allowed': True, 'obligations': ['weak']}]
        blocked_policy = [
            {'license': with_key, 'allowed': False, 'obligations': []}]
        # The WITH key is atomic: an unknown full key blocks, an explicitly
        # allowed one allows, and the base license never grants the WITH key.
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'blocked-c', 1, with_key, policy=unknown_policy),
            put('e2', 1, 'allowed-c', 1, with_key, policy=allowed_policy),
            put('e3', 1, 'denied-c', 1, with_key, policy=blocked_policy)]},
            'd1')
        report = self.query(['blocked-c', 'allowed-c', 'denied-c'], 0, 5)
        by_id = {entry['id']: entry for entry in report['result']['components']}
        self.assertEqual(by_id['blocked-c']['blocked_seconds'], 4)
        self.assertEqual(by_id['allowed-c']['allowed_seconds'], 4)
        self.assertEqual(by_id['denied-c']['blocked_seconds'], 4)


class WindowValidation(WarehouseFixture):
    def _reject(self, data):
        with self.assertRaises(WarehouseError):
            window_report(self.conn, data, 'w')

    def test_window_shape_and_bounds(self):
        self._reject({})
        self._reject({'components': [], 'start': 0, 'end': 10, 'x': 1})
        self._reject({'components': [], 'start': 10, 'end': 10})
        self._reject({'components': [], 'start': 20, 'end': 10})
        for bad in (-1, 4102444801, True, 1.0):
            self._reject({'components': [], 'start': bad, 'end': 10})
            self._reject({'components': [], 'start': 0, 'end': bad})
        self._reject({'components': [], 'start': 0,
                      'end': 366 * 86400 + 1})
        # Exactly 366 days is the inclusive maximum.
        report = window_report(
            self.conn,
            {'components': [], 'start': 0, 'end': 366 * 86400}, 'w')
        self.assertEqual(len(report['result']['days']), 366)

    def test_midnight_aligned_window_is_one_day(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 86400, ALLOWED_EXPR)]}, 'd1')
        report = self.query(['x'], 86400, 2 * 86400)
        self.assertEqual([d['day'] for d in report['result']['days']],
                         ['1970-01-02'])
        self.assertEqual(report['result']['components'],
                         [{'id': 'x', 'allowed_seconds': 86400,
                           'blocked_seconds': 0, 'unknown_seconds': 0}])

    def test_component_list_rules(self):
        self._reject({'components': ['x'] * 2, 'start': 0, 'end': 10})
        self._reject({'components': ['c%d' % i for i in range(1001)],
                      'start': 0, 'end': 10})
        self._reject({'components': ['ghost'], 'start': 0, 'end': 10})

    def test_empty_components_still_emits_all_days(self):
        report = window_report(
            self.conn,
            {'components': [], 'start': 86400 - 5, 'end': 2 * 86400 + 7},
            'w')
        days = report['result']['days']
        self.assertEqual([d['day'] for d in days],
                         ['1970-01-01', '1970-01-02', '1970-01-03'])
        for day in days:
            self.assertEqual(
                (day['allowed_seconds'], day['blocked_seconds'],
                 day['unknown_seconds']), (0, 0, 0))
        self.assertEqual(report['result']['components'], [])

    def test_components_sorted_and_exact_fields(self):
        for cid in ('b', 'a', 'c'):
            import_observations(self.conn, {'observations': [
                put('e-' + cid, 1, cid, 0, ALLOWED_EXPR)]}, 'd-' + cid)
        report = window_report(
            self.conn, {'components': ['b', 'a', 'c'],
                        'start': 0, 'end': 10}, 'w')
        self.assertEqual([c['id'] for c in report['result']['components']],
                         ['a', 'b', 'c'])
        for entry in report['result']['components']:
            self.assertEqual(set(entry),
                             {'id', 'allowed_seconds', 'blocked_seconds',
                              'unknown_seconds'})

    def test_query_never_creates_database(self):
        missing = Path(self.tmp.name) / 'missing.db'
        with self.assertRaises(WarehouseError):
            open_warehouse(missing, create=False)
        self.assertFalse(missing.exists())


class FaultInjection(WarehouseFixture):
    def test_failure_after_facts_rolls_everything_back(self):
        import_observations(self.conn, {'observations': [
            put('e1', 1, 'x', 10, ALLOWED_EXPR)]}, 'd1')
        before_facts = self.conn.execute(
            'SELECT * FROM observations ORDER BY event_id, revision'
        ).fetchall()
        before_intervals = self.conn.execute(
            'SELECT * FROM intervals').fetchall()
        before_gen = int(self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])

        def hook(_conn, _affected):
            raise RuntimeError('injected interval update failure')

        set_fault_hook(hook)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                import_observations(self.conn, {'observations': [
                    put('e2', 1, 'x', 20, BLOCKED_EXPR),
                    put('e3', 1, 'y', 5, ALLOWED_EXPR)]}, 'd2')
        finally:
            set_fault_hook(None)
        self.assertEqual(self.conn.execute(
            'SELECT * FROM observations ORDER BY event_id, revision'
        ).fetchall(), before_facts)
        self.assertEqual(self.conn.execute(
            'SELECT * FROM intervals').fetchall(), before_intervals)
        self.assertEqual(int(self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0]), before_gen)
        # y never reached the component directory; x keeps its old timeline.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM components WHERE component_id='y'"
        ).fetchone()[0], 0)
        self.assertEqual(
            self.query(['x'], 0, 100)['result']['components'][0]
            ['allowed_seconds'], 90)


class RandomizedOracleProperty(WarehouseFixture):
    def build_random_history(self, rng, component_count, event_count):
        rows = []
        for component_index in range(component_count):
            cid = 'c%02d' % component_index
            for event_index in range(event_count):
                eid = '%s-e%02d' % (cid, event_index)
                revision = rng.randint(1, 6)
                # Sometimes seed a high revision first so later low revisions
                # arrive late.
                ordered = list(range(1, revision + 1))
                if rng.random() < 0.5:
                    rng.shuffle(ordered)
                for rev in ordered:
                    if rng.random() < 0.15:
                        rows.append(retract(eid, rev, cid))
                    else:
                        rows.append({
                            'event_id': eid, 'revision': rev,
                            'component_id': cid, 'action': 'put',
                            'effective_at': rng.randint(0, 4 * 86400),
                            'policy': POLICY,
                            'expression': rng.choice(
                                [ALLOWED_EXPR, BLOCKED_EXPR, WITH_EXPR])})
        rng.shuffle(rows)
        # Split into batches of varied sizes and arbitrary ordering.
        batches = []
        cursor = 0
        while cursor < len(rows):
            step = rng.randint(1, 25)
            batches.append(rows[cursor:cursor + step])
            cursor += step
        return batches

    def test_random_histories_match_oracle(self):
        rng = random.Random(20261118)
        cids = ['c%02d' % i for i in range(8)]
        for trial in range(6):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'w.db'
                conn = open_warehouse(path, create=True)
                for batch in self.build_random_history(
                        rng, len(cids), 4):
                    import_observations(
                        conn, {'observations': batch}, 'd')
                windows = [(0, 100), (50, 5 * 86400),
                           (86400 - 3, 86400 + 3),
                           (0, 366 * 86400), (123, 456)]
                for start, end in windows:
                    report = window_report(
                        conn, {'components': cids, 'start': start,
                               'end': end}, 'w')
                    expected, expected_days = oracle_window(
                        path, cids, start, end)
                    for entry in report['result']['components']:
                        self.assertEqual(
                            (entry['allowed_seconds'],
                             entry['blocked_seconds'],
                             entry['unknown_seconds']),
                            tuple(expected[entry['id']]))
                    self.assertEqual(report['result']['days'], expected_days)
                conn.close()

    def test_batch_permutation_invariance(self):
        rng = random.Random(99)
        base = self.build_random_history(rng, 5, 3)
        flat = [row for batch in base for row in batch]
        receipts = []
        for seed in range(4):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'w.db'
                conn = open_warehouse(path, create=True)
                shuffled = list(flat)
                random.Random(seed).shuffle(shuffled)
                cursor = 0
                while cursor < len(shuffled):
                    step = rng.randint(1, 30)
                    import_observations(
                        conn, {'observations': shuffled[cursor:cursor + step]},
                        'd')
                    cursor += step
                report = window_report(
                    conn, {'components': ['c%02d' % i for i in range(5)],
                           'start': 0, 'end': 4 * 86400}, 'w')
                receipts.append(json.dumps(report['result'], sort_keys=True))
                conn.close()
        # The materialized state is independent of arrival/ordering; the
        # final generation equals the distinct version count in every run.
        self.assertEqual(len(set(receipts)), 1)


if __name__ == '__main__':
    unittest.main()
