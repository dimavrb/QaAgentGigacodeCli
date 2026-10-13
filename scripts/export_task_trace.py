#!/usr/bin/env python3
"""Export committed registry events as trace JSONL without delivery tokens."""
import argparse
import json
from pathlib import Path

from agent_trace import registry_context
from task_exchange import read_state


def export_records(state):
    rows = []
    for event in state['events']:
        ctx = event.get('trace')
        if ctx is None:
            ctx = registry_context(state, event.get('request_id'))
            ctx.update(agent_version=None, coordinator_sha256=None, attempt_id=None)
        row = dict(schema_version=1, event_id=f"registry:{event['event_id']}",
                   created_at=event['created_at'], kind=event['kind'],
                   **ctx)
        # Whitelist numeric/ID fields. Free-text decisions and dispatch credentials stay in the registry.
        for key in ('criterion_ids', 'qa_outcome'):
            if key in event:
                row[key] = event[key]
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New export file; never overwrite')
    args = parser.parse_args()
    try:
        rows = export_records(read_state(args.state))
        with args.output.open('x', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        print(json.dumps(dict(events=len(rows), output=str(args.output.resolve()))))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Trace export error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
