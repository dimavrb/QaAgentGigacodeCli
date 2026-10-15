"""Bounded author/reviewer rounds over the existing managed execution supervisor."""
import copy
import hashlib
import json
from pathlib import Path

import execution_supervisor as supervisor
import action_gateway
import context_builder
import task_exchange as exchange
from qa_contract import parse_message


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    state = exchange.read_json(path)
    if (state.get('schema_version') != 1 or state.get('kind') != 'content_review_cycle'
            or digest(state['plan']) != state['plan_sha256']):
        raise ValueError('Damaged or changed review cycle plan')
    return state


def update(path, operation):
    with exchange.locked(path) as target:
        state = read(target)
        operation(state)
        state['revision'] += 1
        exchange.atomic_write(target, state)


def initialize(path, source, base):
    if not isinstance(source, dict) or type(source.get('schema_version')) is not int or source['schema_version'] != 1:
        raise ValueError('Review cycle needs schema_version=1')
    author = supervisor.normalize_plan(source.get('author_plan'), base)
    if len(author['steps']) != 1:
        raise ValueError('Review cycle requires one author step; compose tasks through the registry')
    assignment = author['steps'][0]['assignment']
    if assignment.get('review_target') or assignment['agent'] in ('qa-result-reviewer', 'qa-autotest-reviewer', 'qa-conclusion-reviewer'):
        raise ValueError('Reviewer cannot recursively author a review cycle')
    registry = exchange.read_state(author['task_state'])
    if registry['execution_status'] == 'completed':
        raise ValueError('Task already completed')
    slots = source.get('reviewers')
    if not isinstance(slots, list) or not 1 <= len(slots) <= 2:
        raise ValueError('Review cycle needs one or two reviewers')
    scopes, normalized = set(), []
    for slot in slots:
        if not isinstance(slot, dict):
            raise ValueError('Review slot must be an object')
        scope = exchange.required(slot.get('scope'), 'scope')
        if not scope.replace('-', '').replace('_', '').isalnum() or scope in scopes:
            raise ValueError('Review scope must be unique and filesystem-safe')
        scopes.add(scope)
        agent = slot.get('agent', 'qa-result-reviewer')
        if agent not in ('qa-result-reviewer', 'qa-autotest-reviewer', 'qa-conclusion-reviewer'):
            raise ValueError('Unknown reviewer role')
        review = dict(scope=scope, agent=agent,
            adapter=copy.deepcopy(slot.get('adapter', author['adapter'])),
            max_wall_seconds=supervisor.number(slot.get('max_wall_seconds', 300), 'review wall time', .05, 86400))
        if 'focus' in slot:
            review['focus'] = exchange.required(slot['focus'], 'review focus')
        if agent == 'qa-conclusion-reviewer':
            sources = [exchange.local_path(p, base) for p in exchange.id_list(slot.get('review_sources', []), 'review_sources')]
            if not sources:
                raise ValueError('Conclusion review cycle needs primary review_sources')
            review['review_sources'] = sources
            review['source_hashes'] = {p: exchange.fingerprint(p) for p in sources}
        if 'gateway_policy' in slot:
            review['gateway_policy'] = action_gateway.validate_policy(slot['gateway_policy'])
        for key in ('context_mode', 'max_context_characters', 'test_data_sections', 'instruction_paths'):
            if key in slot:
                review[key] = copy.deepcopy(slot[key])
        context_builder.profile(dict(agent=agent), review)
        limit = review.get('max_context_characters', 200000)
        if type(limit) is not int or not 1000 <= limit <= 10000000:
            raise ValueError('max_context_characters must be an integer in 1000..10000000')
        normalized.append(review)
    if assignment['agent'] == 'qa-test-automation' and not any(s['agent'] == 'qa-autotest-reviewer' for s in normalized):
        raise ValueError('Automation requires its independent qa-autotest-reviewer')
    for cid in assignment['criterion_ids']:
        definition = registry['criteria'][cid]['definition']
        for required in definition.get('required_reviews', []):
            if not any(s['scope'] == required['scope'] and s['agent'] == required['agent'] for s in normalized):
                raise ValueError('Cycle does not include every required review scope')
            if required.get('execution'):
                selected = next(s for s in normalized if s['scope'] == required['scope'] and s['agent'] == required['agent'])
                policy = selected.get('gateway_policy', {})
                if any(check['tool'] not in policy.get('tools', {}) for check in required['execution']):
                    raise ValueError('Runtime review needs its own policy with every required execution tool')
        role = definition.get('required_review_agent')
        if role and not any(s['agent'] == role for s in normalized):
            raise ValueError('Cycle omits the required reviewer role')
    corrections = source.get('max_corrections', 2)
    if type(corrections) is not int or not 0 <= corrections <= 5:
        raise ValueError('max_corrections must be 0..5')
    plan = dict(author_plan=author, reviewers=normalized, max_corrections=corrections,
                max_active_seconds=supervisor.number(source.get('max_active_seconds', 1800), 'cycle active budget', 1, 86400))
    from review_discussion import configuration
    discussion = configuration(source.get('discussion'), normalized)
    if discussion:
        plan['discussion'] = discussion
    target = Path(path).resolve()
    protected = {str(target), str(target) + '.lock', str(target) + '.controller.lock'}
    review_sources = {p for s in normalized for p in s.get('review_sources', [])}
    if review_sources & {assignment['result_path'], author['task_state'], author['failure_journal'], *protected}:
        raise ValueError('Conclusion sources cannot overlap mutable results/state')
    if protected & {assignment['result_path'], *assignment['inputs'], author['task_state'], author['failure_journal']}:
        raise ValueError('Cycle state cannot be an input/result or overwrite another state')
    # Validate all review adapters at initialization, without starting any process.
    for slot in normalized:
        probe = copy.deepcopy(author)
        probe['adapter'] = slot['adapter']
        probe['steps'][0].pop('gateway_policy', None)
        if slot.get('gateway_policy'):
            probe['steps'][0]['gateway_policy'] = slot['gateway_policy']
        supervisor.normalize_plan(probe, Path('/'))
    with exchange.locked(target) as locked:
        if locked.exists():
            raise ValueError('Review cycle already exists')
        exchange.atomic_write(locked, dict(schema_version=1, kind='content_review_cycle', revision=1,
            plan=plan, plan_sha256=digest(plan), task_id=registry['task_id'], status='pending',
            round=0, children=[], feedback=[], rejection_signatures=[], cancel_requested=False))
    return read(target)


