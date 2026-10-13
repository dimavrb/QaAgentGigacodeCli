#!/usr/bin/env python3
"""Compute procedure dispersion from repeated complete grading of fixed answers."""
import argparse
import json
from pathlib import Path
from benchmark_grading import read, write, sha
from benchmark_summary import calibration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='Config pointing to independent evaluation files')
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result, sources = calibration(read(args.input))
        if not args.session_id.strip(): raise ValueError('session-id must not be empty')
        sources[str(args.input.resolve())] = sha(args.input)
        result.update(session_id=args.session_id, source_hashes=sources)
        write(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Dispersion gate error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
