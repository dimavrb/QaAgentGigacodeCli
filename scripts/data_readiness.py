"""Bind test-data readiness to coordinator requirements and current observations."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from validate_test_cases import read_json


def require(condition, message):
    if not condition:
        raise ValueError('test data: ' + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return isinstance(value, list) and all(text(v) for v in value) and len(value) == len(set(value))


def timestamp(value):
    require(text(value), 'timestamp required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('test data: invalid timestamp') from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'timestamp needs timezone')
    return parsed


def local(base, value):
    require(text(value) and not value.startswith(('http://', 'https://')), 'local source/evidence path required')
    return (Path(base) / value).resolve()


def sha(path):
    require(path.is_file(), 'source/evidence file unavailable: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_assignment(path):
    path = Path(path).resolve()
    packet = read_json(path.read_text(encoding='utf-8'))
    require(isinstance(packet, dict) and type(packet.get('schema_version')) is int
            and packet['schema_version'] == 1, 'assignment needs schema_version=1')
    for key in ('task_id', 'environment_id', 'product_version'):
        require(text(packet.get(key)), 'assignment needs ' + key)
    require(timestamp(packet.get('created_at')) < timestamp(packet.get('expires_at')),
            'assignment needs a positive validity window')
    scenarios = packet.get('scenario_ids')
    require(strings(scenarios) and bool(scenarios), 'assignment needs unique scenario_ids')
    source = packet.get('requirements_source')
    require(isinstance(source, dict), 'requirements_source required')
    source_path = local(path.parent, source.get('path'))
    require(sha(source_path) == source.get('sha256'), 'requirements source hash changed')
    body = source_path.read_text(encoding='utf-8')
    requirements = packet.get('requirements')
    require(isinstance(requirements, list) and bool(requirements), 'nonempty requirements required')
    seen, covered = set(), set()
    for row in requirements:
        require(isinstance(row, dict) and text(row.get('requirement_id')), 'requirement_id required')
        require(row['requirement_id'] not in seen, 'duplicate requirement_id')
        seen.add(row['requirement_id'])
        require(row.get('kind') in ('property', 'relation', 'visibility'), 'invalid requirement kind')
        require(text(row.get('quote')) and row['quote'] in body, 'requirement quote absent from source')
        require(text(row.get('expected_result')), 'expected_result required')
        ids = row.get('scenario_ids', scenarios)
        require(strings(ids) and bool(ids) and set(ids) <= set(scenarios), 'invalid requirement scenarios')
        covered.update(ids)
    require(covered == set(scenarios), 'requirements must cover all assigned scenarios')
    return packet, {str(path), str(source_path)}


def check_result(message, base, assignment_path, result_path=None, *, context_only=False):
    packet, paths = load_assignment(assignment_path)
    protected = {Path(p) for p in paths}
    if result_path is not None:
        protected.add(Path(result_path).resolve())
    payload = message['payload']
    require(message['task_id'] == packet['task_id'], 'assignment belongs to another task')
    for key in ('environment_id', 'product_version'):
        require(payload.get(key) == packet[key], key + ' differs from assignment')
    require(payload.get('data_assignment_sha256') == sha(Path(assignment_path).resolve()),
            'result must bind coordinator data_assignment hash')
    require(payload.get('data_state') in ('available', 'cleaned', 'unknown'), 'data_state required')
    rows = payload.get('verified_properties')
    require(isinstance(rows, list), 'verified_properties must contain requirement observations')
    requirements = {row['requirement_id']: row for row in packet['requirements']}
    checks = {}
    lower, upper = timestamp(packet['created_at']), datetime.now(timezone.utc)
    for row in rows:
        require(isinstance(row, dict) and text(row.get('requirement_id')), 'structured property check required')
        rid = row['requirement_id']
        require(rid in requirements and rid not in checks, 'unknown or duplicate property check')
        require(row.get('status') in ('passed', 'failed', 'blocked', 'not_run'), 'invalid property status')
        require(text(row.get('actual_result')), 'property actual_result required')
        evidence = row.get('evidence')
        require(strings(evidence), 'property evidence must be unique local paths')
        if row['status'] in ('passed', 'failed'):
            require(bool(evidence), 'observed property requires evidence')
            require(lower <= timestamp(row.get('observed_at')) <= min(upper, timestamp(packet['expires_at'])),
                    'property observation predates scope or is in the future')
        for ref in evidence:
            path = local(base, ref)
            require(path not in protected, 'assignment/source/result do not prove data properties')
            require(path.stat().st_size > 0, 'empty evidence does not prove data properties')
            sha(path)
            paths.add(str(path))
        checks[rid] = row
    require(checks.keys() == requirements.keys(), 'checks must cover every data requirement exactly once')
    scenarios = packet['scenario_ids']
    ready = {sid for sid in scenarios if all(checks[rid]['status'] == 'passed'
        for rid, row in requirements.items() if sid in row.get('scenario_ids', scenarios))}
    event = message['event']
    if event in ('data_ready', 'data_partial'):
        require(payload['data_state'] == 'available', 'cleaned or unknown data cannot be ready')
        require(context_only or upper < timestamp(packet['expires_at']),
                'data validity window expired; reverify in a new assignment')
        if event == 'data_ready':
            require(ready == set(scenarios), 'data_ready requires every property, relation and visibility check passed')
        else:
            require(bool(ready) and ready != set(scenarios), 'partial requires a proven ready subset')
        require(strings(payload.get('scenario_ids')) and set(payload['scenario_ids']) == ready,
                'ready scenarios differ from property checks')
        if event == 'data_partial':
            require(strings(payload.get('blocked_scenarios'))
                    and set(payload['blocked_scenarios']) == set(scenarios) - ready,
                    'blocked scenarios differ from property checks')
    else:
        require(event == 'blocked', 'unexpected data event')
    return paths
