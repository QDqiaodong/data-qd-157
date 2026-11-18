DESCRIPTION = 'SPDX expression policy alternatives and obligation evidence'
import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from .inventory import run_inventory
from .model import analyze
from .warehouse import import_observations, open_warehouse, window_report


def run(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest,
            'result': analyze(data)}


def _distinct_paths(paths):
    resolved = [Path(path).resolve() for path in paths if path is not None]
    if len(set(resolved)) != len(resolved):
        raise ValueError('input, database and output paths must all differ')


def _mode(args):
    has_source = args.source is not None
    has_output = args.output is not None
    has_warehouse = args.warehouse is not None
    has_observations = args.observations is not None
    has_window = args.window is not None
    has_inventory = args.inventory is not None
    if has_inventory:
        if (has_source or has_warehouse or has_observations or has_window
                or not has_output):
            raise ValueError(
                'inventory mode requires exactly --inventory and --output')
        return 'inventory'
    if has_source and not has_output:
        raise ValueError('--source requires --output')
    if has_output and not (has_source or has_window):
        raise ValueError('--output requires --source or --window')
    if has_source:
        if has_warehouse or has_observations or has_window:
            raise ValueError('the three operating modes are mutually exclusive')
        return 'legacy'
    if has_observations:
        if has_window or has_output or not has_warehouse:
            raise ValueError(
                'warehouse import requires exactly --warehouse and --observations')
        return 'import'
    if has_window:
        if not has_warehouse or not has_output:
            raise ValueError(
                'window mode requires exactly --warehouse, --window and --output')
        return 'window'
    raise ValueError(
        'choose --source/--output, --warehouse/--observations, '
        '--warehouse/--window/--output or --inventory/--output')


def _run_import(args):
    _distinct_paths([args.warehouse, args.observations])
    data, digest = read(args.observations)
    conn = open_warehouse(args.warehouse, create=True)
    try:
        receipt = import_observations(conn, data, digest)
    finally:
        conn.close()
    print(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))


def _run_window(args):
    _distinct_paths([args.warehouse, args.window, args.output])
    data, digest = read(args.window)
    conn = open_warehouse(args.warehouse, create=False, readonly=True)
    try:
        report = window_report(conn, data, digest)
    finally:
        conn.close()
    save(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--warehouse', type=Path)
    parser.add_argument('--observations', type=Path)
    parser.add_argument('--window', type=Path)
    parser.add_argument('--inventory', type=Path)
    args = parser.parse_args()
    try:
        mode = _mode(args)
        if mode == 'legacy':
            _distinct_paths([args.source, args.output])
            report = run(args.source)
            save(args.output, report)
            print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                             allow_nan=False))
        elif mode == 'inventory':
            try:
                receipt = run_inventory(args.inventory, args.output)
            except (KeyError, TypeError) as error:
                raise ValueError('invalid inventory input: %s' % error)
            print(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                             allow_nan=False))
        elif mode == 'import':
            _run_import(args)
        else:
            _run_window(args)
    except Exception as error:
        # Every contract violation, storage failure or I/O problem is an
        # input-level error for this single-shot CLI: message on stderr,
        # exit 2, never a traceback and never a half-written report.
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
