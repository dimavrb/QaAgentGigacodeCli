#!/usr/bin/env python3
"""Synthetic peer review exchange; tests protocol, not model reasoning."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from self_review_fixtures import review_for
from conclusion_review import pointer
from qa_contract import parse_message

parser=argparse.ArgumentParser()
parser.add_argument('--mode',default='resolve',choices=['resolve','unresolved','round-two','no-evidence','peer-evidence','false-alarm','agree','unknown','hold','receipt'])
mode=parser.parse_args().mode
assignment=json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())['assignment']
message=dict(task_id=assignment['task_id'],request_id=assignment['request_id'],agent=assignment['agent'],artifacts=[],blockers=[],self_review=review_for())
if not assignment.get('review_target'):
    corrected='.revision-' in assignment['request_id'] or mode in ('false-alarm','agree','unknown')
    summary='Продуктовый запуск не выполнялся.' if corrected else 'Продуктовая интеграция успешно проверена.'
    message.update(event='environment_ready',summary=summary,payload=dict(environment_id='fixture',product_version='fixture-v1',
        verified_capabilities=['Synthetic read probe recorded'],limitations=['Synthetic only']))
else:
    target=assignment['review_target']
    author,_=parse_message(Path(target['result_path']).read_text())
    source=assignment['review_sources'][0]
    correct='не выполнялся' in author['summary']
    scope=target['scope']
    discussion=assignment.get('review_discussion')
    status='passed' if correct else 'failed'
    if not discussion:
        # Controlled initial misjudgment: one reviewer overlooks the source.
        if mode=='false-alarm' and scope=='inference' and '.revision-' not in target['request_id']:
            status='failed'
        elif mode=='unknown':
            status='unknown' if scope=='inference' else 'passed'
        elif not correct and scope=='facts':
            status='passed'
    elif mode=='hold':
        time.sleep(30)
    if discussion and mode in ('unresolved','round-two') and (mode=='unresolved' or discussion['round']==1):
        status='passed' if scope=='facts' else 'failed'
    event='blocked' if status=='unknown' else 'review_passed' if status=='passed' else 'changes_requested'
    findings=[] if status!='failed' else [dict(finding_id='F-PRODUCT',criterion_id='C-CONCLUSION',blocking=True,
        problem='Product conclusion differs from recorded not_run',evidence=source,resolution_condition='Preserve not_run and verified read scope')]
    payload=dict(target_request_id=target['request_id'],target_sha256=target['artifact_hashes'][target['result_path']],
        review_scope=scope,reviewed_revision=target['request_id'],checks=[dict(criterion_id=cid,status=status,note='Synthetic primary fact assessment',evidence=[source])
        for cid in assignment['review_criteria_ids']],findings=findings,limitations=['Trusted fixture; no model/product evaluation'],
        conclusion_checks=[dict(criterion_id=cid,pointer=p,conclusion=pointer(author,p),status=status,
            note='Synthetic assessment of product not_run',source_refs=[] if status=='unknown' else [dict(path=source,quote='"product_run": "not_run"')])
            for cid,paths in assignment['conclusion_paths'].items() for p in paths])
    if discussion:
        packet=json.loads(Path(discussion['packet_path']).read_text())
        prior=next(r for r in packet['reviews'] if r['request_id']==discussion['prior_request_id'])
        prior_message,_=parse_message(Path(prior['result_path']).read_text())
        own={(c['criterion_id'],c['pointer']):c['status'] for c in prior_message['payload']['conclusion_checks']}
        own.update({(c['criterion_id'],''):c['status'] for c in prior_message['payload']['checks']})
        replies=[]
        for issue in packet['issues']:
            changed=own[issue['criterion_id'],issue['pointer']]!=status
            refs=[dict(path=source,quote='"product_run": "not_run"')]
            if mode=='no-evidence':refs=[]
            if mode=='peer-evidence':refs=[dict(path=prior['result_path'],quote=prior_message['summary'])]
            unresolved=mode=='unresolved' or mode=='round-two' and discussion['round']==1
            replies.append(dict(issue_id=issue['issue_id'],position='revise' if changed else 'maintain',status='unresolved' if unresolved else 'resolved',
                note='Primary not_run resolves product scope; peer opinion alone is insufficient',source_refs=refs))
        payload['discussion_checks']=replies
        if mode=='receipt':
            from action_gateway import MCPClient
            directory=Path(os.environ['QA_EXECUTION_HANDOFF']).parent
            client=MCPClient([sys.executable,str(ROOT/'scripts/mcp_action_gateway.py'),'--policy',str(directory/'policy.json'),
                '--journal',os.environ['QA_EXECUTION_ACTIONS'],'--task-id',assignment['task_id'],'--step-id',os.environ['QA_EXECUTION_STEP'],
                '--execution-state',os.environ['QA_EXECUTION_STATE'],'--attempt-token',os.environ['QA_EXECUTION_ATTEMPT']],timeout=4)
            try:
                client.request('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo={'name':'discussion-fixture','version':'1'}))
                client.notify('notifications/initialized')
                result=client.request('tools/call',dict(name='find_ticket',arguments=dict(project='SYNTHETIC',client_id='discussion')))
                for reply in replies:reply.update(source_refs=[],receipt=result['_meta']['qa_review_execution_receipt']['path'])
            finally:client.close()
        # A reviewer can preserve its own passing conclusion while a dispute
        # remains open; the overall report must honestly remain blocked.
        if any(r['status']=='unresolved' for r in replies) and event=='review_passed':
            event='blocked'
    message.update(event=event,summary='Synthetic '+scope+' assessment',payload=payload,
        blockers=['Disputed observation remains unresolved'] if event=='blocked' else [])
print(json.dumps(message,ensure_ascii=False))
