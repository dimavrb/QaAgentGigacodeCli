#!/usr/bin/env python3
"""Build or verify a business/configuration test-report snapshot (stdlib only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from test_reporting import build_report, check_bundle, render_markdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', type=Path)
    parser.add_argument('--runs', type=Path)
    parser.add_argument('--revision')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output-dir', type=Path, help='New directory; existing snapshots are never overwritten')
    mode.add_argument('--check', type=Path, help='Verify JSON, Markdown, counters and all local source hashes')
    args = parser.parse_args()
    if args.check and any((args.scope, args.runs, args.revision)):
        parser.error('--check uses saved inputs; do not combine with generation arguments')
    if not args.check and not all((args.scope, args.runs, args.revision)):
        parser.error('building requires --scope, --runs and --revision')
    try:
        if args.check:
            report = check_bundle(args.check)
        else:
            report = build_report(args.scope, args.runs, args.revision)
            directory = args.output_dir.expanduser().resolve()
            directory.parent.mkdir(parents=True, exist_ok=True)
            directory.mkdir()  # Exclusive creation, including rejecting symlinks.
            (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            (directory / 'report.md').write_text(render_markdown(report), encoding='utf-8')
            check_bundle(directory)
        print(f"valid test report {report['report_revision']}: {report['counts']['target_executed']}/{report['counts']['planned']} target checks executed")
        print('Source integrity and aggregation only; coordinator content review and release decision remain separate.')
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print(f'Report error: {exc}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
