"""Synthetic design adapter for a real bounded author/reviewer process cycle."""
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
from combination_coverage import binding_keys, load_bundles
from self_review_fixtures import review_for

handoff = json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())
assignment = handoff['assignment']
directory = Path(os.environ['QA_EXECUTION_HANDOFF']).parent
message = dict(task_id=assignment['task_id'], request_id=assignment['request_id'], agent=assignment['agent'],
    summary='Synthetic design adapter; no product or model behavior measured', artifacts=[], blockers=[], self_review=review_for())
if 'review_target' not in assignment:
    for name in ('combinations.json', 'ready-cases.json', 'ready-combinations.json', 'requirements.md'):
        shutil.copy(ROOT/'examples/test-design'/name, directory/name)
    message.update(event='plan_ready', payload=dict(plan_artifact=str(directory/'requirements.md'), plan_revision='v1',
        test_cases_artifact=str(directory/'ready-cases.json'), requirement_coverage=[dict(requirement_id='R-1',
            source=str(directory/'requirements.md'), scenario_ids=['TC-ACCESS'], priority='high', status='ready',
            verification='Synthetic UI/API design', evidence='Synthetic design observation')], open_questions=[],
        combination_coverage=[dict(bundle_id='access', model_artifact=str(directory/'combinations.json'),
            combinations_artifact=str(directory/'ready-combinations.json'), catalog_artifact=str(directory/'ready-cases.json'))]))
else:
    target = assignment['review_target']
    author = json.loads(Path(target['result_path']).read_text())
    proof = directory/'combination-notes.md'
    proof.write_text('Synthetic per-binding condition/rule/observation notes; no independent model judgment')
    combinations = [dict(bundle_id=bid, binding_id=key, status='passed', note='Synthetic binding review', evidence=[str(proof)])
        for bundle in load_bundles(author['payload'], Path(target['result_path']).parent) for bid, key in sorted(binding_keys(bundle))]
    if '--generic-review' in sys.argv:
        combinations = []
    message.update(event='review_passed', payload=dict(target_request_id=target['request_id'],
        target_sha256=target['artifact_hashes'][target['result_path']], review_scope=target['scope'], reviewed_revision='v1',
        checks=[dict(criterion_id=cid, status='passed', note='Synthetic design check', evidence=[str(proof)])
            for cid in assignment['review_criteria_ids']], combination_checks=combinations, findings=[], limitations=['Synthetic protocol only']))
print(json.dumps(message))
