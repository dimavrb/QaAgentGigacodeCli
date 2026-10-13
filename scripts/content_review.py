"""Bound content reviews and execution criteria; never infer oracle truth."""
from pathlib import Path
import re


def validate_review(payload, event):
    errors = []
    from review_execution import validate_checks
    errors.extend(validate_checks(payload, event))
    for key in ('target_request_id', 'target_sha256', 'review_scope'):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            errors.append(f'payload.{key} must be a non-empty string')
    if not re.fullmatch('[0-9a-f]{64}', str(payload.get('target_sha256', ''))):
        errors.append('target_sha256 must be a SHA-256 digest')
    seen = set()
    checks = payload.get('checks')
    if not isinstance(checks, list) or not checks:
        errors.append('content review needs checks')
        checks = []
    for check in checks:
        if not isinstance(check, dict):
            errors.append('review check must be an object')
            continue
        cid = check.get('criterion_id')
        if not isinstance(cid, str) or not cid.strip() or cid in seen:
            errors.append('review checks need unique criterion_id')
        else:
            seen.add(cid)
        if check.get('status') not in ('passed', 'failed', 'unknown'):
            errors.append('review check status must be passed/failed/unknown')
        if not isinstance(check.get('note'), str) or not check['note'].strip():
            errors.append('review check needs a concrete note')
        refs = check.get('evidence')
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            errors.append('review check needs evidence references')
        if event == 'review_passed' and check.get('status') != 'passed':
            errors.append('review_passed cannot have unverified checks')
    combination_checks = payload.get('combination_checks', [])
    if not isinstance(combination_checks, list):
        errors.append('combination_checks must be a list')
        combination_checks = []
    pairs = set()
    for check in combination_checks:
        if not isinstance(check, dict):
            errors.append('combination check must be an object')
            continue
        keys = (check.get('bundle_id'), check.get('binding_id'))
        if any(not isinstance(k, str) or not k.strip() for k in keys):
            errors.append('combination check needs bundle_id/binding_id')
        elif keys in pairs:
            errors.append('duplicate combination check')
        else:
            pairs.add(keys)
        if check.get('status') not in ('passed', 'failed', 'unknown'):
            errors.append('combination check status must be passed/failed/unknown')
        if not isinstance(check.get('note'), str) or not check['note'].strip():
            errors.append('combination check needs a concrete note on conditions/rule/observation')
        refs = check.get('evidence')
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
            errors.append('combination check needs evidence')
        if event == 'review_passed' and check.get('status') != 'passed':
            errors.append('review_passed cannot have unverified combination checks')
    findings = payload.get('findings')
    if not isinstance(findings, list):
        errors.append('review findings must be a list')
        findings = []
    ids = set()
    for finding in findings:
        if not isinstance(finding, dict):
            errors.append('review finding must be an object')
            continue
        fid = finding.get('finding_id')
        if not isinstance(fid, str) or not fid.strip() or fid in ids:
            errors.append('review findings need unique finding_id')
        else:
            ids.add(fid)
        if finding.get('criterion_id') not in seen:
            errors.append('finding references an unchecked criterion')
        for key in ('problem', 'evidence', 'resolution_condition'):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                errors.append(f'review finding needs {key}')
        if type(finding.get('blocking')) is not bool:
            errors.append('review finding needs blocking bool')
        if event == 'review_passed' and finding.get('blocking'):
            errors.append('review_passed cannot have blocking findings')
    if event == 'changes_requested' and not any(f.get('blocking') for f in findings if isinstance(f, dict)):
        errors.append('changes_requested needs a blocking finding')
    if not isinstance(payload.get('limitations'), list) or any(not isinstance(x, str) for x in payload['limitations']):
        errors.append('review limitations must be a list of strings')
    return errors


