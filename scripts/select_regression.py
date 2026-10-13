#!/usr/bin/env python3
"""Build a conservative regression recommendation. Does not execute tests."""
import argparse
import json
from pathlib import Path

from impact_analysis import build_selection, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='Coordinator-owned frozen impact input')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--additional-scenarios', type=Path, help='JSON list of evidence-backed additions; cannot remove tests')
    args = parser.parse_args()
    try:
        additions = read(args.additional_scenarios) if args.additional_scenarios else []
        if not isinstance(additions, list):
            raise ValueError('additional-scenarios must contain a JSON list')
        result = build_selection(args.input, additions)
        # Exclusive creation also protects symlinks and every frozen source snapshot.
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        print(str(exc))
        return 2
    print(f"{result['selection_mode']}: {len(result['run_scenario_ids'])} run, {len(result['excluded_scenario_ids'])} excluded")
    print('Recommendation only. Source completeness and exclusions need independent review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
