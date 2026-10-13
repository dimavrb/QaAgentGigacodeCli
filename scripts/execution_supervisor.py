"""Durable, bounded local execution of explicitly planned QA assignments."""
from __future__ import annotations
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import tempfile

import action_gateway
import context_builder
import agent_runtime
import agent_trace
import failure_journal
import process_control
import task_exchange as exchange

ROOT = Path(__file__).resolve().parents[1]
CHILDREN = {}
TERMINAL = {'finished', 'abandoned'}


def number(value, field, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{field}: expected {low}..{high}')
    return value


def normalize_plan(source, base):
    if not isinstance(source, dict) or type(source.get('schema_version')) is not int or source['schema_version'] != 1:
        raise ValueError('Execution plan needs schema_version=1')
    plan = copy.deepcopy(source)
    plan['task_state'] = exchange.local_path(source.get('task_state'), base)
    plan['workspace'] = exchange.local_path(source.get('workspace'), base)
    if not Path(plan['workspace']).is_dir():
        raise ValueError('Execution workspace must exist')
    runtime = agent_runtime.load_runtime()
    adapter = plan.get('adapter', {'kind': 'gigacode', 'executable': runtime['executable'], 'backend': runtime['backend']})
    if not isinstance(adapter, dict) or adapter.get('kind') not in ('gigacode', 'cli', 'command'):
        raise ValueError('Adapter must be gigacode, cli or a trusted command adapter; legacy plans require an explicit new plan')
    if adapter['kind'] == 'command':
        if (not isinstance(adapter.get('argv'), list) or not adapter['argv']
                or any(not isinstance(arg, str) or not arg for arg in adapter['argv'])):
            raise ValueError('Command adapter needs explicit argv')
    else:
        if adapter['kind'] == 'cli' and (not adapter.get('executable') or not adapter.get('backend')):
            raise ValueError('Generic CLI adapter requires explicit executable and backend')
        adapter['executable'] = exchange.required(adapter.get('executable', runtime['executable']), 'executable')
        if adapter.get('expected_version') is not None:
            adapter['expected_version'] = exchange.required(adapter['expected_version'], 'expected_version')
    if adapter['kind'] != 'command':
        adapter['backend'] = exchange.required(adapter.get('backend', 'gigacode' if adapter['kind'] == 'gigacode' else 'cli'), 'backend')
    plan['adapter'] = adapter
    plan['scope_kind'] = plan.get('scope_kind', 'operational')
    if plan['scope_kind'] not in ('operational', 'synthetic'):
        raise ValueError('scope_kind must be operational/synthetic')
    plan['failure_journal'] = exchange.local_path(plan.get('failure_journal',
        str(Path(plan['workspace']) / 'artifacts/failures/agent-errors.jsonl')), base)
    plan['heartbeat_timeout_seconds'] = number(plan.get('heartbeat_timeout_seconds', 15), 'heartbeat timeout', 1, 300)
    plan['max_active_seconds'] = number(plan.get('max_active_seconds', 1800), 'task active budget', 1, 86400)
    plan['auto_apply_answers'] = plan.get('auto_apply_answers', False)
    if type(plan['auto_apply_answers']) is not bool:
        raise ValueError('auto_apply_answers must be bool')
    rows, seen, rids = plan.get('steps'), set(), set()
    if not isinstance(rows, list) or not rows:
        raise ValueError('Plan needs steps')
    for step in rows:
        if not isinstance(step, dict):
            raise ValueError('Step must be object')
        sid = exchange.required(step.get('step_id'), 'step_id')
        if not sid.replace('-', '').replace('_', '').isalnum() or sid in seen:
            raise ValueError('step_id must be unique and filesystem-safe')
        seen.add(sid)
        step['step_id'] = sid
        assignment = exchange.assignment_record(step.get('assignment'), base)
        if assignment['request_id'] in rids:
            raise ValueError('Base request IDs must be unique')
        rids.add(assignment['request_id'])
        step['assignment'] = assignment
        step['context_profile'] = context_builder.profile(assignment, step)
        step['context_mode'] = step['context_profile']['mode']
        step['instruction_paths'] = list(step['context_profile']['documents'])
        step['instruction_hashes'] = {name: exchange.fingerprint(ROOT / name) for name in step['instruction_paths']}
        step['context_builder_sha256'] = exchange.fingerprint(ROOT / 'scripts/context_builder.py')
        context_limit = step.get('max_context_characters', 200000)
        if type(context_limit) is not int or not 1000 <= context_limit <= 10000000:
            raise ValueError('max_context_characters must be an integer in 1000..10000000')
        step['max_context_characters'] = context_limit
        step['input_hashes'] = {path: exchange.fingerprint(path) for path in assignment['inputs']}
        step['wait_for_questionnaire_ids'] = exchange.id_list(step.get('wait_for_questionnaire_ids', []), 'wait_for_questionnaire_ids')
        step['retry_safe'] = step.get('retry_safe', False)
        if type(step['retry_safe']) is not bool:
            raise ValueError('retry_safe must be bool')
        step['max_attempts'] = step.get('max_attempts', 1)
        step['max_identical_failures'] = step.get('max_identical_failures', 2)
        for key in ('max_attempts', 'max_identical_failures'):
            if type(step[key]) is not int or not 1 <= step[key] <= 5:
                raise ValueError(f'{key} must be 1..5')
        step['retry_on'] = step.get('retry_on', [])
        if not isinstance(step['retry_on'], list) or set(step['retry_on']) - {'transient', 'timeout', 'process_lost', 'invalid_result'}:
            raise ValueError('Unknown retry category')
        step['retry_backoff_seconds'] = number(step.get('retry_backoff_seconds', 1), 'retry backoff', 0, 60)
        step['max_wall_seconds'] = number(step.get('max_wall_seconds', 300), 'step wall budget', .05, 86400)
        step['max_session_turns'] = step.get('max_session_turns', 30)
        if type(step['max_session_turns']) is not int or not 1 <= step['max_session_turns'] <= 200:
            raise ValueError('max_session_turns must be 1..200')
        codes = step.get('transient_exit_codes', [75])
        if not isinstance(codes, list) or any(type(code) is not int or not 1 <= code <= 125 for code in codes):
            raise ValueError('Transient exit codes must be explicit nonzero exit codes')
        step['transient_exit_codes'] = codes
        policy = step.get('gateway_policy')
        if policy is not None:
            step['gateway_policy'] = action_gateway.validate_policy(policy)
        if assignment.get('review_execution'):
            raise ValueError('review_execution binding is allocated by the supervisor, not the plan')
    return plan


def read(path):
    state = exchange.read_json(path)
    if state.get('schema_version') != 1 or not isinstance(state.get('steps'), dict) or not isinstance(state.get('plan'), dict):
        raise ValueError('Damaged execution state')
    if hashlib.sha256(json.dumps(state['plan'], sort_keys=True).encode()).hexdigest() != state['plan_sha256']:
        raise ValueError('Frozen execution plan changed')
    return state


def update(path, operation):
    with exchange.locked(path) as target:
        state = read(target)
        result = operation(state)
        state['revision'] += 1
        state['updated_at'] = exchange.now()
        exchange.atomic_write(target, state)
        return result


def note(state, kind, sid=None, **details):
    step = state['steps'].get(sid, {})
    attempt = step.get('attempts', [])[-1] if step.get('attempts') else {}
    state['events'].append(dict(event_id=len(state['events']) + 1, created_at=exchange.now(), kind=kind,
        trace_id=state['trace_id'], task_id=state['task_id'], step_id=sid,
        request_id=attempt.get('request_id'), attempt_id=attempt.get('attempt_id'),
        execution_attempt=attempt.get('index'), **details))


def initialize(path, source, base):
    plan = normalize_plan(source, base)
    registry = exchange.read_state(plan['task_state'])
    if registry['execution_status'] == 'completed':
        raise ValueError('Task already completed')
    paths = {str(Path(path).resolve()), str(Path(path).resolve()) + '.lock', plan['task_state'], plan['task_state'] + '.lock'}
    if plan['failure_journal'] in paths:
        raise ValueError('Failure journal cannot overwrite execution/registry state')
    paths.update({plan['failure_journal'], plan['failure_journal'] + '.lock'})
    private = Path(path).resolve().parent / (Path(path).name + '.runs')
    results = [step['assignment']['result_path'] for step in plan['steps']]
    for step in plan['steps']:
        if step['assignment']['task_id'] != registry['task_id']:
            raise ValueError('Plan assignment belongs to another task')
        if set(step['assignment']['criterion_ids']) - set(registry.get('criteria', {})):
            raise ValueError('Unknown task criteria')
        if step['assignment']['result_path'] in paths or set(step['assignment']['inputs']) & paths:
            raise ValueError('Execution/registry state cannot be overwritten or used as evidence')
        if any(Path(name).is_relative_to(private) for name in [step['assignment']['result_path'], *step['assignment']['inputs']]):
            raise ValueError('Private execution files cannot be assignment inputs/results')
    if len(set(results)) != len(results):
        raise ValueError('Planned result paths must be unique')
    with exchange.locked(path) as target:
        if target.exists():
            raise ValueError('Execution already exists; use tick/serve to continue')
        state = dict(schema_version=1, revision=1, task_id=registry['task_id'], trace_id=registry.get('trace_id'),
                     status='pending', cancel_requested=False, plan=plan,
                     plan_sha256=hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
                     created_at=exchange.now(), updated_at=exchange.now(), first_run_at=None, events=[],
                     steps={step['step_id']: dict(status='pending', attempts=[], checkpoint=None) for step in plan['steps']})
        note(state, 'execution_initialized')
        exchange.atomic_write(target, state)
    return state


@contextmanager
def controller(path):
    with Path(path).with_name(Path(path).name + '.controller.lock').open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another supervisor controls this execution') from None
        yield


def action_path(path, sid):
    return Path(path).parent / (Path(path).name + '.runs') / sid / 'actions.json'


def uncertain_actions(path, sid):
    journal = action_path(path, sid)
    if not journal.exists():
        return False
    return any(row['status'] in ('started', 'unknown') for row in exchange.read_json(journal)['actions'].values())


def safe_group_gone(attempt):
    worker = attempt.get('worker_identity')
    if not worker:
        return True  # Worker must publish identity under lock before launching the model.
    return not process_control.group_alive(worker['pgid'])


def active_seconds(state):
    total = 0
    for row in state['steps'].values():
        for attempt in row['attempts']:
            start = attempt.get('launch_intent_at')
            if start is not None:
                end = attempt.get('finished_epoch')
                if end is None and attempt['status'] in TERMINAL:
                    end = attempt.get('heartbeat_at') or start
                total += max(0, (end if end is not None else time.time()) - start)
    return total


def drain_terminal(path, sid, attempt):
    """Wait for a finishing owner, then stop remaining owned descendants."""
    if safe_group_gone(attempt):
        return True
    if (process_control.matches(attempt.get('worker_identity'))
            and time.time() - attempt.get('finished_epoch', attempt.get('heartbeat_at') or time.time()) < 3):
        return False
    try:
        return process_control.stop_group(attempt.get('worker_identity'), attempt.get('child_identity'))
    except (OSError, ValueError):
        def uncertain(value):
            value['steps'][sid]['status'] = value['status'] = 'needs_reconciliation'
            note(value, 'process_group_uncertain', sid)
        update(path, uncertain)
        return False


def registry_failure(state, attempt, reason):
    path = state['plan']['task_state']
    registry = exchange.read_state(path)
    row = registry['requests'].get(attempt['request_id'])
    if row and row['status'] in ('queued', 'dispatching', 'running'):
        exchange.fail_request(path, attempt['request_id'], reason, None)


def capture_result(state, attempt):
    path = state['plan']['task_state']
    registry = exchange.read_state(path)
    row = registry['requests'][attempt['request_id']]
    if row['status'] not in ('dispatching', 'running'):
        return row
    output = Path(attempt['run_dir']) / 'stdout.json'
    target = Path(row['assignment']['result_path'])
    data = output.read_bytes()
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError('Result path differs from captured output')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            try:
                os.link(temporary, target)  # Exclusive atomic publication; never replace evidence.
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ValueError('Concurrent result differs from captured output')
        finally:
            temporary.unlink(missing_ok=True)
    # Validate and transition against the locked current registry; unrelated answer
    # notifications must not turn a valid report into a false invalid_result.
    exchange.submit_result(path, attempt['request_id'], None)
    return exchange.read_state(path)['requests'][attempt['request_id']]


def questions_ready(state, step):
    path = state['plan']['task_state']
    for qid in step['wait_for_questionnaire_ids']:
        registry = exchange.read_state(path)
        row = registry['questions'].get(qid)
        if row and row['status'] == 'waiting':
            from questionnaire import check_saved_response, normalize_packet
            packet = normalize_packet(exchange.read_json(row['input_path']))
            check_saved_response(row['input_path'], row['answers_path'], packet, path)
            registry = exchange.read_state(path)
            row = registry['questions'][qid]
        if not row or row['status'] == 'waiting' or row['unresolved_question_ids']:
            return False
        if row['status'] == 'answers_ready' and state['plan']['auto_apply_answers']:
            # Pre-authorized binding: transfer validated answers as context, never broaden tool scope.
            exchange.apply_answers(path, qid, 'Ответы переданы в заранее согласованный этап; полномочия не расширены', registry['revision'])
        elif row['status'] != 'applied':
            return False
        exchange.check_answers(exchange.read_state(path), [qid])
    return True


def serve(path, max_seconds=1200, answers_poll_seconds=60):
    """Keep the controller alive; poll waiting answers slowly, active workers promptly."""
    number(max_seconds, 'serve duration', .1, 86400)
    number(answers_poll_seconds, 'answers poll interval', .01, 60)
    deadline = time.monotonic() + max_seconds
    next_tick = 0
    with controller(path):
        while True:
            state = read(path)
            if state['cancel_requested'] or state['status'] in ('completed', 'blocked', 'needs_reconciliation'):
                return state
            current = time.monotonic()
            if current >= next_tick or current >= deadline:
                state = tick_locked(path)
                interval = answers_poll_seconds if state['status'] == 'waiting_answers' else .2
                next_tick = time.monotonic() + interval
            if (state['cancel_requested'] or state['status'] in ('completed', 'blocked', 'needs_reconciliation')
                    or time.monotonic() >= deadline):
                return state
            time.sleep(max(0, min(.2, next_tick - time.monotonic(), deadline - time.monotonic())))


def effective_assignment(state, step, index):
    assignment = copy.deepcopy(step['assignment'])
    assignment['questionnaire_ids'] = list(dict.fromkeys(assignment['questionnaire_ids'] + step['wait_for_questionnaire_ids']))
    aliases = {plan_step['assignment']['request_id']: state['steps'][plan_step['step_id']]['checkpoint']['request_id']
               for plan_step in state['plan']['steps'] if state['steps'][plan_step['step_id']]['checkpoint']}
    assignment['depends_on'] = [aliases.get(rid, rid) for rid in assignment['depends_on']]
    assignment['dependency_modes'] = {aliases.get(rid, rid): mode for rid, mode in assignment['dependency_modes'].items()}
    if index > 1:
        assignment['request_id'] += f'.retry-{index}'
        result = Path(assignment['result_path'])
        assignment['result_path'] = str(result.with_name(result.stem + f'.retry-{index}' + result.suffix))
    return assignment


def prepare_attempt(path, sid, step):
    state = read(path)
    row = state['steps'][sid]
    if row['attempts'] and row['attempts'][-1]['status'] == 'preparing':
        attempt = row['attempts'][-1]
        assignment = attempt['assignment']
    else:
        index = len(row['attempts']) + 1
        assignment = effective_assignment(state, step, index)
        for name, digest in step['input_hashes'].items():
            if exchange.fingerprint(name) != digest:
                raise ValueError('Planned input changed; create a new plan')
        for name, digest in step['instruction_hashes'].items():
            if exchange.fingerprint(ROOT / name) != digest:
                raise ValueError('Planned instructions changed; create a new plan')
        registry = exchange.read_state(state['plan']['task_state'])
        exchange.assignment_sources(registry, assignment)
        run_dir = Path(path).parent / (Path(path).name + '.runs') / sid / f'attempt-{index}'
        run_dir.mkdir(parents=True, exist_ok=True)
        if Path(assignment['result_path']).exists():
            raise ValueError('Result path already exists; refusing to overwrite evidence')
        attempt = dict(token=secrets.token_hex(16), index=index, request_id=assignment['request_id'],
                       assignment=assignment, status='preparing', run_dir=str(run_dir.resolve()),
                       worker_identity=None, child_identity=None, heartbeat_at=None, category=None)
        if assignment.get('review_target') and step.get('gateway_policy'):
            from review_execution import policy_hash
            assignment['review_execution'] = dict(execution_state=str(Path(path).resolve()), step_id=sid,
                attempt_token=attempt['token'], policy_sha256=policy_hash(step['gateway_policy']))
        def reserve(value):
            if value['cancel_requested']:
                raise ValueError('Execution cancelled')
            value['steps'][sid]['attempts'].append(attempt)
            value['steps'][sid]['status'] = 'running'
            note(value, 'attempt_preparing', sid)
        update(path, reserve)
    registry_path = state['plan']['task_state']
    registry = exchange.read_state(registry_path)
    existing = registry['requests'].get(assignment['request_id'])
    if existing is None:
        exchange.assign(registry_path, assignment, Path('/'), None)
    elif any(existing['assignment'].get(key) != assignment.get(key) for key in assignment if key != 'inputs'):
        raise ValueError('Request ID occupied by a different assignment')
    registry = exchange.read_state(registry_path)
    existing = registry['requests'][assignment['request_id']]
    if existing['status'] == 'queued':
        handoff = exchange.prepare_handoff(registry_path, assignment['request_id'], None)['result']
    elif existing['status'] == 'dispatching':
        exchange.check_inputs(existing)
        handoff = dict(task_id=registry['task_id'], registry_revision=registry['revision'],
                       assignment=existing['assignment'], delivery_id=existing['delivery_id'],
                       trace_context=exchange.registry_context(registry, assignment['request_id']),
                       task_criteria={cid:registry['criteria'][cid]['definition'] for cid in assignment['criterion_ids']},
                       sources=exchange.assignment_sources(registry, existing['assignment']),
                       answer_sources=exchange.check_answers(registry, existing['assignment']['questionnaire_ids']))
    else:
        raise ValueError('Unowned prepared request; coordinator reconciliation required')
    exchange.atomic_write(Path(attempt['run_dir']) / 'handoff.json', handoff)
    if step.get('gateway_policy'):
        exchange.atomic_write(Path(attempt['run_dir']) / 'policy.json', step['gateway_policy'])
    def starting(value):
        target = value['steps'][sid]['attempts'][-1]
        if value['cancel_requested'] or target['token'] != attempt['token']:
            raise ValueError('Attempt fenced')
        target.update(status='starting', launch_intent_at=time.time(), attempt_id=handoff['trace_context']['attempt_id'])
        note(value, 'launch_intent', sid)
    update(path, starting)
    try:
        child = subprocess.Popen([sys.executable, str(ROOT / 'scripts/execution_worker.py'), '--state', str(Path(path).resolve()),
                                  '--step-id', sid, '--attempt-token', attempt['token']], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        def failed(value):
            value['steps'][sid]['attempts'][-1].update(status='abandoned', category='configuration', failure_signature='spawn_error')
        update(path, failed)
        return
    CHILDREN[attempt['token']] = child


def handle_failure(path, sid, step, attempt):
    state = read(path)
    registry_failure(state, attempt, 'Managed attempt failed: ' + str(attempt.get('category')))
    record_failure(path, sid, attempt)
    if uncertain_actions(path, sid):
        status = 'needs_reconciliation'
    elif not safe_group_gone(attempt):
        status = 'needs_reconciliation'
    else:
        same = sum(row.get('failure_signature') == attempt.get('failure_signature') for row in state['steps'][sid]['attempts'])
        retry = (step['retry_safe'] and attempt.get('category') in step['retry_on']
                 and len(state['steps'][sid]['attempts']) < step['max_attempts']
                 and same < step['max_identical_failures'])
        status = 'retry_wait' if retry else 'blocked'
    def save(value):
        row = value['steps'][sid]
        row.update(status=status, retry_at=time.time() + step['retry_backoff_seconds'] * 2 ** (len(row['attempts']) - 1))
        value['status'] = status
        note(value, 'attempt_failure_processed', sid, category=attempt.get('category'), next_status=status)
    update(path, save)


def record_failure(path, sid, attempt):
    state = read(path)
    if attempt.get('category') == 'cancelled' or attempt.get('failure_incident_id'):
        return
    incident = 'EXEC-' + attempt['token']
    snapshot = Path(attempt['run_dir']) / 'failure-observation.json'
    if not snapshot.exists():
        exchange.atomic_write(snapshot, dict(task_id=state['task_id'], request_id=attempt['request_id'],
            attempt_token=attempt['token'], category=attempt.get('category'), exit_code=attempt.get('exit_code'),
            failure_signature=attempt.get('failure_signature')))
    journal = Path(state['plan']['failure_journal'])
    records = failure_journal.read_journal(journal)[0] if journal.exists() else []
    if not any(record['entry']['event_id'] == incident + '-observed' for record in records):
        category = attempt.get('category')
        symptom = {'timeout':'timeout', 'invalid_result':'invalid_result', 'process_lost':'lost_progress'}.get(category, 'blocked_execution')
        entry = dict(schema_version=1, event_id=incident+'-observed', incident_id=incident,
            occurred_at=exchange.now(), actor='qa-runner', task_id=state['task_id'], request_id=attempt['request_id'],
            agent=attempt['assignment']['agent'], agent_version=agent_trace.package_identity()['agent_version'],
            stage='orchestration', scope_kind=state['plan']['scope_kind'],
            summary='Managed attempt stopped: '+str(category), symptom=symptom,
            impact='Planned step has not been accepted; retry requires policy and verified external outcomes',
            scenario_ids=attempt['assignment']['scenario_ids'],
            context=dict(step_id=sid, adapter=state['plan']['adapter']['kind'], failure_signature=attempt.get('failure_signature')),
            evidence=[str(snapshot)], cause=dict(status='unknown', category='unknown', description=None, evidence=[]),
            recovery=dict(outcome='not_attempted', action=None, result=None, evidence=[]))
        failure_journal.append_entry(journal, entry, Path('/'))
    def saved(value):
        value['steps'][sid]['attempts'][-1]['failure_incident_id'] = incident
    update(path, saved)


def tick_locked(path):
    for token, child in list(CHILDREN.items()):
        if child.poll() is not None:
            del CHILDREN[token]
    state = read(path)
    if state['cancel_requested']:
        return state
    if state['status'] == 'completed':
        return state
    if state['first_run_at'] is None:
        update(path, lambda value: value.update(first_run_at=time.time()))
        state = read(path)
    if active_seconds(state) >= state['plan']['max_active_seconds']:
        cancel(path, 'Execution active budget exhausted', user_stop=False)
        return read(path)
    for step in state['plan']['steps']:
        sid = step['step_id']
        row = state['steps'][sid]
        if row['checkpoint']:
            # A completed step is reused only while its accepted evidence remains intact.
            registry = exchange.read_state(state['plan']['task_state'])
            source = registry['requests'][row['checkpoint']['request_id']]
            if source['status'] != 'accepted':
                raise ValueError('Checkpoint acceptance changed; coordinator review required')
            exchange.check_inputs(source)
            exchange.assignment_sources(registry, source['assignment'])
            exchange.check_answers(registry, source['assignment']['questionnaire_ids'])
            if exchange.inspect_result(source['assignment'])['artifact_hashes'] != row['checkpoint']['artifact_hashes']:
                raise ValueError('Checkpoint evidence changed; coordinator review required')
            continue
        if row['status'] in ('blocked', 'needs_reconciliation', 'cancelled'):
            return state
        if row['status'] == 'retry_wait' and time.time() < row['retry_at']:
            return state
        if row['status'] in ('pending', 'waiting_answers', 'waiting_dependencies', 'retry_wait'):
            if not questions_ready(state, step):
                def waiting(value):
                    value['steps'][sid]['status'] = 'waiting_answers'
                    value['status'] = 'waiting_answers'
                update(path, waiting)
                return read(path)
            try:
                prepare_attempt(path, sid, step)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                def waiting(value):
                    if value['cancel_requested']:
                        return
                    value['steps'][sid].update(status='waiting_dependencies', reason=str(exc))
                    value['status'] = 'waiting_dependencies'
                update(path, waiting)
            return read(path)
        attempt = row['attempts'][-1]
        if attempt['status'] == 'preparing':
            prepare_attempt(path, sid, step)
            return read(path)
        if attempt['status'] == 'starting' and not attempt['worker_identity']:
            if time.time() - attempt['launch_intent_at'] < 3:
                return state
            # Fencing under lock makes a late, unclaimed worker exit before it can launch a model.
            def abandon(value):
                current = value['steps'][sid]['attempts'][-1]
                if current['token'] == attempt['token'] and current['status'] == 'starting' and not current['worker_identity']:
                    current.update(status='abandoned', category='process_lost', failure_signature='unclaimed_worker', finished_epoch=time.time())
                    note(value, 'unclaimed_worker_fenced', sid)
            update(path, abandon)
            return read(path)
        if attempt['status'] in ('running', 'stopping'):
            if process_control.matches(attempt['worker_identity']):
                if time.time() - attempt['launch_intent_at'] > step['max_wall_seconds'] + 2:
                    def expired(value):
                        current = value['steps'][sid]['attempts'][-1]
                        if current['token'] == attempt['token'] and current['status'] in ('running', 'stopping'):
                            current.update(status='stopping', stop_cause=current.get('stop_cause') or 'timeout')
                            note(value, 'supervisor_timeout', sid)
                    update(path, expired)
                    process_control.stop_group(attempt['worker_identity'], attempt.get('child_identity'))
                    return read(path)
                if state['status'] != 'suspected' and attempt['heartbeat_at'] and time.time() - attempt['heartbeat_at'] > state['plan']['heartbeat_timeout_seconds']:
                    def suspected(value):
                        value.update(status='suspected')
                        note(value, 'heartbeat_expired', sid)
                    update(path, suspected)
                return read(path)
            # Worker may have published its final result since this tick's snapshot.
            latest = read(path)['steps'][sid]['attempts'][-1]
            if latest['token'] != attempt['token'] or latest['status'] not in ('running', 'stopping'):
                return read(path)
            if not safe_group_gone(attempt):
                try:
                    process_control.stop_group(attempt['worker_identity'], attempt.get('child_identity'))
                except ValueError:
                    def uncertain(value):
                        value['steps'][sid]['status'] = 'needs_reconciliation'
                        value['status'] = 'needs_reconciliation'
                        note(value, 'process_group_uncertain', sid)
                    update(path, uncertain)
                    return read(path)
            def lost(value):
                current = value['steps'][sid]['attempts'][-1]
                if current['token'] != attempt['token'] or current['status'] not in ('running', 'stopping'):
                    return
                current.update(status='finished', category=current.get('stop_cause') or 'process_lost',
                               failure_signature=current.get('stop_cause') or 'worker_lost', exit_code=None,
                               finished_epoch=time.time())
                note(value, 'worker_loss_observed', sid)
            update(path, lost)
            return read(path)
        if attempt['status'] in TERMINAL:
            if not drain_terminal(path, sid, attempt):
                return read(path)
            if attempt.get('category') is not None:
                handle_failure(path, sid, step, attempt)
                return read(path)
            if uncertain_actions(path, sid):
                def uncertain(value):
                    value['steps'][sid]['status'] = value['status'] = 'needs_reconciliation'
                    note(value, 'external_outcome_uncertain', sid)
                update(path, uncertain)
                return read(path)
            registry_path = state['plan']['task_state']
            registry = exchange.read_state(registry_path)
            request = registry['requests'][attempt['request_id']]
            if request['status'] in ('dispatching', 'running'):
                try:
                    request = capture_result(state, attempt)
                except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
                    signature = hashlib.sha256(str(exc).encode()).hexdigest()
                    def invalid(value):
                        value['steps'][sid]['attempts'][-1].update(category='invalid_result', failure_signature=signature)
                    update(path, invalid)
                    return read(path)
                registry = exchange.read_state(registry_path)
                request = registry['requests'][attempt['request_id']]
            if request['status'] == 'accepted':
                current = exchange.inspect_result(request['assignment'])
                if current['artifact_hashes'] != request['result']['artifact_hashes']:
                    raise ValueError('Accepted result changed before checkpoint')
                def checkpoint(value):
                    value['steps'][sid].update(status='completed', checkpoint=dict(request_id=attempt['request_id'],
                        artifact_hashes=current['artifact_hashes'], event=current['event'], completed_at=exchange.now()))
                    value['status'] = 'running'
                    note(value, 'checkpoint_saved', sid)
                update(path, checkpoint)
            elif request['status'] == 'reported':
                def review(value):
                    value['steps'][sid]['status'] = 'awaiting_review'
                    value['status'] = 'awaiting_review'
                update(path, review)
            else:
                def blocked(value):
                    value['steps'][sid]['status'] = 'blocked'
                    value['status'] = 'blocked'
                    note(value, 'coordinator_changes_required', sid)
                update(path, blocked)
            return read(path)
    def completed(value):
        value.update(status='completed')
        note(value, 'execution_plan_completed')
    update(path, completed)
    return read(path)


def tick(path):
    with controller(path):
        return tick_locked(path)


def cancel(path, reason, user_stop=True):
    reason = exchange.required(reason, 'stop reason')
    def request(value):
        value.update(cancel_requested=True, status='cancelling', stop_reason=reason, user_stop=user_stop)
        note(value, 'cancellation_requested')
    update(path, request)  # Persist stop before signalling; restart never clears it.
    state = read(path)
    stopped = True
    for sid, row in state['steps'].items():
        if not row['attempts']:
            continue
        attempt = row['attempts'][-1]
        if attempt['status'] == 'finished' and attempt.get('category') is None:
            try:
                capture_result(state, attempt)
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                registry_failure(state, attempt, 'Captured result invalid during cancellation')
        if attempt.get('worker_identity') and not safe_group_gone(attempt):
            try:
                stopped &= process_control.stop_group(attempt['worker_identity'], attempt.get('child_identity'))
            except (OSError, ValueError, KeyError, TypeError):
                stopped = False
        registry_failure(state, attempt, 'Execution stopped: ' + reason)
    def finish(value):
        value['status'] = 'cancelled' if stopped else 'needs_reconciliation'
        for row in value['steps'].values():
            if not row['checkpoint']:
                row['status'] = 'cancelled' if stopped else 'needs_reconciliation'
                if stopped and row['attempts'] and row['attempts'][-1]['status'] not in TERMINAL:
                    row['attempts'][-1].update(status='abandoned', category='cancelled', finished_epoch=time.time())
        note(value, 'cancellation_processed', stopped=stopped)
    update(path, finish)
    return read(path)


def resume(path, decision):
    exchange.required(decision, 'explicit resume decision')
    state = read(path)
    if not state['cancel_requested']:
        raise ValueError('Execution is not cancelled')
    if state['status'] != 'cancelled':
        raise ValueError('Cancellation is still uncertain; reconcile processes first')
    if active_seconds(state) >= state['plan']['max_active_seconds']:
        raise ValueError('Task budget exhausted; create a new authorized plan')
    for sid, row in state['steps'].items():
        if row['checkpoint']:
            continue
        if uncertain_actions(path, sid) or (row['attempts'] and not safe_group_gone(row['attempts'][-1])):
            raise ValueError('External effects/process still uncertain; reconcile first')
        step = next(s for s in state['plan']['steps'] if s['step_id'] == sid)
        attempt = row['attempts'][-1] if row['attempts'] else None
        registry_row = exchange.read_state(state['plan']['task_state'])['requests'].get(attempt['request_id']) if attempt else None
        preserved = attempt and attempt['status'] == 'finished' and attempt.get('category') is None and registry_row and registry_row['status'] in ('reported', 'accepted')
        if not preserved and len(row['attempts']) >= step['max_attempts']:
            raise ValueError('Attempt budget exhausted; create a new authorized plan')
    def apply(value):
        value.update(cancel_requested=False, status='pending')
        for row in value['steps'].values():
            if not row['checkpoint']:
                if row['attempts']:
                    attempt = row['attempts'][-1]
                    registry_row = exchange.read_state(value['plan']['task_state'])['requests'].get(attempt['request_id'])
                    if attempt['status'] == 'finished' and attempt.get('category') is None and registry_row and registry_row['status'] in ('reported', 'accepted'):
                        row['status'] = 'awaiting_review'
                        continue
                if row['attempts']:
                    row['attempts'][-1].update(status='abandoned', category='cancelled')
                row['status'] = 'pending'
        note(value, 'explicit_resume', decision=decision)
    update(path, apply)
    return read(path)


def retry_after_reconciliation(path, sid, decision):
    exchange.required(decision, 'coordinator retry decision')
    state = read(path)
    if state['cancel_requested']:
        raise ValueError('Explicit resume required after cancellation')
    row = state['steps'][sid]
    step = next(s for s in state['plan']['steps'] if s['step_id'] == sid)
    if row['status'] not in ('blocked', 'needs_reconciliation') or not row['attempts']:
        raise ValueError('Step is not awaiting a retry decision')
    attempt = row['attempts'][-1]
    reuse = attempt['status'] == 'finished' and attempt.get('category') is None
    if (not reuse and len(row['attempts']) >= step['max_attempts']) or uncertain_actions(path, sid) or not safe_group_gone(attempt):
        raise ValueError('Retry budget exhausted or outcome remains uncertain')
    def apply(value):
        status = 'running' if reuse else 'retry_wait'
        value['steps'][sid].update(status=status, retry_at=time.time())
        value['status'] = status
        note(value, 'coordinator_continuation_authorized' if reuse else 'coordinator_retry_authorized', sid, decision=decision)
    update(path, apply)
    return read(path)