def normalize_target(assignment):
    target = assignment.get('review_target')
    if not isinstance(target, dict):
        raise ValueError('Independent reviewer requires review_target')
    for key in ('request_id', 'result_path', 'scope'):
        if not isinstance(target.get(key), str) or not target[key].strip():
            raise ValueError(f'review_target needs {key}')
    hashes = target.get('artifact_hashes')
    if (not isinstance(hashes, dict) or target['result_path'] not in hashes or
            any(not Path(p).is_absolute() or not re.fullmatch('[0-9a-f]{64}', str(h)) for p, h in hashes.items())):
        raise ValueError('review_target needs absolute artifact_hashes including result_path')
    ids = assignment.get('review_criteria_ids')
    if (not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x.strip() for x in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('review_criteria_ids must be nonempty and unique')
    if not set(hashes) <= set(assignment['inputs']):
        raise ValueError('review inputs must include all target artifacts')


def check_target(state, assignment):
    """A reported author may be reviewed before acceptance; it is not ready input."""
    import task_exchange as exchange
    target = assignment['review_target']
    row = state['requests'].get(target['request_id'])
    if not row or row['status'] not in ('reported', 'accepted'):
        raise ValueError('Review target must be a submitted author result')
    if row['assignment'].get('review_target'):
        raise ValueError('Recursive reviewer target is not supported')
    exchange.check_inputs(row)
    current = exchange.inspect_result(row['assignment'])
    if current['path'] != target['result_path'] or current['artifact_hashes'] != target['artifact_hashes']:
        raise ValueError('Review target differs from the submitted revision')
    if current['artifact_hashes'] != row['result']['artifact_hashes']:
        raise ValueError('Submitted review target changed')
    if assignment['agent'] == 'qa-conclusion-reviewer':
        from conclusion_review import check_assignment
        check_assignment(state, assignment)


def check_binding(message, assignment):
    import task_exchange as exchange
    target, payload = assignment['review_target'], message['payload']
    if (payload.get('target_request_id') != target['request_id'] or
            payload.get('target_sha256') != target['artifact_hashes'][target['result_path']] or
            payload.get('review_scope') != target['scope']):
        raise ValueError('Review does not match assigned target/scope/hash')
    if {c['criterion_id'] for c in payload['checks']} != set(assignment['review_criteria_ids']):
        raise ValueError('Review checks differ from assigned criteria')
    from review_execution import check_binding as check_execution_binding
    check_execution_binding(payload, assignment)
    from conclusion_review import check_binding as check_conclusion_binding
    check_conclusion_binding(payload, assignment)
    from review_discussion import check_binding as check_discussion_binding
    check_discussion_binding(payload, assignment)
    for path, digest in target['artifact_hashes'].items():
        if exchange.fingerprint(path) != digest:
            raise ValueError('Reviewed target artifact changed')
    if payload.get('combination_checks'):
        from qa_contract import parse_message
        from combination_coverage import binding_keys, load_bundles
        author, _ = parse_message(Path(target['result_path']).read_text(encoding='utf-8'))
        known = set().union(*(binding_keys(b) for b in load_bundles(
            author['payload'], Path(target['result_path']).parent)))
        if any((c['bundle_id'], c['binding_id']) not in known for c in payload['combination_checks']):
            raise ValueError('Combination review references a binding outside the assigned target')


def check_execution_criterion(definition, source, state, results):
    from qa_contract import parse_message
    candidates = {sid: [] for sid in definition['scenario_ids']}
    for rid, result in results.items():
        if state['requests'][rid]['assignment']['agent'] != 'qa-feature-tester':
            continue
        message, _ = parse_message(Path(result['path']).read_text(encoding='utf-8'))
        for row in message['payload'].get('scenario_results', []):
            if row['scenario_id'] in candidates:
                candidates[row['scenario_id']].append((rid, row))
    selected = source.get('scenario_sources', {})
    if not isinstance(selected, dict) or set(selected) - set(candidates):
        raise ValueError('scenario_sources must map criterion scenario IDs to request IDs')
    contexts = set()
    authors = set()
    for sid, choices in candidates.items():
        if sid in selected:
            choices = [(rid, row) for rid, row in choices if rid == selected[sid]]
        if len(choices) != 1:
            raise ValueError(f'{sid}: missing or ambiguous execution; use scenario_sources for repeats')
        rid, row = choices[0]
        authors.add(rid)
        if row['status'] not in ('passed', 'failed'):
            raise ValueError(f'{sid}: mandatory scenario was not executed')
        for key, expected in definition.get('execution_context', {}).items():
            if row[key] != expected:
                raise ValueError(f'{sid}: execution context differs from criterion')
        contexts.add((row['environment_id'], row['product_version']))
    if len(contexts) > 1:
        raise ValueError('Execution criterion combines different environments/builds')
    return authors


def check_required_reviews(definition, state, results, selected_authors=None, combination_bundles=None):
    from qa_contract import parse_message
    authors = selected_authors if selected_authors is not None else {
        rid for rid in results if not state['requests'][rid]['assignment'].get('review_target')}
    if not authors:
        raise ValueError('missing independent content review: no selected author')
    author_owners = {state['requests'][rid]['owner_task_id'] for rid in authors}
    candidates = []
    for author in sorted(authors):
        from review_discussion import check_cohort
        check_cohort(state, results, author)
        if not state['requests'][author]['owner_task_id']:
            raise ValueError('missing independent content review: author owner unknown')
        # Passing votes cannot erase a blocking finding on these exact bytes.
        for other in state['requests'].values():
            if (other['status'] == 'accepted' and other['assignment'].get('review_target', {}).get('request_id') == author
                    and other['result']['event'] == 'changes_requested'):
                raise ValueError('Unresolved blocking review of the current target')
        for slot in definition['required_reviews']:
            owners = set()
            for rid, result in results.items():
                row = state['requests'][rid]
                assignment = row['assignment']
                target = assignment.get('review_target', {})
                owner = row['owner_task_id']
                if (assignment['agent'] != slot['agent'] or target.get('scope') != slot['scope']
                        or target.get('request_id') != author or not owner or owner in author_owners):
                    continue
                message, _ = parse_message(Path(result['path']).read_text(encoding='utf-8'))
                if slot.get('conclusions'):
                    from conclusion_review import covers
                    if not covers(message['payload'], assignment, definition['criterion_id'], slot['conclusions']):
                        continue
                if slot.get('execution'):
                    from review_execution import covers
                    if not covers(message['payload'], assignment, definition['criterion_id'], slot['execution']):
                        continue
                if combination_bundles is not None:
                    from combination_coverage import review_covers
                    if not review_covers(message['payload'], combination_bundles[author]):
                        continue
                if message['event'] == 'review_passed' and any(
                        c['criterion_id'] == definition['criterion_id'] and c['status'] == 'passed'
                        for c in message['payload']['checks']):
                    owners.add(owner)
            if not owners:
                raise ValueError(f'{author}/{slot["scope"]}: missing independent content review')
            candidates.append((slot['scope'], sorted(owners)))
    # One reviewer may cover the same direction across fragments; independent
    # directions must never share a session. Search all assignments, not first vote.
    def match(index, used):
        if index == len(candidates):
            return True
        scope, owners = candidates[index]
        for owner in owners:
            if owner in used and used[owner] != scope:
                continue
            if match(index + 1, dict(used, **{owner: scope})):
                return True
        return False
    if not match(0, {}):
        raise ValueError('missing independent content review: directions share sessions')
