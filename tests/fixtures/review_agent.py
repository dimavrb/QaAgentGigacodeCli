#!/usr/bin/env python3
"""Trusted synthetic author/reviewer adapter; not a model-quality measurement."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from self_review_fixtures import review_for

parser = argparse.ArgumentParser()
parser.add_argument('--mode', default='correct-once')
args = parser.parse_args()
directory = Path(os.environ['QA_EXECUTION_HANDOFF']).parent
assignment = json.loads((directory / 'handoff.json').read_text())['assignment']
if args.mode == 'hold':
    time.sleep(30)
message = dict(task_id=assignment['task_id'], request_id=assignment['request_id'],
               agent=assignment['agent'], summary='Synthetic local review fixture', artifacts=[], blockers=[],
               self_review=review_for())
if 'review_target' not in assignment:
    strong = args.mode == 'strong' or args.mode != 'always-weak' and '.revision-' in assignment['request_id']
    test = directory / 'test_filter.py'
    assertion = "self.assertEqual([11], [row['id'] for row in rows], 'unexpected composition')" if strong else 'self.assertGreater(len(rows), 0)'
    test.write_text('import os, unittest\nclass FilterTest(unittest.TestCase):\n'
        '    def test_composition(self):\n'
        "        rows = [{'id':11, 'owner':'alice', 'status':'paid'}]\n"
        "        if os.environ.get('QA_REVIEW_FAULT'): rows.append({'id':12,'owner':'bob','status':'paid'})\n"
        '        ' + assertion + '\n')
    run = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(directory), '-p', 'test_filter.py'],
                         capture_output=True, text=True, env={**os.environ, 'QA_REVIEW_FAULT': ''})
    proof, diff = directory / 'normal-run.txt', directory / 'changes.diff'
    proof.write_text(run.stdout + run.stderr)
    diff.write_text('Synthetic generated assertion: ' + assertion)
    message.update(event='ready_for_review', artifacts=[str(test), str(proof), str(diff)], payload=dict(
        scenario_id='TC-1', requirement_source='Synthetic requirement: return only prepared own paid order 11',
        base_revision='fixture-base', current_revision=assignment['request_id'], changed_files=[str(test)],
        diff_artifact=str(diff), run_result=f'Local fixture unittest exit={run.returncode}', limitations=['Synthetic service response only']))
else:
    target = assignment['review_target']
    author = json.loads(Path(target['result_path']).read_text())
    test = Path(author['payload']['changed_files'][0])
    run = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(test.parent), '-p', test.name],
        capture_output=True, text=True, env={**os.environ, 'QA_REVIEW_FAULT': 'owner'})
    proof = directory / 'counterexample-run.txt'
    proof.write_text(run.stdout + run.stderr)
    detected = run.returncode == 1 and 'unexpected composition' in run.stderr and 'AssertionError' in run.stderr
    unknown = args.mode == 'unknown'
    passed = detected and args.mode != 'reject'
    event = 'blocked' if unknown else 'review_passed' if passed else 'changes_requested'
    checks = [dict(criterion_id=cid, status='unknown' if unknown else 'passed' if passed else 'failed',
                   note='Synthetic runtime counterexample ' + ('detected' if detected else 'not detected'), evidence=[str(proof)])
              for cid in assignment['review_criteria_ids']]
    findings = [] if passed or unknown else [dict(finding_id='F-1', criterion_id=assignment['review_criteria_ids'][0],
        blocking=True, problem='Foreign paid row can pass the test', evidence='Local controlled foreign-row response passed',
        resolution_condition='The test must fail on the composition assert for the foreign row')]
    # Deliberately use nested checks evidence only: registry must protect it.
    message.update(event=event, artifacts=[], blockers=['Required expectation is unavailable'] if unknown else [],
        payload=dict(target_request_id=target['request_id'], target_sha256=target['artifact_hashes'][target['result_path']],
                     review_scope=target['scope'], reviewed_revision=author['payload']['current_revision'], checks=checks,
                     findings=findings, limitations=['Synthetic response; no real service/CI exercised']))
print(json.dumps(message))
