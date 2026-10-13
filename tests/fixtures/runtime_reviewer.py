"""Real gateway calls in a trusted synthetic reviewer adapter; not a CLI eval."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
from action_gateway import MCPClient, pointer
from review_execution import equal
from self_review_fixtures import review_for

handoff_path = Path(os.environ['QA_EXECUTION_HANDOFF'])
handoff = json.loads(handoff_path.read_text())
assignment = handoff['assignment']
target = assignment['review_target']
author = json.loads(Path(target['result_path']).read_text())
executions, checks, findings, blockers = [], [], [], []
client = MCPClient([sys.executable, str(ROOT/'scripts/mcp_action_gateway.py'),
    '--policy', str(handoff_path.parent/'policy.json'), '--journal', os.environ['QA_EXECUTION_ACTIONS'],
    '--task-id', assignment['task_id'], '--step-id', os.environ['QA_EXECUTION_STEP'],
    '--execution-state', os.environ['QA_EXECUTION_STATE'], '--attempt-token', os.environ['QA_EXECUTION_ATTEMPT']], timeout=4)
try:
    client.request('initialize', dict(protocolVersion='2024-11-05', capabilities={}, clientInfo={'name': 'runtime-fixture', 'version': '1'}))
    client.notify('notifications/initialized')
    for cid, definition in handoff['task_criteria'].items():
        failed, unknown = False, False
        required = next(s for s in definition['required_reviews'] if s['scope'] == target['scope'])
        if '--skip-execution' not in sys.argv:
            for spec in required.get('execution', []):
                row = dict(criterion_id=cid, check_id=spec['check_id'], status='unknown', note='Synthetic runner unavailable')
                try:
                    arguments = {k[1:]: v for k, v in spec['expected_arguments'].items()}
                    result = client.request('tools/call', dict(name=spec['tool'], arguments=arguments))
                    meta = result['_meta']['qa_review_execution_receipt']
                    passed = all(equal(pointer(result, key), value) for key, value in spec['expected_result'].items())
                    row.update(status='passed' if passed else 'failed', receipt=meta['path'],
                        note='Actual synthetic exit code and assertion observation ' + ('match' if passed else 'differ from') + ' the frozen criterion')
                    failed |= not passed
                except (OSError, ValueError, KeyError, EOFError, TimeoutError):
                    unknown = True
                    blockers.append('Synthetic required runner call failed or unavailable')
                executions.append(row)
        checks.append(dict(criterion_id=cid, status='unknown' if unknown else 'failed' if failed else 'passed',
            note='Independent synthetic runtime observation', evidence=[target['result_path']]))
        if failed:
            findings.append(dict(finding_id='F-' + cid, criterion_id=cid, blocking=True,
                problem='Required assertion did not detect the controlled foreign row', evidence=executions[-2].get('receipt', target['result_path']),
                resolution_condition='Counterexample must fail on the composition assertion, then normal state must pass'))
finally:
    client.close()
event = 'blocked' if blockers else 'changes_requested' if findings else 'review_passed'
print(json.dumps(dict(task_id=assignment['task_id'], request_id=assignment['request_id'], agent=assignment['agent'],
    event=event, summary='Synthetic managed runtime review', artifacts=[], blockers=blockers, self_review=review_for(limited=bool(blockers)),
    payload=dict(target_request_id=target['request_id'], target_sha256=target['artifact_hashes'][target['result_path']],
        review_scope=target['scope'], reviewed_revision=author['payload']['current_revision'], checks=checks,
        execution_checks=executions, findings=findings, limitations=['Synthetic MCP/backend; no live CLI or real product'],
        **(dict(conclusion_checks=[dict(criterion_id=cid,pointer=p,conclusion=pointer(author,p),status='passed',
            note='Summary names a synthetic local fixture, not live model/product evidence',
            source_refs=[dict(path=assignment['review_sources'][0],quote='Synthetic local review fixture')])
            for cid,paths in assignment['conclusion_paths'].items() for p in paths])
           if assignment['agent']=='qa-conclusion-reviewer' else {})))))
