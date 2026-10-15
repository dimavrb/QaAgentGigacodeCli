"""Durable local assignments, validated handoffs and coordinator notifications."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from qa_contract import ROLE_EVENTS, parse_message
from task_acceptance import criteria_packet, check_completion
from agent_trace import registry_context
from content_review import check_blocking_reviews


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def required(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}: требуется непустая строка')
    return value.strip()


def id_list(value, field):
    if not isinstance(value, list) or any(not isinstance(i, str) or not i.strip() for i in value):
        raise ValueError(f'{field}: требуется список ID')
    if len(value) != len(set(value)):
        raise ValueError(f'{field}: ID повторяются')
    return value


def local_path(value, base):
    return str((Path(base) / required(value, 'path')).resolve())


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_state(path):
    state = read_json(path)
    if (not isinstance(state, dict) or state.get('schema_version') != 1
            or not isinstance(state.get('task_id'), str) or not state['task_id']
            or type(state.get('revision')) is not int or state['revision'] < 1
            or not isinstance(state.get('requests'), dict)
            or not isinstance(state.get('questions'), dict)
            or not isinstance(state.get('events'), list)):
        raise ValueError('Неподдерживаемое или повреждённое состояние задачи')
    return state


def state_summary(state):
    from qa_tasks import presentation_reference
    return dict(task_id=state['task_id'], title=state['title'], revision=state['revision'],
                execution_status=state['execution_status'], review_status=state['review_status'],
                qa_outcome=state.get('qa_outcome'), active_request_ids=state['active_request_ids'],
                trace_id=state.get('trace_id'), acceptance_mode=state.get('acceptance_mode', 'legacy'),
                criteria=[dict(criterion_id=cid, description=row['definition']['description'],
                               required=row['definition']['required'],
                               status=row['assessment']['status'] if row['assessment'] else 'unknown')
                          for cid, row in state.get('criteria', {}).items()],
                open_question_ids=state.get('open_question_ids', []),
                requests=[dict(request_id=rid, agent=row['assignment']['agent'], stage=row['assignment']['stage'],
                               status=row['status'], owner_task_id=row['owner_task_id'],
                               result_path=row['assignment']['result_path'], event=row['result']['event'] if row['result'] else None,
                               **({'presentation': presentation_reference(row['presentation'])} if 'presentation' in row else {}))
                          for rid, row in state['requests'].items()],
                questions=[dict(questionnaire_id=key, status=row['status'], answers_path=row['answers_path'],
                                unresolved_question_ids=row['unresolved_question_ids']) for key, row in state['questions'].items()],
                last_event_id=len(state['events']),
                unacknowledged_event_count=sum(not e['acknowledged'] for e in state['events']))


@contextmanager
def locked(path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield path


def event(state, kind, **details):
    row = dict(event_id=len(state['events']) + 1, kind=kind, created_at=now(), acknowledged=False, **details)
    row['trace'] = registry_context(state, details.get('request_id'))
    state['events'].append(row)
    return row


def refresh(state):
    pending = [rid for rid, row in state['requests'].items() if row['status'] in ('queued', 'dispatching', 'running')]
    state['active_request_ids'] = pending
    waiting = any(q['status'] in ('waiting', 'answers_ready') for q in state['questions'].values())
    statuses = [r['status'] for r in state['requests'].values()]
    latest = {}
    for row in state['requests'].values():
        latest[row['assignment']['stage']] = row['status']
    latest_statuses = list(latest.values())
    questions = {}
    for row in state['questions'].values():
        questions.update(row.get('answer_statuses', {}) if row['status'] == 'applied'
                         else {qid: 'waiting' for qid in row['question_ids']})
    state['open_question_ids'] = sorted(qid for qid, status in questions.items() if status != 'answered')
    if state.get('execution_status') != 'completed':
        state['execution_status'] = 'running' if pending else 'waiting' if statuses or waiting else 'queued'
    state['review_status'] = ('changes_requested' if 'changes_requested' in latest_statuses else
                              'pending' if 'reported' in statuses else
                              'passed' if latest_statuses and all(s == 'accepted' for s in latest_statuses) else 'not_required')


def mutate(path, expected_revision, operation):
    with locked(path) as target:
        state = read_state(target)
        if expected_revision is not None and state['revision'] != expected_revision:
            raise ValueError(f"Ревизия изменилась: ожидалась {expected_revision}, текущая {state['revision']}. Прочитайте состояние заново.")
        previous = copy.deepcopy(state)
        result = operation(state)
        if state != previous:
            state['revision'] += 1
            state['updated_at'] = now()
            refresh(state)
            atomic_write(target, state)
        return dict(revision=state['revision'], result=result)


def init_task(path, task_id, title, criteria=None):
    task_id, title = required(task_id, 'task_id'), required(title, 'title')
    definitions = criteria_packet(criteria) if criteria is not None else None
    with locked(path) as target:
        if target.exists():
            existing = read_state(target)
            if existing['task_id'] != task_id or existing['title'] != title:
                raise ValueError('Реестр уже принадлежит другой задаче')
            if definitions is not None and {k: v['definition'] for k, v in definitions.items()} != {
                    k: v['definition'] for k, v in existing.get('criteria', {}).items()}:
                raise ValueError('Критерии существующего реестра отличаются; используйте set-criteria')
            return existing
        state = dict(schema_version=1, task_id=task_id, title=title, revision=1,
                     created_at=now(), updated_at=now(), execution_status='queued',
                     review_status='not_required', active_request_ids=[], requests={}, questions={}, events=[])
        state['open_question_ids'] = []
        state.update(trace_id=secrets.token_hex(16), acceptance_mode='criteria' if definitions else 'legacy',
                     criteria=definitions or {})
        atomic_write(target, state)
        return state


def assignment_record(source, base):
    if not isinstance(source, dict):
        raise ValueError('Назначение должно быть JSON-объектом')
    record = copy.deepcopy(source)
    if source.get('self_review_required', True) is not True:
        raise ValueError('Новые назначения требуют self_review_required=true')
    record['self_review_required'] = True
    for key in ('task_id', 'request_id', 'agent', 'goal', 'stage', 'stop_condition', 'expected_artifact', 'result_path'):
        record[key] = required(source.get(key), key)
    if record['agent'] not in ROLE_EVENTS:
        raise ValueError('Неизвестная роль исполнителя')
    criteria = source.get('acceptance_criteria')
    if not isinstance(criteria, list) or not criteria:
        raise ValueError('acceptance_criteria: нужен непустой список критериев')
    record['acceptance_criteria'] = [required(i, 'acceptance_criteria') for i in criteria]
    record['depends_on'] = id_list(source.get('depends_on', []), 'depends_on')
    modes = source.get('dependency_modes', {})
    if (not isinstance(modes, dict) or set(modes) - set(record['depends_on'])
            or any(value not in ('ready', 'context') for value in modes.values())):
        raise ValueError('dependency_modes: только ready/context для ID из depends_on')
    record['dependency_modes'] = dict(modes)
    record['criterion_ids'] = id_list(source.get('criterion_ids', []), 'criterion_ids')
    record['questionnaire_ids'] = id_list(source.get('questionnaire_ids', []), 'questionnaire_ids')
    record['scenario_ids'] = id_list(source.get('scenario_ids', []), 'scenario_ids')
    record['inputs'] = [local_path(i, base) for i in id_list(source.get('inputs', []), 'inputs')]
    record['result_path'] = local_path(record['result_path'], base)
    record['artifact_base'] = local_path(source.get('artifact_base', str(Path(record['result_path']).parent)), base)
    if record['agent'] == 'qa-stub-builder':
        record['stub_evidence_required'] = True
    if record['agent'] == 'qa-jira-defect-creator':
        record['jira_checks_required'] = True
    if 'jira_assignment' in record:
        if record['agent'] != 'qa-jira-defect-creator':
            raise ValueError('jira_assignment requires qa-jira-defect-creator')
        from jira_publication import load_assignment as load_jira_assignment
        record['jira_assignment'] = local_path(record['jira_assignment'], base)
        scope, paths = load_jira_assignment(record['jira_assignment'])
        if scope['task_id'] != record['task_id']:
            raise ValueError('Jira publication scope belongs to another task')
        for source_path in sorted(paths):
            if source_path not in record['inputs']:
                record['inputs'].append(source_path)
    if record['agent'] == 'qa-test-data-builder':
        record['data_checks_required'] = True
    if 'data_assignment' in record:
        if record['agent'] != 'qa-test-data-builder':
            raise ValueError('data_assignment requires qa-test-data-builder')
        from data_readiness import load_assignment as load_data_assignment
        record['data_assignment'] = local_path(record['data_assignment'], base)
        scope, paths = load_data_assignment(record['data_assignment'])
        if scope['task_id'] != record['task_id'] or set(scope['scenario_ids']) != set(record['scenario_ids']):
            raise ValueError('Data requirements differ from task/scenario assignment')
        for source_path in sorted(paths):
            if source_path not in record['inputs']:
                record['inputs'].append(source_path)
    if record['agent'] == 'qa-test-env-configurator':
        record['environment_checks_required'] = True
    if 'environment_assignment' in record:
        if record['agent'] != 'qa-test-env-configurator':
            raise ValueError('environment_assignment requires qa-test-env-configurator')
        from environment_readiness import load_assignment
        record['environment_assignment'] = local_path(record['environment_assignment'], base)
        scope, paths = load_assignment(record['environment_assignment'])
        if scope['task_id'] != record['task_id'] or set(scope.get('scenario_ids', [])) != set(record['scenario_ids']):
            raise ValueError('Environment guide scope differs from task/scenario assignment')
        for source_path in sorted(paths):
            if source_path not in record['inputs']:
                record['inputs'].append(source_path)
    if 'plan_partition' in record:
        from partition_test_plan import normalize_assignment
        normalize_assignment(record, base)
    if 'evidence_session_id' in record:
        record['evidence_session_id'] = required(record['evidence_session_id'], 'evidence_session_id')
    if 'improvement_plan' in record:
        if record.get('evidence_session_id'):
            raise ValueError('use a separate assignment for improvement reports and benchmark summaries')
        from improvement_acceptance import plan_inputs
        record['improvement_plan'] = local_path(record['improvement_plan'], base)
        for source_path in plan_inputs(record['improvement_plan'])[1]:
            if source_path not in record['inputs']: record['inputs'].append(source_path)
    for key in ('investigation_state', 'stub_assignment'):
        if key in record:
            record[key] = local_path(record[key], base)
    if 'stub_assignment' in record:
        from stub_contract import normalize_assignment as normalize_stub_assignment
        normalize_stub_assignment(record)
    if 'impact_input' in record:
        if record['agent'] != 'qa-impact-analyst':
            raise ValueError('impact_input requires qa-impact-analyst')
        from impact_analysis import impact_paths
        record['impact_input'] = local_path(record['impact_input'], base)
        for source_path in sorted(impact_paths(record['impact_input'])):
            if source_path not in record['inputs']:
                record['inputs'].append(source_path)
    if record['agent'] in ('qa-result-reviewer', 'qa-conclusion-reviewer') or record['agent'] == 'qa-autotest-reviewer' and 'review_target' in record:
        from content_review import normalize_target
        normalize_target(record)
        if record['agent'] == 'qa-conclusion-reviewer':
            record['review_sources'] = [local_path(p, base) for p in id_list(source.get('review_sources', []), 'review_sources')]
            from conclusion_review import normalize_assignment
            normalize_assignment(record)
    elif 'review_target' in record:
        raise ValueError('review_target requires a reviewer role')
    if 'review_discussion' in record:
        from review_discussion import normalize_assignment
        normalize_assignment(record)
    if 'blind_review_exclusions' in record:
        excluded = id_list(record['blind_review_exclusions'], 'blind_review_exclusions')
        if not record.get('review_target') or not set(excluded) <= set(record['inputs']):
            raise ValueError('Blind review exclusions must be assigned reviewer inputs')
        if set(excluded) & set(record.get('review_sources', [])):
            raise ValueError('Primary sources cannot be hidden from a reviewer')
    if 'correction_feedback' in record:
        feedback = record['correction_feedback']
        if feedback not in record['inputs'] or record.get('review_target'):
            raise ValueError('correction_feedback must be an author input')
    if 'review_execution' in record:
        binding = record['review_execution']
        if (not record.get('review_target') or not isinstance(binding, dict) or
                set(binding) != {'execution_state', 'step_id', 'attempt_token', 'policy_sha256'} or
                any(not isinstance(v, str) or not v.strip() for v in binding.values()) or
                not Path(binding['execution_state']).is_absolute()):
            raise ValueError('review_execution needs an absolute managed execution binding and review_target')
    return record


def protected_paths(path, state):
    target = Path(path).resolve()
    paths = {str(target), str(target.with_name(target.name + '.lock'))}
    for row in state['questions'].values():
        paths.update((row['input_path'], row['answers_path']))
    return paths


def inspect_result(assignment, *, data_context_only=False):
    """Use the full existing CLI validator; retain hashes of local handoff inputs."""
    path = Path(assignment['result_path'])
    message, _ = parse_message(path.read_text(encoding='utf-8'))
    refs = message.get('artifacts', [])
    if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
        raise ValueError('artifacts: ожидается список ссылок')
    refs = set(refs)
    payload = message.get('payload', {})
    combination_paths = set()
    stub_paths = set()
    if assignment.get('stub_evidence_required') and assignment.get('stub_assignment'):
        from stub_contract import STUB_EVENTS, read_stub_json, validate_stub_bundle
        if message.get('event') in STUB_EVENTS:
            errors = validate_stub_bundle(message, Path(assignment['artifact_base']),
                read_stub_json(Path(assignment['stub_assignment'])), artifact_paths=stub_paths)
            if errors:
                raise ValueError('Результат заглушек не прошёл проверку:\n' + '\n'.join(errors))
    if isinstance(payload, dict):
        if assignment.get('jira_checks_required') or {'publication_artifact', 'jira_assignment_sha256'} & payload.keys():
            scope = assignment.get('jira_assignment')
            if not scope and (message['event'] != 'blocked' or {'publication_artifact', 'jira_assignment_sha256'} & payload.keys()):
                raise ValueError('Jira publication requires coordinator jira_assignment')
            if scope:
                from jira_publication import check_bundle as check_jira_bundle
                refs.update(check_jira_bundle(message, assignment['artifact_base'], scope,
                                             assignment['jira_task_state'], path))
        if assignment.get('data_checks_required') or 'data_assignment_sha256' in payload:
            scope = assignment.get('data_assignment')
            if not scope and message['event'] != 'blocked':
                raise ValueError('Data readiness requires coordinator data_assignment')
            if scope:
                from data_readiness import check_result as check_data_result
                refs.update(check_data_result(message, assignment['artifact_base'], scope, path,
                                              context_only=data_context_only))
        if assignment.get('environment_checks_required') or 'guide_checks' in payload:
            scope = assignment.get('environment_assignment')
            if not scope and message['event'] != 'blocked':
                raise ValueError('Environment readiness requires coordinator environment_assignment')
            if scope:
                from environment_readiness import check_result
                refs.update(check_result(message, assignment['artifact_base'], scope, path))
        if assignment.get('improvement_plan'):
            from improvement_acceptance import check_bundle
            bundle = check_bundle(message, path.parent, assignment['improvement_plan'])
            refs.update(bundle['source_hashes'])
        if assignment.get('evidence_session_id'):
            from benchmark_summary import check_summary
            ref = required(payload.get('benchmark_summary_artifact'), 'benchmark_summary_artifact')
            summary_path = local_path(ref, path.parent)
            summary = check_summary(summary_path, assignment['evidence_session_id'])
            refs.add(summary_path)
            refs.update(summary['source_hashes'])
        if assignment['agent'] == 'qa-impact-analyst' and message.get('event') == 'impact_ready':
            from impact_analysis import impact_paths
            if not assignment.get('impact_input'):
                raise ValueError('impact_ready requires coordinator impact_input')
            refs.update(impact_paths(assignment['impact_input']))
            refs.add(local_path(payload['impact_artifact'], path.parent))
        for key in ('plan_artifact', 'test_cases_artifact', 'diff_artifact', 'verification_artifact',
                    'assignment_artifact', 'profile_artifact', 'report_artifact', 'summary_artifact'):
            value = payload.get(key)
            if isinstance(value, str):
                refs.add(value)
        if 'combination_coverage' in payload:
            from combination_coverage import artifact_paths
            combination_paths = artifact_paths(payload, path.parent)
            refs.update(combination_paths)
        # Execution evidence is primary material, even if the author omitted its
        # duplicate at the top level. Never treat arbitrary payload strings as paths.
        if assignment['agent'] == 'qa-feature-tester':
            for result in payload.get('scenario_results', []):
                if isinstance(result, dict):
                    refs.update(result.get('evidence', []))
                    source = result.get('expected_source')
                    if isinstance(source, str) and not source.startswith(('http://', 'https://')):
                        candidate = local_path(source.split('#', 1)[0], assignment['artifact_base'])
                        if Path(candidate).is_file():
                            refs.add(source)
        if assignment.get('review_target'):
            for key in ('checks', 'combination_checks'):
                checks = payload.get(key, [])
                if not isinstance(checks, list):
                    raise ValueError(f'{key}: ожидается список')
                for check in checks:
                    if isinstance(check, dict):
                        evidence = check.get('evidence', [])
                        if not isinstance(evidence, list) or any(not isinstance(r, str) for r in evidence):
                            raise ValueError(f'{key}.evidence: ожидается список ссылок')
                        refs.update(evidence)
            for check in payload.get('conclusion_checks', []):
                if isinstance(check, dict):
                    refs.update(ref['path'] for ref in check.get('source_refs', []) if isinstance(ref, dict))
            for check in payload.get('discussion_checks', []):
                if isinstance(check, dict):
                    refs.update(ref['path'] for ref in check.get('source_refs', []) if isinstance(ref, dict))
                    if isinstance(check.get('receipt'), str):
                        refs.add(check['receipt'])
            for check in payload.get('execution_checks', []):
                if isinstance(check, dict) and isinstance(check.get('receipt'), str):
                    refs.add(check['receipt'])
    paths = {str(path.resolve())}
    paths.update(assignment['inputs'])
    paths.update(stub_paths)
    remote = []
    for ref in refs:
        ref = required(ref, 'artifacts')
        if ref.startswith(('https://', 'http://')):
            remote.append(ref)
        else:
            paths.add(local_path(ref.split('#', 1)[0], assignment['artifact_base']))
    if assignment['agent'] == 'qa-test-automation' and isinstance(payload.get('changed_files'), list):
        for ref in payload['changed_files']:
            paths.add(local_path(ref, assignment['artifact_base']))
    for key in ('investigation_state', 'stub_assignment'):
        if key in assignment:
            paths.add(assignment[key])
    if assignment.get('plan_partition'):
        from partition_test_plan import verify_result
        paths.update(verify_result(assignment, message))
    before = {p: fingerprint(p) for p in sorted(paths)}
    command = [sys.executable, str(Path(__file__).with_name('validate_qa_result.py')),
               '--input', str(path), '--task-id', assignment['task_id'],
               '--request-id', assignment['request_id'], '--agent', assignment['agent']]
    if assignment.get('self_review_required'):
        command.append('--require-self-review')
    if assignment.get('stub_evidence_required'):
        command.extend(('--stub-evidence-base', assignment['artifact_base']))
    if assignment.get('jira_checks_required'):
        command.append('--require-jira-checks')
    if assignment.get('jira_assignment'):
        command.extend(('--jira-assignment', assignment['jira_assignment'],
                        '--jira-task-state', assignment['jira_task_state'],
                        '--jira-evidence-base', assignment['artifact_base']))
    if assignment.get('data_checks_required'):
        command.append('--require-data-checks')
    if assignment.get('data_assignment'):
        command.extend(('--data-assignment', assignment['data_assignment'],
                        '--data-evidence-base', assignment['artifact_base']))
        if data_context_only:
            command.append('--data-context-only')
    if assignment.get('environment_checks_required'):
        command.append('--require-environment-checks')
    if assignment.get('environment_assignment'):
        command.extend(('--environment-assignment', assignment['environment_assignment']))
        command.extend(('--environment-evidence-base', assignment['artifact_base']))
    if assignment.get('evidence_session_id'):
        command.extend(('--evidence-session-id', assignment['evidence_session_id']))
    for sid in assignment['scenario_ids']:
        command.extend(('--scenario-id', sid))
    for key, flag in (('investigation_state', '--investigation-state'), ('stub_assignment', '--stub-assignment'),
                      ('impact_input', '--impact-input'), ('improvement_plan', '--improvement-plan')):
        if key in assignment:
            command.extend((flag, assignment[key]))
    run = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if run.returncode:
        raise ValueError('Результат не прошёл проверку:\n' + (run.stdout + run.stderr).strip())
    from self_review import validate_self_review
    review_errors = validate_self_review(message, assignment, required=bool(assignment.get('self_review_required')))
    if review_errors:
        raise ValueError('Результат не прошёл самопроверку:\n' + '\n'.join(review_errors))
    if assignment.get('review_target'):
        from content_review import check_binding
        check_binding(message, assignment)
    after = {p: fingerprint(p) for p in sorted(paths)}
    if before != after:
        raise ValueError('Артефакты изменились во время проверки')
    if assignment.get('plan_partition'):
        from partition_test_plan import verify_result
        verify_result(assignment, message)
    if 'combination_coverage' in payload:
        from combination_coverage import artifact_paths
        if artifact_paths(payload, path.parent) != combination_paths:
            raise ValueError('Combination artifact/source links changed during validation')
    # Parse once more and tie the metadata to the same bytes that were checked.
    current, _ = parse_message(path.read_text(encoding='utf-8'))
    if current != message:
        raise ValueError('Отчёт изменился во время проверки')
    ready = readiness(message)
    if assignment['agent'] == 'qa-jira-defect-creator':
        ready['jira_publication_verified'] = bool(assignment.get('jira_assignment'))
    if assignment['agent'] == 'qa-test-env-configurator':
        ready['admin_guide_verified'] = bool(assignment.get('environment_assignment'))
    if assignment['agent'] == 'qa-test-data-builder':
        ready['data_properties_verified'] = bool(assignment.get('data_assignment'))
        if assignment.get('data_assignment'):
            from data_readiness import load_assignment as load_data_assignment
            ready['data_expires_at'] = load_data_assignment(assignment['data_assignment'])[0]['expires_at']
    return dict(path=str(path.resolve()), event=message['event'], summary=message['summary'],
                artifact_hashes=after, remote_artifacts=sorted(remote), validated_at=now(),
                readiness=ready, current_revision=payload.get('current_revision'),
                reviewed_revision=payload.get('reviewed_revision'))


def readiness(message):
    kind, payload = message['event'], message['payload']
    complete = kind in {'analysis_ready', 'impact_ready', 'plan_ready', 'environment_ready', 'data_ready',
                        'stubs_ready', 'investigation_complete', 'feature_test_complete',
                        'automation_complete', 'review_passed', 'jira_defect_created',
                        'jira_duplicate_found', 'test_report_ready'}
    scenarios = None
    if kind == 'impact_ready':
        scenarios = payload['run_scenario_ids']
    if kind in ('environment_partial', 'data_partial', 'stubs_partial', 'data_ready', 'stubs_ready'):
        scenarios = payload.get('ready_scenarios' if kind == 'environment_partial' else 'scenario_ids', [])
    if kind == 'feature_test_complete':
        scenarios = [r['scenario_id'] for r in payload['scenario_results'] if r['status'] in ('passed', 'failed')]
        complete = not payload['uncovered_scenarios']
    return dict(complete=complete, ready_scenario_ids=scenarios)


def check_readiness(source, scenario_ids, consumer_agent):
    kind, ready = source['event'], source['readiness']
    if kind in ('jira_defect_created', 'jira_duplicate_found') and not ready.get('jira_publication_verified'):
        raise ValueError('Публикация Jira не подтверждена; используйте context или новое назначение')
    if kind in ('data_ready', 'data_partial') and not ready.get('data_properties_verified'):
        raise ValueError('Свойства данных не подтверждены по назначению; используйте context или новое назначение')
    if kind in ('data_ready', 'data_partial') and ready.get('data_expires_at'):
        from data_readiness import timestamp
        if datetime.now(timezone.utc) >= timestamp(ready['data_expires_at']):
            raise ValueError('Data validity window expired; use context or a new assignment')
    if kind in ('environment_ready', 'environment_partial') and not ready.get('admin_guide_verified'):
        raise ValueError('Источник стенда не подтверждён по админгайду; используйте context или новое назначение')
    if kind == 'ready_for_review' and consumer_agent == 'qa-autotest-reviewer':
        return
    if kind == 'stubs_configured' and consumer_agent == 'qa-stub-builder':
        return
    scenarios = ready['ready_scenario_ids']
    if scenario_ids and scenarios is not None:
        if set(scenario_ids) <= set(scenarios):
            return
        raise ValueError('Источник не готов для запрошенных сценариев')
    if not ready['complete']:
        raise ValueError(f'Источник не готов для исполнения: {kind}; для диагностики используйте context')


def assignment_sources(state, assignment):
    sources = check_sources(state, assignment['depends_on'], scenario_ids=assignment.get('scenario_ids', []),
                         modes=assignment.get('dependency_modes', {}), consumer_agent=assignment['agent'])
    if assignment.get('jira_assignment'):
        from jira_publication import check_source, load_assignment as load_jira_assignment
        check_source(state, assignment, load_jira_assignment(assignment['jira_assignment'])[0])
    return sources


def check_sources(state, dependencies, checked=None, visiting=None, scenario_ids=None, modes=None, consumer_agent=None):
    checked = {} if checked is None else checked
    visiting = set() if visiting is None else visiting
    sources = []
    for rid in dependencies:
        if rid in visiting:
            raise ValueError('Обнаружен цикл зависимостей назначений')
        if rid in checked:
            if (modes or {}).get(rid, 'ready') == 'ready':
                check_blocking_reviews(state, rid)
                check_readiness(checked[rid], scenario_ids, consumer_agent)
            sources.append(checked[rid])
            continue
        row = state['requests'].get(rid)
        if not row or row['status'] != 'accepted':
            raise ValueError(f'{rid}: источник ещё не принят координатором')
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        visiting.add(rid)
        assignment = row['assignment']
        check_sources(state, assignment['depends_on'], checked, visiting,
                      assignment.get('scenario_ids', []), assignment.get('dependency_modes', {}), assignment['agent'])
        visiting.remove(rid)
        current = inspect_result(row['assignment'], data_context_only=(modes or {}).get(rid, 'ready') == 'context')
        if current['artifact_hashes'] != row['result']['artifact_hashes']:
            raise ValueError(f'{rid}: принятые артефакты изменились; требуется новое назначение и ревью')
        source = dict(request_id=rid, agent=row['assignment']['agent'],
                            result_path=current['path'], event=current['event'], summary=current['summary'],
                            artifacts=list(current['artifact_hashes']), result_sha256=current['artifact_hashes'][current['path']],
                            remote_artifacts=current['remote_artifacts'],
                            readiness=current['readiness'],
                            decision=row['review']['decision'])
        if (modes or {}).get(rid, 'ready') == 'ready':
            check_blocking_reviews(state, rid)
            check_readiness(source, scenario_ids, consumer_agent)
        checked[rid] = source
        sources.append(source)
    return sources


def assign(path, source, base, expected_revision):
    record = assignment_record(source, base)
    if record['agent'] == 'qa-jira-defect-creator':
        record['jira_task_state'] = str(Path(path).resolve())
    def operation(state):
        if state['execution_status'] == 'completed':
            raise ValueError('Задача уже завершена')
        if record['task_id'] != state['task_id']:
            raise ValueError('Назначение относится к другой задаче')
        if record.get('plan_partition'):
            from partition_test_plan import load
            binding = record['plan_partition']
            manifest, _, _ = load(binding['path'],binding['sha256'])
            if manifest['state_path'] != str(Path(path).resolve()):
                raise ValueError('Partition assignment belongs to another task registry')
        rid = record['request_id']
        if rid in state['requests']:
            raise ValueError('request_id уже использован; для нового назначения нужен новый ID')
        if record['result_path'] in protected_paths(path, state):
            raise ValueError('Результат не должен перезаписывать реестр или вопросы')
        if any(r['assignment']['result_path'] == record['result_path'] for r in state['requests'].values()):
            raise ValueError('Путь результата уже принадлежит другому назначению')
        assignment_sources(state, record)
        if record.get('review_target'):
            from content_review import check_target
            check_target(state, record)
        if set(record['criterion_ids']) - set(state.get('criteria', {})):
            raise ValueError('criterion_ids: неизвестный критерий задачи')
        answer_sources = check_answers(state, record['questionnaire_ids'])
        for source in answer_sources:
            for name in ('input_path', 'answers_path'):
                if source[name] not in record['inputs']:
                    record['inputs'].append(source[name])
        if record['result_path'] in record['inputs']:
            raise ValueError('Результат не должен перезаписывать входной артефакт')
        if set(record['inputs']) & {str(Path(path).resolve()), str(Path(path).resolve()) + '.lock'}:
            raise ValueError('Изменяемый реестр нельзя использовать как входное доказательство')
        state['requests'][rid] = dict(assignment=record, status='queued', owner_task_id=None,
                                     created_at=now(), result=None, review=None, delivery_id=None,
                                     input_hashes={p: fingerprint(p) for p in record['inputs']})
        return event(state, 'assignment_ready', request_id=rid)
    return mutate(path, expected_revision, operation)


def check_inputs(row):
    if row['assignment'].get('plan_partition'):
        from partition_test_plan import load
        binding = row['assignment']['plan_partition']
        load(binding['path'], binding['sha256'])
    if {p: fingerprint(p) for p in row['assignment']['inputs']} != row['input_hashes']:
        raise ValueError('Исходные материалы назначения изменились; требуется новое назначение')


def prepare_handoff(path, rid, expected_revision):
    def operation(state):
        row = state['requests'].get(rid)
        if not row or row['status'] != 'queued':
            raise ValueError('Передача допустима только для ожидающего назначения')
        check_inputs(row)
        sources = assignment_sources(state, row['assignment'])
        answers = check_answers(state, row['assignment']['questionnaire_ids'])
        row.update(status='dispatching', delivery_id=secrets.token_urlsafe(18), attempt_id=secrets.token_hex(16))
        event(state, 'handoff_prepared', request_id=rid, delivery_id=row['delivery_id'])
        return dict(task_id=state['task_id'], registry_revision=state['revision'] + 1,
                    delivery_id=row['delivery_id'],
                    trace_context=registry_context(state, rid),
                    task_criteria={cid: state['criteria'][cid]['definition'] for cid in row['assignment'].get('criterion_ids', [])},
                    assignment=row['assignment'], sources=sources,
                    answer_sources=answers,
                    instruction='Запиши JSON-отчёт в result_path. Выполни самопроверку по docs/SELF_REVIEW.md и сохрани self_review. Затем проверь полный контракт. Потребности передавай координатору.',
                    self_review_guide=str(Path(__file__).resolve().parents[1] / 'docs' / 'SELF_REVIEW.md'),
                    check_quality_guide=(str(Path(__file__).resolve().parents[1] / '.gigacode/skills/qa-check-quality/SKILL.md')
                        if row['assignment']['agent'] in ('qa-analyst', 'qa-impact-analyst', 'qa-test-plan-writer', 'qa-feature-tester',
                            'qa-test-automation', 'qa-autotest-reviewer', 'qa-defect-investigator', 'qa-result-reviewer', 'qa-conclusion-reviewer') else None))
    return mutate(path, expected_revision, operation)


def start_request(path, rid, owner_task_id, delivery_id, expected_revision):
    owner_task_id = required(owner_task_id, 'owner_task_id')
    def operation(state):
        row = state['requests'].get(rid)
        if not row or row['status'] != 'dispatching' or row['delivery_id'] != delivery_id:
            raise ValueError('Назначение уже запущено или недоступно')
        if any(other['owner_task_id'] == owner_task_id and other['status'] == 'running'
               for key, other in state['requests'].items() if key != rid):
            raise ValueError('Этот исполнитель уже выполняет другое активное назначение')
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        assignment_sources(state, row['assignment'])
        target = row['assignment'].get('review_target')
        if target:
            from content_review import check_target
            check_target(state, row['assignment'])
            author_owner = state['requests'][target['request_id']]['owner_task_id']
            if owner_task_id == author_owner:
                raise ValueError('Reviewer must use a different owner/session')
        row.update(status='running', owner_task_id=owner_task_id, started_at=now())
        return event(state, 'assignment_started', request_id=rid, owner_task_id=owner_task_id)
    return mutate(path, expected_revision, operation)


def submit_result(path, rid, expected_revision=None):
    def operation(state):
        row = state['requests'].get(rid)
        if not row or row['status'] not in ('dispatching', 'running', 'reported', 'accepted'):
            raise ValueError('Назначение недоступно для регистрации результата')
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        assignment_sources(state, row['assignment'])
        result = inspect_result(row['assignment'])
        if row['result'] is not None:
            if result['artifact_hashes'] != row['result']['artifact_hashes']:
                raise ValueError('Зарегистрированный результат изменился; нужен новый request_id')
            return next(e for e in state['events'] if e['kind'] == 'result_ready' and e['request_id'] == rid)
        row.update(status='reported', result=result, reported_at=now())
        return event(state, 'result_ready', request_id=rid, result_path=result['path'])
    return mutate(path, expected_revision, operation)


def review_result(path, rid, decision, passed, expected_revision):
    decision = required(decision, 'decision')
    def operation(state):
        row = state['requests'].get(rid)
        if not row or row['status'] != 'reported':
            raise ValueError('Для ревью нужен проверенный зарегистрированный результат')
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        assignment_sources(state, row['assignment'])
        result = inspect_result(row['assignment'])
        if result['artifact_hashes'] != row['result']['artifact_hashes']:
            raise ValueError('Результат изменился после регистрации; требуется новое назначение')
        if passed:
            check_blocking_reviews(state, rid)
        row.update(status='accepted' if passed else 'changes_requested',
                   review=dict(status='passed' if passed else 'changes_requested', decision=decision, reviewed_at=now()))
        return event(state, 'result_accepted' if passed else 'changes_requested', request_id=rid)
    accepted = mutate(path, expected_revision, operation)
    # Rendering is a derived artifact, published only after acceptance is durable.
    # A rendering failure remains visible without undoing the content decision.
    if passed:
        row = read_state(path)['requests'][rid]
        if (row['assignment']['agent'] == 'qa-test-plan-writer' and row['result']['event'] == 'plan_ready'
                and row['assignment'].get('plan_partition', {}).get('mode') != 'group'):
            from build_test_plan_html import publish
            presentation = publish(path, rid)
            accepted['revision'] = presentation['revision']
            accepted['result'] = dict(accepted['result'], presentation=presentation['result'])
        elif row['assignment']['agent'] == 'qa-test-env-configurator':
            from build_environment_html import publish
            presentation = publish(path, rid)
            accepted['revision'] = presentation['revision']
            accepted['result'] = dict(accepted['result'], presentation=presentation['result'])
    return accepted


def register_questions(path, input_path, answers_path, expected_revision=None):
    from questionnaire import normalize_packet
    input_path, answers_path = Path(input_path).resolve(), Path(answers_path).resolve()
    packet = normalize_packet(read_json(input_path))
    key = packet['questionnaire_id']
    record = dict(input_path=str(input_path), answers_path=str(answers_path),
                  request_id=packet['request_id'], packet_revision=packet['revision'],
                  question_ids=[q['question_id'] for q in packet['questions']], status='waiting',
                  answers_hash=None, unresolved_question_ids=[], decision=None)
    def operation(state):
        if packet['task_id'] != state['task_id']:
            raise ValueError('Вопросы относятся к другой задаче')
        existing = state['questions'].get(key)
        if existing:
            for field in ('input_path', 'answers_path', 'packet_revision'):
                if existing[field] != record[field]:
                    raise ValueError('Пакет вопросов уже зарегистрирован с другим содержимым или путём')
            return dict(questionnaire_id=key, status=existing['status'])
        if expected_revision is None:
            raise ValueError('Для регистрации нового пакета укажите expected_revision')
        if state['execution_status'] == 'completed':
            raise ValueError('Задача уже завершена')
        if any(q['request_id'] == record['request_id'] for q in state['questions'].values()):
            raise ValueError('request_id пакета вопросов уже использован')
        used = protected_paths(path, state) | {r['assignment']['result_path'] for r in state['requests'].values()}
        if record['answers_path'] in used or record['answers_path'] == record['input_path']:
            raise ValueError('Путь ответов уже используется другим артефактом')
        if record['input_path'] in used:
            # Role result JSON may also serve as the question source, but never the registry.
            if record['input_path'] not in {r['assignment']['result_path'] for r in state['requests'].values()}:
                raise ValueError('Путь вопросов уже используется реестром или другим пакетом')
        state['questions'][key] = record
        return event(state, 'questions_waiting', questionnaire_id=key)
    return mutate(path, expected_revision, operation)


def notify_answers(path, packet, answers_path):
    from questionnaire import normalize_packet, validate_response
    answers_path = Path(answers_path).resolve()
    def operation(state):
        row = state['questions'].get(packet['questionnaire_id'])
        if not row or packet['task_id'] != state['task_id'] or row['packet_revision'] != packet['revision']:
            raise ValueError('Этот пакет вопросов не зарегистрирован в состоянии задачи')
        if str(answers_path) != row['answers_path']:
            raise ValueError('Ответы сохранены по другому пути')
        current = normalize_packet(read_json(row['input_path']))
        if current['revision'] != row['packet_revision']:
            raise ValueError('Исходные вопросы изменились; зарегистрируйте новый пакет')
        digest = fingerprint(answers_path)
        response = validate_response(packet, read_json(answers_path))
        if fingerprint(answers_path) != digest:
            raise ValueError('Ответы изменились во время проверки')
        if row['answers_hash']:
            if row['answers_hash'] != digest:
                raise ValueError('Зарегистрированные ответы изменились')
            return next(e for e in state['events'] if e['kind'] == 'answers_ready' and e['questionnaire_id'] == packet['questionnaire_id'])
        row.update(status='answers_ready', answers_hash=digest,
                   answer_statuses={a['question_id']: a['status'] for a in response['answers']},
                   unresolved_question_ids=[a['question_id'] for a in response['answers'] if a['status'] == 'deferred'])
        return event(state, 'answers_ready', questionnaire_id=packet['questionnaire_id'],
                     answers_path=str(answers_path), unresolved_question_ids=row['unresolved_question_ids'])
    return mutate(path, None, operation)


def apply_answers(path, key, decision, expected_revision):
    decision = required(decision, 'decision')
    def operation(state):
        row = state['questions'].get(key)
        if not row or row['status'] != 'answers_ready':
            raise ValueError('Нет зарегистрированных ответов для применения')
        from questionnaire import normalize_packet, validate_response
        packet = normalize_packet(read_json(row['input_path']))
        if packet['revision'] != row['packet_revision'] or fingerprint(row['answers_path']) != row['answers_hash']:
            raise ValueError('Вопросы или ответы изменились после регистрации')
        validate_response(packet, read_json(row['answers_path']))
        row.update(status='applied', decision=decision, applied_at=now())
        return event(state, 'answers_applied', questionnaire_id=key,
                     unresolved_question_ids=row['unresolved_question_ids'])
    return mutate(path, expected_revision, operation)


def check_answers(state, questionnaire_ids):
    from questionnaire import normalize_packet, validate_response
    sources = []
    for key in questionnaire_ids:
        row = state['questions'].get(key)
        if not row or row['status'] != 'applied':
            raise ValueError(f'{key}: ответы ещё не применены координатором')
        packet = normalize_packet(read_json(row['input_path']))
        if packet['revision'] != row['packet_revision'] or fingerprint(row['answers_path']) != row['answers_hash']:
            raise ValueError('Вопросы или применённые ответы изменились')
        validate_response(packet, read_json(row['answers_path']))
        sources.append(dict(questionnaire_id=key, input_path=row['input_path'], answers_path=row['answers_path'],
                            answers_hash=row['answers_hash'], decision=row['decision'],
                            unresolved_question_ids=row['unresolved_question_ids']))
    return sources


def acknowledge(path, event_id, expected_revision):
    def operation(state):
        for row in state['events']:
            if row['event_id'] == event_id:
                if not row['acknowledged']:
                    row.update(acknowledged=True, acknowledged_at=now())
                return row
        raise ValueError('Событие не найдено')
    return mutate(path, expected_revision, operation)


def fail_request(path, rid, reason, expected_revision):
    reason = required(reason, 'reason')
    def operation(state):
        row = state['requests'].get(rid)
        if not row or row['status'] not in ('queued', 'dispatching', 'running'):
            raise ValueError('Нельзя пометить это назначение сбоем запуска')
        row.update(status='failed', failure_reason=reason, finished_at=now())
        return event(state, 'assignment_failed', request_id=rid, reason=reason)
    return mutate(path, expected_revision, operation)


def set_criteria(path, packet, expected_revision):
    definitions = criteria_packet(packet)
    def operation(state):
        if state['execution_status'] == 'completed' or state['requests']:
            raise ValueError('Критерии фиксируются до первого назначения; для нового объёма создайте новую задачу')
        state.update(criteria=definitions, acceptance_mode='criteria')
        return event(state, 'criteria_defined', criterion_ids=list(definitions))
    return mutate(path, expected_revision, operation)


def check_assessment(state, definition, source, recheck=False):
    item = {key: required(source.get(key), key) for key in ('criterion_id', 'status', 'decision')}
    if item['criterion_id'] != definition['criterion_id']:
        raise ValueError('Оценка относится к другому критерию')
    if item['status'] not in ('verified', 'failed', 'unknown', 'not_applicable'):
        raise ValueError('Неизвестный статус критерия')
    rids = id_list(source.get('request_ids', []), 'request_ids')
    evidence = id_list(source.get('evidence', []), 'evidence')
    if item['status'] in ('verified', 'not_applicable') and (not rids or not evidence):
        raise ValueError('Принятый критерий требует назначения и доказательства')
    hashes, results = {}, {}
    for rid in rids:
        row = state['requests'].get(rid)
        if not row or row['status'] != 'accepted':
            raise ValueError('Критерий требует принятых результатов')
        if item['criterion_id'] not in row['assignment'].get('criterion_ids', []):
            raise ValueError('Назначение не связано с этим критерием')
        if item['status'] == 'verified':
            check_blocking_reviews(state, rid)
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        assignment_sources(state, row['assignment'])
        current = inspect_result(row['assignment'])
        if current['artifact_hashes'] != row['result']['artifact_hashes']:
            raise ValueError('Доказательства принятого результата изменились')
        if item['status'] == 'verified' and current['event'] in ('jira_defect_created', 'jira_duplicate_found'):
            check_readiness(current, [], None)
        hashes.update(current['artifact_hashes'])
        results[rid] = current
    selected = {}
    for path in evidence:
        resolved = str(Path(path).resolve())
        if resolved not in hashes:
            raise ValueError('Доказательство отсутствует среди проверенных артефактов результатов')
        selected[resolved] = hashes[resolved]
    reviewer = definition.get('required_review_agent')
    if reviewer and item['status'] == 'verified':
        reviews = [rid for rid in rids if state['requests'][rid]['assignment']['agent'] == reviewer]
        if not reviews or not any(state['requests'][rid]['assignment']['agent'] != reviewer for rid in rids):
            raise ValueError('Нет обязательного независимого ревью и результата автора')
        eligible = False
        for rid in reviews:
            assignment = state['requests'][rid]['assignment']
            author_ids = {other for other in rids if state['requests'][other]['assignment']['agent'] != reviewer}
            targets = set(assignment['depends_on'])
            if assignment.get('review_target'):
                targets.add(assignment['review_target']['request_id'])
            linked_authors = author_ids & targets
            review_owner = state['requests'][rid]['owner_task_id']
            linked_authors = {author for author in linked_authors if review_owner
                and state['requests'][author]['owner_task_id']
                and review_owner != state['requests'][author]['owner_task_id']}
            if linked_authors and results[rid]['readiness']['complete']:
                if reviewer != 'qa-autotest-reviewer' or results[rid]['event'] == 'review_passed':
                    eligible = reviewer != 'qa-autotest-reviewer' or any(
                        results[author]['current_revision'] and results[author]['current_revision'] == results[rid]['reviewed_revision']
                        for author in linked_authors)
                    if eligible:
                        break
        if not eligible:
            raise ValueError('Ревью не подтверждает принятый результат автора')
    item.update(request_ids=rids, evidence=list(selected), evidence_hashes=selected,
                result_hashes={rid: results[rid]['artifact_hashes'] for rid in rids})
    selected_authors = None
    if item['status'] == 'verified' and definition.get('kind') == 'scenario_execution':
        from content_review import check_execution_criterion
        selected_authors = check_execution_criterion(definition, source, state, results)
        if 'scenario_sources' in source:
            item['scenario_sources'] = copy.deepcopy(source['scenario_sources'])
    combination_bundles = None
    if item['status'] == 'verified' and definition.get('kind') == 'combination_coverage':
        from combination_coverage import check_criterion
        combination_bundles = check_criterion(definition, state, results)
        selected_authors = set(combination_bundles)
    if item['status'] == 'verified' and definition.get('required_reviews'):
        from content_review import check_required_reviews
        check_required_reviews(definition, state, results, selected_authors, combination_bundles)
    if recheck and (source.get('evidence_hashes') != selected or source.get('result_hashes') != item['result_hashes']):
        raise ValueError('Доказательства оценки критерия изменились')
    return item


def assess_criteria(path, packet, expected_revision):
    if not isinstance(packet, dict) or not isinstance(packet.get('assessments'), list) or not packet['assessments']:
        raise ValueError('Нужен непустой список assessments')
    def operation(state):
        if state['execution_status'] == 'completed' or state.get('acceptance_mode') != 'criteria':
            raise ValueError('Оценка недоступна для завершённой задачи или режима legacy')
        seen = set()
        for source in packet['assessments']:
            if not isinstance(source, dict):
                raise ValueError('assessment: нужен объект')
            cid = required(source.get('criterion_id'), 'criterion_id')
            if cid in seen or cid not in state['criteria']:
                raise ValueError('Неизвестный или повторный criterion_id')
            seen.add(cid)
            assessment = check_assessment(state, state['criteria'][cid]['definition'], source)
            state['criteria'][cid]['assessment'] = dict(assessment, assessed_at=now())
        return event(state, 'criteria_assessed', criterion_ids=sorted(seen))
    return mutate(path, expected_revision, operation)


def matching_automation_review(state, row, author_result):
    try:
        target = row['assignment'].get('review_target')
        author_ids = [target['request_id']] if target else row['assignment']['depends_on']
        for author_id in author_ids:
            author = state['requests'][author_id]
            if author['assignment']['agent'] != 'qa-test-automation':
                continue
            if not row['owner_task_id'] or not author['owner_task_id'] or row['owner_task_id'] == author['owner_task_id']:
                return False
        check_inputs(row)
        check_answers(state, row['assignment']['questionnaire_ids'])
        assignment_sources(state, row['assignment'])
        result = inspect_result(row['assignment'])
        return (result['artifact_hashes'] == row['result']['artifact_hashes']
                and result['event'] == 'review_passed'
                and result['reviewed_revision'] == author_result['current_revision'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def finish_task(path, outcome, decision, expected_revision):
    if outcome not in ('passed', 'defect_confirmed', 'not_reproduced', 'expected_behavior', 'inconclusive', 'not_applicable'):
        raise ValueError('Неизвестный qa_outcome')
    decision = required(decision, 'decision')
    def operation(state):
        if state['execution_status'] == 'completed':
            raise ValueError('Задача уже завершена')
        latest = {}
        for row in state['requests'].values():
            if row['status'] in ('queued', 'dispatching', 'running', 'reported'):
                raise ValueError('Есть незавершённые назначения или ревью')
            latest[row['assignment']['stage']] = row
        if not latest or any(r['status'] != 'accepted' for r in latest.values()):
            raise ValueError('Последние результаты каждого этапа должны быть приняты')
        for row in latest.values():
            check_blocking_reviews(state, row['assignment']['request_id'])
            check_inputs(row)
            check_answers(state, row['assignment']['questionnaire_ids'])
            assignment_sources(state, row['assignment'])
            result = inspect_result(row['assignment'])
            if result['artifact_hashes'] != row['result']['artifact_hashes']:
                raise ValueError('Принятый результат изменился')
            if result['event'] in ('jira_defect_created', 'jira_duplicate_found'):
                check_readiness(result, [], None)
            if result['event'] in ('blocked', 'coverage_ready', 'changes_requested') or result['event'].startswith('needs_') or result['event'].endswith('_partial'):
                raise ValueError('Последний результат этапа содержит незавершённые условия')
            if result['event'] == 'ready_for_review':
                reviewed = [r for r in state['requests'].values()
                            if r['status'] == 'accepted' and r['assignment']['agent'] == 'qa-autotest-reviewer'
                            and (row['assignment']['request_id'] in r['assignment']['depends_on']
                                 or r['assignment'].get('review_target', {}).get('request_id') == row['assignment']['request_id'])]
                if not any(matching_automation_review(state, r, result) for r in reviewed):
                    raise ValueError('Изменения автотестов требуют принятого независимого review_passed')
        check_completion(state, check_assessment)
        questions = {}
        check_answers(state, list(state['questions']))
        for row in state['questions'].values():
            if row['status'] != 'applied':
                raise ValueError('Есть неприменённые ответы или ожидающие вопросы')
            if fingerprint(row['answers_path']) != row['answers_hash']:
                raise ValueError('Применённые ответы изменились')
            questions.update(row['answer_statuses'])
        if 'deferred' in questions.values():
            raise ValueError('Остались вопросы «Пока не знаю»')
        state.update(execution_status='completed', qa_outcome=outcome, completion_decision=decision, completed_at=now())
        return event(state, 'task_completed', qa_outcome=outcome)
    return mutate(path, expected_revision, operation)


def wait_events(path, after=0, timeout=60):
    if after < 0 or not 0 <= timeout <= 60:
        raise ValueError('after >= 0; timeout должен быть от 0 до 60 секунд')
    deadline = time.monotonic() + timeout
    while True:
        state = read_state(path)
        events = [e for e in state['events'] if e['event_id'] > after and not e['acknowledged']]
        if events or time.monotonic() >= deadline:
            return dict(task_id=state['task_id'], revision=state['revision'], events=events,
                        last_event_id=len(state['events']))
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
