#!/usr/bin/env python3
"""Trusted synthetic adapter for a contradicted product conclusion, not model evaluation."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from self_review_fixtures import review_for
from environment_fixtures import attach_environment_checks
from action_gateway import pointer

parser = argparse.ArgumentParser()
parser.add_argument('--mode', default='correct-once', choices=['correct-once', 'generic', 'unknown', 'always-wrong'])
mode = parser.parse_args().mode
assignment = json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())['assignment']
message = dict(task_id=assignment['task_id'], request_id=assignment['request_id'], agent=assignment['agent'],
               artifacts=[], blockers=[], self_review=review_for())
if not assignment.get('review_target'):
    corrected = '.revision-' in assignment['request_id'] and mode != 'always-wrong'
    summary = ('Проверена только доступность чтения учебного стенда; продуктовый запуск не выполнялся.'
               if corrected else 'Продуктовая интеграция успешно проверена.')
    message.update(event='environment_ready', summary=summary, payload=dict(environment_id='fixture', product_version='fixture-v1',
        verified_capabilities=['Synthetic read capability recorded in primary facts'], limitations=['Synthetic records only']))
else:
    target = assignment['review_target']
    author = json.loads(Path(target['result_path']).read_text())
    source = assignment['review_sources'][0]
    facts = json.loads(Path(source).read_text())
    passed = facts['product_run'] == 'not_run' and 'не выполнялся' in author['summary']
    status = 'unknown' if mode == 'unknown' else 'passed' if passed else 'failed'
    event = 'blocked' if mode == 'unknown' else 'review_passed' if passed or mode == 'generic' else 'changes_requested'
    findings = [] if event != 'changes_requested' else [dict(finding_id='F-PRODUCT',criterion_id=assignment['review_criteria_ids'][0],
        blocking=True, problem='Product success is contradicted by product_run=not_run', evidence=source,
        resolution_condition='Preserve verified read capability and state that the product run was not performed')]
    checks = [dict(criterion_id=cid, status='passed' if mode=='generic' else status,
                   note='Synthetic primary records checked', evidence=[source]) for cid in assignment['review_criteria_ids']]
    conclusion_checks = [dict(criterion_id=cid, pointer=p, conclusion=pointer(author,p), status=status,
        note='Product not_run must not be presented as tested integration; synthetic record only',
        source_refs=[] if mode=='unknown' else [dict(path=source, quote='"product_run": "not_run"')])
        for cid, paths in assignment['conclusion_paths'].items() for p in paths]
    message.update(event=event, summary='Synthetic independent conclusion review',
        blockers=['Primary product observation unavailable'] if mode=='unknown' else [],
        payload=dict(target_request_id=target['request_id'], target_sha256=target['artifact_hashes'][target['result_path']],
                     review_scope=target['scope'], reviewed_revision=target['request_id'], checks=checks,
                     findings=findings, limitations=['Synthetic adapter, no CLI or live product measurement'],
                     conclusion_checks=[] if mode=='generic' else conclusion_checks))
if not assignment.get('review_target'):
    attach_environment_checks(message, assignment, [assignment['inputs'][0]])
print(json.dumps(message,ensure_ascii=False))
