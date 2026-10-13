"""Check defect verification links and consistency, not the truth of evidence."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_verification(message: dict, state: dict, base: Path) -> list[str]:
    """Called after state validation, so registered local evidence is hash checked."""
    payload = message['payload']
    reference = payload.get('verification_artifact')
    if not nonempty(reference):
        return ['verification_artifact is required for defect verification']
    path = Path(reference)
    path = path if path.is_absolute() else base / path
    # The verification itself must participate in state/review hashes.
    attempts = {a['attempt_id']: a for a in state['attempts']}
    named = payload.get('attempt_ids', [])
    registry = {e['evidence_id']: e for e in state['evidence'] if e['attempt_id'] in named}
    registration = [e for e in registry.values() if e['path'] == reference and e.get('sha256')]
    if not registration or reference not in message.get('artifacts', []) or reference not in payload.get('evidence', []):
        return ['verification_artifact must be registered with sha256 and linked in result artifacts/evidence']
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        return [f'verification_artifact cannot be read: {exc}']
    if not isinstance(record, dict):
        return ['verification must be an object']
    errors = []
    if type(record.get('schema_version')) is not int or record['schema_version'] != 1:
        errors.append('verification.schema_version must equal 1')
    for key in ('task_id', 'request_id'):
        if record.get(key) != message[key]:
            errors.append(f'verification.{key} differs from result')
    outcome = payload.get('outcome')
    if record.get('outcome') != outcome:
        errors.append('verification.outcome differs from result')

    def block(key):
        value = record.get(key)
        if not isinstance(value, dict):
            errors.append(f'verification.{key} must be an object')
            return {}
        fields = {
            'business_rule': ('source', 'expected_result', 'relation'),
            'observation': ('actual_result', 'runtime_evidence_ids'),
            'code_path': ('status', 'repository', 'revision', 'deployment_match', 'deployment_evidence_ids', 'steps', 'assessment'),
            'cause': ('status', 'scope', 'statement', 'evidence_ids'),
            'next_check': ('action', 'goal', 'gap', 'reason', 'repeat_of'),
        }
        for field in fields[key]:
            if field not in value:
                errors.append(f'verification.{key}.{field} is required')
        return value

    def ids(value, label):
        if not isinstance(value, list) or any(not nonempty(v) for v in value):
            errors.append(f'{label} must be a list of evidence IDs')
            return set()
        if len(set(value)) != len(value):
            errors.append(f'{label} has duplicate evidence IDs')
        result = set(value)
        if not result <= registry.keys():
            errors.append(f'{label} must refer to evidence from named attempts')
        if result & {e['evidence_id'] for e in registration}:
            errors.append(f'{label} cannot use verification itself as proof')
        return result

    rule = block('business_rule')
    if rule.get('source') != payload.get('expected_source') or not nonempty(rule.get('expected_result')):
        errors.append('business_rule needs the assigned source and expected_result')
    relation = rule.get('relation')
    required_relation = {'defect_confirmed': 'violated', 'expected_behavior': 'matches',
                         'not_reproduced': 'undetermined', 'inconclusive': 'undetermined'}
    if relation != required_relation.get(outcome):
        errors.append('business_rule.relation is inconsistent with outcome')
    observation = block('observation')
    if not nonempty(observation.get('actual_result')) or observation.get('actual_result') != payload.get('actual_result'):
        errors.append('observation.actual_result must match result')
    runtime = ids(observation.get('runtime_evidence_ids'), 'observation.runtime_evidence_ids')
    for eid in runtime & registry.keys():
        attempt = attempts[registry[eid]['attempt_id']]
        if attempt['status'] != 'executed' or attempt['context'] != state['context']:
            errors.append('runtime evidence needs an executed attempt in the current context')
    if outcome in ('defect_confirmed', 'expected_behavior', 'not_reproduced') and not runtime:
        errors.append('conclusive outcome needs runtime evidence; code alone cannot confirm a defect')

    code = block('code_path')
    code_status = code.get('status')
    if code_status not in ('traced', 'unavailable', 'not_needed'):
        errors.append('code_path.status must be traced, unavailable or not_needed')
    deployment = code.get('deployment_match')
    if deployment not in ('matched', 'mismatch', 'unknown'):
        errors.append('code_path.deployment_match must be matched, mismatch or unknown')
    deployed = ids(code.get('deployment_evidence_ids'), 'code_path.deployment_evidence_ids')
    steps = code.get('steps')
    source_ids = set()
    if not isinstance(steps, list):
        errors.append('code_path.steps must be a list')
        steps = []
    for step in steps:
        if not isinstance(step, dict):
            errors.append('code_path step must be an object')
            continue
        file = step.get('file')
        file_path = PurePosixPath(file) if nonempty(file) else None
        if file_path is None or file_path.is_absolute() or '..' in file_path.parts or '\\' in file:
            errors.append('code_path step needs a repository-relative file')
        if type(step.get('line')) is not int or step['line'] < 1:
            errors.append('code_path step needs a positive line')
        if not nonempty(step.get('behavior')):
            errors.append('code_path step needs behavior')
        proof = ids(step.get('evidence_ids'), 'code_path.step.evidence_ids')
        if not proof:
            errors.append('code_path step needs saved source evidence')
        source_ids |= proof
    if code_status == 'traced' and (not steps or not nonempty(code.get('repository')) or not nonempty(code.get('revision'))):
        errors.append('traced code needs repository, revision and steps')
    if code_status in ('unavailable', 'not_needed') and (steps or deployment != 'unknown' or deployed):
        errors.append('untraced code must not claim steps or a deployment match')
    if code_status in ('unavailable', 'not_needed') and (code.get('repository') is not None or code.get('revision') is not None):
        errors.append('untraced code needs null repository/revision')
    if deployment == 'matched' and (code_status != 'traced' or not deployed):
        errors.append('matched deployment needs traced code and deployment evidence')
    if not nonempty(code.get('assessment')):
        errors.append('code_path.assessment must explain coverage or why code was not traced')
    if runtime & source_ids:
        errors.append('source evidence cannot also be runtime evidence')
    if deployed & source_ids:
        errors.append('source evidence cannot establish the deployed revision')
    for eid in deployed & registry.keys():
        attempt = attempts[registry[eid]['attempt_id']]
        if attempt['status'] != 'executed' or attempt['context'] != state['context']:
            errors.append('deployment evidence needs an executed attempt in the current context')

    cause = block('cause')
    cause_status = cause.get('status')
    cause_ids = ids(cause.get('evidence_ids'), 'cause.evidence_ids')
    if cause_status not in ('unknown', 'suspected', 'confirmed'):
        errors.append('cause.status must be unknown, suspected or confirmed')
    if cause_status == 'unknown':
        if cause.get('statement') is not None or cause.get('scope') is not None or cause_ids:
            errors.append('unknown cause needs null statement/scope and empty evidence_ids')
    else:
        if not nonempty(cause.get('statement')) or cause.get('scope') not in ('application', 'test', 'environment') or not cause_ids:
            errors.append('suspected/confirmed cause needs statement, scope and evidence')
    if cause_status == 'confirmed':
        if not runtime or not cause_ids & runtime:
            errors.append('confirmed cause needs supporting runtime evidence')
        if cause.get('scope') == 'application' and (code_status != 'traced' or deployment != 'matched' or not cause_ids & source_ids):
            errors.append('confirmed application cause needs traced deployed code and supporting source evidence')

    check = block('next_check')
    action = check.get('action')
    if action not in ('none', 'inspect_evidence', 'targeted_check', 'retest', 'blocked'):
        errors.append('next_check.action is invalid')
    if not nonempty(check.get('reason')):
        errors.append('next_check needs a concrete reason')
    if action == 'none':
        if check.get('gap') is not None or check.get('goal') is not None or check.get('repeat_of') is not None:
            errors.append('next_check none needs null gap, goal and repeat_of')
    else:
        if not nonempty(check.get('gap')) or check.get('goal') not in ('defect_confirmation', 'cause_analysis'):
            errors.append('next_check needs a missing fact and goal')
        if outcome in ('defect_confirmed', 'expected_behavior') and check.get('goal') == 'defect_confirmation':
            errors.append('conclusive verdict cannot leave defect confirmation pending')
    repeat = check.get('repeat_of')
    if (repeat is not None and (not nonempty(repeat) or repeat not in named)) or (action == 'retest' and repeat is None):
        errors.append('next_check repeat needs a named previous attempt')
    limits = record.get('limitations')
    if not isinstance(limits, list) or any(not nonempty(x) for x in limits):
        errors.append('verification.limitations must be a list of non-empty strings')
    elif limits != payload.get('limitations'):
        errors.append('verification.limitations must match result')
    return errors
