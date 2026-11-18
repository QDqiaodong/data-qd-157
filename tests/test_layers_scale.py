"""Reproducible >30 000-file / 60 000-operation layered delivery scale test.

The case is generated from a fixed seed with a deterministic layout, is a
legal 8 MB JSON input, and is executed by the production CLI in an independent
subprocess.  It contains, on purpose:

* 128 ordered layers (including layers with no entries at all);
* 25 000 base files spread over 100 layers, plus 5 000 temporary files and
  4 000 directories;
* 5 000 base files overwritten in later layers while 5 000 earlier-created
  hardlinks keep pointing at the *old* immutable objects;
* 1 000 cross-layer whiteouts of distinct files plus 5 000 temporary files
  whiteouted in the same deletion layers;
* one layer with a 10 000-step forward hardlink chain (link 0 -> link 1 ->
  ... -> chain/root), every link binding the same object without recursion.

The expected exposure, origins and obligation evidence are recomputed here
from the generator's own bookkeeping, never by replaying production code.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE_COUNT = 25000
BASE_LAYERS = 100
PER_LAYER = 250
OVERWRITTEN = 5000
TEMP_FILES = 5000
DIRECTORIES = 4000
CHAIN_LENGTH = 10000
DELETED_BASE = 1000
TOTAL_LAYERS = 128
BITES = 8

POLICY = [
    {'license': 'MIT', 'allowed': True, 'obligations': ['notice']},
    {'license': 'Apache-2.0', 'allowed': True,
     'obligations': ['notice', 'patent']},
]
COMPONENTS = [
    {'id': 'c0', 'expression': 'MIT'},
    {'id': 'c1', 'expression': 'Apache-2.0'},
    {'id': 'c2', 'expression': 'MIT AND Apache-2.0'},
    {'id': 'c3', 'expression': 'Apache-2.0 OR MIT'},
]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


BASE_SHA = {i: sha('base-%d' % i) for i in range(BASE_COUNT)}
OVER_SHA = {i: sha('over-%d' % i) for i in range(BASE_COUNT)}
TEMP_SHA = {i: sha('tmp-%d' % i) for i in range(TEMP_FILES)}
ROOT_SHA = sha('chain-root')
NOTICE_SHA = BASE_SHA[1]
PATENT_SHA = ROOT_SHA


def build():
    """Return ``(data, expected)`` with expected exposure bookkeeping."""
    layers = []
    base_layer = {}
    for number in range(BASE_LAYERS):
        entries = []
        for index in range(number * PER_LAYER, (number + 1) * PER_LAYER):
            entries.append({'kind': 'file', 'path': 'f/%06d' % index,
                            'sha256': BASE_SHA[index],
                            'component': 'c%d' % (index % 4)})
            base_layer[index] = number
        layers.append({'id': 'base-%03d' % number, 'entries': entries})

    layers.append({'id': 'temp', 'entries': [
        {'kind': 'file', 'path': 'tmp/%05d' % i, 'sha256': TEMP_SHA[i],
         'component': None} for i in range(TEMP_FILES)]})
    layers.append({'id': 'dirs', 'entries': [
        {'kind': 'dir', 'path': 'dirs/%04d' % i}
        for i in range(DIRECTORIES)]})

    chain_entries = [{'kind': 'file', 'path': 'chain/root',
                      'sha256': ROOT_SHA, 'component': 'c0'}]
    for index in range(CHAIN_LENGTH):
        target = ('h/%05d' % (index + 1) if index + 1 < CHAIN_LENGTH
                  else 'chain/root')
        chain_entries.append({'kind': 'hardlink',
                              'path': 'h/%05d' % index, 'target': target})
    layers.append({'id': 'chain', 'entries': chain_entries})

    overwritten = [index for index in range(BASE_COUNT)
                   if index % 5 == 0][:OVERWRITTEN]
    overwritten_set = set(overwritten)
    overwrite_layer = {}
    layers.append({'id': 'oldlinks', 'entries': [
        {'kind': 'hardlink', 'path': 'old/%06d' % index,
         'target': 'f/%06d' % index} for index in overwritten]})
    for number in range(9):
        chunk = overwritten[number * len(overwritten) // 9:
                            (number + 1) * len(overwritten) // 9]
        entries = []
        for index in chunk:
            entries.append({'kind': 'file', 'path': 'f/%06d' % index,
                            'sha256': OVER_SHA[index],
                            'component': 'c%d' % (index % 4)})
            overwrite_layer[index] = 'over-%d' % number
        layers.append({'id': 'over-%d' % number, 'entries': entries})

    deleted_base = [index for index in range(BASE_COUNT - 1, -1, -1)
                    if index not in overwritten_set][:DELETED_BASE]
    deleted_set = set(deleted_base)
    deletions = ['tmp/%05d' % i for i in range(TEMP_FILES)] + \
        ['f/%06d' % i for i in deleted_base]
    for number in range(7):
        chunk = deletions[number * len(deletions) // 7:
                          (number + 1) * len(deletions) // 7]
        layers.append({'id': 'del-%d' % number, 'entries': [
            {'kind': 'whiteout', 'path': path} for path in chunk]})
    while len(layers) < TOTAL_LAYERS:
        layers.append({'id': 'empty-%d' % len(layers), 'entries': []})

    data = {
        'policy': POLICY,
        'components': COMPONENTS,
        'materials': [
            {'obligation': 'notice', 'sha256': NOTICE_SHA},
            {'obligation': 'patent', 'sha256': PATENT_SHA},
        ],
        'layers': layers,
    }

    expected_paths = {}
    for index in range(BASE_COUNT):
        if index in overwritten_set:
            expected_paths['f/%06d' % index] = (
                OVER_SHA[index], 'c%d' % (index % 4),
                overwrite_layer[index])
        elif index in deleted_set:
            continue
        else:
            expected_paths['f/%06d' % index] = (
                BASE_SHA[index], 'c%d' % (index % 4),
                'base-%03d' % base_layer[index])
    for index in overwritten:
        expected_paths['old/%06d' % index] = (
            BASE_SHA[index], 'c%d' % (index % 4),
            'base-%03d' % base_layer[index])
    for index in range(CHAIN_LENGTH):
        expected_paths['h/%05d' % index] = (ROOT_SHA, 'c0', 'chain')
    expected_paths['chain/root'] = (ROOT_SHA, 'c0', 'chain')
    return data, expected_paths, base_layer, overwrite_layer, overwritten


class LayersScale(unittest.TestCase):
    def test_128_layers_30000_files_60000_operations(self):
        data, expected_paths, base_layer, overwrite_layer, overwritten = \
            build()
        operations = sum(len(layer['entries']) for layer in data['layers'])
        self.assertEqual(len(data['layers']), TOTAL_LAYERS)
        self.assertGreaterEqual(operations, 60000)
        self.assertGreaterEqual(len(expected_paths), 30000)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / 'layers.json'
            raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                             allow_nan=False,
                             separators=(',', ':')).encode()
            self.assertLess(len(raw), BITES * 1000 * 1000)
            source.write_bytes(raw)
            report_path = directory / 'report.json'
            started = time.time()
            result = subprocess.run(
                [sys.executable, '-m', 'license_planner.cli',
                 '--layers', str(source), '--output', str(report_path)],
                capture_output=True, text=True,
                env={'PYTHONPATH': str(ROOT / 'src')})
            elapsed = time.time() - started
            self.assertEqual(result.returncode, 0, result.stderr)

            report = json.loads(report_path.read_text())
            self.assertEqual(report['source_sha256'],
                             hashlib.sha256(raw).hexdigest())
            business = report['result']

            rows = {row['path']: row for row in business['files']}
            self.assertEqual(set(rows), set(expected_paths))
            self.assertEqual([row['path'] for row in business['files']],
                             sorted(expected_paths))
            for path, (digest, component, layer) in expected_paths.items():
                row = rows[path]
                self.assertEqual(row['sha256'], digest, path)
                self.assertEqual(row['component'], component, path)
                self.assertEqual(row['origin']['layer'], layer, path)
            # Old hardlinks never re-resolve to the overwritten targets.
            for index in overwritten:
                row = rows['old/%06d' % index]
                self.assertEqual(row['sha256'], BASE_SHA[index])
                self.assertEqual(row['origin'],
                                 {'layer': 'base-%03d'
                                  % base_layer[index],
                                  'path': 'f/%06d' % index})
            # Deep forward chain resolved iteratively to one object.
            for index in range(CHAIN_LENGTH):
                row = rows['h/%05d' % index]
                self.assertEqual(row['sha256'], ROOT_SHA)
                self.assertEqual(row['origin'],
                                 {'layer': 'chain', 'path': 'chain/root'})

            exposed = {}
            for path, (_digest, component, _layer) in expected_paths.items():
                if component is not None:
                    exposed.setdefault(component, []).append(path)
            components = business['components']
            self.assertEqual([row['id'] for row in components],
                             sorted(exposed))
            for row in components:
                self.assertEqual(row['paths'], sorted(exposed[row['id']]))
                self.assertEqual(row['status'], 'allowed')
            expected_selected = {
                'c0': {'licenses': ['MIT'], 'obligations': ['notice']},
                'c1': {'licenses': ['Apache-2.0'],
                       'obligations': ['notice', 'patent']},
                'c2': {'licenses': ['Apache-2.0', 'MIT'],
                       'obligations': ['notice', 'patent']},
                'c3': {'licenses': ['MIT'], 'obligations': ['notice']},
            }
            for row in components:
                self.assertEqual(row['selected'],
                                 expected_selected[row['id']])

            obligations = {row['obligation']: row
                           for row in business['obligations']}
            self.assertEqual(set(obligations), {'notice', 'patent'})
            for row in obligations.values():
                self.assertTrue(row['satisfied'])
            self.assertEqual(obligations['notice']['components'],
                             ['c0', 'c1', 'c2', 'c3'])
            self.assertEqual(obligations['patent']['components'],
                             ['c1', 'c2'])
            self.assertEqual(obligations['notice']['paths'],
                             ['f/000001'])
            self.assertEqual(len(obligations['patent']['paths']),
                             CHAIN_LENGTH + 1)
            self.assertTrue(business['compliant'])
            # The report is substantial but produced in one ordinary process.
            self.assertGreater(report_path.stat().st_size, 1_000_000)
            self.assertLess(elapsed, 120)

            print('\n--- layers scale evidence ---')
            print('input bytes: %d, layers: %d, operations: %d, files: %d'
                  % (len(raw), len(data['layers']), operations,
                     len(business['files'])))
            print('forward chain: %d, retained old-object links: %d, '
                  'elapsed: %.2fs'
                  % (CHAIN_LENGTH, OVERWRITTEN, elapsed))


if __name__ == '__main__':
    unittest.main()