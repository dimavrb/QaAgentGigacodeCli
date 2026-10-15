"""Synthetic review for protocol tests; no model or product checks are claimed."""

def review_for(*, limited=False):
    return dict(schema_version=1, checks=[
        dict(id=cid, status='limited' if limited else 'passed',
             note='Синтетический результат проверяется только как протокол.',
             evidence=['assignment:/acceptance_criteria'] if cid == 'scope' else ['report:/payload'])
        for cid in ('scope', 'evidence', 'consistency', 'role')
    ], fixes=[], remaining_risks=['Учебное недостающее условие'] if limited else [])


def publish_content_review(state_path, author_id, rid='blocking-review', event='changes_requested'):
    """Publish an accepted, exact-target synthetic review through the public API."""
    import json
    from pathlib import Path
    import task_exchange as exchange
    state = exchange.read_state(state_path)
    author = state['requests'][author_id]
    target = author['result']
    criteria = author['assignment']['criterion_ids'] or ['C-AUDIT']
    result_path = Path(state_path).parent / (rid + '.json')
    assignment = dict(task_id=state['task_id'], request_id=rid, agent='qa-result-reviewer',
        stage='content-review', goal='Check synthetic author evidence',
        acceptance_criteria=['Check the assigned target'], stop_condition='Return bound review',
        expected_artifact='QA JSON', result_path=str(result_path),
        inputs=list(target['artifact_hashes']), criterion_ids=author['assignment']['criterion_ids'],
        review_criteria_ids=criteria, review_target=dict(request_id=author_id, result_path=target['path'],
            artifact_hashes=target['artifact_hashes'], scope='oracle'))
    exchange.assign(state_path, assignment, result_path.parent, None)
    handoff = exchange.prepare_handoff(state_path, rid, None)['result']
    exchange.start_request(state_path, rid, 'reviewer-' + rid, handoff['delivery_id'], None)
    message = dict(task_id=state['task_id'], request_id=rid, agent='qa-result-reviewer',
        event=event, summary='Synthetic bound review', artifacts=[], blockers=[], self_review=review_for(),
        payload=dict(target_request_id=author_id, target_sha256=target['artifact_hashes'][target['path']],
            reviewed_revision=target.get('current_revision') or 'synthetic-revision', review_scope='oracle',
            checks=[dict(criterion_id=cid, status='failed' if event == 'changes_requested' else 'passed',
                note='Synthetic evidence assessment', evidence=[target['path']]) for cid in criteria],
            findings=[dict(finding_id='F-1', criterion_id=criteria[0], blocking=True,
                problem='Synthetic evidence does not establish readiness', evidence=target['path'],
                resolution_condition='New author revision with sufficient evidence')]
                if event == 'changes_requested' else [], limitations=[]))
    result_path.write_text(json.dumps(message))
    exchange.submit_result(state_path, rid, None)
    exchange.review_result(state_path, rid, 'Synthetic review evidence checked', True, None)
