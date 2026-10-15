#!/usr/bin/env python3
"""Build an isolated synthetic conclusion review cycle; no model or external actions."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import task_exchange as exchange
from environment_fixtures import bind_environment


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    facts = output / 'primary-facts.json'
    exchange.atomic_write(facts, dict(environment='fixture', read_probe='passed', product_run='not_run'))
    criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-CONCLUSION', description='Conclusion matches primary observations',
        source=str(facts), required=True, required_reviews=[dict(scope='conclusions', agent='qa-conclusion-reviewer', conclusions=['/summary'])])])
    state = output / 'task.json'
    exchange.init_task(state, 'QA-CONCLUSION-DEMO', 'Synthetic independent conclusion verification', criteria)
    adapter = dict(kind='command', argv=[sys.executable, str(ROOT / 'tests/fixtures/conclusion_agent.py')])
    assignment = dict(task_id='QA-CONCLUSION-DEMO', request_id='author', agent='qa-test-env-configurator', stage='environment',
        goal='Report verified synthetic read capability and limits without claiming a product run', acceptance_criteria=['Conclusions match provided facts'],
        criterion_ids=['C-CONCLUSION'], stop_condition='Return QA report', expected_artifact='QA JSON',
        result_path=str(output / 'author.json'), inputs=[str(facts)], depends_on=[])
    bind_environment(assignment, output, 'fixture', 'fixture-v1')
    plan = dict(schema_version=1, max_corrections=1, max_active_seconds=45,
        author_plan=dict(schema_version=1, task_state=str(state), workspace=str(output), adapter=adapter, scope_kind='synthetic',
                         steps=[dict(step_id='author', assignment=assignment, max_wall_seconds=5)]),
        reviewers=[dict(scope='conclusions', agent='qa-conclusion-reviewer', review_sources=[str(facts)], adapter=adapter, max_wall_seconds=5)])
    exchange.atomic_write(output / 'plan.json', plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    build(args.output_dir)
    print(args.output_dir.resolve() / 'plan.json')


if __name__ == '__main__':
    main()
