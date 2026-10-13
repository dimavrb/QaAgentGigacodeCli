"""Bounded exchange of disputed assertions; peer agreement is never primary evidence."""
from pathlib import Path


def configuration(value, reviewers):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'max_rounds'}:
        raise ValueError('discussion needs max_rounds only')
    rounds = value.get('max_rounds', 2)
    if type(rounds) is not int or not 1 <= rounds <= 2:
        raise ValueError('Discussion max_rounds must be 1..2')
    if len(reviewers) != 2 or any(r['agent'] != 'qa-conclusion-reviewer' for r in reviewers):
        raise ValueError('Discussion needs two independent qa-conclusion-reviewer scopes')
    return dict(max_rounds=rounds)


def positions(message):
    payload = message['payload']
    result = {(c['criterion_id'], c['pointer']): c['status'] for c in payload['conclusion_checks']}
    result.update({(c['criterion_id'], ''): c['status'] for c in payload['checks']})
    return result


def disputes(reviews):
    left, right = [positions(r[1]) for r in reviews]
    fields = sorted(k for k in set(left) & set(right) if left[k] != right[k] and k[1])
    fields += sorted(k for k in set(left) & set(right) if left[k] != right[k] and not k[1]
                     and not any(p[0] == k[0] for p in fields))
    return [dict(issue_id=f'I-{i+1}', criterion_id=cid, pointer=p) for i, (cid, p) in enumerate(fields)]


def normalize_assignment(assignment):
    import task_exchange as exchange
    binding = assignment.get('review_discussion')
    if not isinstance(binding, dict) or set(binding) != {'packet_path', 'packet_sha256', 'round', 'prior_request_id'}:
        raise ValueError('Invalid review_discussion binding')
    path = binding['packet_path']
    if (assignment['agent'] != 'qa-conclusion-reviewer' or not isinstance(path, str)
            or not Path(path).is_absolute() or path not in assignment['inputs']
            or type(binding['round']) is not int or not 1 <= binding['round'] <= 2
            or exchange.fingerprint(path) != binding['packet_sha256']):
        raise ValueError('Discussion needs its frozen packet and a conclusion reviewer')
    packet = exchange.read_json(path)
    target = assignment['review_target']
    if (packet.get('schema_version') != 1 or packet.get('author_request') != target['request_id']
            or packet.get('target_sha256') != target['artifact_hashes'][target['result_path']]
            or packet.get('round') != binding['round'] or not packet.get('issues')):
        raise ValueError('Discussion packet differs from the assigned target/round')
    own = [r for r in packet['reviews'] if r['scope'] == target['scope'] and r['request_id'] == binding['prior_request_id']]
    if len(own) != 1:
        raise ValueError('Discussion must reference its own prior direction')
    for review in packet['reviews']:
        if not set(review['artifact_hashes']) <= set(assignment['inputs']):
            raise ValueError('Discussion inputs must include the frozen prior reviews')
        for path, digest in review['artifact_hashes'].items():
            if exchange.fingerprint(path) != digest:
                raise ValueError('Discussion prior review changed')
    if set(assignment['review_sources']) & {r['result_path'] for r in packet['reviews']}:
        raise ValueError('Peer verdict cannot be a primary discussion source')
    return packet, own[0]


def validate_checks(payload, event):
    errors = []
    checks = payload.get('discussion_checks', [])
    if not isinstance(checks, list):
        return ['discussion_checks must be a list']
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            errors.append('Discussion check must be an object')
            continue
        iid = check.get('issue_id')
        if not isinstance(iid, str) or not iid.strip() or iid in seen:
            errors.append('Discussion checks need unique issue_id')
        else:
            seen.add(iid)
        if check.get('position') not in ('maintain', 'revise') or check.get('status') not in ('resolved', 'unresolved'):
            errors.append('Discussion needs maintain/revise and resolved/unresolved')
        if not isinstance(check.get('note'), str) or not check['note'].strip():
            errors.append('Discussion needs a reason for the position')
        refs = check.get('source_refs')
        if (not isinstance(refs, list) or (check.get('status') == 'resolved' or check.get('position') == 'revise')
                and not refs and not isinstance(check.get('receipt'), str)):
            errors.append('Resolved or revised discussion needs primary evidence')
            refs = []
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not Path(ref['path']).is_absolute()
                    or not isinstance(ref.get('quote'), str) or not ref['quote'].strip()):
                errors.append('Discussion source needs an absolute path and exact quote')
        if 'receipt' in check and (not isinstance(check['receipt'], str) or not check['receipt'].strip()):
            errors.append('Discussion receipt must be a local observation reference')
        if event == 'review_passed' and check.get('status') != 'resolved':
            errors.append('Passing discussion cannot contain unresolved issues')
    return errors


def check_binding(payload, assignment):
    import task_exchange as exchange
    from qa_contract import parse_message
    if not assignment.get('review_discussion'):
        if payload.get('discussion_checks'):
            raise ValueError('discussion_checks require an assigned discussion')
        return
    packet, own = normalize_assignment(assignment)
    checks = payload.get('discussion_checks', [])
    if {c['issue_id'] for c in checks} != {i['issue_id'] for i in packet['issues']}:
        raise ValueError('Discussion must address every assigned dispute')
    prior, _ = parse_message(Path(own['result_path']).read_text())
    before = positions(prior)
    after = positions(dict(payload=payload))
    allowed = {(i['criterion_id'], i['pointer']) for i in packet['issues']}
    if any(before[k] != after[k] and k not in allowed and (k[1] or not any(p[0] == k[0] for p in allowed)) for k in before):
        raise ValueError('Discussion cannot change an undisputed check')
    issues = {i['issue_id']: i for i in packet['issues']}
    for check in checks:
        issue = issues[check['issue_id']]
        key = issue['criterion_id'], issue['pointer']
        changed = before[key] != after[key]
        if (check['position'] == 'revise') != changed:
            raise ValueError('Discussion position differs from the recorded prior verdict')
        for ref in check['source_refs']:
            if ref['path'] not in assignment['review_sources']:
                raise ValueError('Discussion cites a peer verdict or unassigned source')
            if ref['quote'] not in Path(ref['path']).read_text():
                raise ValueError('Discussion quote is absent from primary source')
        if check.get('receipt'):
            from review_execution import check_receipt
            check_receipt(check['receipt'], assignment)


