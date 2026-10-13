"""Build frozen impact inputs from normalized coverage and compare shadow runs."""
import copy
from decimal import Decimal
from pathlib import Path, PurePosixPath
import statistics

from frozen_reports import Sources, boolean, number, rows, string, strings, read, sha
from impact_analysis import build_selection, load_inputs
from qa_timing import summarize_timing


def attempts(row):
    values = row.get('attempts')
    if not isinstance(values, list) or not values:
        raise ValueError('test result needs every attempt, including initial failures')
    identifiers = set()
    for value in values:
        if not isinstance(value, dict): raise ValueError('invalid attempt')
        attempt = string(value.get('attempt_id'), 'attempt_id')
        if attempt in identifiers: raise ValueError('duplicate attempt')
        identifiers.add(attempt)
        if value.get('status') not in ('pass', 'fail', 'blocked', 'error'):
            raise ValueError('unknown test status')
        number(value.get('duration_seconds'), 'attempt.duration_seconds')
    return values


def update_map(input_path, observations_path, *, environment_id, test_data_id):
    """No observed hit is absence of knowledge, never proof of non-coverage."""
    initial = build_selection(input_path)
    original, changes, primary = load_inputs(input_path)
    sources = Sources()
    sources.absorb(initial['source_hashes'])
    sources.load(input_path, 'impact_input')
    observations = sources.load(observations_path, 'coverage_observations')
    for key in ('environment_id', 'test_data_id', 'collection_id'):
        string(observations.get(key), key)
    if observations['environment_id'] != string(environment_id, 'expected environment') or observations['test_data_id'] != string(test_data_id, 'expected test data'):
        raise ValueError('coverage environment/test data differ from coordinator scope')
    if observations.get('catalog_sha256') != original['catalog']['sha256']:
        raise ValueError('coverage catalog does not match impact input')
    fresh = observations.get('revision') in (original['base_revision'], original['target_revision'])
    string(observations.get('revision'), 'coverage revision')
    dependencies_complete = boolean(observations.get('dependencies_complete'), 'dependencies_complete')
    history_complete = boolean(observations.get('history_complete'), 'history_complete')
    # Import only dependencies inside the declared component scope. Completeness is an
    # explicit collector assertion, subject to independent review; it is not inferred.
    graph = rows(observations.get('components'), 'components', 'component_id')
    original_ids = {r['component_id'] for r in original['components']}
    if graph.keys() != original_ids: raise ValueError('coverage component scope differs')
    for component in graph.values():
        if set(strings(component.get('depends_on'), 'depends_on')) - graph.keys():
            raise ValueError('unknown dependency')
        files = strings(component.get('files'), 'component.files')
        for path in files:
            if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('coverage paths must be relative product paths')
    observed = rows(observations.get('tests'), 'coverage.tests', 'scenario_id')
    all_ids = {t['scenario_id'] for t in original['tests']}
    if observed.keys() - all_ids: raise ValueError('coverage contains unknown tests')
    ref = str(Path(observations_path).resolve())
    result = copy.deepcopy(original)
    # Resolve existing relative sources before saving the new input in another directory.
    for entry in [result['change_set'], result['catalog'], *result['evidence_sources']]:
        entry['path'] = str((Path(input_path).resolve().parent / entry['path']).resolve())
    for section in ('components', 'change_components', 'tests'):
        for row in result[section]:
            row['evidence'] = [str((Path(input_path).resolve().parent / e.split('#', 1)[0]).resolve()) +
                ('#' + e.split('#', 1)[1] if '#' in e else '') for e in row['evidence']]
    result['policy']['evidence'] = [str((Path(input_path).resolve().parent / e.split('#', 1)[0]).resolve()) +
        ('#' + e.split('#', 1)[1] if '#' in e else '') for e in result['policy']['evidence']]
    if ref in {r['path'] for r in [result['change_set'], result['catalog'], *result['evidence_sources']]}:
        raise ValueError('coverage must be a new primary snapshot')
    result['evidence_sources'].append(dict(path=ref, sha256=sha(ref)))
    result['components'] = [dict(component_id=cid, depends_on=row['depends_on'], evidence=[ref + '#' + cid])
        for cid, row in sorted(graph.items())]
    result['change_components'] = []
    for change in changes['changes']:
        known_files = set().union(*(set(row['files']) for row in graph.values())) if graph else set()
        fully_mapped = set(change['paths']) <= known_files
        owners = sorted(cid for cid, row in graph.items() if set(change['paths']).intersection(row['files'])) if fresh and fully_mapped else []
        result['change_components'].append(dict(change_id=change['change_id'], component_ids=owners, evidence=[ref]))
    known_failures = set(result['policy']['previously_failed_scenario_ids'])
    for test in result['tests']:
        row = observed.get(test['scenario_id'])
        test.update(component_ids=[], mapping_status='unknown', evidence=[ref], duration_seconds=None)
        if row is None: continue
        components = strings(row.get('component_ids'), 'test.component_ids')
        if set(components) - graph.keys(): raise ValueError('test references unknown component')
        runs = attempts(row)
        duration = statistics.median([a['duration_seconds'] for a in runs])
        if fresh: test['duration_seconds'] = duration
        if any(a['status'] == 'fail' for a in runs): known_failures.add(test['scenario_id'])
        complete = boolean(row.get('coverage_complete'), 'coverage_complete')
        if fresh and complete and components:
            test.update(component_ids=components, mapping_status='confirmed', evidence=[ref + '#' + test['scenario_id']])
    result['policy']['previously_failed_scenario_ids'] = sorted(known_failures)
    result['policy']['evidence'].append(ref)
    result['completeness'] = dict(
        dependencies=fresh and dependencies_complete,
        test_mapping=fresh and observed.keys() == all_ids and all(t['mapping_status'] == 'confirmed' for t in result['tests']),
        history=fresh and history_complete and observed.keys() == all_ids)
    result['coverage_binding'] = dict(revision=observations['revision'], catalog_sha256=observations['catalog_sha256'],
        environment_id=observations['environment_id'], test_data_id=observations['test_data_id'], source_path=ref)
    result['execution_context'] = dict(environment_id=environment_id, test_data_id=test_data_id)
    sources.finish({})
    return result


