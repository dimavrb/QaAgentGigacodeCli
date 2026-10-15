#!/usr/bin/env python3
"""Create fresh synthetic guide evidence; never accesses a product environment."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import task_exchange as exchange


def build(output, *, with_questions=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    guide = output / 'admin-guide.md'
    guide.write_text('# Учебный админгайд\n\n1. Подтвердить версию fixture-1.\n2. Подтвердить доступность учебного API.\n')
    scope = dict(schema_version=1, task_id='QA-ENV-DEMO', environment_id='synthetic',
        product_version='fixture-1', created_at=datetime.now(timezone.utc).isoformat(),
        admin_guide=dict(path=str(guide), sha256=exchange.fingerprint(guide)), scenario_ids=['TC-1'],
        points=[dict(point_id='ADM-1', quote='Подтвердить версию fixture-1.', expected_result='fixture-1'),
                dict(point_id='ADM-2', quote='Подтвердить доступность учебного API.', expected_result='Учебный ответ доступен')])
    scope_path = output / 'environment-assignment.json'
    exchange.atomic_write(scope_path, scope)
    checks = []
    for point in scope['points']:
        proof = output / (point['point_id'] + '.json')
        exchange.atomic_write(proof, dict(measurement_kind='fixture', environment_id='synthetic',
            product_version='fixture-1', observation=point['expected_result']))
        checks.append(dict(point_id=point['point_id'], status='passed', actions=['Прочитан синтетический материал'],
            actual_result=point['expected_result'], observed_at=datetime.now(timezone.utc).isoformat(), evidence=[str(proof)]))
    assignment = dict(task_id='QA-ENV-DEMO', request_id='env-1', agent='qa-test-env-configurator', stage='environment',
        goal='Проверить протокол админгайда на синтетических наблюдениях',
        acceptance_criteria=['Каждый пункт связан с наблюдением'], stop_condition='Вернуть учебный отчёт',
        expected_artifact='QA JSON', result_path=str(output / 'result.json'), scenario_ids=['TC-1'],
        criterion_ids=['C-GUIDE'], inputs=[], depends_on=[], environment_assignment=str(scope_path))
    message = dict(task_id='QA-ENV-DEMO', request_id='env-1', agent='qa-test-env-configurator', event='environment_ready',
        summary='Синтетический протокол выполнен; реальный стенд не проверялся.', artifacts=[], blockers=[],
        payload=dict(environment_id='synthetic', product_version='fixture-1',
            verified_capabilities=['Только учебная доступность'], limitations=['Продуктовые действия не выполнялись'],
            environment_assignment_sha256=exchange.fingerprint(scope_path), guide_checks=checks, open_questions=[]),
        self_review=dict(schema_version=1, checks=[dict(id=cid, status='passed',
            note='Проверен только синтетический протокол.', evidence=['assignment:/acceptance_criteria']
            if cid == 'scope' else ['report:/payload']) for cid in ('scope', 'evidence', 'consistency', 'role')],
            fixes=[], remaining_risks=['Не подтверждает живой стенд']))
    if with_questions:
        checks[-1].update(status='blocked', actions=[], actual_result='Адрес учебного API не предоставлен',
                          evidence=[])
        message.update(event='blocked', summary='Учебная настройка остановлена: нужен адрес API.',
                       blockers=['Не предоставлен адрес API для пункта ADM-2'])
        message['payload']['verified_capabilities'] = ['Учебная версия fixture-1 подтверждена']
        message['payload']['open_questions'] = [dict(question_id='Q-API', status='open', blocking=True,
            question='Какой адрес API использовать на этом стенде?', context='Пункт ADM-2 ожидает подтверждённый адрес.',
            point_ids=['ADM-2'], scenario_ids=['TC-1'])]
    criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-GUIDE', description='Учебный протокол всех пунктов',
                                                   source=str(guide), required=True)])
    for name, value in (('assignment.json', assignment), ('result.json', message), ('criteria.json', criteria)):
        exchange.atomic_write(output / name, value)
    return assignment, message, criteria


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--with-questions', action='store_true', help='Build a blocked synthetic report with a remaining question')
    args = parser.parse_args()
    build(args.output_dir, with_questions=args.with_questions)
    print(args.output_dir.resolve())
