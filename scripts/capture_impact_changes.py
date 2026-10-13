#!/usr/bin/env python3
"""Capture committed Git changes between exact revisions without changing the repo."""
import argparse
import json
from pathlib import Path
import subprocess


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, timeout=60).stdout


def capture(repo, base, target):
    revisions = [git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()
                 for ref in (base, target)]
    raw = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--find-renames', '--name-status', '-z', *revisions, '--')
    tokens = raw.decode('utf-8').split('\0')
    result, index = [], 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index]
        count = 2 if status.startswith(('R', 'C')) else 1
        paths = tokens[index + 1:index + 1 + count]
        if len(paths) != count or any(not p for p in paths):
            raise ValueError('invalid Git name-status output')
        result.append(dict(change_id=f'CH-{len(result)+1:03d}', status=status, paths=paths,
                           evidence=[f'git:{revisions[0]}..{revisions[1]}']))
        index += count + 1
    patch = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--find-renames', '--patch', *revisions, '--').decode('utf-8', errors='replace')
    return dict(schema_version=1, base_revision=revisions[0], target_revision=revisions[1], complete=True,
                source='git_committed_diff', changes=result, diff=patch,
                limitations=['Only committed Git changes. Runtime configuration, external contracts and requirements need separate evidence.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--base', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = capture(args.repo, args.base, args.target)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        print(str(exc)); return 2
    print(f"Captured {len(result['changes'])} changes between resolved commits")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
