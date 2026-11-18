"""Strict-input and transport tests for the layered delivery audit mode."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from license_planner.layers import audit  # noqa: E402

SHA = {
    name: hashlib.sha256(name.encode()).hexdigest()
    for name in ('a', 'notice', 'wrong', 'payload')
}
POLICY = [{'license': 'MIT', 'allowed': True, 'obligations': ['notice']}]
GOOD = 'a' * 64
WRONG = 'b' * 64


def layer(identifier, *entries):
    return {'id': identifier, 'entries': list(entries)}


def file(path, digest, component=None):
    return {'kind': 'file', 'path': path, 'sha256': digest,
            'component': component}


def base():
    return {
        'policy': [dict(row) for row in POLICY],
        'components': [{'id': 'a', 'expression': 'MIT'}],
        'materials': [{'obligation': 'notice', 'sha256': GOOD}],
        'layers': [],
    }


class LayersInput(unittest.TestCase):
    def test_acceptance_four_layer_sequence(self):
        data = base()
        data['layers'] = [
            layer('L1', file('bin/a', SHA['payload'], 'a'),
                  file('doc/n', GOOD)),
            layer('L2', {'kind': 'hardlink', 'path': 'bin/b',
                         'target': 'bin/a'},
                  {'kind': 'hardlink', 'path': 'doc/saved',
                   'target': 'doc/n'}),
            layer('L3', {'kind': 'whiteout', 'path': 'bin/a'},
                  file('doc/n', WRONG)),
            layer('L4', {'kind': 'opaque', 'path': 'doc'},
                  file('doc/n', WRONG)),
        ]
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']],
                         ['bin/b', 'doc/n'])
        self.assertEqual(result['files'][0]['origin'],
                         {'layer': 'L1', 'path': 'bin/a'})
        self.assertEqual(result['files'][1]['origin'],
                         {'layer': 'L4', 'path': 'doc/n'})
        self.assertEqual(result['components'][0]['paths'], ['bin/b'])
        self.assertEqual(result['components'][0]['status'], 'allowed')
        self.assertEqual(result['obligations'],
                         [{'obligation': 'notice', 'components': ['a'],
                           'satisfied': False, 'paths': []}])
        self.assertFalse(result['compliant'])
        # Layer 3 alone still has the hardlink evidence at doc/saved.
        data['layers'] = data['layers'][:3]
        third = audit(data)
        self.assertEqual({row['path'] for row in third['files']},
                         {'bin/b', 'doc/n', 'doc/saved'})
        saved = {row['path']: row for row in third['files']}['doc/saved']
        self.assertEqual(saved['origin'], {'layer': 'L1', 'path': 'doc/n'})
        self.assertEqual(third['obligations'][0]['paths'], ['doc/saved'])
        self.assertTrue(third['obligations'][0]['satisfied'])
        self.assertTrue(third['compliant'])

    def test_same_layer_whiteout_then_recreate_same_path(self):
        data = base()
        data['layers'] = [
            layer('L1', file('p', GOOD, 'a')),
            layer('L2', {'kind': 'whiteout', 'path': 'p'},
                  file('p', SHA['payload'], 'a')),
        ]
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']], ['p'])
        self.assertEqual(result['files'][0]['origin'],
                         {'layer': 'L2', 'path': 'p'})

    def test_root_fields_exact(self):
        for mutate in (
                lambda d: d.pop('policy'),
                lambda d: d.update(extra=1),
                lambda d: d.update(policy={}),
                lambda d: d.update(materials={}),
                lambda d: d.update(layers={})):
            data = base()
            mutate(data)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                audit(data)
        for raw in ([], 'text', 3, None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                audit(raw)

    def test_policy_rules_reused(self):
        bad = [
            [{'license': 'MIT OR Apache-2.0', 'allowed': True,
              'obligations': []}],                    # not atomic
            [{'license': 'MIT', 'allowed': 1, 'obligations': []}],
            [{'license': 'MIT', 'allowed': True, 'obligations': ['x', 'x']}],
            [{'license': 'MIT', 'allowed': True, 'obligations': [], 'x': 1}],
            [{'license': 'MIT', 'allowed': True, 'obligations': []}] * 201,
        ]
        for policy in bad:
            data = base()
            data['policy'] = policy
            with self.subTest(policy=str(policy)[:40]), \
                    self.assertRaises(ValueError):
                audit(data)

    def test_components_exact_and_expression_validated_even_if_hidden(self):
        cases = [
            [{'id': 'a', 'expression': 'MIT', 'extra': 1}],
            [{'id': 'a'}],
            [{'id': 'a', 'expression': 'MIT'}, {'id': 'a',
                                                'expression': 'MIT'}],
            [{'id': '', 'expression': 'MIT'}],
            [{'id': 'a', 'expression': 'MIT OR'}],
            [{'id': 'a', 'expression': 5}],
            [{'id': 'x' * 129, 'expression': 'MIT'}],
        ]
        for components in cases:
            data = base()
            data['components'] = components
            with self.subTest(components=components), \
                    self.assertRaises(ValueError):
                audit(data)
        # An invalid expression on a never-exposed component is still fatal.
        data = base()
        data['components'].append({'id': 'hidden', 'expression': '(MIT'})
        data['layers'] = [layer('L', file('p', GOOD, 'a'))]
        with self.assertRaises(ValueError):
            audit(data)

    def test_materials_rules(self):
        cases = [
            [{'obligation': 'notice', 'sha256': GOOD, 'extra': 1}],
            [{'obligation': 'notice'}],
            [{'obligation': '', 'sha256': GOOD}],
            [{'obligation': 'notice', 'sha256': GOOD.upper()}],
            [{'obligation': 'notice', 'sha256': 'g' * 64}],
            [{'obligation': 'notice', 'sha256': 'a' * 63}],
            [{'obligation': 'notice', 'sha256': 'a' * 65}],
            [{'obligation': 'notice', 'sha256': 7}],
            [{'obligation': 'notice', 'sha256': GOOD},
             {'obligation': 'notice', 'sha256': GOOD}],
        ]
        for materials in cases:
            data = base()
            data['materials'] = materials
            with self.subTest(materials=materials), \
                    self.assertRaises(ValueError):
                audit(data)
        # One obligation may carry several distinct digests.
        data = base()
        data['materials'] = [
            {'obligation': 'notice', 'sha256': GOOD},
            {'obligation': 'notice', 'sha256': WRONG}]
        audit(data)

    def test_layers_and_entry_shape(self):
        cases = [
            [{'id': 'L'}],
            [{'id': 'L', 'entries': [], 'extra': 1}],
            [{'id': 'L', 'entries': {}}],
            [{'id': '', 'entries': []}],
            [{'id': 'L', 'entries': []}, {'id': 'L', 'entries': []}],
            [layer('L', {'kind': 'dir', 'path': 'p', 'extra': 1})],
            [layer('L', {'kind': 'file', 'path': 'p', 'sha256': GOOD})],
            [layer('L', {'kind': 'file', 'path': 'p', 'component': None,
                         'sha256': GOOD, 'x': 0})],
            [layer('L', {'kind': 'hardlink', 'path': 'p'})],
            [layer('L', {'kind': 'hardlink', 'path': 'p', 'target': 't',
                         'sha256': GOOD})],
            [layer('L', {'kind': 'whiteout', 'path': 'p',
                         'component': None})],
            [layer('L', {'kind': 'opaque'})],
            [layer('L', {'kind': 'symlink', 'path': 'p', 'target': 't'})],
            [layer('L', {})],
            [layer('L', [])],
        ]
        for layers in cases:
            data = base()
            data['layers'] = layers
            with self.subTest(layers=str(layers)[:60]), \
                    self.assertRaises(ValueError):
                audit(data)

    def test_path_contract(self):
        bad = [
            '', '/a', 'a/', 'a//b', '.', './a', 'a/.', '..', 'a/../b',
            'a\\b', 'a\x00b', 'a\x1fb', 'a\nb', 'a' * 1025,
            '/'.join(['x'] * 129),
        ]
        for path in bad:
            data = base()
            data['layers'] = [layer('L', {'kind': 'dir', 'path': path})]
            with self.subTest(path=path), self.assertRaises(ValueError):
                audit(data)
        # 1024 characters and 128 segments are inside the limits; case and
        # Unicode normalisation are preserved verbatim.
        ok_paths = ['a' * 1024, '/'.join(['x'] * 128),
                    'Case/Sensitive', 'é', 'é']
        data = base()
        data['layers'] = [layer('L', *[
            file(path, GOOD, 'a') for path in ok_paths])]
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']],
                         sorted(ok_paths))

    def test_duplicate_write_and_delete_paths(self):
        data = base()
        data['layers'] = [layer('L', file('p', GOOD), file('p', WRONG))]
        with self.assertRaises(ValueError):
            audit(data)
        data['layers'] = [layer('L', {'kind': 'whiteout', 'path': 'p'},
                                {'kind': 'opaque', 'path': 'p'})]
        with self.assertRaises(ValueError):
            audit(data)
        # One write plus one delete on the same path is allowed.
        data['layers'] = [layer('L', {'kind': 'whiteout', 'path': 'p'},
                                file('p', GOOD))]
        audit(data)

    def test_file_or_link_cannot_be_ancestor_of_another_write(self):
        data = base()
        data['layers'] = [layer('L', file('p', GOOD), file('p/q', GOOD))]
        with self.assertRaises(ValueError):
            audit(data)
        data['layers'] = [layer('L', {'kind': 'hardlink', 'path': 'p',
                                      'target': 'x'},
                                file('p/q', GOOD))]
        with self.assertRaises(ValueError):
            audit(data)
        # A directory write may be an ancestor.
        data['layers'] = [layer('L', {'kind': 'dir', 'path': 'p'},
                                file('p/q', GOOD))]
        audit(data)

    def test_missing_parent_file_requires_explicit_dir(self):
        data = base()
        data['layers'] = [layer('L0', file('p', GOOD)),
                          layer('L1', file('p/q', GOOD))]
        with self.assertRaises(ValueError):
            audit(data)
        data['layers'][1]['entries'].insert(0, {'kind': 'dir', 'path': 'p'})
        result = audit(data)
        self.assertEqual([row['path'] for row in result['files']], ['p/q'])

    def test_limits(self):
        data = base()
        data['layers'] = [layer('L%d' % i) for i in range(257)]
        with self.assertRaises(ValueError):
            audit(data)
        data = base()
        data['components'] = [{'id': 'c%d' % i, 'expression': 'MIT'}
                              for i in range(10001)]
        with self.assertRaises(ValueError):
            audit(data)
        data = base()
        data['materials'] = [{'obligation': 'o%d' % i, 'sha256': '%064x' % i}
                             for i in range(20001)]
        with self.assertRaises(ValueError):
            audit(data)
        data = base()
        entries = [file('p%d' % i, GOOD) for i in range(1001)]
        data['layers'] = [layer('L%d' % i, *entries[i::100])
                          for i in range(100)]
        data['layers'][0]['entries'].extend(
            [file('extra%d' % i, GOOD) for i in range(100000 - 1000 + 1)])
        with self.assertRaises(ValueError):
            audit(data)
        # Exactly 100000 operations and exactly 256 layers are inside the
        # bound: 250 layers of 400 directories each.
        data = base()
        data['layers'] = [
            layer('E%d' % number, *[{'kind': 'dir', 'path': 'd%d-%d' % (
                number, index)} for index in range(400)])
            for number in range(250)] + [
            layer('F%d' % number) for number in range(6)]
        audit(data)


class LayersTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *arguments, env=None):
        return subprocess.run(
            [sys.executable, '-m', 'license_planner.cli', *map(str, arguments)],
            capture_output=True, text=True,
            env=env or {'PYTHONPATH': str(ROOT / 'src')})

    def write(self, name, data):
        target = self.dir / name
        target.write_text(json.dumps(data, sort_keys=True))
        return target

    def test_success_report_shape_digest_and_exit_0(self):
        data = base()
        data['layers'] = [layer('L1', file('bin/a', GOOD, 'a')),
                          layer('L2', {'kind': 'hardlink', 'path': 'bin/b',
                                       'target': 'bin/a'})]
        source = self.write('in.json', data)
        report = self.dir / 'report.json'
        result = self.cli('--layers', source, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        published = json.loads(report.read_text())
        self.assertEqual(set(published),
                         {'schema_version', 'source_sha256', 'result'})
        self.assertEqual(published['schema_version'], 1)
        self.assertEqual(published['source_sha256'],
                         hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(set(published['result']),
                         {'compliant', 'files', 'components', 'obligations'})
        self.assertEqual(set(published['result']['files'][0]),
                         {'path', 'sha256', 'component', 'origin'})
        self.assertEqual(set(published['result']['files'][0]['origin']),
                         {'layer', 'path'})
        self.assertEqual(set(published['result']['components'][0]),
                         {'id', 'status', 'selected', 'paths'})
        self.assertEqual(set(published['result']['obligations'][0]),
                         {'obligation', 'components', 'satisfied', 'paths'})
        # stdout echoes the report; no stderr noise on success.
        self.assertEqual(json.loads(result.stdout), published)
        self.assertEqual(result.stderr, '')
        self.assertTrue(report.read_bytes().endswith(b'\n'))

    def test_mode_mutual_exclusion_and_required_output(self):
        data = base()
        source = self.write('in.json', data)
        other = self.write('other.json', {})
        report = self.dir / 'r.json'
        cases = [
            ('--layers', source),
            ('--layers', source, '--output', report, '--source', other),
            ('--layers', source, '--output', report, '--inventory', other),
            ('--layers', source, '--output', report, '--warehouse', other),
            ('--layers', source, '--output', report, '--observations', other),
            ('--layers', source, '--output', report, '--window', other),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli(*arguments).returncode, 2)
                self.assertFalse(report.exists())
        self.assertEqual(self.cli().returncode, 2)

    def test_input_and_output_must_differ_and_symlink_rejected(self):
        data = base()
        source = self.write('in.json', data)
        result = self.cli('--layers', source, '--output', source)
        self.assertEqual(result.returncode, 2)
        self.assertIn('differ', result.stderr)
        link = self.dir / 'link.json'
        link.symlink_to(source)
        before = source.read_bytes()
        result = self.cli('--layers', source, '--output', link)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), before)

    def test_input_errors_exit_2_keep_report_and_clean_temp(self):
        report = self.dir / 'report.json'
        report.write_text('previous bytes\n')
        original = report.read_bytes()
        bad_inputs = [
            b'{"policy":[],"components":[],"materials":[],"layers":[],'
            b'"layers":[]}',
            b'{"policy":[],"components":[],"materials":[],"layers":[],'
            b'"x":NaN}',
            b'{}',
        ]
        for index, raw in enumerate(bad_inputs):
            with self.subTest(index=index):
                source = self.dir / ('bad%d.json' % index)
                source.write_bytes(raw)
                result = self.cli('--layers', source, '--output', report)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(report.read_bytes(), original)
                self.assertEqual(
                    [p.name for p in self.dir.iterdir()
                     if p.name.startswith('tmp') and p.is_file()
                     and p.name != 'bad0.json'], [])

    def test_over_8mb_rejected(self):
        source = self.dir / 'big.json'
        source.write_bytes(b'{"policy":[],"components":[],"materials":[],'
                           b'"layers":[],"pad":' + b' ' * 8_000_001 + b'}')
        result = self.cli('--layers', source,
                          '--output', self.dir / 'r.json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('8 MB', result.stderr)

    def test_fault_between_temp_write_and_replace(self):
        """Failure after the temp report exists but before os.replace.

        The CLI runs in-process with ``os.replace`` patched to fail.  At that
        point the temporary report has been fully serialised; the published
        report must stay byte-identical, the temp file be cleaned up, and the
        CLI report the failure with exit code 2 instead of a traceback.
        """
        data = base()
        data['layers'] = [layer('L1', file('bin/a', GOOD, 'a'))]
        source = self.write('in.json', data)
        report = self.dir / 'report.json'
        report.write_text('old report\n')
        original = report.read_bytes()

        from license_planner.cli import main

        test = self
        seen = {}

        def fail_replace(temp, destination):
            test.assertTrue(os.path.exists(temp), 'temp report missing')
            seen['temp'] = temp
            test.assertEqual(Path(destination), report)
            raise RuntimeError('injected pre-replace failure')

        argv = ['license-planner', '--layers', str(source),
                '--output', str(report)]
        errors = io.StringIO()
        with unittest.mock.patch.object(sys, 'argv', argv), \
                unittest.mock.patch('license_planner.common.os.replace',
                                    side_effect=fail_replace), \
                contextlib.redirect_stderr(errors):
            code = main()
        self.assertEqual(code, 2)
        self.assertIn('injected pre-replace failure', errors.getvalue())
        self.assertFalse(os.path.exists(seen['temp']))
        self.assertEqual(report.read_bytes(), original)
        leftovers = [p.name for p in self.dir.iterdir()
                     if p.name.startswith('tmp') and p.is_file()]
        self.assertEqual(leftovers, [])

    def test_business_blocking_still_exits_0(self):
        data = base()
        data['components'] = [{'id': 'a', 'expression': 'GPL-3.0-only'}]
        data['layers'] = [layer('L', file('p', GOOD, 'a'))]
        source = self.write('blocked.json', data)
        report = self.dir / 'report.json'
        result = self.cli('--layers', source, '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        published = json.loads(report.read_text())
        component = published['result']['components'][0]
        self.assertEqual((component['status'], component['selected']),
                         ('blocked', None))
        self.assertEqual(published['result']['obligations'], [])
        self.assertFalse(published['result']['compliant'])

    def test_legacy_source_mode_unchanged(self):
        report = self.dir / 'legacy.json'
        result = self.cli('--source', ROOT / 'fixtures' / 'input.json',
                          '--output', report)
        self.assertEqual(result.returncode, 0, result.stderr)
        legacy = json.loads(report.read_text())
        self.assertEqual(set(legacy), {'schema_version', 'source_sha256',
                                       'result'})
        self.assertFalse(legacy['result']['compliant'])


class Compatibility(unittest.TestCase):
    def test_selected_matches_baseline_analyze(self):
        from license_planner.model import analyze
        expressions = ['MIT', 'Apache-2.0', 'MIT OR Apache-2.0',
                       'MIT AND Apache-2.0', 'GPL-3.0-only',
                       'MIT OR GPL-3.0-only', 'MIT WITH E',
                       'Apache-2.0 AND MIT']
        data = base()
        data['policy'] = [
            {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
            {'license': 'Apache-2.0', 'allowed': True,
             'obligations': ['patent', 'notice']},
            {'license': 'GPL-3.0-only', 'allowed': False, 'obligations': []},
            {'license': 'MIT WITH E', 'allowed': True,
             'obligations': ['notice', 'exception-notice']}]
        components = [{'id': 'c%d' % i, 'expression': expression}
                      for i, expression in enumerate(expressions)]
        baseline = analyze({'policy': data['policy'],
                            'components': components})
        data['components'] = components
        data['layers'] = [layer('L', *[file('p%d' % i, GOOD, 'c%d' % i)
                                       for i in range(len(expressions))])]
        result = audit(data)
        expected = {row['id']: row for row in baseline['components']}
        for row in result['components']:
            self.assertEqual(row['status'], expected[row['id']]['status'])
            self.assertEqual(row['selected'], expected[row['id']]['selected'])


if __name__ == '__main__':
    unittest.main()
