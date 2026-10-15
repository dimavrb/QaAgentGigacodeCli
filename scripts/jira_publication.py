"""Accept Jira publication only against accepted investigation and saved readback."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from validate_test_cases import read_json


def require(condition, message):
    if not condition:
        raise ValueError('jira: ' + message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def stamp(value):
    require(text(value), 'timestamp required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('jira: invalid timestamp') from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'timestamp needs timezone')
    return parsed


def sha(path):
    require(path.is_file() and path.stat().st_size > 0, 'nonempty local artifact required: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local(base, ref):
    require(text(ref) and not urlsplit(ref).scheme, 'local artifact path required')
    return (Path(base) / ref).resolve()


def load(path):
    return read_json(Path(path).read_text(encoding='utf-8'))


def identity(key, url, scope):
    require(text(key) and re.fullmatch(re.escape(scope['project_key']) + r'-[1-9][0-9]*', key),
            'issue key differs from assigned project')
    require(url == scope['jira_base_url'] + '/browse/' + key, 'issue URL differs from assigned Jira/key')


def load_assignment(path):
    path = Path(path).resolve()
    scope = load(path)
    require(isinstance(scope, dict) and type(scope.get('schema_version')) is int
            and scope['schema_version'] == 1, 'assignment needs schema_version=1')
    for key in ('task_id', 'investigation_request_id', 'investigation_sha256', 'project_key', 'jira_base_url'):
        require(text(scope.get(key)), 'assignment needs ' + key)
    require(re.fullmatch('[0-9a-f]{64}', scope['investigation_sha256']), 'invalid investigation hash')
    require(re.fullmatch('[A-Z][A-Z0-9_]*', scope['project_key']), 'invalid project key')
    url = urlsplit(scope['jira_base_url'])
    require(url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
            and not url.query and not url.fragment and not scope['jira_base_url'].endswith('/'), 'invalid Jira base URL')
    stamp(scope.get('created_at'))
    fields = scope.get('expected_fields')
    require(isinstance(fields, dict) and all(text(fields.get(k)) for k in
            ('project_key', 'issue_type', 'summary', 'description')), 'expected Jira fields required')
    require(fields['project_key'] == scope['project_key'], 'expected project differs from assignment')
    skill = scope.get('skill')
    require(isinstance(skill, dict), 'installed jira-defect-creator skill snapshot required')
    skill_path = local(path.parent, skill.get('path'))
    require(sha(skill_path) == skill.get('sha256'), 'skill hash changed')
    gateway = scope.get('gateway')
    require(isinstance(gateway, dict), 'gateway binding required')
    for key in ('step_id', 'create_tool', 'issue_key_pointer'):
        require(text(gateway.get(key)), 'gateway needs ' + key)
    require(gateway['issue_key_pointer'].startswith('/'), 'issue key pointer required')
    require(isinstance(gateway.get('create_arguments'), dict) and bool(gateway['create_arguments']),
            'exact creation arguments required')
    from action_gateway import validate_policy
    policy_path = local(path.parent, gateway.get('policy_path'))
    policy = validate_policy(load(policy_path))
    require(policy['max_write_actions'] == 1 and policy['max_action_attempts'] == 1,
            'Jira step permits one write action and one creation attempt')
    writes = [name for name, rule in policy['tools'].items() if rule['effect'] == 'write']
    require(writes == [gateway['create_tool']], 'only the assigned creation tool may write')
    state_path = local(path.parent, gateway.get('state_path'))
    require(state_path not in (path, policy_path, skill_path), 'gateway state must have a separate path')
    return scope, {str(path), str(policy_path), str(skill_path)}


def check_source(state, assignment, scope):
    """Run at assign, prepare, submit, review and subsequent source revalidation."""
    rid = scope['investigation_request_id']
    require(scope['task_id'] == state['task_id'] == assignment['task_id'], 'scope belongs to another task')
    require(rid in assignment['depends_on'] and assignment.get('dependency_modes', {}).get(rid, 'ready') == 'ready',
            'confirmed investigation must be an explicit ready dependency')
    row = state['requests'].get(rid)
    require(row is not None and row['status'] == 'accepted'
            and row['assignment']['agent'] == 'qa-defect-investigator', 'accepted investigation required')
    require(bool(row['assignment'].get('investigation_state')), 'state-backed investigation required')
    source_path = Path(row['assignment']['result_path'])
    require(sha(source_path) == scope['investigation_sha256'], 'investigation revision differs from scope')
    source = load(source_path)
    require(source.get('event') == 'investigation_complete'
            and source.get('payload', {}).get('outcome') == 'defect_confirmed', 'only defect_confirmed may be published')
    current_gateway = scope['gateway']
    scope_base = Path(assignment['jira_assignment']).parent if assignment.get('jira_assignment') else None
    if scope_base is not None:
        for previous in state['requests'].values():
            previous_path = previous['assignment'].get('jira_assignment')
            if not previous_path:
                continue
            from task_exchange import check_inputs
            check_inputs(previous)
            old = load(previous_path)
            if (old.get('investigation_sha256') == scope['investigation_sha256']
                    and old.get('jira_base_url') == scope['jira_base_url']
                    and old.get('project_key') == scope['project_key']):
                require(local(Path(previous_path).parent, old['gateway']['state_path'])
                        == local(scope_base, current_gateway['state_path'])
                        and old['gateway']['step_id'] == current_gateway['step_id'],
                        'same defect must reuse its durable gateway operation, including after timeout')
    return row


def check_bundle(message, base, assignment_path, task_state, result_path=None):
    from action_gateway import ActionGateway, pointer
    from task_exchange import read_state, check_sources
    scope, paths = load_assignment(assignment_path)
    require(message['task_id'] == scope['task_id'], 'report belongs to another task')
    state = read_state(task_state)
    rid = scope['investigation_request_id']
    synthetic_assignment = dict(task_id=message['task_id'], depends_on=[rid], jira_assignment=str(assignment_path))
    source = check_source(state, synthetic_assignment, scope)
    # Check the source role before recursively validating dependencies.
    sources = check_sources(state, [rid])
    paths.update(sources[0]['artifacts'])
    require(message['event'] in ('jira_defect_created', 'jira_duplicate_found', 'blocked'), 'unexpected Jira event')
    payload = message['payload']
    if message['event'] == 'blocked' and 'publication_artifact' not in payload:
        return paths
    require(payload.get('jira_assignment_sha256') == sha(Path(assignment_path).resolve()), 'assignment hash differs')
    publication_path = local(base, payload.get('publication_artifact'))
    protected = {Path(p) for p in paths} | {Path(task_state).resolve()}
    if result_path is not None:
        protected.add(Path(result_path).resolve())
    require(publication_path not in protected, 'publication proof must be separate from inputs/result')
    record = load(publication_path)
    require(isinstance(record, dict) and type(record.get('schema_version')) is int
            and record['schema_version'] == 1, 'publication needs schema_version=1')
    require(record.get('task_id') == message['task_id'] and record.get('request_id') == message['request_id'],
            'publication task/request differs')
    paths.add(str(publication_path))
    lower, upper = stamp(scope['created_at']), datetime.now(timezone.utc)

    def snapshot(ref):
        path = local(base, ref)
        require(path not in protected and path != publication_path, 'source/report is not Jira readback')
        sha(path)
        paths.add(str(path))
        value = load(path)
        require(isinstance(value, dict) and lower <= stamp(value.get('observed_at')) <= upper,
                'Jira observation predates scope or is in the future')
        require(value.get('jira_base_url') == scope['jira_base_url'], 'observation uses another Jira')
        return value

    search = snapshot(record.get('search_artifact'))
    require(search.get('project_key') == scope['project_key'] and text(search.get('query')),
            'scoped duplicate query required')
    require(search.get('complete') is True, 'incomplete duplicate search cannot close publication')
    candidates = search.get('candidates')
    require(isinstance(candidates, list), 'duplicate candidates required')
    seen, matches, candidate_read_times = set(), set(), []
    for candidate in candidates:
        require(isinstance(candidate, dict), 'invalid duplicate candidate')
        key = candidate.get('issue_key')
        identity(key, candidate.get('issue_url'), scope)
        require(key not in seen, 'duplicate search candidate')
        seen.add(key)
        require(candidate.get('assessment') in ('same_defect', 'different') and text(candidate.get('reason')),
                'every candidate needs a resolved assessment and reason')
        observed = snapshot(candidate.get('readback_artifact'))
        candidate_read_times.append(stamp(observed['observed_at']))
        require(observed.get('issue_key') == key and observed.get('issue_url') == candidate['issue_url'],
                'candidate readback differs')
        require(isinstance(observed.get('fields'), dict) and text(observed['fields'].get('summary'))
                and text(observed['fields'].get('description')), 'candidate content must be read')
        if candidate['assessment'] == 'same_defect':
            matches.add(key)
    gateway = scope['gateway']
    gateway_path = local(Path(assignment_path).resolve().parent, gateway['state_path'])
    require(gateway_path.is_file() and gateway_path not in protected and gateway_path != publication_path,
            'existing separate gateway state required')
    policy = load(local(Path(assignment_path).resolve().parent, gateway['policy_path']))
    client = ActionGateway(gateway_path, policy, scope['task_id'], gateway['step_id'], None)
    client.check(gateway['create_tool'], gateway['create_arguments'])
    journal = client._read()
    actions = journal['actions']
    paths.add(str(gateway_path))
    require(isinstance(actions, dict) and len(actions) <= 1, 'multiple Jira writes are not allowed')
    if message['event'] == 'blocked':
        return paths
    key, url = payload.get('issue_key'), payload.get('issue_url')
    identity(key, url, scope)
    if message['event'] == 'jira_duplicate_found':
        require(not actions, 'duplicate_found must not hide a creation attempt')
        require(key in matches, 'selected issue was not confirmed as the same defect')
    else:
        require(not matches, 'matching duplicate prohibits creation')
        require(len(actions) == 1, 'creation requires a durable gateway action')
        action = next(iter(actions.values()))
        require(action.get('tool') == gateway['create_tool']
                and action.get('arguments') == gateway['create_arguments'], 'creation arguments differ from approved scope')
        require(type(action.get('attempts')) is int and action['attempts'] == 1
                and action.get('status') == 'verified', 'unresolved or repeated creation cannot be accepted')
        require(lower <= stamp(action.get('started_at')) <= upper
                and stamp(search['observed_at']) <= stamp(action['started_at'])
                and all(when <= stamp(action['started_at']) for when in candidate_read_times),
                'duplicate search and candidate reads must precede creation')
        response = client._cached(action)
        require(pointer(response, gateway['issue_key_pointer']) == key, 'gateway receipt names another issue')
        paths.add(action['receipt_path'])
        observed = snapshot(record.get('readback_artifact'))
        require(stamp(observed['observed_at']) >= stamp(action['started_at']), 'issue must be read after creation')
        identity(observed.get('issue_key'), observed.get('issue_url'), scope)
        require(observed['issue_key'] == key, 'readback names another issue')
        fields = observed.get('fields')
        require(isinstance(fields, dict) and all(fields.get(k) == v for k, v in scope['expected_fields'].items()),
                'saved issue fields differ from approved content')
    evidence = payload.get('evidence')
    accepted_evidence = {str((Path(source['assignment']['artifact_base']) / ref).resolve())
                         for ref in load(source['assignment']['result_path'])['payload']['evidence']
                         if not urlsplit(ref).scheme}
    require(isinstance(evidence, list) and bool(evidence) and all(text(ref) for ref in evidence),
            'Jira result needs accepted investigation evidence')
    require({str(local(base, ref)) for ref in evidence} <= accepted_evidence,
            'Jira evidence differs from accepted investigation')
    return paths
