#!/usr/bin/env python3
"""Create a persistent synthetic file dataset and fresh readiness protocol."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output, *, partial=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    created = datetime.now(timezone.utc)
    source = output / 'requirements.txt'
    source.write_text('SYNTHETIC ONLY. No application or live model was tested.\n'
        'A paid order exists.\nThe unpaid order belongs to the same owner.\n'
        'The synthetic file dataset can be read.\n', encoding='utf-8')
    dataset = [dict(id='own-paid', owner='buyer', status='paid'),
               dict(id='own-unpaid', owner='other' if partial else 'buyer', status='unpaid'),
               dict(id='foreign-paid', owner='other', status='paid')]
    write(output / 'dataset.json', dataset)
    scope = dict(schema_version=1, task_id='QA-DATA-DEMO', environment_id='synthetic-file-store',
        product_version='fixture-1', created_at=created.isoformat(),
        expires_at=(created + timedelta(hours=1)).isoformat(), scenario_ids=['TC-1', 'TC-2'],
        requirements_source=dict(path='requirements.txt', sha256=digest(source)), requirements=[
            dict(requirement_id='D-PAID', kind='property', quote='A paid order exists.',
                 expected_result='own-paid has status paid', scenario_ids=['TC-1']),
            dict(requirement_id='D-OWNER', kind='relation', quote='The unpaid order belongs to the same owner.',
                 expected_result='own-unpaid and own-paid have the same owner', scenario_ids=['TC-2']),
            dict(requirement_id='D-READ', kind='visibility', quote='The synthetic file dataset can be read.',
                 expected_result='Read all three records from the local JSON file')])
    write(output / 'data-assignment.json', scope)
    write(output / 'criteria.json', dict(schema_version=1, criteria=[dict(criterion_id='C-DATA',
        description='Synthetic dataset requirements have verified observations', source='requirements.txt', required=True)]))
    actual = json.loads((output / 'dataset.json').read_text(encoding='utf-8'))
    passed = [actual[0]['status'] == 'paid', actual[1]['owner'] == actual[0]['owner'], len(actual) == 3]
    checks = []
    for requirement, success in zip(scope['requirements'], passed):
        proof = requirement['requirement_id'] + '.json'
        write(output / proof, dict(synthetic=True, dataset_sha256=digest(output / 'dataset.json'),
            records=actual, check=requirement['expected_result'], passed=success))
        checks.append(dict(requirement_id=requirement['requirement_id'], status='passed' if success else 'failed',
            actual_result='Synthetic file observation: ' + str(success),
            observed_at=datetime.now(timezone.utc).isoformat(), evidence=[proof]))
    assignment = dict(task_id=scope['task_id'], request_id='data-1', agent='qa-test-data-builder',
        stage='data', goal='Verify a synthetic file dataset', acceptance_criteria=['Every assigned data requirement checked'],
        stop_condition='Evidence or blocker', expected_artifact='QA JSON', result_path=str(output / 'result.json'),
        scenario_ids=scope['scenario_ids'], inputs=[str(output / 'dataset.json')], depends_on=[],
        data_assignment=str(output / 'data-assignment.json'), criterion_ids=['C-DATA'])
    result = dict(task_id=scope['task_id'], request_id='data-1', agent='qa-test-data-builder',
        event='data_partial' if partial else 'data_ready', summary='Synthetic file observations only; no live application checked',
        artifacts=['dataset.json'], blockers=['Synthetic owner mismatch for TC-2'] if partial else [],
        payload=dict(environment_id=scope['environment_id'], product_version=scope['product_version'],
            data_assignment_sha256=digest(output / 'data-assignment.json'), data_state='available',
            data_ids=[row['id'] for row in actual], verified_properties=checks,
            cleanup='After dependent checks, delete only this demo directory; dataset is retained until then.',
            scenario_ids=['TC-1'] if partial else scope['scenario_ids']))
    if partial:
        result['payload']['blocked_scenarios'] = ['TC-2']
    result['self_review'] = dict(schema_version=1, checks=[dict(id=key, status='passed',
        note='Synthetic file protocol checked; no claim about an application or live model.',
        evidence=['report:/payload/verified_properties', 'assignment:/acceptance_criteria'])
        for key in ('scope', 'evidence', 'consistency', 'role')], fixes=[], remaining_risks=[])
    write(output / 'assignment.json', assignment)
    write(output / 'result.json', result)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--partial', action='store_true')
    args = parser.parse_args()
    print(build(args.output_dir, partial=args.partial))
