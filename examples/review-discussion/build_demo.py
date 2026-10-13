#!/usr/bin/env python3
"""Build a two-reviewer synthetic disagreement and evidence-bound correction."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import task_exchange as exchange


def build(output, mode='resolve'):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    facts = output / 'primary-facts.json'
    exchange.atomic_write(facts, dict(environment='fixture', read_probe='passed', product_run='not_run'))
    scopes = ('facts', 'inference')
    criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-CONCLUSION', description='Conclusion supported by primary observations',
        source=str(facts), required=True, required_reviews=[dict(scope=s, agent='qa-conclusion-reviewer', conclusions=['/summary']) for s in scopes])])
    state = output / 'task.json'
    exchange.init_task(state, 'QA-DISCUSSION-DEMO', 'Synthetic bounded independent review discussion', criteria)
    adapter = dict(kind='command', argv=[sys.executable, str(ROOT / 'tests/fixtures/discussion_agent.py'), '--mode', mode])
    assignment = dict(task_id='QA-DISCUSSION-DEMO',request_id='author',agent='qa-test-env-configurator',stage='environment',
        goal='Report synthetic read probe and product limitations',acceptance_criteria=['Do not claim an unperformed product run'],
        criterion_ids=['C-CONCLUSION'],stop_condition='Return QA JSON',expected_artifact='QA JSON',result_path=str(output/'author.json'),
        inputs=[str(facts)],depends_on=[])
    plan = dict(schema_version=1,discussion=dict(max_rounds=2),max_corrections=1,max_active_seconds=45,
        author_plan=dict(schema_version=1,task_state=str(state),workspace=str(output),scope_kind='synthetic',adapter=adapter,
                         steps=[dict(step_id='author',assignment=assignment,max_wall_seconds=5)]),
        reviewers=[dict(scope=s,agent='qa-conclusion-reviewer',review_sources=[str(facts)],adapter=adapter,max_wall_seconds=5,
                        focus='Facts, numbers and actual executions' if s=='facts' else 'Inference, counterexamples and scope') for s in scopes])
    exchange.atomic_write(output/'plan.json',plan)
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    build(args.output_dir)
    print(args.output_dir.resolve()/'plan.json')


if __name__=='__main__':
    main()
