"""Deterministic duration-aware scheduling; resource locks are explicit."""
from pathlib import Path

from frozen_reports import Sources, number, rows, string, strings
from impact_analysis import build_selection


def make_schedule(input_path, policy_path):
    selection = build_selection(input_path)
    sources = Sources(); sources.absorb(selection['source_hashes'])
    manifest = sources.load(input_path, 'impact_input')
    policy = sources.load(policy_path, 'execution_policy')
    if policy.get('impact_input_sha256') != selection['source_hashes'][str(Path(input_path).resolve())]:
        raise ValueError('schedule policy is bound to a different impact input')
    workers = rows(policy.get('workers'), 'workers', 'worker_id')
    if not workers:
        raise ValueError('schedule needs workers')
    for worker in workers.values():
        strings(worker.get('capabilities'), 'worker.capabilities')
    resources = rows(policy.get('resources'), 'resources', 'resource_id')
    for resource in resources.values():
        if type(resource.get('capacity')) is not int or resource['capacity'] < 1:
            raise ValueError('resource capacity must be a positive integer')
    tests = rows(policy.get('tests'), 'tests', 'scenario_id')
    all_tests = {r['scenario_id']: r for r in manifest['tests']}
    if tests.keys() != all_tests.keys():
        raise ValueError('execution constraints must cover the entire frozen catalog')
    for test in tests.values():
        strings(test.get('required_capabilities'), 'required_capabilities')
        needed = strings(test.get('resource_ids'), 'resource_ids')
        if set(needed) - resources.keys():
            raise ValueError('test references unknown resource')
        if type(test.get('isolated_test_data')) is not bool:
            raise ValueError('isolated_test_data must be explicit')
        if not test['isolated_test_data'] and not needed:
            raise ValueError('shared test data requires an explicit resource lock')
    unknown = sorted(s for s in selection['run_scenario_ids'] if all_tests[s].get('duration_seconds') is None)
    if unknown:
        return sources.finish(dict(schema_version=1, status='needs_durations', unknown_duration_scenario_ids=unknown,
            assignments=[], estimated_wall_seconds=None, run_scenario_ids=selection['run_scenario_ids']))
    available = {w: 0 for w in workers}
    slots = {r: [0] * v['capacity'] for r, v in resources.items()}
    assignments = []
    ordered = sorted(selection['run_scenario_ids'], key=lambda s: (-all_tests[s]['duration_seconds'], s))
    for sid in ordered:
        test = tests[sid]
        eligible = [w for w, row in workers.items() if set(test['required_capabilities']) <= set(row['capabilities'])]
        if not eligible:
            raise ValueError('no capable worker for ' + sid)
        chosen_slots = {r: min(range(len(slots[r])), key=lambda i: (slots[r][i], i)) for r in test['resource_ids']}
        earliest_resource = max((slots[r][i] for r, i in chosen_slots.items()), default=0)
        worker = min(eligible, key=lambda w: (max(available[w], earliest_resource), w))
        start = max(available[worker], earliest_resource)
        end = number(start + all_tests[sid]['duration_seconds'], 'schedule end')
        available[worker] = end
        for r, index in chosen_slots.items(): slots[r][index] = end
        assignments.append(dict(scenario_id=sid, worker_id=worker, starts_after_seconds=start,
            estimated_duration_seconds=all_tests[sid]['duration_seconds'], ends_after_seconds=end,
            resource_slots=chosen_slots))
    return sources.finish(dict(schema_version=1, status='planned', assignments=assignments,
        estimated_wall_seconds=max(available.values(), default=0), run_scenario_ids=selection['run_scenario_ids'],
        unknown_duration_scenario_ids=[], limitations=['Оценка по историческим длительностям; setup и очередь не измерены.'],
        requires_runtime_resource_locks=True))
