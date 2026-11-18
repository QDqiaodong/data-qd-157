"""Reproducible 20k component / 200k version scale test.

The dataset is generated from a fixed seed and contains, on purpose,
observations on several different dates, same-instant event competitions and
late-arriving older revisions.  Evidence collected by this module (and printed
by :func:`test_scale_dataset`):

* batch sizes and the largest input file byte count (the 8 MB ceiling),
* timing of the full load and of a single-component correction,
* byte/row snapshots of every unaffected component proving only the corrected
  component's derived rows changed,
* an ``EXPLAIN QUERY PLAN`` showing window queries touch ``intervals`` and
  ``components`` but never the ``observations`` version-history table,
* an injected failure mid-import proving atomic rollback at scale.

An independent oracle (see :mod:`tests.test_warehouse`) recomputes expected
window totals from the raw fact table; it shares no production interval or
aggregation code.
"""
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from license_planner.warehouse import (
    TIME_MAX, import_observations, open_warehouse,
    set_fault_hook, window_report)
from tests.test_warehouse import oracle_window

COMPONENT_COUNT = 20000
VERSION_COUNT = 200000
BATCH_SIZE = 2000
SCALE_SEED = 2026111801

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
    {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
]
EXPRESSIONS = ['MIT OR Apache-2.0', 'GPL-3.0-only', 'MIT', 'Apache-2.0']


def generate_dataset():
    """Reproducibly generate 200k versions plus one correction per batch list.

    Each component owns two events with five revisions (20,000 x 2 x 5 =
    200,000 versions).  Revision arrival order is deliberately shuffled, so
    later batches frequently introduce older revisions (late arrivals).
    Effective times span several UTC days and the two events of a component
    can land on the same instant, creating lexicographic competitions.
    """
    rng = random.Random(SCALE_SEED)
    rows = []
    for component_index in range(COMPONENT_COUNT):
        cid = 'component-%05d' % component_index
        for event_index in range(2):
            eid = '%s-event-%02d' % (cid, event_index)
            revisions = list(range(1, 6))
            rng.shuffle(revisions)
            base_day = rng.randint(0, 30) * 86400
            for revision in revisions:
                if revision == 5 and rng.random() < 0.1:
                    rows.append({'event_id': eid, 'revision': revision,
                                 'component_id': cid, 'action': 'retract'})
                    continue
                # Quantized times create same-instant competitions while
                # spread across the month.
                tick = rng.randint(0, 400) * 600
                rows.append({
                    'event_id': eid, 'revision': revision,
                    'component_id': cid, 'action': 'put',
                    'effective_at': min(TIME_MAX, base_day + tick),
                    'policy': POLICY,
                    'expression': rng.choice(EXPRESSIONS)})
    rng.shuffle(rows)
    assert len(rows) == VERSION_COUNT
    batches = [rows[i:i + BATCH_SIZE]
               for i in range(0, len(rows), BATCH_SIZE)]
    return batches


