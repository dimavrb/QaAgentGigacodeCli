#!/usr/bin/env python3
"""Read selected package instruction sections or role frontmatter without its body."""
import argparse
from pathlib import Path
import re

from context_builder import sections, select

ROOT = Path(__file__).resolve().parents[1]


def document_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to((root / 'docs').resolve()) or path.suffix != '.md':
        raise ValueError('Choose a Markdown document inside package docs/')
    return path


def read_sections(root, name, names=None):
    text = document_path(root, name).read_text(encoding='utf-8')
    if names is None:
        return '\n'.join(repr(name) for name in sections(text)) + '\n'
    # Keep the preamble: shared constraints outside named sections still apply.
    return select(text, list(dict.fromkeys(['', *names])))


def role_metadata(root, agent):
    if not re.fullmatch(r'qa-[a-z][a-z0-9-]*', agent):
        raise ValueError('Invalid QA role name')
    text = (root / '.gigacode/agents' / (agent + '.md')).read_text(encoding='utf-8')
    lines = text.splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError('Role frontmatter is missing or incomplete')
    return '\n'.join(lines[:lines.index('---', 1) + 1]) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    document = sub.add_parser('document')
    document.add_argument('--path', required=True, help='Package-relative docs/*.md path')
    choice = document.add_mutually_exclusive_group(required=True)
    choice.add_argument('--list', action='store_true', help='List exact level-two headings')
    choice.add_argument('--section', action='append', help='Read a section; repeat to combine')
    role = sub.add_parser('role')
    role.add_argument('--agent', required=True)
    args = parser.parse_args()
    try:
        content = (role_metadata(ROOT, args.agent) if args.command == 'role'
                   else read_sections(ROOT, args.path, None if args.list else args.section))
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(2, f'QA instruction read error: {exc}\n')
    print(content, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
