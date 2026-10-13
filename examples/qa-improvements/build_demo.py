#!/usr/bin/env python3
"""Create source-bound synthetic coverage, shadow and scheduling examples."""
import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from frozen_reports import read, write_new, sha
from regression_observations import update_map, shadow_report
from regression_schedule import make_schedule
from impact_analysis import build_selection
from grader_controls import prepare_controls
from qa_improvements import report

spec = importlib.util.spec_from_file_location('impact_demo', ROOT / 'examples/impact-analysis/build_demo.py')
impact_demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(impact_demo)


def create_demo(output):
    output = Path(output).resolve(); output.mkdir(parents=True)
    base = impact_demo.create_demo(output / 'sources')
    original = read(base / 'impact-input.json')
    observations = dict(schema_version=1, revision=original['base_revision'], environment_id='fixture-env',
        test_data_id='fixture-data', collection_id='fixture-coverage', catalog_sha256=original['catalog']['sha256'],
        dependencies_complete=True, history_complete=True,
        components=[dict(component_id=c['component_id'], depends_on=c['depends_on'], files=['src/' + c['component_id'] + '.py'])
                    for c in original['components']],
        tests=[dict(scenario_id=t['scenario_id'], component_ids=t['component_ids'] or ['orders'], coverage_complete=True,
            attempts=[dict(attempt_id=t['scenario_id'] + ':1', status='fail' if t['scenario_id'] == 'TC-FAIL' else 'pass', duration_seconds=10)])
            for t in original['tests']])
    write_new(output / 'coverage.json', observations)
    refreshed = update_map(base / 'impact-input.json', output / 'coverage.json', environment_id='fixture-env', test_data_id='fixture-data')
    write_new(output / 'impact-input.json', refreshed)
    selection = build_selection(output / 'impact-input.json'); write_new(output / 'selection.json', selection)
    policy = dict(schema_version=1, impact_input_sha256=sha(output / 'impact-input.json'),
        workers=[dict(worker_id='worker-' + str(i), capabilities=['api']) for i in range(3)],
        resources=[dict(resource_id='orders-data', capacity=1)],
        tests=[dict(scenario_id=t['scenario_id'], required_capabilities=['api'],
            resource_ids=['orders-data'] if t['scenario_id'] in ('TC-ORDER', 'TC-EXPORT') else [],
            isolated_test_data=t['scenario_id'] not in ('TC-ORDER', 'TC-EXPORT')) for t in refreshed['tests']])
    write_new(output / 'execution-policy.json', policy)
    write_new(output / 'schedule.json', report('schedule', dict(input=output / 'impact-input.json', policy=output / 'execution-policy.json')))
    for arm, identifiers, phases in [
        ('full', [t['scenario_id'] for t in refreshed['tests']], [('setup',3),('execution',70),('review',2)]),
        ('selected', selection['run_scenario_ids'], [('analysis',4),('review',2),('setup',3),('queue',1),('execution',60)])]:
        identity = dict(run_id='fixture-' + arm, build_revision=original['target_revision'], environment_id='fixture-env', test_data_id='fixture-data')
        plan = dict(schema_version=1, **identity, required_phase_ids=[p for p,d in phases])
        elapsed = 0; spans = []
        for phase, duration in phases:
            kind = 'queue' if phase == 'queue' else 'active'
            spans.append(dict(span_id=phase, phase_id=phase, kind=kind, started_at=elapsed, finished_at=elapsed+duration,
                status='succeeded', attempt_id=phase, retry=False, input_tokens=0, output_tokens=0))
            elapsed += duration
        trace = dict(schema_version=1, **identity, started_at=0, accepted_at=elapsed, spans=spans)
        write_new(output / (arm + '-plan.json'), plan); write_new(output / (arm + '-trace.json'), trace)
        run = dict(schema_version=1, **identity, catalog_sha256=original['catalog']['sha256'], measurement_kind='fixture',
            results=[dict(scenario_id=sid, attempts=[dict(attempt_id=sid+':1', status='pass', duration_seconds=10)]) for sid in identifiers],
            timing={key:dict(path=arm + '-' + filename + '.json', sha256=sha(output / (arm + '-' + filename + '.json')))
                for key, filename in [('plan','plan'),('trace','trace')]})
        write_new(output / (arm + '-results.json'), run)
    write_new(output / 'shadow.json', report('shadow', dict(input=output / 'impact-input.json', full=output / 'full-results.json', selected=output / 'selected-results.json')))
    prepare_controls(ROOT / 'examples/grader-controls/suite.json', output / 'controls-public.json')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(); print(create_demo(args.output_dir))
