"""Warehouse CLI transport, guard and snapshot tests."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from license_planner.warehouse import open_warehouse, window_report

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
]


def put(event_id, revision, component_id, effective_at, expression):
    return {'event_id': event_id, 'revision': revision,
            'component_id': component_id, 'action': 'put',
            'effective_at': effective_at, 'policy': POLICY,
            'expression': expression}


def retract(event_id, revision, component_id):
    return {'event_id': event_id, 'revision': revision,
            'component_id': component_id, 'action': 'retract'}


class WarehouseCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / 'warehouse.db'
        self.obs = self.dir / 'observations.json'
        self.window_file = self.dir / 'window.json'
        self.report = self.dir / 'report.json'
        self.obs.write_text(json.dumps({'observations': [
            put('e1', 1, 'x', 10, 'GPL-3.0-only'),
            put('e2', 1, 'x', 20, 'MIT')]}))
        self.window_file.write_text(json.dumps(
            {'components': ['x'], 'start': 0, 'end': 40}))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', *map(str, arguments)],
            capture_output=True, text=True)

    def test_import_receipt_only_on_stdout(self):
        result = self.cli('--warehouse', self.db,
                          '--observations', self.obs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        receipt = json.loads(result.stdout)
        raw = self.obs.read_bytes()
        self.assertEqual(set(receipt),
                         {'schema_version', 'source_sha256', 'generation',
                          'inserted', 'duplicates'})
        self.assertEqual(receipt['schema_version'], 1)
        self.assertEqual(receipt['source_sha256'],
                         hashlib.sha256(raw).hexdigest())
        self.assertEqual((receipt['generation'], receipt['inserted'],
                          receipt['duplicates']), (1, 2, 0))

    def test_window_report_via_cli(self):
        self.cli('--warehouse', self.db, '--observations', self.obs)
        result = self.cli('--warehouse', self.db, '--window',
                          self.window_file, '--output', self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        report = json.loads(self.report.read_text())
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'generation',
                          'result'})
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.window_file.read_bytes())
                         .hexdigest())
        self.assertEqual(report['result']['components'],
                         [{'id': 'x', 'allowed_seconds': 20,
                           'blocked_seconds': 10, 'unknown_seconds': 10}])

    def test_reopen_database_persists(self):
        self.cli('--warehouse', self.db, '--observations', self.obs)
        again = self.cli('--warehouse', self.db,
                         '--observations', self.obs)
        receipt = json.loads(again.stdout)
        self.assertEqual((receipt['generation'], receipt['inserted'],
                          receipt['duplicates']), (1, 0, 2))

    def test_legacy_mode_unchanged(self):
        source = Path(__file__).resolve().parents[1] / 'fixtures' \
            / 'input.json'
        result = self.cli('--source', source, '--output', self.report)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertFalse(report['result']['compliant'])

    def test_modes_are_mutually_exclusive(self):
        cases = [
            ('--warehouse', self.db, '--observations', self.obs,
             '--output', self.report),
            ('--source', self.obs, '--output', self.report,
             '--warehouse', self.db),
            ('--warehouse', self.db, '--observations', self.obs,
             '--window', self.window_file),
            ('--warehouse', self.db, '--window', self.window_file),
            ('--warehouse', self.db, '--output', self.report),
            ('--source', self.obs),
            (),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli(*arguments).returncode, 2)

    def test_query_never_creates_warehouse(self):
        missing = self.dir / 'missing.db'
        result = self.cli('--warehouse', missing, '--window',
                          self.window_file, '--output', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_foreign_and_corrupt_database_reported_not_overwritten(self):
        foreign = self.dir / 'foreign.db'
        foreign.write_text('not a database at all')
        original = foreign.read_bytes()
        result = self.cli('--warehouse', foreign, '--window',
                          self.window_file, '--output', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertIn('warehouse', result.stderr)
        self.assertEqual(foreign.read_bytes(), original)
        # An import also refuses to hijack the foreign file.
        result = self.cli('--warehouse', foreign,
                          '--observations', self.obs)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(foreign.read_bytes(), original)
        # A zero-byte file is a corrupt database, not an empty warehouse.
        empty = self.dir / 'empty.db'
        empty.write_bytes(b'')
        result = self.cli('--warehouse', empty, '--window',
                          self.window_file, '--output', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(empty.read_bytes(), b'')

    def test_symlink_targets_rejected(self):
        self.cli('--warehouse', self.db, '--observations', self.obs)
        link_db = self.dir / 'db-link'
        link_db.symlink_to(self.db)
        self.assertEqual(self.cli(
            '--warehouse', link_db, '--observations', self.obs
        ).returncode, 2)
        link_out = self.dir / 'out-link'
        link_out.symlink_to(self.report)
        self.assertEqual(self.cli(
            '--warehouse', self.db, '--window', self.window_file,
            '--output', link_out).returncode, 2)

    def test_distinct_path_guard(self):
        result = self.cli('--warehouse', self.obs,
                          '--observations', self.obs)
        self.assertEqual(result.returncode, 2)

    def test_failed_query_preserves_report_byte_for_byte(self):
        self.cli('--warehouse', self.db, '--observations', self.obs)
        self.report.write_text('keep these exact bytes')
        original = self.report.read_bytes()
        bad_window = self.dir / 'bad.json'
        bad_window.write_text('{"components": ["ghost"], "start": 0, "end": 1}')
        result = self.cli('--warehouse', self.db, '--window', bad_window,
                          '--output', self.report)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.report.read_bytes(), original)

    def test_strict_json_rules(self):
        # Duplicate JSON keys.
        dup = self.dir / 'dup.json'
        dup.write_text('{"observations": [], "observations": []}')
        self.assertEqual(self.cli(
            '--warehouse', self.db, '--observations', dup).returncode, 2)
        # Non-finite numbers.
        nan = self.dir / 'nan.json'
        nan.write_text('{"observations": [{"event_id": "e", "revision": 1,'
                       '"component_id": "x", "action": "put", '
                       '"effective_at": NaN, "policy": [], "expression": "A"}]}')
        self.assertEqual(self.cli(
            '--warehouse', self.db, '--observations', nan).returncode, 2)
        # Raw 8 MB ceiling.
        huge = self.dir / 'huge.json'
        huge.write_text('{"observations": [' + ' ' * (8 * 1024 * 1024) + ']}')
        result = self.cli('--warehouse', self.db,
                          '--observations', huge)
        self.assertEqual(result.returncode, 2)
        self.assertIn('8 MB', result.stderr)


class SnapshotConsistency(unittest.TestCase):
    def test_reader_keeps_snapshot_across_committing_writer(self):
        # WAL allows one writer plus concurrent readers; a read transaction
        # opened before the write must keep the pre-commit generation and
        # interval rows until it ends.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'w.db'
            writer = open_warehouse(db, create=True)
            policy = POLICY
            from license_planner.warehouse import import_observations
            import_observations(writer, {'observations': [
                put('e1', 1, 'x', 10, 'MIT')]}, 'd1')
            reader = open_warehouse(db, create=False, readonly=True)
            reader.execute('BEGIN')
            generation_before = int(reader.execute(
                "SELECT value FROM schema_meta WHERE key='generation'"
            ).fetchone()[0])
            intervals_before = reader.execute(
                'SELECT * FROM intervals').fetchall()
            # The writer commits while the reader's transaction is open.
            import_observations(writer, {'observations': [
                put('e2', 1, 'x', 20, 'GPL-3.0-only')]}, 'd2')
            self.assertEqual(int(reader.execute(
                "SELECT value FROM schema_meta WHERE key='generation'"
            ).fetchone()[0]), generation_before)
            self.assertEqual(reader.execute(
                'SELECT * FROM intervals').fetchall(), intervals_before)
            reader.execute('COMMIT')
            # A fresh read transaction observes the committed generation.
            report = window_report(
                reader, {'components': ['x'], 'start': 0, 'end': 30}, 'w')
            self.assertEqual(report['generation'], 2)
            reader.close()
            writer.close()


if __name__ == '__main__':
    unittest.main()
