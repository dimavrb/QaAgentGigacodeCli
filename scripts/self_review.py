"""Validate concise author review; evidence meaning remains a coordinator decision."""

CHECK_IDS = ('scope', 'evidence', 'consistency', 'role')


def resolve_reference(reference, message, assignment=None):
    if not isinstance(reference, str) or ':' not in reference:
        raise ValueError('evidence reference must use report:/... or assignment:/...')
    kind, pointer = reference.split(':', 1)
    roots = {'report': ('payload', 'artifacts', 'blockers', 'summary', 'event'),
             'assignment': ('acceptance_criteria', 'scenario_ids', 'goal', 'stop_condition', 'inputs')}
    if kind not in roots or not pointer.startswith('/'):
        raise ValueError('evidence reference must use report:/... or assignment:/...')
    parts = pointer[1:].split('/')
    if parts[0] not in roots[kind]:
        raise ValueError('evidence reference must point to a result or assignment criterion')
    if any('~' in part.replace('~0', '').replace('~1', '') for part in parts):
        raise ValueError('invalid JSON pointer escape')
    current = message if kind == 'report' else assignment
    if current is None:
        return  # Legacy CLI has no assignment context; checked at registered submission.
    for part in parts:
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isascii() and key.isdigit() and str(int(key)) == key and int(key) < len(current):
            current = current[int(key)]
        else:
            raise ValueError(f'evidence reference does not exist: {reference}')


def validate_self_review(message, assignment=None, required=False):
    review = message.get('self_review')
    if 'self_review' not in message:
        return ['missing self_review'] if required else []
    errors = []
    if not isinstance(review, dict):
        return ['self_review must be an object']
    if type(review.get('schema_version')) is not int or review['schema_version'] != 1:
        errors.append('self_review.schema_version must equal 1')
    checks = review.get('checks')
    seen = set()
    limited = False
    if not isinstance(checks, list):
        errors.append('self_review.checks must be a list')
        checks = []
    for index, check in enumerate(checks):
        name = f'self_review.checks[{index}]'
        if not isinstance(check, dict):
            errors.append(f'{name} must be an object')
            continue
        cid = check.get('id')
        if not isinstance(cid, str) or cid not in CHECK_IDS or cid in seen:
            errors.append(f'{name}.id must be a unique scope/evidence/consistency/role')
        else:
            seen.add(cid)
        status = check.get('status')
        if not isinstance(status, str) or status not in ('passed', 'limited'):
            errors.append(f'{name}.status must be passed or limited')
        limited |= status == 'limited'
        note = check.get('note')
        if not isinstance(note, str) or not note.strip():
            errors.append(f'{name}.note must describe the concrete conclusion')
        evidence = check.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            errors.append(f'{name}.evidence must contain references')
            continue
        for reference in evidence:
            try:
                resolve_reference(reference, message, assignment)
            except ValueError as exc:
                errors.append(f'{name}: {exc}')
        if cid == 'scope' and assignment is not None and 'assignment:/acceptance_criteria' not in evidence:
            errors.append(f'{name}: scope must reference assignment:/acceptance_criteria')
    if seen != set(CHECK_IDS):
        errors.append('self_review.checks must cover scope, evidence, consistency and role')
    for field in ('fixes', 'remaining_risks'):
        value = review.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f'self_review.{field} must be a list of non-empty strings')
    if limited and not review.get('remaining_risks'):
        errors.append('limited self-review needs remaining_risks')
    if assignment is not None:
        for field in ('task_id', 'request_id', 'agent'):
            if assignment.get(field) != message.get(field):
                errors.append(f'self-review assignment {field} differs from report')
    return errors
