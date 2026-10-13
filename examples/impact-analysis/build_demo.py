#!/usr/bin/env python3
"""Create and validate a synthetic impact-analysis result. Does not run GIGACODE."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from impact_analysis import build_selection, check_impact_bundle, digest
from qa_contract import validate_message
from self_review import validate_self_review


def create_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ('requirements.md', 'history.json', 'changes.json', 'catalog.json',
                 'impact-input.json', 'assignment.json', 'criteria.json'):
        shutil.copyfile(Path(__file__).parent / name, output / name)
    assignment = json.loads((output / 'assignment.json').read_text())
    selection = build_selection(output / 'impact-input.json')
    (output / 'selection.json').write_text(json.dumps(selection, ensure_ascii=False, indent=2) + '\n')
    payload = {key: selection[key] for key in ('base_revision', 'target_revision', 'run_scenario_ids',
                                              'review_scenario_ids', 'excluded_scenario_ids')}
    payload.update(impact_artifact='selection.json', impact_input='impact-input.json',
                   input_sha256=digest(output / 'impact-input.json'),
                   limitations=['Синтетическая модель и длительности. Продукт/GIGACODE не запускались.',
                                'TC-UNKNOWN включён в прогон и требует проверки связи.',
                                'Исключения и полнота источников требуют независимого ревью.'])
    message = dict(task_id=assignment['task_id'], request_id=assignment['request_id'], agent=assignment['agent'],
                   event='impact_ready', summary='Учебный набор: 6 из 7 сценариев, TC-PAY вне влияния по модели.',
                   artifacts=['selection.json', 'impact-input.json'], blockers=[], payload=payload)
    message['self_review'] = dict(schema_version=1, checks=[
        dict(id='scope', status='passed', note='Все семь ID каталога отражены.',
             evidence=['assignment:/acceptance_criteria', 'assignment:/scenario_ids', 'report:/payload/run_scenario_ids', 'report:/payload/excluded_scenario_ids']),
        dict(id='evidence', status='passed', note='Хеши учебных источников проверены, первичные снимки сохранены.',
             evidence=['report:/payload/input_sha256', 'report:/artifacts']),
        dict(id='consistency', status='passed', note='Набор пересчитан по снимку, review включён в прогон.',
             evidence=['report:/payload/run_scenario_ids', 'report:/payload/review_scenario_ids']),
        dict(id='role', status='limited', note='Механика проверена на учебной карте, реальные связи не подтверждены.',
             evidence=['report:/payload/limitations'])], fixes=[],
        remaining_risks=['Достоверность карты продукта и реальное сокращение времени требуют пилота.'])
    errors = validate_message(message, assignment['task_id'], assignment['request_id'], assignment['agent'], assignment['scenario_ids'])
    errors += validate_self_review(message, assignment, required=True)
    if errors:
        raise ValueError('; '.join(errors))
    check_impact_bundle(message, output, output / 'impact-input.json')
    (output / 'result.json').write_text(json.dumps(message, ensure_ascii=False, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        output = create_demo(args.output_dir)
    except (OSError, ValueError) as exc:
        print(str(exc)); return 2
    print(f'Synthetic example validated: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
