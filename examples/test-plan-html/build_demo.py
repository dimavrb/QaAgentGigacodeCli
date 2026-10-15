#!/usr/bin/env python3
"""Build a synthetic accepted HTML plan; no model or product test is executed."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import task_exchange as exchange


def synthetic_review():
    return dict(schema_version=1, checks=[dict(id=key, status='passed',
        note='Учебная проверка протокола; модель и продукт не запускались.',
        evidence=['assignment:/acceptance_criteria'] if key == 'scope' else ['report:/payload'])
        for key in ('scope', 'evidence', 'consistency', 'role')], fixes=[], remaining_risks=[])


def report(state, assignment, message, owner):
    exchange.assign(state, assignment, Path(assignment['result_path']).parent, None)
    handoff = exchange.prepare_handoff(state, assignment['request_id'], None)['result']
    exchange.start_request(state, assignment['request_id'], owner, handoff['delivery_id'], None)
    exchange.atomic_write(assignment['result_path'], message)
    exchange.submit_result(state, assignment['request_id'], None)


def build(output):
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    state = output/'task.json'; requirements = output/'requirements.md'
    selected = [json.loads((ROOT/'examples/test-cases/ui'/name).read_text())['test_cases'][0]
                for name in ('create.json', 'filter.json')]
    requirements.write_text('# Учебные требования\n\n## R-CREATE\n' + selected[0]['requirement_source']
        + '\n\n## R-STATE\nСозданный заказ доступен в списке с фактическим номером, товаром и количеством 2.\n\n'
        + '## R-FILTER\n' + selected[1]['requirement_source'] + '\n', encoding='utf-8')
    selected[0]['requirement_source'] = 'requirements.md#r-create'
    selected[1]['requirement_source'] = 'requirements.md#r-filter'
    exchange.atomic_write(output/'catalog.json', dict(schema_version=1, test_cases=selected))
    (output/'plan.md').write_text('# План заказов\n\nСинтетическая демонстрация. Живая модель и продукт не запускались.\n\n'
        'Область: создание заказа, доступность созданного объекта в списке и фильтр статуса.\n'
        'Риски: подтверждение без создания, чужая строка вместо своего заказа, неполная фильтрация.\n'
        'Для исполнения: изолированный стенд, покупатель и данные из предусловий кейсов.\n'
        'Начало: принятое покрытие и подтверждённая подготовка. Завершение: фактические результаты всех назначенных кейсов.\n', encoding='utf-8')
    criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-PLAN', description='Кейсы связаны с учебными требованиями',
        source=str(requirements), required=True, required_reviews=[dict(agent='qa-result-reviewer', scope='sources')])])
    exchange.init_task(state, 'QA-HTML-DEMO', 'Учебный план · создание и фильтрация заказов', criteria)
    author = dict(task_id='QA-HTML-DEMO', request_id='plan-v1', agent='qa-test-plan-writer', stage='planning',
        goal='Показать навигацию от кейсов к требованиям после ревью', test_type='ui',
        acceptance_criteria=['Сохранить кейсы и все связи требований'], criterion_ids=['C-PLAN'],
        stop_condition='Синтетический отчёт', expected_artifact='QA JSON', result_path=str(output/'result.json'),
        inputs=[str(requirements)], depends_on=[])
    payload = dict(plan_artifact='plan.md', test_cases_artifact='catalog.json', plan_revision='v1',
        requirement_coverage=[dict(requirement_id=rid, source='requirements.md#'+rid.lower(),
            scenario_ids=[selected[index]['scenario_id']], priority='high', verification='UI',
            evidence=evidence, status='ready') for rid,index,evidence in
            [('R-CREATE',0,'Подтверждение создания'), ('R-STATE',0,'Строка по фактическому ID'),
             ('R-FILTER',1,'Точный состав списка')]], open_questions=[])
    message = dict(task_id=author['task_id'], request_id=author['request_id'], agent=author['agent'], event='plan_ready',
        summary='Учебный план готов к проверке протокола', artifacts=['plan.md', 'catalog.json'], blockers=[],
        self_review=synthetic_review(), payload=payload)
    report(state, author, message, 'fixture-author')
    target = exchange.read_state(state)['requests'][author['request_id']]['result']
    reviewer = dict(task_id=author['task_id'], request_id='review-v1', agent='qa-result-reviewer', stage='review',
        goal='Учебная проверка связей', acceptance_criteria=author['acceptance_criteria'], criterion_ids=['C-PLAN'],
        stop_condition='Синтетический отзыв', expected_artifact='QA JSON', result_path=str(output/'review.json'),
        inputs=list(target['artifact_hashes']), depends_on=[], review_criteria_ids=['C-PLAN'],
        review_target=dict(request_id=author['request_id'], result_path=target['path'],
            artifact_hashes=target['artifact_hashes'], scope='sources'))
    review = dict(task_id=author['task_id'], request_id=reviewer['request_id'], agent=reviewer['agent'], event='review_passed',
        summary='Синтетический отзыв; живой GIGACODE не оценивался', artifacts=[], blockers=[], self_review=synthetic_review(),
        payload=dict(target_request_id=author['request_id'], target_sha256=target['artifact_hashes'][target['path']],
            review_scope='sources', reviewed_revision='v1', checks=[dict(criterion_id='C-PLAN', status='passed',
                note='Учебные связи R-CREATE/R-STATE/R-FILTER', evidence=[str(requirements)])], findings=[], limitations=[]))
    report(state, reviewer, review, 'fixture-reviewer')
    exchange.review_result(state, reviewer['request_id'], 'Учебный отзыв зарегистрирован', True, None)
    result = exchange.review_result(state, author['request_id'], 'Учебное принятие после отдельного fixture-ревью', True, None)
    presentation = result['result']['presentation']
    if presentation['status'] != 'ready':
        raise ValueError(presentation['error'])
    return Path(presentation['path'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output_dir))


if __name__ == '__main__':
    main()