def check_cohort(state, results, author):
    """Do not cherry-pick initial votes and later discussion replies."""
    import task_exchange as exchange
    chosen = [state['requests'][rid] for rid in results
              if state['requests'][rid]['assignment'].get('review_target', {}).get('request_id') == author]
    discussed = [r for r in chosen if r['assignment'].get('review_discussion')]
    if not discussed:
        return
    bindings = {r['assignment']['review_discussion']['packet_path'] for r in discussed}
    if len(bindings) != 1 or len(discussed) != len(chosen):
        raise ValueError('Cannot mix independent votes and discussion rounds')
    packet = exchange.read_json(next(iter(bindings)))
    if {r['scope'] for r in packet['reviews']} != {r['assignment']['review_target']['scope'] for r in chosen}:
        raise ValueError('Discussion acceptance needs both directions of the same round')


def advance(path, author_request, initial_reviews):
    """Return final reviews, or None while owned children run; persist every round."""
    import review_cycle as cycle
    import execution_supervisor as supervisor
    import task_exchange as exchange
    from qa_contract import parse_message
    state = cycle.read(path)
    issues = disputes(initial_reviews)
    if not issues:
        return initial_reviews
    key = str(state['round'])
    if key not in state.get('discussions', {}):
        log = dict(author_request=author_request, initial_review_ids=[r[0] for r in initial_reviews],
                   issues=issues, rounds=[], status='pending')
        def start(value):
            value.setdefault('discussions', {})[key] = log
            value['status'] = 'discussing'
        cycle.update(path, start)
    log = cycle.read(path)['discussions'][key]
    reviews = initial_reviews
    for number in range(1, state['plan']['discussion']['max_rounds'] + 1):
        registry = exchange.read_state(state['plan']['author_plan']['task_state'])
        snapshots = [dict(request_id=rid, scope=registry['requests'][rid]['assignment']['review_target']['scope'],
                          result_path=registry['requests'][rid]['result']['path'],
                          artifact_hashes=registry['requests'][rid]['result']['artifact_hashes']) for rid, _, _ in reviews]
        target = registry['requests'][author_request]['result']
        packet = dict(schema_version=1, author_request=author_request,
                      target_sha256=target['artifact_hashes'][target['path']], round=number, reviews=snapshots, issues=log['issues'])
        base = Path(path).resolve().parent / (Path(path).name + '.rounds') / f'round-{state["round"]}'
        packet_path = base / f'discussion-{number}.json'
        if packet_path.exists():
            if exchange.read_json(packet_path) != packet:
                raise ValueError('Frozen discussion packet changed')
        else:
            exchange.atomic_write(packet_path, packet)
        binding = dict(packet_path=str(packet_path), packet_sha256=exchange.fingerprint(packet_path), round=number)
        answers = []
        for slot in state['plan']['reviewers']:
            own = next(r for r in snapshots if r['scope'] == slot['scope'])
            assignment_binding = dict(binding, prior_request_id=own['request_id'])
            child = cycle.ensure_child(path, cycle.read(path), 'discussion', slot, author_request, assignment_binding)
            progress = supervisor.tick(child['path'])
            if progress['status'] in ('blocked', 'needs_reconciliation'):
                raise ValueError('Discussion worker requires coordinator: ' + slot['scope'])
            reported = cycle.reported(child)
            if not reported:
                return None
            rid, row = reported
            current = exchange.inspect_result(row['assignment'])
            if current['artifact_hashes'] != row['result']['artifact_hashes']:
                raise ValueError('Discussion response changed')
            response, _ = parse_message(Path(current['path']).read_text())
            if row['status'] == 'reported':
                exchange.review_result(state['plan']['author_plan']['task_state'], rid, 'Evidence-bound discussion recorded; original verdict retained', True, None)
            answers.append((rid, response, row['owner_task_id']))
        author_owner = registry['requests'][author_request]['owner_task_id']
        if len({r[2] for r in answers}) != 2 or any(not r[2] or r[2] == author_owner for r in answers):
            raise ValueError('Discussion directions require independent owners')
        resolved = not disputes(answers) and all(c['status'] == 'resolved' for r in answers for c in r[1]['payload']['discussion_checks'])
        record = dict(round=number, packet_path=str(packet_path), packet_sha256=binding['packet_sha256'],
                      review_ids=[r[0] for r in answers], resolved=resolved)
        def save(value):
            entry = value['discussions'][key]
            if len(entry['rounds']) < number:
                entry['rounds'].append(record)
            elif entry['rounds'][number-1] != record:
                raise ValueError('Discussion round changed after recording')
            entry['status'] = 'resolved' if resolved else 'pending'
        cycle.update(path, save)
        if resolved:
            return answers
        reviews = answers
    cycle.update(path, lambda value: value['discussions'][key].update(status='unresolved'))
    raise ValueError('Discussion budget exhausted: unresolved disagreement')