def active_seconds(state):
    return sum(supervisor.active_seconds(supervisor.read(row['path']))
               for row in state['children'] if Path(row['path']).exists())


def author_plan(state):
    plan = copy.deepcopy(state['plan']['author_plan'])
    step = plan['steps'][0]
    for name, expected in step['input_hashes'].items():
        if exchange.fingerprint(name) != expected:
            raise ValueError('Original author input changed')
    for name, expected in step['instruction_hashes'].items():
        if exchange.fingerprint(supervisor.ROOT / name) != expected:
            raise ValueError('Original author instructions changed')
    assignment = step['assignment']
    if state['round']:
        suffix = f'.revision-{state["round"] + 1}'
        assignment['request_id'] += suffix
        result = Path(assignment['result_path'])
        assignment['result_path'] = str(result.with_name(result.stem + suffix + result.suffix))
        feedback = state['feedback'][-1]
        if exchange.fingerprint(feedback['path']) != feedback['sha256']:
            raise ValueError('Correction feedback changed')
        assignment['inputs'] += [feedback['path']]
        assignment['correction_feedback'] = feedback['path']
        assignment['goal'] += '\nИсправь подтверждённые замечания из feedback в пределах исходных критериев и полномочий. Не ослабляй ожидание; неизвестное сохрани блокером.'
    return plan