def load_results(path, sources, selection, wanted):
    run = sources.load(path, 'regression_results')
    for key in ('run_id', 'environment_id', 'test_data_id', 'catalog_sha256'):
        string(run.get(key), key)
    if run.get('build_revision') != selection['target_revision']:
        raise ValueError('regression was executed on another build')
    if run.get('measurement_kind') not in ('live', 'fixture'):
        raise ValueError('unknown measurement kind')
    results = rows(run.get('results'), 'results', 'scenario_id')
    if results.keys() != set(wanted):
        raise ValueError('run results do not exactly cover the assigned selection')
    defects, unresolved, unstable, executions = {}, [], [], 0
    for sid, row in results.items():
        values = attempts(row); executions += len(values)
        statuses = {a['status'] for a in values}
        if 'pass' in statuses and statuses - {'pass'}: unstable.append(sid)
        for attempt in values:
            if attempt.get('capture') is not None:
                captured = sources.reference(Path(path).resolve().parent, attempt['capture'], 'attempt capture')
                record = sources.load(captured, 'attempt capture')
                derived = 'error' if record.get('error') else 'pass' if record.get('exit_code') == 0 else 'fail'
                if record.get('scenario_id') != sid or record.get('attempt_id') != attempt['attempt_id'] or derived != attempt['status']:
                    raise ValueError('test verdict differs from primary attempt capture')
            if attempt['status'] == 'pass':
                if any(attempt.get(k) is not None for k in ('classification', 'defect_id', 'confirmation')):
                    raise ValueError('successful attempt cannot contain a failure verdict')
                continue
            classification = attempt.get('classification')
            if classification not in ('confirmed_defect', 'infrastructure', 'unclassified'):
                raise ValueError('failed attempt needs explicit classification')
            if classification == 'confirmed_defect':
                defect = string(attempt.get('defect_id'), 'defect_id')
                proof = sources.reference(Path(path).resolve().parent, attempt.get('confirmation'), 'defect confirmation')
                if proof == Path(path).resolve(): raise ValueError('defect cannot confirm itself')
                defects.setdefault(defect, set()).add(sid)
            else:
                unresolved.append(dict(scenario_id=sid, attempt_id=attempt['attempt_id'], classification=classification))
    return run, defects, unresolved, unstable, executions


