#!/usr/bin/env python3
"""Two isolated fixture authors, reviewed groups, checked merge and final HTML."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import partition_test_plan as partition
import task_exchange as exchange

spec = importlib.util.spec_from_file_location('partition_html_fixture',ROOT/'examples/test-plan-html/build_demo.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


def review(state, assignment, scope, name):
    row = exchange.read_state(state)['requests'][assignment['request_id']]; result = row['result']
    checks = assignment.get('criterion_ids') or ['AC-'+str(index) for index,_ in enumerate(assignment['acceptance_criteria'],1)]
    reviewer = dict(task_id=assignment['task_id'],request_id=name,agent='qa-result-reviewer',stage=name,
        goal='Учебная проверка группы/согласованности; живая модель не запускалась',
        acceptance_criteria=assignment['acceptance_criteria'],criterion_ids=assignment.get('criterion_ids',[]),
        scenario_ids=assignment['scenario_ids'],stop_condition='Синтетический отзыв',expected_artifact='QA JSON',
        result_path=str(Path(assignment['result_path']).with_name(name+'.json')),inputs=list(result['artifact_hashes']),
        depends_on=[],plan_partition=copy.deepcopy(assignment['plan_partition']),review_criteria_ids=checks,
        review_target=dict(request_id=assignment['request_id'],result_path=result['path'],artifact_hashes=result['artifact_hashes'],scope=scope))
    context = next(iter(assignment['inputs']))
    message = dict(task_id=assignment['task_id'],request_id=name,agent='qa-result-reviewer',event='review_passed',
        summary='Fixture-ревью протокола, не оценка GIGACODE или продукта',artifacts=[],blockers=[],self_review=fixture.synthetic_review(),
        payload=dict(target_request_id=assignment['request_id'],target_sha256=result['artifact_hashes'][result['path']],
            review_scope=scope,reviewed_revision=assignment['plan_revision'],
            checks=[dict(criterion_id=cid,status='passed',note='Учебная проверка структуры/связей',evidence=[context]) for cid in checks],
            findings=[],limitations=[]))
    fixture.report(state,reviewer,message,'fixture-'+name)
    exchange.review_result(state,name,'Учебный отдельный отзыв принят',True,None)
    exchange.review_result(state,assignment['request_id'],'Учебное принятие после fixture-ревью; модель и продукт не запускались',True,None)


def build(output):
    output = Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    fixture.build(output/'scope')
    state = output/'scope/task.json'
    selection = dict(schema_version=1,plan_revision='v2',integration_request_id='plan-integrated',groups=[
        dict(group_id='create',request_id='plan-create',test_type='ui',scenario_ids=['EX-UI-001']),
        dict(group_id='filter',request_id='plan-filter',test_type='ui',scenario_ids=['EX-UI-005'])])
    partition.build(state,'plan-v1',selection,output/'v2')
    original = json.loads((output/'scope/catalog.json').read_text())
    for name in ('create','filter'):
        assignment = partition.file_json(output/'v2'/name/'assignment.json')
        context = partition.file_json(output/'v2'/name/'context.json')
        cases = [copy.deepcopy(c) for c in original['test_cases'] if c['scenario_id'] in assignment['scenario_ids']]
        for case in cases:
            case['requirement_source'] = str(output/'scope/requirements.md')+'#'+case['requirement_source'].partition('#')[2]
        exchange.atomic_write(output/'v2'/name/'catalog.json',dict(schema_version=1,test_cases=cases))
        (output/'v2'/name/'plan.md').write_text('Учебная группа '+name+'. Детализированы только назначенные ID. Продукт не выполнялся.\n')
        payload = dict(base_revision='v1',plan_revision='v2',plan_artifact='plan.md',test_cases_artifact='catalog.json',
            requirement_coverage=[dict(row,status='ready') for row in context['requirement_coverage']],open_questions=context['open_questions'])
        message = dict(task_id=assignment['task_id'],request_id=assignment['request_id'],agent=assignment['agent'],event='plan_ready',
            summary='Учебная группа готова для fixture-проверки',artifacts=['plan.md','catalog.json'],blockers=[],
            self_review=fixture.synthetic_review(),payload=payload)
        fixture.report(state,assignment,message,'fixture-author-'+name)
        review(state,assignment,'sources','review-'+name)
    partition.merge(output/'v2/partition.json')
    assignment = partition.file_json(output/'v2/integration/assignment.json')
    payload = partition.file_json(output/'v2/integration/payload.json')
    (output/'v2/integration/plan.md').write_text('# Общий учебный план\n\n'
        'Два автора подготовили отдельные группы создания и фильтрации заказов. '\
        'Структурная сборка сохраняет все три требования и два кейса.\n\n'
        'Общие условия: изолированный учебный стенд, покупатель, стабильные данные. '
        'Тесты не выполнялись. Отзывы являются fixture-проверкой протокола, не оценкой живой модели.\n')
    message = dict(task_id=assignment['task_id'],request_id=assignment['request_id'],agent=assignment['agent'],event='plan_ready',
        summary='Учебное объединённое покрытие',artifacts=['plan.md','catalog.json','partition-merge.json'],blockers=[],
        self_review=fixture.synthetic_review(),payload=payload)
    fixture.report(state,assignment,message,'fixture-integrator')
    review(state,assignment,'sources','review-integrated')
    presentation = exchange.read_state(state)['requests']['plan-integrated']['presentation']
    if presentation['status'] != 'ready': raise ValueError(presentation['error'])
    return Path(presentation['path'])


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output-dir',type=Path,required=True)
    print(build(parser.parse_args().output_dir))


if __name__=='__main__': main()