def review_plan(state, slot, author_request, base, discussion=None):
    author = exchange.read_state(state['plan']['author_plan']['task_state'])['requests'][author_request]
    result = exchange.inspect_result(author['assignment'])
    message, _ = parse_message(Path(result['path']).read_text(encoding='utf-8'))
    ids = author['assignment']['criterion_ids'] or [f'AC-{i+1}' for i in range(len(author['assignment']['acceptance_criteria']))]
    target = dict(request_id=author_request, result_path=result['path'],
                  artifact_hashes=result['artifact_hashes'], scope=slot['scope'])
    assignment = dict(task_id=state['task_id'], request_id=author_request + '.review-' + slot['scope'],
        agent=slot['agent'], stage=state['plan']['author_plan']['steps'][0]['assignment']['stage'] + '.review-' + slot['scope'],
        goal='Независимо проверь назначенное направление по источникам, критериям и первичным доказательствам; не исправляй автора.',
        acceptance_criteria=author['assignment']['acceptance_criteria'], criterion_ids=author['assignment']['criterion_ids'],
        scenario_ids=author['assignment']['scenario_ids'], stop_condition='Вернуть связанное ревью либо честный blocked',
        expected_artifact='QA JSON content review', result_path=str(base / ('review-' + slot['scope'] + '.json')),
        inputs=list(result['artifact_hashes']), depends_on=[], review_target=target, review_criteria_ids=ids,
        author_revision=message['payload'].get('current_revision') or message['payload'].get('plan_revision'))
    if slot.get('focus'):
        assignment['goal'] += '\nФокус направления: ' + slot['focus']
    assignment['blind_review_exclusions'] = [f['path'] for f in state['feedback'] if f['path'] in assignment['inputs']]
    if slot['agent'] == 'qa-conclusion-reviewer':
        for path, digest in slot['source_hashes'].items():
            if exchange.fingerprint(path) != digest:
                raise ValueError('Conclusion primary sources changed since cycle initialization')
        assignment['review_sources'] = slot['review_sources']
        assignment['review_source_hashes'] = slot['source_hashes']
        assignment['inputs'] = list(dict.fromkeys(assignment['inputs'] + slot['review_sources']))
        registry = exchange.read_state(state['plan']['author_plan']['task_state'])
        assignment['conclusion_paths'] = {}
        for cid in ids:
            slots = registry.get('criteria', {}).get(cid, {}).get('definition', {}).get('required_reviews', [])
            expected = next((s['conclusions'] for s in slots if s['agent'] == slot['agent'] and s['scope'] == slot['scope']), ['/summary'])
            assignment['conclusion_paths'][cid] = expected
    if discussion:
        packet = exchange.read_json(discussion['packet_path'])
        assignment['review_discussion'] = discussion
        suffix = f'.discussion-{discussion["round"]}'
        assignment['request_id'] += suffix
        assignment['result_path'] = str(base / (f'discussion-{discussion["round"]}-' + slot['scope'] + '.json'))
        assignment['inputs'] = list(dict.fromkeys(assignment['inputs'] + [discussion['packet_path']]
            + [p for review in packet['reviews'] for p in review['artifact_hashes']]))
        assignment['goal'] += '\nРазбери только назначенные расхождения по docs/REVIEW_DISCUSSION.md. Сохрани discussion_checks; чужой verdict не является первичным доказательством.'
    source = state['plan']['author_plan']
    instructions = ['docs/CONTENT_REVIEW.md', '.gigacode/skills/qa-check-quality/SKILL.md']
    if message['payload'].get('test_cases_artifact'):
        instructions += ['docs/TEST_CASE_REVIEW.md','docs/TEST_CASE_STYLE.md',
                         'docs/TEST_CASE_SELECTION.md','.gigacode/skills/qa-test-cases/SKILL.md']
        if author['assignment'].get('test_type') is not None:
            assignment['test_type'] = author['assignment']['test_type']
        instructions += [p for p in source['steps'][0].get('instruction_paths', [])
                         if p.startswith('examples/test-cases/')]
    if author['assignment'].get('plan_partition'):
        assignment['plan_partition'] = copy.deepcopy(author['assignment']['plan_partition'])
        instructions.append('docs/TEST_PLAN_PARTITION.md')
    if discussion:
        instructions.append('docs/REVIEW_DISCUSSION.md')
    if message['payload'].get('combination_coverage'):
        instructions.append('docs/TEST_DESIGN.md')
    step = dict(step_id='review-' + slot['scope'], assignment=assignment, max_wall_seconds=slot['max_wall_seconds'],
                instruction_paths=instructions)
    for key in ('context_mode', 'max_context_characters', 'test_data_sections'):
        if key in slot:
            step[key] = copy.deepcopy(slot[key])
    step['instruction_paths'] += slot.get('instruction_paths', [])
    if slot.get('gateway_policy'):
        step['gateway_policy'] = copy.deepcopy(slot['gateway_policy'])
    return dict(schema_version=1, task_state=source['task_state'], workspace=source['workspace'],
        adapter=slot['adapter'], scope_kind=source['scope_kind'], failure_journal=source['failure_journal'],
        max_active_seconds=state['plan']['max_active_seconds'],
        steps=[step])