def shadow_report(input_path, full_path, selected_path):
    selection = build_selection(input_path)
    sources = Sources(); sources.absorb(selection['source_hashes'])
    manifest = sources.load(input_path, 'impact_input')
    full_ids = [r['scenario_id'] for r in manifest['tests']]
    full, full_defects, full_unknown, full_unstable, full_attempts = load_results(full_path, sources, selection, full_ids)
    reduced, found, reduced_unknown, reduced_unstable, selected_attempts = load_results(
        selected_path, sources, selection, selection['run_scenario_ids'])
    for key in ('environment_id', 'test_data_id', 'catalog_sha256', 'measurement_kind'):
        if full[key] != reduced[key]: raise ValueError('shadow identity mismatch: ' + key)
    if full['catalog_sha256'] != manifest['catalog']['sha256']:
        raise ValueError('shadow catalog differs')
    if manifest.get('execution_context') and any(full[k] != manifest['execution_context'][k] for k in ('environment_id', 'test_data_id')):
        raise ValueError('shadow differs from frozen impact execution context')
    if full['run_id'] == reduced['run_id']: raise ValueError('shadow requires distinct run IDs')
    # Both executions are observed: inferring the selected result from the full result
    # would hide setup overhead, retries and execution-dependent defects.
    timing = {}
    for name, run, path in [('full', full, full_path), ('selected', reduced, selected_path)]:
        spec = run.get('timing')
        if not isinstance(spec, dict): raise ValueError('run needs timing plan and trace')
        plan = sources.reference(Path(path).resolve().parent, spec.get('plan'), 'timing plan')
        trace = sources.reference(Path(path).resolve().parent, spec.get('trace'), 'timing trace')
        report = summarize_timing(plan, trace)
        for key in ('run_id', 'build_revision', 'environment_id', 'test_data_id'):
            if report[key] != run[key]: raise ValueError('timing belongs to another run')
        sources.absorb(report['source_hashes']); timing[name] = report
    missed = sorted(full_defects.keys() - found.keys())
    extra = sorted(found.keys() - full_defects.keys())
    incomplete = bool(full_unknown or reduced_unknown or extra or not all(r['complete_timing'] for r in timing.values()))
    outcome = 'missed_defects' if missed else 'inconclusive' if incomplete else 'no_miss_in_observed_run'
    comparable = not incomplete and not missed
    seconds = None
    if comparable:
        seconds = float(Decimal(str(timing['full']['wall_seconds'])) - Decimal(str(timing['selected']['wall_seconds'])))
    return sources.finish(dict(schema_version=1, measurement_kind=full['measurement_kind'],
        outcome=outcome, missed_defect_ids=missed, selected_only_defect_ids=extra,
        full_defect_scenarios={k: sorted(v) for k, v in sorted(full_defects.items())},
        selected_defect_scenarios={k: sorted(v) for k, v in sorted(found.items())},
        unresolved_failures={'full': full_unknown, 'selected': reduced_unknown},
        unstable_scenario_ids={'full': full_unstable, 'selected': reduced_unstable},
        scenario_counts={'full': len(full_ids), 'selected': len(selection['run_scenario_ids'])},
        execution_attempts={'full': full_attempts, 'selected': selected_attempts}, timing=timing,
        observed_saved_wall_seconds=seconds, promotion_eligible=False,
        limitations=['Один прогон не доказывает сохранение качества на будущих изменениях.',
            'Подтверждение дефекта требует содержательного независимого ревью.']))
