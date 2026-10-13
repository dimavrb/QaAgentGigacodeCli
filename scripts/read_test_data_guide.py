#!/usr/bin/env python3
"""Print the mandatory checklist and only the requested test-data guide sections."""
from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECTIONS = {
    'composition': 'Состав набора до создания',
    'recipe': 'Рецепт и фикстура',
    'isolation': 'Изоляция, повтор и время',
    'cleanup': 'Готовность и очистка',
    'profile': 'Компактный профиль и передача',
    'review': 'Ревью данных',
}
ROLE_SECTIONS = {
    'qa-test-data-builder': ('composition', 'profile'),
    'qa-test-automation': ('recipe',),
    'qa-autotest-reviewer': ('review',),
}


def selected_sections(role: str, extra=(), additional_only=False) -> tuple[str, ...]:
    if role not in ROLE_SECTIONS:
        raise ValueError(f'Unknown test-data role: {role}')
    defaults = () if additional_only else ROLE_SECTIONS[role]
    result = tuple(dict.fromkeys((*defaults, *extra)))
    if not result:
        raise ValueError('At least one additional section is required')
    if any(key not in SECTIONS for key in result):
        raise ValueError('Unknown test-data section')
    return result


def build_context(root: Path, role: str, extra=(), additional_only=False) -> str:
    """File reads happen locally; unselected text is never included in the prompt."""
    selected = selected_sections(role, extra, additional_only)
    checklist = ''
    if not additional_only:
        checklist = (root / 'docs/TEST_DATA_CHECKLIST.md').read_text(encoding='utf-8').strip()
        if not checklist:
            raise ValueError('Empty test-data checklist')
    sections = {}
    current = None
    for line in (root / 'docs/TEST_DATA.md').read_text(encoding='utf-8').splitlines():
        if line.startswith('## '):
            current = line[3:].strip()
            if current in sections:
                raise ValueError(f'Duplicate test-data heading: {current}')
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    for heading in SECTIONS.values():
        if heading not in sections or not any(line.strip() for line in sections[heading][1:]):
            raise ValueError(f'Missing or empty test-data section: {heading}')
    parts = ([checklist] if checklist else [])
    parts.append(f'Разделы для {role}: ' + ', '.join(selected) + '.')
    parts.extend('\n'.join(sections[SECTIONS[key]]).strip() for key in selected)
    return '\n\n'.join(parts) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', required=True, choices=tuple(ROLE_SECTIONS))
    parser.add_argument('--section', action='append', default=[], choices=tuple(SECTIONS),
                        help='Add a section when the task requires more detail (repeatable)')
    parser.add_argument('--only-sections', action='store_true',
                        help='Print only additional sections when the checklist was already supplied')
    args = parser.parse_args()
    try:
        content = build_context(ROOT, args.role, args.section, args.only_sections)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(2, f'Test-data context error: {exc}\n')
    print(content, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
