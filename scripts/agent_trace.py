"""Small local trace records; no prompts, secrets or model reasoning in events."""
import fcntl
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_identity():
    return dict(agent_version=(ROOT / 'VERSION').read_text().strip(),
                coordinator_sha256=hashlib.sha256((ROOT / 'docs/COORDINATOR.md').read_bytes()).hexdigest())


def context(task_id, request_id=None, attempt_id=None, agent=None, trace_id=None, stage=None):
    return dict(trace_id=trace_id or secrets.token_hex(16), task_id=task_id,
                request_id=request_id, attempt_id=attempt_id, agent=agent, stage=stage,
                **package_identity())


def record(ctx, kind, **metrics):
    return dict(schema_version=1, event_id=secrets.token_hex(16),
                created_at=datetime.now(timezone.utc).isoformat(), kind=kind,
                **ctx, **metrics)


def append(path, ctx, kind, **metrics):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    value = record(ctx, kind, **metrics)
    # A partial tail is an error, never silently followed by another event.
    with target.open('a+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            if not line.endswith('\n'):
                raise ValueError('Trace has an incomplete tail')
            json.loads(line)
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return value


def registry_context(state, rid=None):
    row = state['requests'].get(rid, {})
    assignment = row.get('assignment', {})
    return context(state['task_id'], rid, row.get('attempt_id'), assignment.get('agent'),
                   trace_id=state.get('trace_id') or hashlib.sha256(state['task_id'].encode()).hexdigest()[:32],
                   stage=assignment.get('stage'))
