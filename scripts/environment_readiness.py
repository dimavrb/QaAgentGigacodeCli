"""Coordinator-owned admin-guide scope and evidence for every guide point."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from validate_test_cases import read_json


def require(condition, message):
    if not condition:
        raise ValueError('environment: ' + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return isinstance(value, list) and all(text(v) for v in value) and len(value) == len(set(value))


def timestamp(value):
    require(text(value), 'observed_at/created_at needs a timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('environment: invalid timestamp') from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'timestamp needs timezone')
    return parsed


def local(base, value):
    require(text(value) and not value.startswith(('http://', 'https://')), 'local source/evidence path required')
    return (base / value).resolve()


def sha(path):
    require(path.is_file(), 'source/evidence file is unavailable: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_questions(payload, event, point_ids=None, scenario_ids=None, checks=None):
    questions = payload.get('open_questions', [])
    require(isinstance(questions, list), 'open_questions must be a list')
    seen = set()
    for question in questions:
        require(isinstance(question, dict) and text(question.get('question_id')), 'question_id required')
        qid = question['question_id']
        require(qid not in seen, 'duplicate question_id')
        seen.add(qid)
        require(text(question.get('question')), 'question text required')
        require(question.get('status') in ('open', 'resolved'), 'question status must be open/resolved')
        require(type(question.get('blocking')) is bool, 'question blocking must be bool')
        for key in ('context', 'blocks'):
            require(key not in question or isinstance(question[key], str), 'question ' + key + ' must be a string')
        points = question.get('point_ids', [])
        scenarios = question.get('scenario_ids', [])
        require(strings(points) and strings(scenarios), 'question point/scenario IDs must be unique strings')
        if point_ids is not None:
            require(set(points) <= set(point_ids), 'question references unknown guide point')
        if scenario_ids is not None:
            require(set(scenarios) <= set(scenario_ids), 'question references unknown scenario')
        if question['status'] == 'resolved':
            require(text(question.get('resolution')) and text(question.get('decision_source')),
                    'resolved question needs resolution and decision_source')
        elif question['blocking']:
            require(event != 'environment_ready', 'open blocking question excludes full readiness')
            if point_ids is not None:
                require(bool(points), 'blocking question must identify unfinished guide points')
                require(checks is None or all(checks[pid]['status'] != 'passed' for pid in points),
                        'blocking question contradicts passed guide point')


def load_assignment(path):
    path = Path(path).resolve()
    packet = read_json(path.read_text(encoding='utf-8'))
    require(isinstance(packet, dict) and type(packet.get('schema_version')) is int
            and packet['schema_version'] == 1, 'assignment needs schema_version=1')
    for key in ('task_id', 'environment_id', 'product_version'):
        require(text(packet.get(key)), 'assignment needs ' + key)
    timestamp(packet.get('created_at'))
    guide = packet.get('admin_guide')
    require(isinstance(guide, dict), 'admin_guide source required')
    guide_path = local(path.parent, guide.get('path'))
    require(sha(guide_path) == guide.get('sha256'), 'admin guide hash changed')
    guide_text = guide_path.read_text(encoding='utf-8')
    scenario_ids = packet.get('scenario_ids', [])
    require(strings(scenario_ids), 'assignment scenario_ids must be unique strings')
    points = packet.get('points')
    require(isinstance(points, list) and bool(points), 'complete nonempty guide point list required')
    seen = set()
    for point in points:
        require(isinstance(point, dict) and text(point.get('point_id')), 'point_id required')
        require(point['point_id'] not in seen, 'duplicate guide point')
        seen.add(point['point_id'])
        require(text(point.get('quote')) and point['quote'] in guide_text, 'point quote is absent from admin guide')
        require(text(point.get('expected_result')), 'point expected_result required')
        ids = point.get('scenario_ids', scenario_ids)
        require(strings(ids) and set(ids) <= set(scenario_ids), 'point scenario_ids differ from assignment')
        require(not scenario_ids or bool(ids), 'each point must identify affected scenarios')
    if scenario_ids:
        covered = set().union(*(set(p.get('scenario_ids', scenario_ids)) for p in points))
        require(covered == set(scenario_ids), 'guide points must cover every assigned scenario')
    return packet, {str(path), str(guide_path)}


def check_result(message, base, assignment_path, result_path=None):
    packet, paths = load_assignment(assignment_path)
    protected = {Path(p) for p in paths}
    if result_path is not None:
        protected.add(Path(result_path).resolve())
    payload = message['payload']
    require(message['task_id'] == packet['task_id'], 'guide assignment belongs to another task')
    require(payload.get('environment_id') == packet['environment_id'], 'environment differs from guide assignment')
    require(payload.get('product_version') == packet['product_version'], 'product version differs from guide assignment')
    require(payload.get('environment_assignment_sha256') == sha(Path(assignment_path).resolve()),
            'result must bind the coordinator guide assignment hash')
    rows = payload.get('guide_checks')
    require(isinstance(rows, list), 'guide_checks required')
    points = {point['point_id']: point for point in packet['points']}
    checks = {}
    lower = timestamp(packet['created_at'])
    upper = datetime.now(timezone.utc)
    for row in rows:
        require(isinstance(row, dict) and text(row.get('point_id')), 'guide check needs point_id')
        pid = row['point_id']
        require(pid in points and pid not in checks, 'unknown or duplicate guide check')
        status = row.get('status')
        require(status in ('passed', 'failed', 'blocked', 'not_run'), 'invalid guide check status')
        require(text(row.get('actual_result')), 'guide check actual_result required')
        actions, evidence = row.get('actions'), row.get('evidence')
        require(isinstance(actions, list) and all(text(action) for action in actions), 'guide actions must be strings')
        require(strings(evidence), 'guide evidence must be unique local paths')
        if status in ('passed', 'failed'):
            require(bool(actions) and bool(evidence), 'executed guide point requires actions and evidence')
            observed = timestamp(row.get('observed_at'))
            require(lower <= observed <= upper, 'guide observation predates scope or is in the future')
        for ref in evidence:
            evidence_path = local(Path(base), ref)
            require(evidence_path not in protected,
                    'guide/result files do not prove execution')
            sha(evidence_path)
            paths.add(str(evidence_path))
        checks[pid] = row
    require(checks.keys() == points.keys(), 'guide_checks must cover every admin guide point exactly once')
    complete = all(row['status'] == 'passed' for row in checks.values())
    event = message['event']
    scenarios = packet.get('scenario_ids', [])
    ready = {sid for sid in scenarios if all(checks[pid]['status'] == 'passed'
        for pid, point in points.items() if sid in point.get('scenario_ids', scenarios))}
    if event == 'environment_ready':
        require(complete, 'environment_ready requires every admin guide point passed')
    elif event == 'environment_partial':
        require(not complete, 'all guide points passed; use environment_ready')
        require(bool(ready) and ready != set(scenarios), 'partial requires a proven ready subset')
        require(set(payload['ready_scenarios']) == ready
                and set(payload['blocked_scenarios']) == set(scenarios) - ready,
                'partial readiness differs from guide point results')
    else:
        require(event == 'blocked' and not complete, 'blocked result must retain unfinished guide points')
    validate_questions(payload, event, points, scenarios, checks)
    return paths
