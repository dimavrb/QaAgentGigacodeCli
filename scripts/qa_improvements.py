#!/usr/bin/env python3
"""Reproducible evaluator controls, coverage import, shadow pilot and QA timing."""
import argparse
import agent_runtime
from pathlib import Path
import subprocess

from frozen_reports import read, write_new, schema, same_json
from grader_controls import prepare_controls, run_controls, check_controls
from regression_observations import update_map, shadow_report
from regression_schedule import make_schedule
from qa_timing import summarize_timing, import_metrics
from execute_regression_plan import execute

FUNCTIONS = {'controls-check': check_controls, 'shadow': shadow_report, 'schedule': make_schedule, 'timing': summarize_timing}
FIELDS = {'controls-check': ('suite', 'pack', 'review'), 'shadow': ('input', 'full', 'selected'),
          'schedule': ('input', 'policy'), 'timing': ('plan', 'trace')}


def report(command, inputs):
    values = {key: str(Path(inputs[key]).resolve()) for key in FIELDS[command]}
    result = FUNCTIONS[command](*(values[k] for k in FIELDS[command]))
    if command == 'controls-check':
        summary = 'Исполнение оценщика провалено.' if result['status'] != 'scored' else (
            f"Контроль оценщика: {result['metrics']['checks']} проверок; неверных одобрений {result['metrics']['false_approvals']}; "
            f"ложных блокировок {result['metrics']['false_blocks']}; воздержаний {result['metrics']['abstentions']}.")
    elif command == 'shadow':
        savings = result['observed_saved_wall_seconds']
        summary = (f"Пилот регресса: полный набор {result['scenario_counts']['full']}, выбранный {result['scenario_counts']['selected']}; "
            f"пропущенных подтверждённых дефектов {len(result['missed_defect_ids'])}. " +
            ('Экономия времени не подтверждена.' if savings is None else f'Измеренная разница полного времени: {savings} с.'))
    elif command == 'schedule':
        summary = ('Для плана исполнения не хватает измеренных длительностей.' if result['status'] != 'planned' else
            f"План исполнения: {len(result['assignments'])} сценариев; оценка времени {result['estimated_wall_seconds']} с без setup и очереди.")
    else:
        summary = (f"Полное время до приёмки: {result['wall_seconds']} с; активная работа исполнителей: {result['active_effort_seconds']} с. "
            + ('Измерение полное.' if result['complete_timing'] else 'Измерение неполное.'))
    if result.get('measurement_kind') == 'fixture': summary += ' Учебные данные.'
    if result.get('provisional_gold'): summary += ' Эталонная разметка требует экспертного ревью.'
    return dict(result, command=command, command_inputs=values, summary_text=summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, fields in FIELDS.items():
        command = sub.add_parser(name)
        for field in fields: command.add_argument('--' + field, required=True, type=Path)
        command.add_argument('--output', required=True, type=Path)
    verify = sub.add_parser('verify'); verify.add_argument('--report', required=True, type=Path)
    prepare = sub.add_parser('controls-prepare')
    prepare.add_argument('--suite', required=True, type=Path); prepare.add_argument('--output', required=True, type=Path)
    run = sub.add_parser('controls-run')
    run.add_argument('--pack', required=True, type=Path); run.add_argument('--output', required=True, type=Path)
    agent_runtime.add_runtime_arguments(run); run.add_argument('--model'); run.add_argument('--max-wall-time', default='5m')
    run.add_argument('--measurement-kind', choices=('live', 'fixture'), default='live')
    mapping = sub.add_parser('map')
    mapping.add_argument('--input', required=True, type=Path); mapping.add_argument('--observations', required=True, type=Path)
    mapping.add_argument('--output', required=True, type=Path)
    mapping.add_argument('--environment-id', required=True); mapping.add_argument('--test-data-id', required=True)
    importer = sub.add_parser('timing-import')
    for field in ('plan', 'trace', 'index'): importer.add_argument('--' + field, required=True, type=Path)
    importer.add_argument('--output', required=True, type=Path)
    runner = sub.add_parser('execute-plan')
    for field in ('input', 'policy', 'commands'): runner.add_argument('--' + field, required=True, type=Path)
    runner.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command in FUNCTIONS:
            result = report(args.command, vars(args)); write_new(args.output, result)
        elif args.command == 'verify':
            result = schema(read(args.report), 'saved report')
            command = result.get('command')
            if command not in FUNCTIONS: raise ValueError('unknown report command')
            if not same_json(result, report(command, result['command_inputs'])): raise ValueError('saved report differs from recomputation')
        elif args.command == 'controls-prepare': result = prepare_controls(args.suite, args.output)
        elif args.command == 'controls-run':
            result = run_controls(args.pack, args.output, cli=args.cli, model=args.model,
                max_wall_time=args.max_wall_time, measurement_kind=args.measurement_kind, runtime_config=args.runtime_config)
        elif args.command == 'map':
            result = update_map(args.input, args.observations, environment_id=args.environment_id, test_data_id=args.test_data_id)
            write_new(args.output, result)
        elif args.command == 'timing-import':
            result = import_metrics(args.plan, args.trace, args.index); write_new(args.output, result)
        else: result = execute(args.input, args.policy, args.commands, args.output_dir)
        print('Saved primary files verified.' if args.command == 'verify' else 'Output saved; see limitations and measurement_kind.')
        if args.command == 'controls-run' and result['status'] == 'failed': return 1
        if args.command == 'controls-check' and (result['status'] != 'scored' or result['metrics']['correct'] != result['metrics']['checks']): return 1
        if args.command == 'shadow' and result['outcome'] != 'no_miss_in_observed_run': return 1
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print('QA report rejected: ' + str(exc)); return 2


if __name__ == '__main__': raise SystemExit(main())
