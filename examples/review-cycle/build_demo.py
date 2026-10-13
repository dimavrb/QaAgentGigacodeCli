#!/usr/bin/env python3
"""Create an isolated synthetic review-cycle plan; no model or product calls."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import task_exchange as exchange


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    registry = output / 'task.json'
    criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-FILTER', description='The test rejects a foreign paid row',
        source='Synthetic fixture requirement: only own paid order 11', required=True,
        required_reviews=[dict(scope='oracle',agent='qa-result-reviewer'),dict(scope='execution',agent='qa-autotest-reviewer')])])
    exchange.init_task(registry,'QA-REVIEW-DEMO','Synthetic bounded correction',criteria)
    adapter = dict(kind='command',argv=[sys.executable,str(ROOT/'tests/fixtures/review_agent.py')])
    assignment = dict(task_id='QA-REVIEW-DEMO',request_id='author',agent='qa-test-automation',stage='automation',
        goal='Write a test for the synthetic own paid row',acceptance_criteria=['Return order 11; reject foreign rows'],
        criterion_ids=['C-FILTER'],scenario_ids=['TC-1'],stop_condition='Return report',expected_artifact='QA JSON',
        result_path=str(output/'author.json'),inputs=[],depends_on=[])
    plan = dict(schema_version=1,max_corrections=2,max_active_seconds=60,
        author_plan=dict(schema_version=1,task_state=str(registry),workspace=str(output),scope_kind='synthetic',adapter=adapter,
            steps=[dict(step_id='author',assignment=assignment,max_wall_seconds=5)]),
        reviewers=[dict(scope='oracle',agent='qa-result-reviewer',adapter=adapter,max_wall_seconds=5),
                   dict(scope='execution',agent='qa-autotest-reviewer',adapter=adapter,max_wall_seconds=5)])
    exchange.atomic_write(output/'plan.json',plan)
    print(output/'plan.json')


if __name__=='__main__':main()
