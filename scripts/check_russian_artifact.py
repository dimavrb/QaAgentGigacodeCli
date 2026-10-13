#!/usr/bin/env python3
"""Gate Russian artifacts; literal defect quotations require bounded source exceptions."""
import argparse
import json
from pathlib import Path
from benchmark_grading import read, sha
from evidence_language import text_errors


def check_text(text, exceptions, primary_root, artifact_path):
    spans = []
    for item in exceptions:
        if not isinstance(item, dict) or type(item.get('start')) is not int or type(item.get('end')) is not int:
            raise ValueError('literal exception needs explicit character start/end')
        start, end = item['start'], item['end']
        quote = item.get('quote')
        if not isinstance(quote, str) or not quote or start < 0 or end > len(text) or start >= end or text[start:end] != quote:
            raise ValueError('exception span differs from the artifact quotation')
        if primary_root is None: raise ValueError('exceptions require independent --primary-source-root')
        source = Path(item['source_path']).resolve()
        if not source.is_relative_to(Path(primary_root).resolve()) or source == Path(artifact_path).resolve():
            raise ValueError('exception source is not an independent primary artifact')
        if sha(source) != item.get('source_sha256') or quote not in source.read_text():
            raise ValueError('literal exception is not present in its frozen source')
        if any(start < old_end and end > old_start for old_start, old_end in spans):
            raise ValueError('overlapping exception spans')
        spans.append((start, end))
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text_errors(text)


def check_json(value, exceptions, primary_root, artifact_path):
    by_pointer = {}
    for item in exceptions:
        if not isinstance(item, dict) or not isinstance(item.get('pointer'), str):
            raise ValueError('JSON quotation exceptions require a string pointer')
        by_pointer.setdefault(item['pointer'], []).append(item)
    seen, errors = set(), []
    def visit(node, pointer=''):
        if isinstance(node, str):
            seen.add(pointer)
            errors.extend(pointer + ': ' + e for e in check_text(node, by_pointer.get(pointer, []), primary_root, artifact_path))
        elif isinstance(node, dict):
            for key, item in node.items():
                token = key.replace('~', '~0').replace('/', '~1')
                visit(item, pointer + '/' + token)
        elif isinstance(node, list):
            for index, item in enumerate(node): visit(item, pointer + '/' + str(index))
    visit(value)
    if set(by_pointer) - seen: raise ValueError('quotation exception references an unknown string pointer')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--exceptions', type=Path, help='JSON list of exact source-backed quotation spans')
    parser.add_argument('--primary-source-root', type=Path)
    args = parser.parse_args()
    try:
        exceptions = read(args.exceptions) if args.exceptions else []
        if not isinstance(exceptions, list): raise ValueError('exceptions must be a list')
        if args.input.suffix.lower() == '.json':
            errors = check_json(read(args.input), exceptions, args.primary_source_root, args.input)
        else:
            errors = check_text(args.input.read_text(), exceptions, args.primary_source_root, args.input)
        if errors: raise ValueError('; '.join(errors))
        print('Russian artifact writing-system gate passed')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Russian artifact error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
