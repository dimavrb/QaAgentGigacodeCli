"""Validate investigation records and prepare a continuation without browser actions."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from defect_verification import validate_verification

CONTEXT_FIELDS = ('environment_id', 'product_version', 'requirements_revision', 'data_revision')
REVIEW_CHECKS = ('expected_source', 'actual_attempt', 'evidence_support', 'observation_vs_cause', 'limitations')
OUTCOMES = ('defect_confirmed', 'not_reproduced', 'expected_behavior', 'inconclusive')


def text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def strings(value: Any, nonempty: bool = False) -> bool:
    return isinstance(value, list) and (bool(value) or not nonempty) and all(text(x) for x in value)


def timestamp(value: Any) -> bool:
    try:
        return text(value) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def context_errors(value: Any, prefix: str = 'context') -> list[str]:
    if not isinstance(value, dict):
        return [f'{prefix} must be an object']
    return [f'{prefix}.{k} must be a non-empty string' for k in CONTEXT_FIELDS if not text(value.get(k))]


def fingerprint(attempt: dict) -> tuple:
    context = attempt['context']
    return (tuple(context[k] for k in CONTEXT_FIELDS), tuple(attempt['preconditions']), tuple(attempt['steps']))


def validate_state(state: Any, task_id: str, base: Path) -> list[str]:
    if not isinstance(state, dict):
        return ['state must be an object']
    errors = []
    if state.get('schema_version') != 1:
        errors.append('schema_version must equal 1')
    if 'verification_required' in state and type(state['verification_required']) is not bool:
        errors.append('verification_required must be a boolean')
    if state.get('task_id') != task_id:
        errors.append('state task_id does not match assignment')
    for key in ('request_id', 'expected_source'):
        if not text(state.get(key)):
            errors.append(f'{key} must be a non-empty string')
    used_requests = state.get('used_request_ids')
    if not strings(used_requests, nonempty=True) or len(set(used_requests)) != len(used_requests):
        errors.append('used_request_ids must contain unique assignment IDs')
        used_requests = []
    if state.get('request_id') not in used_requests:
        errors.append('current request_id must be recorded in used_request_ids')
    errors.extend(context_errors(state.get('context')))
    if not timestamp(state.get('updated_at')):
        errors.append('updated_at must be an ISO timestamp with timezone')
    if state.get('execution_status') not in ('queued', 'running', 'waiting', 'completed', 'failed'):
        errors.append('unknown execution_status')
    if not strings(state.get('blockers')):
        errors.append('blockers must be a list of non-empty strings')
    next_step = state.get('next_step')
    if not isinstance(next_step, dict) or any(not text(next_step.get(k)) for k in ('goal', 'stop_condition', 'expected_artifact')):
        errors.append('next_step needs goal, stop_condition and expected_artifact')
    hypotheses = state.get('hypotheses')
    ids = set()
    if not isinstance(hypotheses, list):
        errors.append('hypotheses must be a list')
        hypotheses = []
    for h in hypotheses:
        if not isinstance(h, dict) or not text(h.get('hypothesis_id')) or not text(h.get('statement')) or h.get('status') not in ('open', 'supported', 'refuted'):
            errors.append('hypothesis needs id, statement and valid status')
            continue
        if h['hypothesis_id'] in ids:
            errors.append('duplicate hypothesis_id')
        ids.add(h['hypothesis_id'])
    attempts = state.get('attempts')
    if not isinstance(attempts, list):
        errors.append('attempts must be a list')
        attempts = []
    by_id, seen = {}, {}
    for a in attempts:
        if not isinstance(a, dict):
            errors.append('attempt must be an object')
            continue
        prefix = f"attempt {a.get('attempt_id', '?')}"
        for key in ('attempt_id', 'request_id', 'hypothesis_id', 'actual_result'):
            if not text(a.get(key)):
                errors.append(f'{prefix}: {key} required')
        if a.get('request_id') not in used_requests:
            errors.append(f'{prefix}: request_id not recorded in used_request_ids')
        if not text(a.get('hypothesis_id')) or a['hypothesis_id'] not in ids:
            errors.append(f'{prefix}: unknown hypothesis_id')
        if a.get('status') not in ('executed', 'blocked'):
            errors.append(f'{prefix}: status must be executed or blocked')
        if not timestamp(a.get('started_at')):
            errors.append(f'{prefix}: started_at needs timezone')
        ce = context_errors(a.get('context'), prefix + '.context')
        errors.extend(ce)
        valid_lists = True
        for key in ('preconditions', 'steps', 'observations', 'evidence_ids'):
            if not strings(a.get(key), nonempty=key in ('preconditions', 'steps', 'observations') and a.get('status') == 'executed'):
                errors.append(f'{prefix}: invalid {key}')
                valid_lists = False
        aid = a.get('attempt_id')
        if not text(aid):
            continue
        if aid in by_id:
            errors.append(f'{prefix}: duplicate attempt_id')
        previous = a.get('repeat_of')
        if previous is not None:
            if not text(previous) or previous not in by_id or not text(a.get('repeat_reason')):
                errors.append(f'{prefix}: repeat needs an earlier repeat_of and repeat_reason')
        if not ce and valid_lists:
            fp = fingerprint(a)
            if fp in seen and previous != seen[fp]:
                errors.append(f'{prefix}: repeated actions need repeat_of={seen[fp]} and a reason')
            seen[fp] = aid
        by_id[aid] = a
    if errors:
        return errors
    evidence = state.get('evidence')
    if not isinstance(evidence, list):
        errors.append('evidence must be a list')
        evidence = []
    evidence_ids = set()
    for e in evidence:
        if not isinstance(e, dict) or any(not text(e.get(k)) for k in ('evidence_id', 'attempt_id', 'path', 'description')):
            errors.append('evidence needs id, attempt_id, path and description')
            continue
        eid = e['evidence_id']
        if eid in evidence_ids:
            errors.append('duplicate evidence_id')
        evidence_ids.add(eid)
        owner = by_id.get(e['attempt_id'])
        if owner is None or eid not in owner.get('evidence_ids', []):
            errors.append(f'{eid}: evidence is not linked to its attempt')
        if urlparse(e['path']).scheme in ('http', 'https'):
            continue  # Remote availability/content is checked by the coordinator, never inferred here.
        path = Path(e['path'])
        path = path if path.is_absolute() else base / path
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != e.get('sha256'):
                errors.append(f'{eid}: artifact hash differs or sha256 is missing')
        except OSError:
            errors.append(f'{eid}: artifact cannot be read: {e["path"]}')
    for a in by_id.values():
        for eid in a.get('evidence_ids', []):
            if eid not in evidence_ids:
                errors.append(f'{a["attempt_id"]}: unknown evidence_id {eid}')
            elif not any(e.get('evidence_id') == eid and e.get('attempt_id') == a['attempt_id'] for e in evidence if isinstance(e, dict)):
                errors.append(f'{a["attempt_id"]}: evidence belongs to another attempt')
    return errors


def continuation(state: dict, current: dict, request_id: str, base: Path) -> dict:
    errors = validate_state(state, state.get('task_id'), base)
    if not isinstance(current, dict):
        errors.append('current observation must be an object')
        current = {}
    errors.extend(context_errors(current.get('context'), 'current.context'))
    if not timestamp(current.get('verified_at')) or not strings(current.get('verification_evidence'), nonempty=True):
        errors.append('current observation needs verified_at and verification_evidence from a fresh check')
    for reference in current.get('verification_evidence', []) if strings(current.get('verification_evidence')) else []:
        if urlparse(reference).scheme not in ('http', 'https') and not (base / reference).is_file():
            errors.append('current verification artifact cannot be read: ' + reference)
    if errors:
        return {'status': 'invalid', 'errors': errors}
    if datetime.fromisoformat(current['verified_at'].replace('Z', '+00:00')) < datetime.fromisoformat(state['updated_at'].replace('Z', '+00:00')):
        errors.append('current observation predates saved state; verify conditions again')
    used = set(state['used_request_ids'])
    if not text(request_id) or request_id in used:
        errors.append('continuation requires a new request_id')
    if errors:
        return {'status': 'invalid', 'errors': errors}
    unknown = {'unknown', 'неизвестно', 'не настроено'}
    changed = [k for k in CONTEXT_FIELDS if state['context'][k] != current['context'][k] or current['context'][k].strip().casefold() in unknown]
    return {
        'status': 'revalidate_conditions' if changed else 'resume_ready',
        'task_id': state['task_id'], 'request_id': request_id,
        'changed_conditions': changed,
        'previous_attempts': [a['attempt_id'] for a in state['attempts']],
        'do_not_repeat_without_reason': [a['attempt_id'] for a in state['attempts'] if a['context'] == current['context']],
        'open_hypotheses': [h['hypothesis_id'] for h in state['hypotheses'] if h['status'] == 'open'],
        'blockers': state['blockers'], 'next_step': state['next_step'],
        'note': 'Verify actual conditions before acting; this output does not execute or authorize browser actions.',
    }


def review_digest(state: dict) -> str:
    record = {key: value for key, value in state.items() if key != 'review'}
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def result_digest(message: dict) -> str:
    return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_review(message: dict, state: dict, base: Path) -> list[str]:
    """Check recorded coordinator assessment; never judge artifact content automatically."""
    errors = validate_state(state, message['task_id'], base)
    if errors:
        return errors
    payload = message['payload']
    if state['request_id'] != message['request_id']:
        errors.append('state request_id does not match result')
    if state['expected_source'] != payload.get('expected_source'):
        errors.append('expected_source differs between state and result')
    attempt_ids = payload.get('attempt_ids')
    if not strings(attempt_ids, nonempty=True) or len(set(attempt_ids)) != len(attempt_ids):
        errors.append('state-backed investigation needs unique payload.attempt_ids')
        attempt_ids = []
    attempts = {a['attempt_id']: a for a in state['attempts']}
    if any(aid not in attempts for aid in attempt_ids):
        errors.append('result refers to an unknown attempt')
    linked = [a for aid, a in attempts.items() if aid in attempt_ids]
    refs = {e['path'] for e in state['evidence'] if e['attempt_id'] in attempt_ids}
    if not strings(payload.get('evidence'), nonempty=True) or not set(payload.get('evidence', [])).issubset(refs):
        errors.append('result evidence must link to its named attempts')
    if payload.get('outcome') in ('defect_confirmed', 'not_reproduced', 'expected_behavior') and not any(a['status'] == 'executed' for a in linked):
        errors.append('conclusive outcome needs an executed attempt')
    if not errors and (state.get('verification_required') or 'verification_artifact' in payload):
        errors.extend(validate_verification(message, state, base))
    review = state.get('review')
    if not isinstance(review, dict):
        return errors + ['coordinator review is required; structure alone cannot confirm a defect']
    if review.get('request_id') != message['request_id'] or review.get('outcome') != payload.get('outcome'):
        errors.append('review must refer to this request_id and outcome')
    if review.get('state_sha256') != review_digest(state):
        errors.append('coordinator review is stale: state changed after assessment')
    if review.get('result_sha256') != result_digest(message):
        errors.append('coordinator review is stale: result differs from assessed report')
    if review.get('decision') != 'passed':
        errors.append('coordinator review has not passed')
    checks = review.get('checks', {})
    if not isinstance(checks, dict):
        checks = {}
    for key in REVIEW_CHECKS:
        check = checks.get(key)
        if not isinstance(check, dict) or check.get('status') != 'passed' or not text(check.get('note')):
            errors.append(f'coordinator check {key} needs passed and a concrete assessment note')
    return errors