class ScaleWarehouse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.db = cls.dir / 'scale.db'
        cls.batch_files = []
        cls.largest_bytes = 0
        cls.batch_timings = []
        started = time.perf_counter()
        conn = open_warehouse(cls.db, create=True)
        for index, batch in enumerate(generate_dataset()):
            path = cls.dir / ('batch-%03d.json' % index)
            raw = json.dumps({'observations': batch}).encode('utf-8')
            path.write_bytes(raw)
            cls.largest_bytes = max(cls.largest_bytes, len(raw))
            assert len(raw) <= 8 * 1024 * 1024
            assert len(batch) <= 10000
            t0 = time.perf_counter()
            import_observations(conn, json.loads(raw),
                                'sha-placeholder-%d' % index)
            cls.batch_timings.append(time.perf_counter() - t0)
            cls.batch_files.append(path)
        cls.load_seconds = time.perf_counter() - started
        cls.initial_generation = int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])
        cls.conn = conn

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_01_scale_dataset(self):
        conn = self.conn
        # Persisted facts and directory counts.
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM observations').fetchone()[0], VERSION_COUNT)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM components').fetchone()[0], COMPONENT_COUNT)
        distinct_versions = conn.execute(
            'SELECT COUNT(*) FROM (SELECT DISTINCT event_id, revision '
            'FROM observations)').fetchone()[0]
        self.assertEqual(distinct_versions, VERSION_COUNT)
        # test ordering is alphabetical; this first test still sees the
        # generation produced purely by the initial load batches.
        self.assertEqual(self.initial_generation, len(self.batch_files))
        generation = int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])
        self.assertEqual(generation, self.initial_generation)
        interval_rows = conn.execute(
            'SELECT COUNT(*) FROM intervals').fetchone()[0]
        # Intervals are a compressed timeline, not a fact copy.
        self.assertLess(interval_rows, VERSION_COUNT)
        # Half-open, abutting coverage per component; last row open-ended.
        gaps = conn.execute(
            'SELECT COUNT(*) FROM intervals a JOIN intervals b '
            'ON a.component_id=b.component_id AND a.end_ts=b.start_ts '
            'WHERE a.end_ts IS NOT NULL').fetchone()[0]
        open_ended = conn.execute(
            'SELECT COUNT(DISTINCT component_id) FROM intervals '
            'WHERE end_ts IS NULL').fetchone()[0]
        self.assertEqual(open_ended, conn.execute(
            'SELECT COUNT(*) FROM (SELECT DISTINCT component_id FROM '
            'intervals)').fetchone()[0])
        self.assertGreater(gaps, 0)

        # Window totals are exact integers summing to the window length.
        sample = ['component-%05d' % i
                  for i in (0, 1, 17, 999, 12345, 19999)]
        start, end = 5 * 86400 + 123, 20 * 86400 + 456
        report = window_report(
            conn, {'components': sample, 'start': start, 'end': end}, 'w')
        expected, expected_days = oracle_window(
            self.db, sample, start, end)
        for entry in report['result']['components']:
            self.assertEqual(
                (entry['allowed_seconds'], entry['blocked_seconds'],
                 entry['unknown_seconds']), tuple(expected[entry['id']]))
            self.assertEqual(sum(expected[entry['id']]), end - start)
        self.assertEqual(report['result']['days'], expected_days)

        # Evidence: window query plans never read version history.
        plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT start_ts,end_ts,status FROM intervals '
            "WHERE component_id='component-00000' ORDER BY start_ts"
        ).fetchall()
        scanned = ' '.join(step[3] for step in plan)
        self.assertIn('intervals', scanned)
        self.assertNotIn('observations', scanned)
        full_plan = conn.execute(
            'EXPLAIN QUERY PLAN SELECT component_id FROM components '
            "WHERE component_id IN ('component-00000')").fetchall()
        self.assertNotIn('observations',
                         ' '.join(step[3] for step in full_plan))

        print('\n--- scale evidence ---')
        print('batches: %d, rows/batch: %d, largest input: %.2f MiB'
              % (len(self.batch_files), BATCH_SIZE,
                 self.largest_bytes / 1048576))
        print('total load: %.2fs, slowest batch: %.2fs'
              % (self.load_seconds, max(self.batch_timings)))
        print('materialized interval rows: %d (vs %d facts)'
              % (interval_rows, VERSION_COUNT))
        print('window query plan:', scanned)

    def test_02_correcting_one_component_rewrites_nothing_else(self):
        conn = self.conn
        target = 'component-00007'
        others = ['component-%05d' % i for i in range(COMPONENT_COUNT)
                  if i != 7]
        # Snapshot every other component's derived rows before the correction.
        placeholders = ','.join('?' for _ in others)
        before = conn.execute(
            'SELECT component_id,start_ts,end_ts,status FROM intervals '
            'WHERE component_id IN (%s) ORDER BY component_id,start_ts'
            % placeholders, others).fetchall()
        directory_before = conn.execute(
            'SELECT component_id FROM components ORDER BY component_id'
        ).fetchall()
        facts_before = conn.execute(
            'SELECT event_id,revision,component_id,action,effective_at,'
            'policy_json,expression,status FROM observations '
            'WHERE component_id!=? ORDER BY event_id,revision',
            (target,)).fetchall()
        gen_before = int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])

        # A higher revision that moves an observation across a UTC day
        # boundary, plus a brand-new late-arriving event whose low revision is
        # the first record seen for that (event_id).  Both must be stored;
        # only the target component's derived timeline changes.
        t0 = time.perf_counter()
        receipt = import_observations(conn, {'observations': [
            {'event_id': target + '-event-00', 'revision': 6,
             'component_id': target, 'action': 'put',
             'effective_at': 25 * 86400 + 3600, 'policy': POLICY,
             'expression': 'GPL-3.0-only'},
            {'event_id': target + '-late-event', 'revision': 1,
             'component_id': target, 'action': 'put',
             'effective_at': 2 * 86400 + 7, 'policy': POLICY,
             'expression': 'MIT'}]}, 'correction')
        correction_seconds = time.perf_counter() - t0
        self.assertEqual((receipt['inserted'], receipt['duplicates'],
                          receipt['generation']),
                         (2, 0, gen_before + 1))

        after = conn.execute(
            'SELECT component_id,start_ts,end_ts,status FROM intervals '
            'WHERE component_id IN (%s) ORDER BY component_id,start_ts'
            % placeholders, others).fetchall()
        self.assertEqual(after, before)
        self.assertEqual(conn.execute(
            'SELECT component_id FROM components ORDER BY component_id'
        ).fetchall(), directory_before)
        self.assertEqual(conn.execute(
            'SELECT event_id,revision,component_id,action,effective_at,'
            'policy_json,expression,status FROM observations '
            'WHERE component_id!=? ORDER BY event_id,revision',
            (target,)).fetchall(), facts_before)

        # Only the target's derived rows were replaced.
        changed = conn.execute(
            'SELECT COUNT(*) FROM intervals WHERE component_id=?',
            (target,)).fetchone()[0]
        self.assertGreater(changed, 0)

        # Oracle still agrees on the corrected component across day boundaries.
        start, end = 86400 - 100, 26 * 86400 + 100
        report = window_report(
            conn, {'components': [target], 'start': start, 'end': end}, 'w')
        expected, expected_days = oracle_window(
            self.db, [target], start, end)
        self.assertEqual(
            (report['result']['components'][0]['allowed_seconds'],
             report['result']['components'][0]['blocked_seconds'],
             report['result']['components'][0]['unknown_seconds']),
            tuple(expected[target]))
        self.assertEqual(report['result']['days'], expected_days)
        print('\nsingle-component correction: %.4fs; untouched component '
              'interval rows compared: %d'
              % (correction_seconds, len(before)))

    def test_03_fault_injection_rollback_at_scale(self):
        conn = self.conn
        gen_before = int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])
        facts_before = conn.execute(
            'SELECT COUNT(*) FROM observations').fetchone()[0]
        intervals_before = conn.execute(
            'SELECT COUNT(*) FROM intervals').fetchall()

        def hook(_conn, _affected):
            raise RuntimeError('scale injected failure')

        set_fault_hook(hook)
        try:
            with self.assertRaisesRegex(RuntimeError, 'scale injected'):
                import_observations(conn, {'observations': [
                    {'event_id': 'rollback-event', 'revision': 1,
                     'component_id': 'component-00001', 'action': 'put',
                     'effective_at': 1000, 'policy': POLICY,
                     'expression': 'MIT'}]}, 'faulty')
        finally:
            set_fault_hook(None)
        self.assertEqual(int(conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0]), gen_before)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM observations').fetchone()[0], facts_before)
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM intervals').fetchall(), intervals_before)
        # Reopening the database shows the same logical state.
        conn.close()
        reopened = open_warehouse(self.db, create=False, readonly=True)
        self.assertEqual(int(reopened.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0]), gen_before)
        reopened.close()
        # Assign on the class: unittest instantiates a fresh TestCase per
        # method, so an instance attribute would not reach the next test.
        type(self).conn = open_warehouse(self.db, create=True)
        self.assertEqual(type(self).conn.execute('SELECT 1').fetchall(),
                         [(1,)])

    def test_04_batch_files_respect_contract_limits(self):
        for path in self.batch_files:
            raw = path.read_bytes()
            self.assertLessEqual(len(raw), 8 * 1024 * 1024)
            document = json.loads(raw)
            self.assertLessEqual(len(document['observations']), 10000)
        # Every batch is itself re-importable as a pure duplicate: no new
        # versions, no generation movement, same duplicate accounting.
        gen_before = int(self.conn.execute(
            "SELECT value FROM schema_meta WHERE key='generation'"
        ).fetchone()[0])
        first = json.loads(self.batch_files[0].read_text())
        receipt = import_observations(self.conn, first, 'repeat')
        self.assertEqual((receipt['inserted'], receipt['duplicates'],
                          receipt['generation']),
                         (0, len(first['observations']), gen_before))


if __name__ == '__main__':
    unittest.main()
