"""Conservative regression selection from frozen, coordinator-owned evidence.

Checks consistency and dependencies, not the truth/completeness of a product model.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

IMPACT_EVENT = 'impact_ready'


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a non-empty string')
    return value


def ids(value, field, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f'{field} must contain string IDs')
    if len(value) != len(set(value)) or nonempty and not value:
        raise ValueError(f'{field} must contain unique' + (' non-empty' if nonempty else '') + ' IDs')
    return value


def obj(value, field):
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    return value


def version(value, field):
    obj(value, field)
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError(f'{field}.schema_version must equal 1')


def rows(value, field, key):
    if not isinstance(value, list):
        raise ValueError(f'{field} must be a list')
    result = {}
    for item in value:
        obj(item, field)
        identifier = text(item.get(key), field + '.' + key)
        if identifier in result:
            raise ValueError(f'{field}: duplicate {key}: {identifier}')
        result[identifier] = item
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def source_ref(value, base, field):
    obj(value, field)
    ref = text(value.get('path'), field + '.path')
    if ref.startswith(('http:', 'https:')):
        raise ValueError(f'{field} must reference a local snapshot')
    if not isinstance(value.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['sha256']):
        raise ValueError(f'{field}.sha256 must be a SHA-256 digest')
    path = (Path(base) / ref).resolve()
    if digest(path) != value['sha256']:
        raise ValueError(f'{field} snapshot changed')
    return path


def load_inputs(input_path):
    """Load all primary snapshots and validate IDs before making any exclusion."""
    input_path = Path(input_path).resolve()
    manifest = read(input_path)
    version(manifest, 'impact_input')
    base = text(manifest.get('base_revision'), 'base_revision')
    target = text(manifest.get('target_revision'), 'target_revision')
    paths = {str(input_path)}
    data = {}
    for name in ('change_set', 'catalog'):
        path = source_ref(manifest.get(name), input_path.parent, name)
        paths.add(str(path))
        data[name] = read(path)
    evidence_sources = manifest.get('evidence_sources')
    if not isinstance(evidence_sources, list):
        raise ValueError('evidence_sources must contain frozen local source references')
    for index, ref in enumerate(evidence_sources):
        path = source_ref(ref, input_path.parent, f'evidence_sources[{index}]')
        if str(path) in paths:
            raise ValueError('duplicate evidence source')
        paths.add(str(path))
    changes = data['change_set']
    version(changes, 'change_set')
    if changes.get('base_revision') != base or changes.get('target_revision') != target:
        raise ValueError('change_set revisions differ from impact_input')
    if type(changes.get('complete')) is not bool:
        raise ValueError('change_set.complete must be a boolean')
    text(changes.get('source'), 'change_set.source')
    change_rows = rows(changes.get('changes'), 'changes', 'change_id')
    for item in change_rows.values():
        ids(item.get('paths'), 'change.paths', nonempty=True)
        ids(item.get('evidence'), 'change.evidence', nonempty=True)
        for ref in item['paths']:
            if PurePosixPath(ref).is_absolute() or '..' in PurePosixPath(ref).parts:
                raise ValueError('change.paths must be relative product paths')
    from validate_test_cases import validate_catalog
    errors = validate_catalog(data['catalog'])
    if errors:
        raise ValueError('catalog: ' + '; '.join(errors))
    catalog_ids = {case['scenario_id'] for case in data['catalog']['test_cases']}
    completeness = obj(manifest.get('completeness'), 'completeness')
    binding = manifest.get('coverage_binding')
    if binding is not None:
        obj(binding, 'coverage_binding')
        for key in ('revision', 'catalog_sha256', 'environment_id', 'test_data_id', 'source_path'):
            text(binding.get(key), 'coverage_binding.' + key)
        if str((input_path.parent / binding['source_path']).resolve()) not in paths:
            raise ValueError('coverage binding needs a frozen primary snapshot')
        context = obj(manifest.get('execution_context'), 'execution_context')
        for key in ('environment_id', 'test_data_id'):
            text(context.get(key), 'execution_context.' + key)
    for key in ('dependencies', 'test_mapping', 'history'):
        if type(completeness.get(key)) is not bool:
            raise ValueError('completeness.' + key + ' must be a boolean')
    components = rows(manifest.get('components'), 'components', 'component_id')
    for item in components.values():
        deps = ids(item.get('depends_on'), 'depends_on')
        if set(deps) - components.keys():
            raise ValueError('depends_on references unknown components')
        ids(item.get('evidence'), 'component.evidence', nonempty=True)
    mappings = rows(manifest.get('change_components'), 'change_components', 'change_id')
    if mappings.keys() - change_rows.keys():
        raise ValueError('change_components references unknown changes')
    tests = rows(manifest.get('tests'), 'tests', 'scenario_id')
    if tests.keys() != catalog_ids:
        raise ValueError('tests must exactly cover the frozen catalog')
    for item in list(mappings.values()) + list(tests.values()):
        refs = ids(item.get('component_ids'), 'component_ids')
        if set(refs) - components.keys():
            raise ValueError('component_ids references unknown components')
        ids(item.get('evidence'), 'mapping.evidence', nonempty=True)
    for item in tests.values():
        if item.get('mapping_status') not in ('confirmed', 'unknown'):
            raise ValueError('mapping_status must be confirmed or unknown')
        if item['mapping_status'] == 'confirmed' and not item['component_ids']:
            raise ValueError('confirmed test needs component_ids')
        duration = item.get('duration_seconds')
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
            raise ValueError('duration_seconds must be a finite nonnegative number or null')
    policy = obj(manifest.get('policy'), 'policy')
    for key in ('mandatory_scenario_ids', 'new_scenario_ids', 'previously_failed_scenario_ids'):
        if set(ids(policy.get(key), 'policy.' + key)) - catalog_ids:
            raise ValueError('policy references unknown scenarios')
    if type(policy.get('force_full')) is not bool:
        raise ValueError('policy.force_full must be a boolean')
    ids(policy.get('evidence'), 'policy.evidence', nonempty=True)
    for item in list(components.values()) + list(mappings.values()) + list(tests.values()) + [policy]:
        check_evidence(item['evidence'], input_path.parent, paths)
    return manifest, changes, paths


def check_evidence(refs, base, source_paths):
    for ref in refs:
        if ref.startswith(('http:', 'https:', 'git:')) or str((Path(base) / ref.split('#', 1)[0]).resolve()) not in source_paths:
            raise ValueError('mapping/policy evidence must reference a frozen local source: ' + ref)


def affected_components(seeds, components):
    """If A depends on B, a change in B affects A. Cycles terminate via a set."""
    affected = set(seeds)
    while True:
        expanded = affected | {cid for cid, row in components.items() if affected.intersection(row['depends_on'])}
        if expanded == affected:
            return affected
        affected = expanded


def select_regression(manifest, changes, additional_scenarios=None):
    components = {row['component_id']: row for row in manifest['components']}
    tests = {row['scenario_id']: row for row in manifest['tests']}
    mappings = {row['change_id']: row for row in manifest['change_components']}
    additions = rows(additional_scenarios or [], 'additional_scenarios', 'scenario_id')
    for sid, item in additions.items():
        if sid not in tests:
            raise ValueError('additional_scenarios references unknown scenarios')
        text(item.get('reason'), 'additional.reason')
        ids(item.get('evidence'), 'additional.evidence', nonempty=True)
    fallback, gaps, per_change = [], [], {}
    if not changes['complete']:
        fallback.append('incomplete_changes')
    for key, complete in manifest['completeness'].items():
        if not complete:
            fallback.append('incomplete_' + key)
    if manifest['policy']['force_full']:
        fallback.append('policy_force_full')
    binding = manifest.get('coverage_binding')
    if binding is not None and (binding['revision'] not in (manifest['base_revision'], manifest['target_revision'])
            or binding['catalog_sha256'] != manifest['catalog']['sha256']):
        fallback.append('stale_coverage')
    if binding is not None and any(binding[k] != manifest['execution_context'][k] for k in ('environment_id', 'test_data_id')):
        fallback.append('incompatible_coverage_context')
    for change in changes['changes']:
        cid = change['change_id']
        seeds = mappings.get(cid, {}).get('component_ids', [])
        if not seeds:
            fallback.append('unmapped_change:' + cid)
        affected = affected_components(seeds, components)
        per_change[cid] = affected
        if not any(t['mapping_status'] == 'confirmed' and affected.intersection(t['component_ids']) for t in tests.values()):
            gaps.append(dict(change_id=cid, reason='no_confirmed_test_link'))
            fallback.append('coverage_gap:' + cid)
    affected = set().union(*per_change.values()) if per_change else set()
    decisions = []
    policy = manifest['policy']
    for sid, test in sorted(tests.items()):
        related = sorted(cid for cid, nodes in per_change.items() if nodes.intersection(test['component_ids']))
        reasons = []
        if fallback: reasons.append('full_fallback')
        if related: reasons.append('impacted')
        for key, reason in [('mandatory_scenario_ids', 'mandatory'), ('new_scenario_ids', 'new'),
                            ('previously_failed_scenario_ids', 'previously_failed')]:
            if sid in policy[key]: reasons.append(reason)
        if sid in additions: reasons.append('additional')
        unknown = test['mapping_status'] == 'unknown'
        if unknown: reasons.append('unknown_mapping')
        evidence = set(test['evidence'] + policy['evidence'])
        for cid in related:
            evidence.update(mappings[cid]['evidence'])
            for node in per_change[cid]: evidence.update(components[node]['evidence'])
        if sid in additions: evidence.update(additions[sid]['evidence'])
        decisions.append(dict(scenario_id=sid, decision='review' if unknown else 'run' if reasons else 'exclude',
                              reasons=reasons or ['outside_impact'], change_ids=related, evidence=sorted(evidence)))
    run_ids = [r['scenario_id'] for r in decisions if r['decision'] != 'exclude']
    def duration(selected):
        values = [tests[sid].get('duration_seconds') for sid in selected]
        known = sum(v for v in values if v is not None)
        try:
            finite = math.isfinite(known)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('duration sum is not finite')
        return dict(known_seconds=known,
                    missing_scenario_ids=[sid for sid in selected if tests[sid].get('duration_seconds') is None],
                    total_seconds=known if all(v is not None for v in values) else None)
    return dict(schema_version=1, base_revision=manifest['base_revision'], target_revision=manifest['target_revision'],
                selection_mode='full' if fallback else 'targeted', fallback_reasons=sorted(set(fallback)),
                affected_component_ids=sorted(affected), decisions=decisions, run_scenario_ids=run_ids,
                review_scenario_ids=[r['scenario_id'] for r in decisions if r['decision'] == 'review'],
                excluded_scenario_ids=[r['scenario_id'] for r in decisions if r['decision'] == 'exclude'],
                coverage_gaps=gaps, additional_scenarios=list(additions.values()),
                estimated_serial_duration=dict(full=duration(sorted(tests)), selected=duration(run_ids)))


def build_selection(input_path, additional_scenarios=None):
    input_sha256 = digest(input_path)
    manifest, changes, paths = load_inputs(input_path)
    result = select_regression(manifest, changes, additional_scenarios)
    for addition in result['additional_scenarios']:
        check_evidence(addition['evidence'], Path(input_path).resolve().parent, paths)
    result['source_hashes'] = {name: digest(name) for name in sorted(paths)}
    expected_hashes = {str(Path(input_path).resolve()): input_sha256}
    for ref in [manifest['change_set'], manifest['catalog'], *manifest['evidence_sources']]:
        expected_hashes[str((Path(input_path).resolve().parent / ref['path']).resolve())] = ref['sha256']
    if result['source_hashes'] != expected_hashes:
        raise ValueError('impact sources changed during selection')
    return result


def validate_impact_payload(payload, expected_scenario_ids=None):
    try:
        for key in ('impact_artifact', 'impact_input', 'base_revision', 'target_revision'):
            text(payload.get(key), 'payload.' + key)
        if not isinstance(payload.get('input_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', payload['input_sha256']):
            raise ValueError('payload.input_sha256 must be a SHA-256 digest')
        run = set(ids(payload.get('run_scenario_ids'), 'payload.run_scenario_ids'))
        review = set(ids(payload.get('review_scenario_ids'), 'payload.review_scenario_ids'))
        excluded = set(ids(payload.get('excluded_scenario_ids'), 'payload.excluded_scenario_ids'))
        if not review <= run or run & excluded:
            raise ValueError('review scenarios must run and exclusions must be disjoint')
        if expected_scenario_ids is not None and run | excluded != set(expected_scenario_ids):
            raise ValueError('impact scenarios differ from the assigned catalog scope')
        ids(payload.get('limitations'), 'payload.limitations')
        return []
    except ValueError as exc:
        return [str(exc)]


def impact_paths(input_path):
    return load_inputs(input_path)[2]


def check_impact_bundle(message, result_base, input_path):
    """Recompute selection using independent assignment inputs, never author scope."""
    payload = message['payload']
    authoritative = Path(input_path).resolve()
    declared = (Path(result_base) / payload['impact_input']).resolve()
    if declared != authoritative or digest(authoritative) != payload['input_sha256']:
        raise ValueError('impact_input differs from coordinator assignment or changed')
    artifact = (Path(result_base) / payload['impact_artifact']).resolve()
    if artifact in impact_paths(authoritative):
        raise ValueError('impact_artifact must not overwrite a primary input')
    saved = read(artifact)
    obj(saved, 'impact_artifact')
    additions = saved.get('additional_scenarios')
    if not isinstance(additions, list):
        raise ValueError('additional_scenarios must be a list')
    expected = build_selection(authoritative, additions)
    if saved != expected:
        raise ValueError('impact_artifact differs from recomputed regression selection')
    for key in ('base_revision', 'target_revision', 'run_scenario_ids', 'review_scenario_ids', 'excluded_scenario_ids'):
        if payload[key] != expected[key]:
            raise ValueError('impact payload differs from artifact: ' + key)
    return expected
