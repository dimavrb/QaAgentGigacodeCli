#!/usr/bin/env python3
"""Record, verify and summarize QA process incidents (POSIX, stdlib only)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from failure_journal import append_entry, find_recoveries, read_journal, render_summary, summarize, STAGES, SYMPTOMS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('init', 'record', 'check', 'summarize', 'find'))
    parser.add_argument('--journal', required=True, type=Path)
    parser.add_argument('--entry', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--scope-kind', choices=('operational', 'synthetic', 'all'), default='operational')
    parser.add_argument('--query', help='Keywords of a similar failure; all terms must match')
    parser.add_argument('--agent', help='Affected role for experience search')
    parser.add_argument('--stage', choices=sorted(STAGES))
    parser.add_argument('--symptom', choices=sorted(SYMPTOMS))
    parser.add_argument('--context-file', type=Path, help='JSON object of known current conditions')
    parser.add_argument('--limit', type=int, help='Maximum candidates for find (default 5, maximum 20)')
    args = parser.parse_args()
    if (args.command == 'record') != bool(args.entry):
        parser.error('--entry is required only for record')
    if (args.command == 'summarize') != bool(args.output_dir):
        parser.error('--output-dir is required only for summarize')
    filters = (args.query, args.agent, args.stage, args.symptom, args.context_file, args.limit)
    if args.command != 'find' and any(value is not None for value in filters):
        parser.error('search options are supported only for find')
    try:
        args.journal = args.journal.expanduser().resolve()
        if args.command == 'init':
            args.journal.parent.mkdir(parents=True, exist_ok=True)
            with args.journal.open('xb'):
                pass
            print('Empty failure journal created')
            return 0
        if args.command == 'record':
            journal = args.journal.expanduser().resolve()
            source = args.entry.expanduser().resolve()
            if source in (journal, journal.with_name(journal.name + '.lock')):
                raise ValueError('entry must not be the journal or its lock')
            entry = json.loads(source.read_text(encoding='utf-8'))
            added = append_entry(journal, entry, source.parent)
            print('Incident recorded' if added else 'Same event already recorded; no duplicate added')
            return 0
        records, fingerprint = read_journal(args.journal)
        if args.command == 'find':
            context = json.loads(args.context_file.read_text(encoding='utf-8')) if args.context_file else None
            result = find_recoveries(records, fingerprint, query=args.query or '', agent=args.agent,
                                     stage=args.stage, symptom=args.symptom, context=context,
                                     limit=args.limit if args.limit is not None else 5, scope_kind=args.scope_kind)
            result['journal'] = str(args.journal)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        summary = summarize(records, fingerprint, args.scope_kind)
        if args.command == 'check':
            # Check all scopes; an old superseded proof remains in history but is not current.
            summary = summarize(records, fingerprint, 'all')
            issues = sum(len(item[field]) for item in summary['incidents_detail']
                         for field in ('evidence_issues', 'cause_evidence_issues', 'recovery_evidence_issues'))
            print(f"Valid history: {len(records)} events; current evidence limitations: {issues}")
            return 1 if issues else 0
        output = args.output_dir.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.mkdir()
        summary['journal'] = str(args.journal.expanduser().resolve())
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (output / 'summary.md').write_text(render_summary(summary), encoding='utf-8')
        print(f"Summary: {summary['incidents']} incidents; {output / 'summary.md'}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print('Failure journal error: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