def ensure_child(path, state, kind, slot=None, author_request=None, discussion=None):
    frozen = state['plan']['author_plan']['steps'][0].get('context_builder_sha256')
    if frozen and exchange.fingerprint(supervisor.ROOT / 'scripts/context_builder.py') != frozen:
        raise ValueError('Context builder changed; create a new review cycle plan')
    scope = slot['scope'] if slot else None
    for row in state['children']:
        if (row['round'] == state['round'] and row['kind'] == kind and row['scope'] == scope
                and row.get('discussion_round') == (discussion['round'] if discussion else None)):
            if not Path(row['path']).exists():
                supervisor.initialize(row['path'], row['plan'], Path('/'))
            return row
    base = Path(path).resolve().parent / (Path(path).name + '.rounds') / f'round-{state["round"]}'
    base.mkdir(parents=True, exist_ok=True)
    plan = author_plan(state) if kind == 'author' else review_plan(state, slot, author_request, base, discussion)
    remaining = state['plan']['max_active_seconds'] - active_seconds(state)
    if remaining < 1:
        raise ValueError('Review cycle active budget exhausted')
    plan['max_active_seconds'] = min(plan.get('max_active_seconds', remaining), remaining)
    for step in plan['steps']:
        step['max_wall_seconds'] = min(step['max_wall_seconds'], remaining)
    name = 'author' if kind == 'author' else 'review-' + scope
    if discussion:
        name = f'discussion-{discussion["round"]}-' + scope
        plan['steps'][0]['step_id'] = name
    row = dict(round=state['round'], kind=kind, scope=scope, path=str(base / (name + '.execution.json')), plan=plan)
    if discussion:
        row['discussion_round'] = discussion['round']
    # Persist intent before initialization so an interrupted tick reuses its child.
    update(path, lambda value: value['children'].append(row))
    supervisor.initialize(row['path'], plan, Path('/'))
    return row


def reported(child):
    state = supervisor.read(child['path'])
    step = next(iter(state['steps'].values()))
    if not step['attempts']:
        return None
    rid = step['attempts'][-1]['request_id']
    registry = exchange.read_state(state['plan']['task_state'])
    row = registry['requests'].get(rid)
    return (rid, row) if row and row['status'] in ('reported', 'accepted') else None


def block(path, reason):
    def apply(value):
        value.update(status='blocked', reason=reason)
    update(path, apply)
    return read(path)


def finish_correction(path, state):
    """Replay a persisted decision after a crash on either side of registry write."""
    intent = state['correction_intent']
    registry_path = state['plan']['author_plan']['task_state']
    row = exchange.read_state(registry_path)['requests'][intent['request_id']]
    if exchange.inspect_result(row['assignment'])['artifact_hashes'] != row['result']['artifact_hashes']:
        raise ValueError('Correction target changed')
    if row['status'] == 'reported':
        exchange.review_result(registry_path, intent['request_id'], 'Independent blocking findings require correction', False, None)
    elif row['status'] != 'changes_requested':
        raise ValueError('Author was accepted outside the pending review policy')
    if intent['stop']:
        return block(path, 'Correction budget exhausted or identical findings without new evidence')
    if exchange.fingerprint(intent['feedback']['path']) != intent['feedback']['sha256']:
        raise ValueError('Correction feedback changed')
    def correction(value):
        value['feedback'].append(intent['feedback'])
        value['rejection_signatures'].append(intent['signature'])
        value['round'] += 1
        value['status'] = 'correcting'
        value.pop('correction_intent', None)
    update(path, correction)
    return read(path)


