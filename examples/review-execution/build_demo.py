#!/usr/bin/env python3
"""Prepare a synthetic runtime review cycle; run it via manage_review_cycle."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import task_exchange as exchange


def build(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    execution = [dict(check_id=mode, tool='run_review_test', run_started_pointer='/structuredContent/started_epoch',
        expected_arguments={'/project': 'SYNTHETIC', '/mode': mode},
        expected_result={'/structuredContent/exit_code': 1 if mode == 'counterexample' else 0,
                         '/structuredContent/assertion_failed': mode == 'counterexample'})
        for mode in ('normal', 'counterexample', 'restored')]
    criterion = dict(criterion_id='C-ASSERT', description='Foreign paid row fails the composition assert; normal behavior is restored',
        source='Synthetic prepared own paid order 11 and foreign paid order 12', required=True,
        required_reviews=[dict(scope='runtime', agent='qa-autotest-reviewer', execution=execution)])
    registry = directory/'task.json'
    exchange.init_task(registry, 'QA-RUNTIME-DEMO', 'Synthetic independent execution review',
        dict(schema_version=1, criteria=[criterion]))
    assignment = dict(task_id='QA-RUNTIME-DEMO', request_id='author', agent='qa-test-automation', stage='automation',
        goal='Detect foreign rows in the prepared synthetic response', criterion_ids=['C-ASSERT'], scenario_ids=['TC-1'],
        acceptance_criteria=['Exact own paid order composition, controlled assertion failure and restored normal state'],
        stop_condition='Return QA JSON', expected_artifact='QA JSON', result_path=str(directory/'author.json'), inputs=[], depends_on=[])
    policy = dict(schema_version=1, max_review_calls=3, call_timeout_seconds=5,
        server_command=[sys.executable, str(ROOT/'tests/fixtures/review_probe_server.py')],
        tools={'run_review_test': dict(effect='read', constraints={'/project': 'SYNTHETIC'}, allowed_arguments=['project', 'mode'])})
    plan = dict(schema_version=1, max_corrections=2, max_active_seconds=60,
        author_plan=dict(schema_version=1, task_state=str(registry), workspace=str(directory), scope_kind='synthetic',
            adapter=dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/review_agent.py'), '--mode', 'correct-once']),
            steps=[dict(step_id='author', assignment=assignment, max_wall_seconds=10)]),
        reviewers=[dict(scope='runtime', agent='qa-autotest-reviewer', max_wall_seconds=15, gateway_policy=policy,
            adapter=dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/runtime_reviewer.py')]))])
    (directory/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    return directory/'plan.json'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output_dir))
