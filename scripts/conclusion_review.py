"""Bind independent conclusion checks to exact author values and supplied source snapshots."""
import re
from pathlib import Path

from review_execution import equal

ROLE = 'qa-conclusion-reviewer'


def pointer(value, path):
    for token in path[1:].split('/'):
        key=token.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if not re.fullmatch('0|[1-9][0-9]*',key):
                raise ValueError('Invalid JSON pointer array index')
            value=value[int(key)]
        elif isinstance(value,dict):
            value=value[key]
        else:
            raise ValueError('JSON pointer traverses a scalar')
    return value


def conclusion_paths(value):
    if (not isinstance(value, list) or not value or '/summary' not in value
            or any(not isinstance(p, str) or not p.startswith('/') or re.search(r'~(?![01])', p) for p in value)
            or len(set(value)) != len(value)):
        raise ValueError('conclusions need unique JSON pointers including /summary')
    return list(value)


def normalize_assignment(assignment):
    paths = assignment.get('conclusion_paths')
    if not isinstance(paths, dict) or set(paths) != set(assignment['review_criteria_ids']):
        raise ValueError('conclusion_paths must cover exactly review_criteria_ids')
    for value in paths.values():
        conclusion_paths(value)
    sources = assignment.get('review_sources')
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in sources)
            or len(set(sources)) != len(sources) or not set(sources) <= set(assignment['inputs'])):
        raise ValueError('Conclusion reviewer needs local review_sources included in frozen inputs')
    if str(Path(assignment['review_target']['result_path']).resolve()) in {str(Path(p).resolve()) for p in sources}:
        raise ValueError('Author conclusions cannot be their own primary source')
    import task_exchange as exchange
    hashes=assignment.get('review_source_hashes')
    if hashes is None:
        hashes={p:exchange.fingerprint(p) for p in sources}
        assignment['review_source_hashes']=hashes
    if (not isinstance(hashes,dict) or set(hashes)!=set(sources)
            or any(not re.fullmatch('[0-9a-f]{64}',str(v)) for v in hashes.values())):
        raise ValueError('review_source_hashes must freeze every primary source')
    if any(exchange.fingerprint(p)!=digest for p,digest in hashes.items()):
        raise ValueError('Conclusion primary source changed from its frozen hash')


def check_assignment(state, assignment):
    """Required scopes freeze the coverage before the author reports a conclusion."""
    for cid, paths in assignment['conclusion_paths'].items():
        definition = state.get('criteria', {}).get(cid, {}).get('definition', {})
        for slot in definition.get('required_reviews', []):
            if slot['agent'] == ROLE and slot['scope'] == assignment['review_target']['scope']:
                if paths != slot['conclusions']:
                    raise ValueError('Conclusion paths differ from agreed review criteria')


def validate_checks(payload, event, required=False):
    errors = []
    checks = payload.get('conclusion_checks', [])
    if not isinstance(checks, list) or required and not checks:
        return ['Conclusion review needs nonempty conclusion_checks']
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            errors.append('Conclusion check must be an object')
            continue
        pair = check.get('criterion_id'), check.get('pointer')
        if (any(not isinstance(v, str) or not v.strip() for v in pair)
                or not str(pair[1]).startswith('/') or pair in seen):
            errors.append('Conclusion checks need unique criterion_id/pointer')
        else:
            seen.add(pair)
        if 'conclusion' not in check:
            errors.append('Conclusion check needs the exact author value')
        if check.get('status') not in ('passed', 'failed', 'unknown'):
            errors.append('Conclusion status must be passed/failed/unknown')
        if not isinstance(check.get('note'), str) or not check['note'].strip():
            errors.append('Conclusion check needs a reason addressing the author assertion')
        refs = check.get('source_refs')
        if not isinstance(refs, list) or check.get('status') == 'passed' and not refs:
            errors.append('Passed conclusion needs source_refs')
            refs = []
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not Path(ref['path']).is_absolute()
                    or not isinstance(ref.get('quote'), str) or not ref['quote'].strip()):
                errors.append('Conclusion source_ref needs an absolute path and exact nonempty quote')
        if event == 'review_passed' and check.get('status') != 'passed':
            errors.append('review_passed cannot have unverified conclusions')
    return errors


def check_binding(payload, assignment):
    if assignment['agent'] != ROLE:
        if payload.get('conclusion_checks'):
            raise ValueError('conclusion_checks require qa-conclusion-reviewer')
        return
    expected = {(cid, p) for cid, paths in assignment['conclusion_paths'].items() for p in paths}
    checks = payload['conclusion_checks']
    if {(c['criterion_id'], c['pointer']) for c in checks} != expected:
        raise ValueError('Conclusion checks must cover every assigned author field')
    from qa_contract import parse_message
    author, _ = parse_message(Path(assignment['review_target']['result_path']).read_text())
    source_paths = set(assignment['review_sources'])
    import task_exchange as exchange
    if any(exchange.fingerprint(p)!=digest for p,digest in assignment['review_source_hashes'].items()):
        raise ValueError('Conclusion primary source changed from its frozen hash')
    for check in checks:
        try:
            actual = pointer(author, check['pointer'])
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ValueError('Conclusion pointer is absent from the author report') from exc
        if not equal(actual, check['conclusion']):
            raise ValueError('Conclusion check differs from the exact author value')
        for ref in check['source_refs']:
            if ref['path'] not in source_paths:
                raise ValueError('Conclusion evidence is outside the assigned source snapshots')
            if ref['quote'] not in Path(ref['path']).read_text(encoding='utf-8'):
                raise ValueError('Conclusion evidence quote is absent from its source')


def covers(payload, assignment, cid, paths):
    check_binding(payload, assignment)
    selected = {c['pointer']: c for c in payload.get('conclusion_checks', []) if c['criterion_id'] == cid}
    return set(selected) == set(paths) and all(c['status'] == 'passed' for c in selected.values())