def finish_cancel(path):
    for child in read(path)['children']:
        if Path(child['path']).exists():
            state = supervisor.read(child['path'])
            if not state['cancel_requested'] or state['status'] != 'cancelled':
                supervisor.cancel(child['path'], read(path)['reason'])
    return read(path)


def finish_resume(path):
    state = read(path)
    for child in state['children']:
        if child['round'] == state['round'] and Path(child['path']).exists():
            progress = supervisor.read(child['path'])
            intent = state.get('correction_intent')
            if intent and child['kind'] == 'author':
                registry = exchange.read_state(state['plan']['author_plan']['task_state'])
                if registry['requests'][intent['request_id']]['status'] == 'changes_requested':
                    # This attempt is terminal. Complete its saved decision, then
                    # launch the authorized next round; do not replay rejected work.
                    continue
            if progress['cancel_requested']:
                supervisor.resume(child['path'], state['resume_intent'])
    def apply(value):
        value.update(cancel_requested=False, status='pending', reason=value.pop('resume_intent'))
    update(path, apply)
    return read(path)


def tick(path):
    with supervisor.controller(path):
        state = read(path)
        if state.get('resume_intent'):
            return finish_resume(path)
        if state['cancel_requested']:
            return finish_cancel(path)
        if state['status'] in ('blocked', 'accepted'):
            return state
        if active_seconds(state) >= state['plan']['max_active_seconds']:
            for child in state['children']:
                if Path(child['path']).exists():
                    supervisor.cancel(child['path'], 'Review cycle active budget exhausted')
            return block(path, 'Review cycle active budget exhausted')
        try:
            if state.get('correction_intent'):
                return finish_correction(path, state)
            author = ensure_child(path, state, 'author')
            progress = supervisor.tick(author['path'])
            if progress['status'] in ('blocked', 'needs_reconciliation'):
                return block(path, 'Author requires coordinator: ' + progress['status'])
            record = reported(author)
            if not record:
                return read(path)
            rid, row = record
            current = exchange.inspect_result(row['assignment'])
            if current['artifact_hashes'] != row['result']['artifact_hashes']:
                return block(path, 'Author target changed after submit')
            message, _ = parse_message(Path(current['path']).read_text(encoding='utf-8'))
            ready = exchange.readiness(message)['complete'] or message['event'] == 'ready_for_review'
            if not ready:
                return block(path, 'Author has unfinished conditions: ' + message['event'])
            reviews = []
            for slot in state['plan']['reviewers']:
                state = read(path)
                child = ensure_child(path, state, 'review', slot, rid)
                progress = supervisor.tick(child['path'])
                if progress['status'] in ('blocked', 'needs_reconciliation'):
                    return block(path, 'Reviewer requires coordinator: ' + slot['scope'])
                review_record = reported(child)
                if not review_record:
                    return read(path)
                review_id, review_row = review_record
                review_current = exchange.inspect_result(review_row['assignment'])
                if review_current['artifact_hashes'] != review_row['result']['artifact_hashes']:
                    return block(path, 'Review changed after submit')
                if not review_row['owner_task_id'] or review_row['owner_task_id'] == row['owner_task_id']:
                    return block(path, 'Reviewer session is not independent')
                review, _ = parse_message(Path(review_current['path']).read_text(encoding='utf-8'))
                if review_row['status'] == 'reported':
                    exchange.review_result(state['plan']['author_plan']['task_state'], review_id,
                        'Bound independent review recorded; the verdict is preserved', True, None)
                if review['event'] == 'blocked' and not state['plan'].get('discussion'):
                    return block(path, 'Review has unknown expectation or unavailable evidence: ' + slot['scope'])
                reviews.append((review_id, review, review_row['owner_task_id']))
            if len({owner for _, _, owner in reviews}) != len(reviews):
                return block(path, 'Review directions require distinct sessions')
            originals = reviews
            if state['plan'].get('discussion'):
                from review_discussion import advance
                reviews = advance(path, rid, originals)
                if reviews is None:
                    return read(path)
            if any(r[1]['event'] == 'blocked' for r in reviews):
                return block(path, 'Review has unknown expectation or unavailable evidence after discussion')
            rejections = [review for _, review, _ in reviews if review['event'] == 'changes_requested']
            if not rejections:
                # Discussion clarifies a false alarm but cannot silently erase
                # a blocking verdict on the same author bytes.
                rejections = [review for _, review, _ in originals if review['event'] == 'changes_requested']
            registry_path = state['plan']['author_plan']['task_state']
            if rejections:
                signature = digest(sorted((f['criterion_id'], f['problem'], f['evidence'])
                    for r in rejections for f in r['payload']['findings'] if f['blocking']))
                if row['status'] != 'reported':
                    return block(path, 'Author was accepted outside the pending review policy')
                feedback = Path(author['path']).parent / 'feedback.json'
                exchange.atomic_write(feedback, dict(schema_version=1, target_request_id=rid,
                    target_sha256=current['artifact_hashes'][current['path']],
                    reviews=[dict(request_id=review_id, payload=review['payload']) for review_id, review, _ in reviews],
                    original_reviews=[dict(request_id=review_id, payload=review['payload']) for review_id, review, _ in originals],
                    instruction='Correct findings within the original assignment; do not expand scope or invent expectations.'))
                intent = dict(request_id=rid, signature=signature,
                    stop=state['round'] >= state['plan']['max_corrections'] or signature in state['rejection_signatures'],
                    feedback=dict(path=str(feedback), sha256=exchange.fingerprint(feedback)))
                update(path, lambda value: value.update(correction_intent=intent))
                return finish_correction(path, read(path))
            # All declared directions passed. Re-check the exact author bytes
            # immediately before acceptance; stale review cannot bless new code.
            current = exchange.inspect_result(row['assignment'])
            if current['artifact_hashes'] != row['result']['artifact_hashes']:
                return block(path, 'Target changed before acceptance')
            registry = exchange.read_state(registry_path)
            for cid in row['assignment']['criterion_ids']:
                definition = registry['criteria'][cid]['definition']
                if definition.get('kind') == 'combination_coverage':
                    from combination_coverage import check_criterion
                    from content_review import check_required_reviews
                    selected = {rid: current, **{review_id: registry['requests'][review_id]['result']
                                                for review_id, _, _ in reviews}}
                    bundles = check_criterion(definition, registry, selected)
                    check_required_reviews(definition, registry, selected, set(bundles), bundles)
                elif definition.get('required_reviews'):
                    from content_review import check_required_reviews
                    selected = {rid: current, **{review_id: registry['requests'][review_id]['result']
                                                for review_id, _, _ in reviews}}
                    check_required_reviews(definition, registry, selected, {rid})
            if row['status'] == 'reported':
                exchange.review_result(registry_path, rid, 'All declared independent review directions passed', True, None)
            def accept(value):
                value.update(status='accepted', result_request_id=rid, review_request_ids=[r[0] for r in reviews])
            update(path, accept)
            return read(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return block(path, str(exc))


def cancel(path, reason):
    with supervisor.controller(path):
        def request(value):
            value.pop('resume_intent', None)  # A newer explicit stop fences an interrupted resume.
            value.update(cancel_requested=True, status='cancelled', reason=exchange.required(reason, 'reason'))
        update(path, request)
        return finish_cancel(path)


def resume(path, decision):
    with supervisor.controller(path):
        state = read(path)
        if not state['cancel_requested']:
            raise ValueError('Only explicit cancellation can be resumed; blocked requires new evidence/plan')
        exchange.required(decision, 'resume decision')
        if active_seconds(state) >= state['plan']['max_active_seconds']:
            raise ValueError('Review budget exhausted')
        update(path, lambda value: value.update(resume_intent=decision))
        return finish_resume(path)
