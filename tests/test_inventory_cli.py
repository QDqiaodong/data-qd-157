"""Inventory CLI transport, strict-parsing and failure-protection tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': []},
    {'license': 'Apache-2.0', 'allowed': True, 'obligations': []},
]


def dumps(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


def claim(source, ref_id, expression):
    return {'kind': 'claim', 'source': source, 'id': ref_id,
            'expression': expression}


def alias(left, right):
    return {'kind': 'alias', 'left': {'source': left[0], 'id': left[1]},
            'right': {'source': right[0], 'id': right[1]}}


class InventoryCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', *map(str, arguments)],
            capture_output=True, text=True, env=ENV)

    def write_manifest(self, files, policy=None):
        manifest = self.dir / 'manifest.json'
        manifest.write_text(dumps({
            'policy': POLICY if policy is None else policy,
            'files': files}))
        return manifest

    def write_shard(self, name, rows, raw=None, newline=True):
        target = self.dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            target.write_bytes(raw)
        else:
            text = '\n'.join(dumps(row) for row in rows)
            if text and newline:
                text += '\n'
            target.write_text(text, encoding='utf-8')
        return target

    def test_success_writes_atomic_ndjson_and_receipt(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        report = self.dir / 'report.ndjson'
        result = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['groups'], 1)
        self.assertEqual(receipt['claims'], 1)
        self.assertEqual(receipt['aliases'], 0)
        lines = report.read_text().splitlines()
        header = json.loads(lines[0])
        self.assertEqual(header['kind'], 'header')
        self.assertEqual(header['schema_version'], 1)
        self.assertEqual(set(header), {'kind', 'schema_version',
                                       'source_sha256', 'files'})
        self.assertEqual(len(header['files']), 1)
        self.assertEqual(set(header['files'][0]), {'path', 'sha256'})
        # Every line is canonical compact JSON ending in LF.
        raw = report.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        for line in raw.splitlines():
            self.assertEqual(json.dumps(json.loads(line), sort_keys=True,
                                        ensure_ascii=False, allow_nan=False,
                                        separators=(',', ':')).encode(),
                             line)

    def test_header_digests_cover_manifest_and_shard_bytes(self):
        import hashlib
        shard = self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        report = self.dir / 'report.ndjson'
        result = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        header = json.loads(report.read_text().splitlines()[0])
        self.assertEqual(header['source_sha256'],
                         hashlib.sha256(manifest.read_bytes()).hexdigest())
        self.assertEqual(header['files'][0]['sha256'],
                         hashlib.sha256(shard.read_bytes()).hexdigest())

    def test_files_sorted_normally_regardless_of_manifest_order(self):
        self.write_shard('z.ndjson', [claim('s', 'z', 'MIT')])
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['z.ndjson', 'a.ndjson'])
        report = self.dir / 'report.ndjson'
        result = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        header = json.loads(report.read_text().splitlines()[0])
        self.assertEqual([item['path'] for item in header['files']],
                         ['a.ndjson', 'z.ndjson'])

    def test_mode_mutual_exclusion_and_required_output(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        other = self.dir / 'other.json'
        other.write_text('{}')
        for arguments in (
            ('--inventory', manifest),
            ('--inventory', manifest, '--output', self.dir / 'r.ndjson',
             '--source', other),
            ('--inventory', manifest, '--output', self.dir / 'r.ndjson',
             '--warehouse', self.dir / 'w.db'),
            ('--inventory', manifest, '--output', self.dir / 'r.ndjson',
             '--observations', other),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli(*arguments).returncode, 2)

    def test_duplicate_claim_endpoint_rejected_even_with_same_expression(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT'),
                                      claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        report = self.dir / 'r.ndjson'
        result = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(report.exists())

    def test_dangling_alias_fails_after_full_read(self):
        # The dangling endpoint appears before its (missing) claim would;
        # forward references are legal but an endpoint without any claim is
        # not, and the check can only run once every shard is read.
        self.write_shard('a.ndjson', [
            alias(('s', 'ghost'), ('s', 'a')), claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('claim', result.stderr)

    def test_duplicate_json_keys_rejected_in_shard(self):
        shard = self.dir / 'a.ndjson'
        shard.write_text(
            '{"expression":"MIT","id":"a","kind":"claim","source":"s",'
            '"source":"t"}\n')
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('duplicate JSON key', result.stderr)

    def test_non_finite_numbers_rejected(self):
        shard = self.dir / 'a.ndjson'
        shard.write_text('{"expression":"MIT","id":"a","kind":"claim",'
                         '"source":NaN}\n')
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)

    def test_unknown_and_missing_fields_and_wrong_types_rejected(self):
        bad_rows = [
            b'{"expression":"MIT","id":"a","kind":"claim","source":"s","x":1}\n',
            b'{"id":"a","kind":"claim","source":"s"}\n',
            b'{"expression":7,"id":"a","kind":"claim","source":"s"}\n',
            b'{"kind":"alias","left":{"source":"s"},"right":'
            b'{"source":"s","id":"b"}}\n',
            b'{"kind":"weird"}\n',
            b'[1,2]\n',
            b'"just a string"\n',
            b'{"expression":"MIT OR","id":"a","kind":"claim","source":"s"}\n',
            b'{"expression":"MIT","id":" a ","kind":"claim","source":"s"}\n',
            b'{"expression":"MIT","id":"a","kind":"claim","source":""}\n',
            b'{"expression":"MIT","id":"a\\u0000","kind":"claim",'
            b'"source":"s"}\n',
        ]
        for raw in bad_rows:
            with self.subTest(raw=raw):
                shard = self.dir / 'a.ndjson'
                shard.write_bytes(raw)
                manifest = self.write_manifest(['a.ndjson'])
                result = self.cli('--inventory', manifest,
                                  '--output', self.dir / 'r.ndjson')
                self.assertEqual(result.returncode, 2, raw)

    def test_bad_utf8_rejected(self):
        shard = self.dir / 'a.ndjson'
        shard.write_bytes(b'{"expression":"MIT","id":"a","kind":"claim",'
                          b'"source":"s"}\n\xff\xfe\n')
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('UTF-8', result.stderr)

    def test_blank_lines_ignored_and_final_newline_optional(self):
        shard = self.dir / 'a.ndjson'
        shard.write_bytes(b'\n   \n' + dumps(claim('s', 'a', 'MIT')).encode()
                          + b'\n\n' + dumps(claim('s', 'b', 'Apache-2.0'))
                          .encode())
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_line_limit_includes_the_line_ending(self):
        # A line whose raw size including LF exceeds 65536 bytes is rejected
        # by the streaming line guard before expression grammar work.
        over = self.dir / 'over.ndjson'
        content = b'{"kind":"claim","source":"s","id":"a","expression":"' \
                  + b'M' * 65500 + b'"}\n'
        self.assertGreater(len(content), 65536)
        over.write_bytes(content)
        manifest = self.write_manifest(['over.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r1.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('65536', result.stderr)
        # Exactly 65536 bytes on a final line without a newline is also
        # rejected: the no-ending line may not exceed the cap either.
        no_end = self.dir / 'noend.ndjson'
        filler = b'x'
        prefix = b'{"kind":"claim","source":"s","id":"a","expression":"'
        suffix = b'"}'
        content = prefix + filler * (65536 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(content), 65536)
        no_end.write_bytes(content)
        manifest = self.write_manifest(['noend.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r2.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('65536', result.stderr)
        # A short line followed by a pathological long unterminated line is
        # still caught while streaming.
        mixed = self.dir / 'mixed.ndjson'
        mixed.write_bytes(dumps(claim('s', 'ok', 'MIT')).encode() + b'\n'
                          + b'x' * 70000)
        manifest = self.write_manifest(['mixed.ndjson'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r3.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('65536', result.stderr)

    def test_manifest_strict_rules(self):
        shard = self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        bad_manifests = [
            b'{"files":["a.ndjson"]}',                       # missing policy
            b'{"policy":[],"files":["a.ndjson"],"extra":1}', # unknown field
            b'{"policy":[],"files":"a.ndjson"}',             # wrong type
            b'{"policy":[],"files":["a.ndjson","a.ndjson"]}',# duplicate
            b'{"policy":[],"files":[],"policy":[]}',         # dup JSON key
            b'{"policy":[],"files":[NaN]}',                  # non-finite
        ]
        for raw in bad_manifests:
            with self.subTest(raw=raw):
                manifest = self.dir / 'manifest.json'
                manifest.write_bytes(raw)
                result = self.cli('--inventory', manifest,
                                  '--output', self.dir / 'r.ndjson')
                self.assertEqual(result.returncode, 2)

    def test_manifest_over_8mb_rejected(self):
        manifest = self.dir / 'manifest.json'
        manifest.write_bytes(b'{"policy":[],"files":[' + b' ' * 8_000_001
                             + b']}')
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)
        self.assertIn('8 MB', result.stderr)

    def test_path_contract_absolute_directory_traversal_and_missing(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        outside = self.dir.parent / ('outside-%d.ndjson' % os.getpid())
        outside.write_text(dumps(claim('s', 'a', 'MIT')))
        try:
            cases = [
                ['/etc/hosts'],
                ['../' + outside.name],
                ['sub/../../' + outside.name],
                [''],
                ['.'],
                ['missing.ndjson'],
                ['a.ndjson/'],
            ]
            for files in cases:
                with self.subTest(files=files):
                    manifest = self.write_manifest(files)
                    result = self.cli('--inventory', manifest,
                                      '--output', self.dir / 'r.ndjson')
                    self.assertEqual(result.returncode, 2)
            # Dot segments normalize away; two spellings of one path collide.
            manifest = self.write_manifest(['./a.ndjson', 'a.ndjson'])
            result = self.cli('--inventory', manifest,
                              '--output', self.dir / 'r.ndjson')
            self.assertEqual(result.returncode, 2)
            self.assertIn('distinct', result.stderr)
        finally:
            outside.unlink(missing_ok=True)

    def test_shard_directory_is_rejected(self):
        os.mkdir(self.dir / 'adir')
        manifest = self.write_manifest(['adir'])
        result = self.cli('--inventory', manifest,
                          '--output', self.dir / 'r.ndjson')
        self.assertEqual(result.returncode, 2)

    def test_output_manifest_and_shard_paths_must_be_distinct(self):
        shard = self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        # Report colliding with a shard.
        manifest = self.write_manifest(['a.ndjson'])
        result = self.cli('--inventory', manifest, '--output', shard)
        self.assertEqual(result.returncode, 2)
        # Report colliding with the manifest itself.
        result = self.cli('--inventory', manifest, '--output', manifest)
        self.assertEqual(result.returncode, 2)

    def test_symlink_shard_escaping_manifest_directory_rejected(self):
        outside = self.dir.parent / ('outside-link-%d.ndjson' % os.getpid())
        outside.write_text(dumps(claim('s', 'a', 'MIT')))
        link = self.dir / 'link.ndjson'
        try:
            link.symlink_to(outside)
            manifest = self.write_manifest(['link.ndjson'])
            result = self.cli('--inventory', manifest,
                              '--output', self.dir / 'r.ndjson')
            self.assertEqual(result.returncode, 2)
        finally:
            outside.unlink(missing_ok=True)

    def test_symlink_report_rejected_even_when_input_valid(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        target = self.dir / 'target.ndjson'
        link = self.dir / 'report-link.ndjson'
        link.symlink_to(target)
        result = self.cli('--inventory', manifest, '--output', link)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())

    def test_failure_preserves_old_report_byte_for_byte(self):
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        report = self.dir / 'report.ndjson'
        good = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(good.returncode, 0, good.stderr)
        original = report.read_bytes()
        # Now make the input invalid; the old report must survive untouched.
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT'),
                                      claim('s', 'a', 'MIT')])
        bad = self.cli('--inventory', manifest, '--output', report)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(report.read_bytes(), original)
        # No temporary files left in the report directory.
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name.startswith('.report-')]
        self.assertEqual(leftovers, [])

    def test_fault_hook_after_temp_write_keeps_old_report_and_workspace(self):
        # Drive the pipeline in-process so the fault hook can be installed.
        sys.path.insert(0, str(ROOT / 'src'))
        from license_planner.inventory import set_fault_hook, run_inventory
        self.write_shard('a.ndjson', [claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        report = self.dir / 'report.ndjson'
        self.assertEqual(run_inventory(manifest, report)['groups'], 1)
        original = report.read_bytes()
        workspaces_before = {p for p in Path(tempfile.gettempdir()).iterdir()
                             if p.name.startswith(
                                 'license-planner-inventory-')}

        def hook(temp_name):
            self.assertTrue(os.path.exists(temp_name))
            raise RuntimeError('injected pre-replace failure')

        set_fault_hook(hook)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                run_inventory(manifest, report)
        finally:
            set_fault_hook(None)
        # The failure happens after the temp report is fully written but
        # before os.replace: the published report is byte-identical.
        self.assertEqual(report.read_bytes(), original)
        workspaces_after = {p for p in Path(tempfile.gettempdir()).iterdir()
                            if p.name.startswith(
                                'license-planner-inventory-')}
        self.assertEqual(workspaces_after, workspaces_before)
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name.startswith('.report-')]
        self.assertEqual(leftovers, [])

    def test_temporary_workspace_removed_on_validation_failure(self):
        sys.path.insert(0, str(ROOT / 'src'))
        from license_planner.inventory import run_inventory
        self.write_shard('a.ndjson', [alias(('s', 'ghost'), ('s', 'a')),
                                      claim('s', 'a', 'MIT')])
        manifest = self.write_manifest(['a.ndjson'])
        before = {p for p in Path(tempfile.gettempdir()).iterdir()
                  if p.name.startswith('license-planner-inventory-')}
        with self.assertRaises(ValueError):
            run_inventory(manifest, self.dir / 'r.ndjson')
        after = {p for p in Path(tempfile.gettempdir()).iterdir()
                 if p.name.startswith('license-planner-inventory-')}
        self.assertEqual(after, before)

    def test_legacy_modes_unchanged(self):
        source = ROOT / 'fixtures' / 'input.json'
        report = self.dir / 'legacy.json'
        result = self.cli('--source', source, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(report.read_text())
        self.assertEqual(data['schema_version'], 1)
        self.assertFalse(data['result']['compliant'])


if __name__ == '__main__':
    unittest.main()
