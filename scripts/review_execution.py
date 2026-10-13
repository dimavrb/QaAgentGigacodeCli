"""Fresh, scoped reviewer observations bound to a managed attempt and exact target."""
import copy
import hashlib
import json
import math
from pathlib import Path

from action_gateway import pointer


def policy_hash(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, allow_nan=False).encode()).hexdigest()


def equal(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def execution_specs(value):
    if not isinstance(value, list) or not value:
        raise ValueError('review execution needs a non-empty list of checks')
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError('execution check specification must be an object')
        for key in ('check_id', 'tool'):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError('execution specification needs check_id and tool')
        if row['check_id'] in seen:
            raise ValueError('duplicate execution check_id')
        seen.add(row['check_id'])
        for key in ('expected_arguments', 'expected_result'):
            refs = row.get(key)
            if (not isinstance(refs, dict) or not refs or
                    any(not isinstance(p, str) or not p.startswith('/') for p in refs)):
                raise ValueError(f'{key} needs explicit JSON pointer expectations')
            json.dumps(refs, allow_nan=False)
        if 'run_started_pointer' in row and (not isinstance(row['run_started_pointer'], str) or
                not row['run_started_pointer'].startswith('/')):
            raise ValueError('run_started_pointer must reference actual backend execution time')
        result.append(copy.deepcopy(row))
    return result


def validate_checks(payload, event):
    checks = payload.get('execution_checks', [])
    if not isinstance(checks, list):
        return ['execution_checks must be a list']
    errors, seen = [], set()
    for row in checks:
        if not isinstance(row, dict):
            errors.append('execution check must be an object')
            continue
        keys = (row.get('criterion_id'), row.get('check_id'))
        if any(not isinstance(k, str) or not k.strip() for k in keys):
            errors.append('execution check needs criterion_id/check_id')
        elif keys in seen:
            errors.append('duplicate execution check')
        else:
            seen.add(keys)
        if row.get('status') not in ('passed', 'failed', 'unknown'):
            errors.append('execution check needs passed/failed/unknown')
        if not isinstance(row.get('note'), str) or not row['note'].strip():
            errors.append('execution check needs a concrete note')
        receipt = row.get('receipt')
        if row.get('status') != 'unknown' or receipt is not None:
            if not isinstance(receipt, str) or not receipt.strip() or receipt.startswith(('http://', 'https://')):
                errors.append('observed execution check needs a local receipt')
        if event == 'review_passed' and row.get('status') != 'passed':
            errors.append('review_passed cannot have unverified execution checks')
    return errors


def context_for(path, sid, token):
    import execution_supervisor as supervisor
    state = supervisor.read(path)
    attempt = state['steps'][sid]['attempts'][-1]
    assignment = attempt['assignment']
    step = next(s for s in state['plan']['steps'] if s['step_id'] == sid)
    if (state['cancel_requested'] or attempt['token'] != token or attempt['status'] != 'running'
            or assignment['agent'] not in ('qa-result-reviewer', 'qa-autotest-reviewer', 'qa-conclusion-reviewer')
            or not assignment.get('review_target') or not step.get('gateway_policy')):
        raise ValueError('Review execution is not an owned, active, authorized attempt')
    import task_exchange as exchange
    for name, digest in assignment['review_target']['artifact_hashes'].items():
        if exchange.fingerprint(name) != digest:
            raise ValueError('Review target changed before/during execution')
    return dict(execution_state=str(Path(path).resolve()), step_id=sid, attempt_token=token,
                request_id=assignment['request_id'], policy_sha256=policy_hash(step['gateway_policy']),
                review_target=copy.deepcopy(assignment['review_target']))


def check_receipt(ref, assignment):
    import execution_supervisor as supervisor
    import task_exchange as exchange
    runtime = assignment.get('review_execution')
    if not isinstance(runtime, dict):
        raise ValueError('Execution evidence requires a managed reviewer policy')
    state = supervisor.read(runtime['execution_state'])
    sid = runtime['step_id']
    choices = [a for a in state['steps'][sid]['attempts'] if a['token'] == runtime['attempt_token']]
    if len(choices) != 1:
        raise ValueError('Unknown review execution attempt')
    attempt = choices[0]
    step = next(s for s in state['plan']['steps'] if s['step_id'] == sid)
    expected_context = dict(runtime, request_id=assignment['request_id'], review_target=assignment['review_target'])
    if (any(attempt['assignment'].get(k) != v for k, v in assignment.items() if k != 'inputs')
            or expected_context['policy_sha256'] != policy_hash(step['gateway_policy'])
            or state['task_id'] != assignment['task_id'] or attempt.get('category') is not None):
        raise ValueError('Review execution scope/attempt differs')
    registry = exchange.read_state(state['plan']['task_state'])
    if registry['requests'][assignment['request_id']]['owner_task_id'] != 'managed-worker:' + attempt['token']:
        raise ValueError('Review execution owner differs')
    path = Path(ref)
    if not path.is_absolute():
        path = Path(assignment['artifact_base']) / path
    path = path.resolve()
    journal_path = supervisor.action_path(runtime['execution_state'], sid)
    journal = exchange.read_json(journal_path)
    receipt = exchange.read_json(path)
    oid = receipt.get('action_id')
    row = journal.get('observations', {}).get(oid)
    if (journal.get('policy_sha256') != runtime['policy_sha256'] or
            journal.get('task_id') != assignment['task_id'] or journal.get('step_id') != sid or
            path.parent != journal_path.with_name(journal_path.name + '.receipts') or
            not isinstance(oid, str) or not path.name.startswith(oid + '.') or
            not row or row['status'] != 'verified' or row.get('fresh') is not True or receipt.get('fresh') is not True or
            str(path) != row['receipt_path'] or exchange.fingerprint(path) != row['receipt_sha256']
            or receipt.get('kind') != 'review_execution' or receipt.get('context') != expected_context
            or receipt.get('task_id') != assignment['task_id'] or receipt.get('step_id') != sid
            or receipt.get('result') != row.get('result') or receipt.get('arguments') != row.get('arguments')
            or receipt.get('tool') != row.get('tool')):
        raise ValueError('Receipt is not a fresh observation of this reviewer/target')
    for key in ('started_epoch', 'finished_epoch'):
        stamp = receipt.get(key)
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError('Execution receipt needs actual timestamps')
    if (receipt['started_epoch'] < attempt['launch_intent_at'] or
            receipt['finished_epoch'] < receipt['started_epoch'] or
            receipt['finished_epoch'] > attempt.get('finished_epoch', float('inf'))):
        raise ValueError('Execution observation falls outside its review attempt')
    if receipt['result'].get('isError'):
        raise ValueError('Failed tool call cannot verify execution')
    return receipt


def check_binding(payload, assignment):
    seen = set()
    for row in payload.get('execution_checks', []):
        if row['criterion_id'] not in assignment['review_criteria_ids']:
            raise ValueError('Execution check references an unassigned criterion')
        if row.get('receipt'):
            receipt = check_receipt(row['receipt'], assignment)
            oid = receipt['action_id']
            if oid in seen:
                raise ValueError('One execution receipt cannot replace multiple launches')
            seen.add(oid)


def covers(payload, assignment, criterion_id, specs):
    checks = [r for r in payload.get('execution_checks', []) if r['criterion_id'] == criterion_id]
    if {r['check_id'] for r in checks} != {s['check_id'] for s in specs}:
        return False
    for spec in specs:
        check = next(r for r in checks if r['check_id'] == spec['check_id'])
        if check['status'] != 'passed' or not check.get('receipt'):
            return False
        receipt = check_receipt(check['receipt'], assignment)
        if receipt['tool'] != spec['tool']:
            return False
        if spec.get('run_started_pointer'):
            try:
                started = pointer(receipt['result'], spec['run_started_pointer'])
            except ValueError:
                return False
            if (type(started) not in (int, float) or not math.isfinite(started) or
                    not receipt['started_epoch'] <= started <= receipt['finished_epoch']):
                return False
        for field, key in (('arguments', 'expected_arguments'), ('result', 'expected_result')):
            try:
                if any(not equal(pointer(receipt[field], ref), value) for ref, value in spec[key].items()):
                    return False
            except ValueError:
                return False
    return True
