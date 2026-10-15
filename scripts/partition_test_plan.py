#!/usr/bin/env python3
"""Freeze author scopes and verify a complete merge; never start model agents."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re

import task_exchange as exchange
from qa_contract import parse_message
from test_plan import PLAN_EVENTS, validate_plan
from validate_test_cases import read_json, validate_catalog


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_json(path):
    from build_test_plan_html import regular_bytes
    return read_json(regular_bytes(path).decode('utf-8'))


def ids(values, name, nonempty=False):
    require(isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values)
            and len(values) == len(set(values)) and (values or not nonempty), name + ' needs unique string IDs')
    return values


def source_ref(source, base):
    from build_test_plan_html import remote_source
    if remote_source(source): return source
    name, marker, fragment = source.partition('#')
    require(bool(name), 'Local requirement needs a file path')
    return exchange.local_path(name, base) + (marker + fragment if marker else '')


def revision_output(request_id, original_request, original_path):
    require(isinstance(request_id,str), 'Author request ID must be a string')
    if request_id == original_request: return original_path
    suffix = request_id.removeprefix(original_request)
    require(request_id.startswith(original_request) and re.fullmatch(r'\.revision-[1-9][0-9]*',suffix),
            'Unknown author revision; create a new partition assignment')
    path = Path(original_path)
    return str(path.with_name(path.stem+suffix+path.suffix))


def group_context(manifest, group, baseline, snapshot):
    """A named, verified projection of selected requirements; no implicit truncation."""
    from build_test_plan_html import markdown_section, remote_source
    wanted = set(group['scenario_ids']); base = Path(manifest['baseline_artifact_base'])
    coverage = []; sources = []
    for row in baseline['payload']['requirement_coverage']:
        selected = [sid for sid in row['scenario_ids'] if sid in wanted]
        if not selected:
            continue
        source = source_ref(row['source'], base)
        coverage.append(dict(row, source=source, scenario_ids=selected))
        body = None
        if not remote_source(source):
            name, _, fragment = source.partition('#')
            body = markdown_section(snapshot.read(base/name), fragment)
        else:
            require(manifest['shared_context_paths'] or group['context_paths'],
                    'Bare group needs a registered text snapshot for external requirements')
        sources.append(dict(requirement_id=row['requirement_id'], source=source, content=body))
    requirements = {row['requirement_id'] for row in coverage}
    questions = []
    for question in baseline['payload']['open_questions']:
        selected = [rid for rid in question['requirement_ids'] if rid in requirements]
        if selected:
            questions.append(dict(question, requirement_ids=selected,
                scenario_ids=[sid for sid in question['scenario_ids'] if sid in wanted]))
    context_paths = list(dict.fromkeys(manifest['shared_context_paths'] + group['context_paths']))
    context = {path:dict(sha256=manifest['source_hashes'][path], content=snapshot.read(path)) for path in context_paths}
    return dict(schema_version=1, task_id=manifest['task_id'], base_revision=manifest['base_revision'],
                plan_revision=manifest['plan_revision'], group_id=group['group_id'],
                scenario_ids=group['scenario_ids'], requirement_coverage=coverage, open_questions=questions,
                requirement_sources=sources, shared_and_group_context=context,
                instruction='Only this scope may be detailed. Unknown expectations require a question, not an invented case.')


def structural_manifest(manifest):
    require(isinstance(manifest, dict) and type(manifest.get('schema_version')) is int
            and manifest['schema_version'] == 1 and manifest.get('kind') == 'test_plan_partition', 'Invalid partition manifest')
    for key in ('task_id','base_revision','plan_revision','state_path','baseline_request_id','baseline_result_path','baseline_artifact_base'):
        exchange.required(manifest.get(key), key)
    require(manifest['base_revision'] != manifest['plan_revision'], 'Integration revision must differ from its baseline')
    require(re.fullmatch('[0-9a-f]{64}',str(manifest.get('baseline_assignment_sha256',''))), 'Missing baseline assignment hash')
    require(isinstance(manifest.get('criterion_hashes'),dict) and all(isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v)
            for v in manifest['criterion_hashes'].values()), 'Invalid criterion hashes')
    hashes = manifest.get('source_hashes')
    require(isinstance(hashes, dict) and bool(hashes) and all(Path(p).is_absolute()
            and isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for p,h in hashes.items()), 'Invalid source hashes')
    shared = ids(manifest.get('shared_context_paths'), 'shared_context_paths')
    require(set(shared) <= set(hashes), 'Shared context must belong to the accepted baseline')
    groups = manifest.get('groups')
    require(isinstance(groups, list) and 1 <= len(groups) <= 32, 'Partition needs 1..32 groups')
    names = set(); scenarios = set(); requests = set(); outputs = set()
    for group in groups:
        require(isinstance(group,dict), 'Partition group must be an object')
        name = group.get('group_id')
        require(isinstance(name,str) and re.fullmatch('[a-z][a-z0-9-]{0,47}',name)
                and name != 'integration' and name not in names, 'Invalid or duplicate group_id')
        names.add(name)
        require(group.get('test_type') in ('api','ui','kafka'), 'Group test_type must be api/ui/kafka')
        assigned = set(ids(group.get('scenario_ids'), 'scenario_ids', True))
        require(not assigned & scenarios, 'A scenario must have exactly one author')
        scenarios.update(assigned)
        request = exchange.required(group.get('request_id'), 'request_id')
        require(request not in requests, 'Duplicate group request_id'); requests.add(request)
        ids(group.get('depends_on'), 'group depends_on')
        ids(group.get('requirement_ids'), 'requirement_ids', True)
        require(set(ids(group.get('context_paths'), 'context_paths')) <= set(hashes), 'Group context is outside the baseline')
        ids(group.get('criterion_ids'), 'criterion_ids')
        paths = []
        for key in ('result_path','plan_path','catalog_path','context_path'):
            path = exchange.required(group.get(key), key)
            require(Path(path).is_absolute() and path not in outputs and path not in hashes, 'Conflicting group output path')
            outputs.add(path); paths.append(Path(path).parent)
        require(len(set(paths)) == 1, 'Group artifacts must share one isolated directory')
        require(re.fullmatch('[0-9a-f]{64}', str(group.get('context_sha256',''))), 'Missing group context hash')
    visiting = set(); done = set(); graph = {g['group_id']:g['depends_on'] for g in groups}
    def visit(name):
        require(name in graph and name not in visiting, 'Unknown or cyclic group dependency')
        if name in done:
            return
        visiting.add(name)
        for target in graph[name]: visit(target)
        visiting.remove(name); done.add(name)
    for name in graph: visit(name)
    final = manifest.get('integration')
    require(isinstance(final, dict) and final.get('request_id') not in requests, 'Invalid integration owner')
    exchange.required(final.get('request_id'), 'integration request_id')
    ids(final.get('criterion_ids'),'integration criterion_ids')
    owners = list(requests) + [final['request_id']]
    for left in owners:
        require(all(right == left or not re.fullmatch(re.escape(left)+r'\.revision-[1-9][0-9]*',right)
                    for right in owners), 'Author request namespaces overlap')
    for key in ('result_path','plan_path','catalog_path','proof_path','payload_path'):
        value = exchange.required(final.get(key), key)
        require(Path(value).is_absolute() and value not in outputs and value not in hashes, 'Conflicting integration output')
        outputs.add(value)


def load(path, expected_sha256=None):
    from build_test_plan_html import Snapshot, regular_bytes
    path = Path(path).resolve(); raw = regular_bytes(path)
    require(expected_sha256 is None or digest(raw) == expected_sha256, 'Partition manifest changed; create a new assignment')
    manifest = read_json(raw.decode('utf-8')); structural_manifest(manifest)
    state = exchange.read_state(manifest['state_path'])
    row = state['requests'].get(manifest['baseline_request_id'])
    require(state['task_id'] == manifest['task_id'] and row and row['status'] == 'accepted'
            and row.get('review',{}).get('status') == 'passed', 'Partition baseline is not accepted')
    require(row['assignment'].get('plan_partition',{}).get('mode') != 'group', 'A group cannot be the global partition baseline')
    require(row['result']['path'] == manifest['baseline_result_path']
            and row['result']['artifact_hashes'] == manifest['source_hashes'], 'Partition uses another baseline snapshot')
    require(digest(encoded(row['assignment'])) == manifest['baseline_assignment_sha256'], 'Baseline assignment changed')
    require(all(cid in state.get('criteria',{}) and digest(encoded(state['criteria'][cid]['definition'])) == sha
                for cid,sha in manifest['criterion_hashes'].items()), 'Partition task criteria changed')
    snapshot = Snapshot(dict(manifest['source_hashes']))
    for name in snapshot.hashes: snapshot.load(name)
    baseline, _ = parse_message(snapshot.read(manifest['baseline_result_path']))
    require(baseline['agent'] == 'qa-test-plan-writer' and baseline['event'] in PLAN_EVENTS
            and baseline['payload']['plan_revision'] == manifest['base_revision'], 'Invalid baseline plan revision')
    reviewed_results(state,manifest['baseline_request_id'],row['result'])
    wanted = {sid for r in baseline['payload']['requirement_coverage'] if r['status'] in ('planned','ready') for sid in r['scenario_ids']}
    actual = {sid for group in manifest['groups'] for sid in group['scenario_ids']}
    require(actual == wanted, 'Partition loses or adds selected scenarios: ' + str(sorted(actual ^ wanted)))
    for group in manifest['groups']:
        context = group_context(manifest, group, baseline, snapshot)
        require(set(group['requirement_ids']) == {r['requirement_id'] for r in context['requirement_coverage']}, 'Group requirement scope changed')
        raw_context = regular_bytes(group['context_path'])
        require(digest(raw_context) == group['context_sha256'] and read_json(raw_context.decode('utf-8')) == context,
                'Group context changed or differs from primary sources')
    snapshot.verify()
    return manifest, baseline, digest(raw)


def normalize_assignment(assignment, base):
    binding = assignment['plan_partition']
    require(isinstance(binding, dict) and binding.get('mode') in ('group','integration')
            and set(binding) == ({'path','sha256','mode','group_id'} if binding.get('mode') == 'group' else {'path','sha256','mode'}),
            'Invalid plan_partition binding')
    binding['path'] = exchange.local_path(binding['path'], base)
    require(re.fullmatch('[0-9a-f]{64}', str(binding.get('sha256',''))), 'Partition binding needs SHA256')
    manifest, _, _ = load(binding['path'], binding['sha256'])
    require(assignment['task_id'] == manifest['task_id'], 'Partition belongs to another task')
    required = [binding['path']]
    if binding['mode'] == 'group':
        group = next((g for g in manifest['groups'] if g['group_id'] == binding['group_id']), None)
        require(group is not None, 'Unknown partition group')
        require(set(assignment['scenario_ids']) == set(group['scenario_ids']), 'Assignment changes its scenario scope')
        required.append(group['context_path'])
        if assignment['agent'] == 'qa-test-plan-writer':
            require(assignment['result_path'] == revision_output(assignment['request_id'],group['request_id'],group['result_path'])
                    and assignment.get('test_type') == group['test_type'], 'Assignment changes the group owner or output')
            require(set(assignment['criterion_ids']) == set(group['criterion_ids']), 'Assignment changes group review criteria')
            by_name = {item['group_id']:item['request_id'] for item in manifest['groups']}
            for dependency in group['depends_on']:
                request = by_name[dependency]
                require(any(dep == request or re.fullmatch(re.escape(request)+r'\.revision-[1-9][0-9]*',dep)
                            for dep in assignment['depends_on']), 'Assignment drops a required group dependency: '+dependency)
    else:
        final = manifest['integration']
        require(set(assignment['scenario_ids']) == {sid for g in manifest['groups'] for sid in g['scenario_ids']},
                'Integration assignment loses global scenario scope')
        if assignment['agent'] == 'qa-test-plan-writer':
            require(assignment['result_path'] == revision_output(assignment['request_id'],final['request_id'],final['result_path']),
                    'Assignment changes the integration owner or output')
            require(set(assignment['criterion_ids']) == set(final['criterion_ids']), 'Integration cannot drop its global criteria')
            for group in manifest['groups']:
                require(any(dep == group['request_id'] or re.fullmatch(re.escape(group['request_id'])+r'\.revision-[1-9][0-9]*',dep)
                            for dep in assignment['depends_on']), 'Integration needs every group dependency')
    require(assignment['agent'] in ('qa-test-plan-writer','qa-result-reviewer','qa-conclusion-reviewer'), 'Partition binding requires a plan author/reviewer')
    for name in required:
        if name not in assignment['inputs']: assignment['inputs'].append(name)


def check_group(message, manifest, group):
    payload = message['payload']
    require(payload.get('base_revision') == manifest['base_revision'] and payload.get('plan_revision') == manifest['plan_revision'], 'Group revision differs from partition')
    context = file_json(group['context_path'])
    expected = {r['requirement_id']:r for r in context['requirement_coverage']}
    rows = payload['requirement_coverage']
    require({r['requirement_id'] for r in rows} == set(expected), 'Group loses or adds requirements')
    for row in rows:
        original = expected[row['requirement_id']]
        require(set(row['scenario_ids']) == set(original['scenario_ids']), 'Group changes requirement/scenario links')
        require(row['source'] == original['source'] and row['priority'] == original['priority'], 'Group changes confirmed source or priority')
    global_questions = {q['question_id']:q for q in context['open_questions']}
    for question in payload['open_questions']:
        if question['question_id'] in global_questions:
            require(question == global_questions[question['question_id']], 'Shared question requires a new baseline revision')
        else:
            require(re.fullmatch('Q-'+re.escape(group['group_id'])+'-[1-9][0-9]*',question['question_id']), 'New question needs the group namespace')
    require(set(global_questions) <= {q['question_id'] for q in payload['open_questions']}, 'Group drops a shared question')
    base = Path(group['result_path']).parent
    require(exchange.local_path(payload['plan_artifact'], base) == group['plan_path'], 'Group writes another plan path')
    if message['event'] in ('plan_ready','plan_partial'):
        require(exchange.local_path(payload['test_cases_artifact'], base) == group['catalog_path'], 'Group writes another catalog path')
    for ref in message.get('artifacts', []):
        if not ref.startswith(('http://','https://')):
            require(Path(exchange.local_path(ref.split('#',1)[0], base)).is_relative_to(base), 'Group artifact is outside its directory')


def verify_result(assignment, message):
    """Scope checks are part of submit/review, not a voluntary author self-check."""
    binding = assignment.get('plan_partition')
    if not binding:
        return set()
    if assignment['agent'] == 'qa-test-plan-writer' and message['event'] in PLAN_EVENTS:
        from qa_contract import validate_message
        errors = validate_message(message,assignment['task_id'],assignment['request_id'],assignment['agent'])
        require(not errors, '; '.join(errors))
    manifest, _, _ = load(binding['path'], binding['sha256'])
    paths = {binding['path']}
    if binding['mode'] == 'group':
        group = next(g for g in manifest['groups'] if g['group_id'] == binding['group_id'])
        paths.add(group['context_path'])
        if assignment['agent'] == 'qa-test-plan-writer' and message['event'] in PLAN_EVENTS:
            check_group(message, manifest, group)
    elif assignment['agent'] == 'qa-test-plan-writer' and message['event'] in PLAN_EVENTS:
        require(message['event'] == 'plan_ready', 'Integration requires complete reviewed groups')
        final = manifest['integration']; base = Path(final['result_path']).parent
        saved_proof = file_json(final['proof_path'])
        catalog, payload, proof = combined(manifest, binding['sha256'], saved_proof.get('group_request_ids'))
        require(set(assignment['depends_on']) >= set(proof['group_request_ids'].values()), 'Integration dependencies differ from selected revisions')
        require(exchange.local_path(message['payload'].get('partition_artifact'),base) == final['proof_path'], 'Integration needs its merge proof')
        require(file_json(final['proof_path']) == proof and file_json(final['catalog_path']) == catalog, 'Merged artifact changed after verification')
        from build_test_plan_html import regular_bytes
        require(digest(regular_bytes(final['catalog_path'])) == proof['catalog_sha256']
                and regular_bytes(final['proof_path']) == encoded(proof), 'Merged artifact bytes changed after verification')
        for key in sorted(payload.keys() - {'plan_artifact','test_cases_artifact','partition_artifact'}):
            require(message['payload'].get(key) == payload[key], 'Integration changes verified '+key)
        for key,target in [('plan_artifact','plan_path'),('test_cases_artifact','catalog_path')]:
            require(exchange.local_path(message['payload'][key],base) == final[target], 'Integration uses another artifact path')
        paths |= set(manifest['source_hashes']) | {final['proof_path']}
        for hashes in proof['group_result_hashes'].values(): paths |= set(hashes)
    return paths


def reviewed_results(state, request_id, result):
    from build_test_plan_html import Snapshot
    from content_review import check_required_reviews
    row = state['requests'][request_id]
    results = {request_id:result}
    for rid, review in state['requests'].items():
        if review['status'] == 'accepted' and review['assignment'].get('review_target',{}).get('request_id') == request_id:
            require(review['result']['event'] == 'review_passed', 'Unresolved blocking content review')
            snapshot = Snapshot(dict(review['result']['artifact_hashes']))
            for name in snapshot.hashes: snapshot.load(name)
            current = exchange.inspect_result(review['assignment'])
            require(current['artifact_hashes'] == review['result']['artifact_hashes'], 'Reviewer artifacts changed')
            results[rid] = current
    for cid in row['assignment']['criterion_ids']:
        definition = state['criteria'][cid]['definition']
        if definition.get('required_reviews'):
            bundles = None
            if definition.get('kind') == 'combination_coverage':
                from combination_coverage import check_criterion
                bundles = check_criterion(definition,state,results)
            check_required_reviews(definition,state,results,{request_id},bundles)
        if definition.get('required_review_agent'):
            require(row['owner_task_id'] and any(state['requests'][other]['assignment']['agent'] == definition['required_review_agent']
                    and state['requests'][other]['owner_task_id'] and state['requests'][other]['owner_task_id'] != row['owner_task_id']
                    for other in results if other != request_id), 'Required independent reviewer is missing')
    return results


def checked_group(manifest, group, manifest_sha, request_id):
    from build_test_plan_html import Snapshot
    revision_output(request_id,group['request_id'],group['result_path'])
    state = exchange.read_state(manifest['state_path']); row = state['requests'].get(request_id)
    require(row and row['status'] == 'accepted' and row.get('review',{}).get('status') == 'passed'
            and row['result']['event'] == 'plan_ready', 'Group is not completely accepted: '+group['group_id'])
    binding = row['assignment'].get('plan_partition', {})
    require(binding.get('mode') == 'group' and binding.get('sha256') == manifest_sha
            and binding.get('group_id') == group['group_id'], 'Group belongs to another partition revision')
    snapshot = Snapshot(dict(row['result']['artifact_hashes']))
    for name in snapshot.hashes: snapshot.load(name)
    result = exchange.inspect_result(row['assignment'])
    require(result['artifact_hashes'] == row['result']['artifact_hashes'], 'Accepted group artifacts changed')
    results = reviewed_results(state,request_id,result)
    message, _ = parse_message(snapshot.read(result['path']))
    catalog = read_json(snapshot.read(group['catalog_path']))
    check_group(message, manifest, group)
    return message, catalog, {path:sha for item in results.values() for path,sha in item['artifact_hashes'].items()}


def combined(manifest, manifest_sha, selected=None):
    baseline, _ = parse_message(Path(manifest['baseline_result_path']).read_text(encoding='utf-8'))
    records = []; cases = []; hashes = {}; questions = copy.deepcopy(baseline['payload']['open_questions'])
    selected = {g['group_id']:g['request_id'] for g in manifest['groups']} if selected is None else selected
    require(isinstance(selected,dict) and set(selected) == {g['group_id'] for g in manifest['groups']}, 'Explicit selection must include every group')
    known_questions = {q['question_id'] for q in questions}
    for group in manifest['groups']:
        message, catalog, verified_hashes = checked_group(manifest, group, manifest_sha, selected[group['group_id']])
        records.extend(message['payload']['requirement_coverage']); cases.extend(catalog['test_cases'])
        hashes[group['group_id']] = verified_hashes
        for question in message['payload']['open_questions']:
            if question['question_id'] not in known_questions:
                questions.append(question); known_questions.add(question['question_id'])
    catalog = dict(schema_version=1, test_cases=sorted(cases,key=lambda c:c['scenario_id']))
    errors = validate_catalog(catalog); require(not errors, '; '.join(errors))
    coverage = []
    for original in baseline['payload']['requirement_coverage']:
        row = copy.deepcopy(original)
        row['source'] = source_ref(row['source'],Path(manifest['baseline_artifact_base']))
        if row['status'] == 'blocked':
            raise ValueError('Global planning blocker remains: '+row['requirement_id'])
        if row['status'] != 'excluded':
            parts = [r for r in records if r['requirement_id'] == row['requirement_id']]
            require(parts and {sid for part in parts for sid in part['scenario_ids']} == set(row['scenario_ids']), 'Merged requirement coverage is incomplete')
            require(all(r['status'] == 'ready' for r in parts), 'Merged coverage is not ready')
            row.update(status='ready', verification='; '.join(dict.fromkeys(r['verification'] for r in parts)),
                       evidence='; '.join(dict.fromkeys(r['evidence'] for r in parts)))
            row.pop('reason',None)
        coverage.append(row)
    final = manifest['integration']
    payload = dict(base_revision=manifest['base_revision'], plan_revision=manifest['plan_revision'],
        plan_artifact=final['plan_path'], test_cases_artifact=final['catalog_path'], partition_artifact=final['proof_path'],
        requirement_coverage=coverage, open_questions=questions)
    for key,value in baseline['payload'].items():
        if key not in payload:
            payload[key] = copy.deepcopy(value)
    if payload.get('combination_coverage'):
        for spec in payload['combination_coverage']:
            for key in ('model_artifact','combinations_artifact'):
                spec[key] = exchange.local_path(spec[key],manifest['baseline_artifact_base'])
            spec['catalog_artifact'] = final['catalog_path']
    require(not (errors := validate_plan(payload,'plan_ready',catalog)), '; '.join(errors))
    proof = dict(schema_version=1,kind='test_plan_merge',partition_sha256=manifest_sha,
        base_revision=manifest['base_revision'],plan_revision=manifest['plan_revision'],
        source_hashes=manifest['source_hashes'],group_result_hashes=hashes,group_request_ids=selected,
        catalog_sha256=digest(encoded(catalog)),coverage_sha256=digest(encoded(coverage)),
        structural_only=True, final_content_review_required=True)
    return catalog, payload, proof


def build(state_path, baseline_request, specification, output):
    from build_test_plan_html import MAX_SOURCE_BYTES, Snapshot, write_once
    state_path = Path(state_path).resolve(); output = Path(output).resolve()
    require(not output.exists(), 'Partition output directory already exists; preserve previous revisions')
    state = exchange.read_state(state_path); row = state['requests'].get(baseline_request)
    require(row and row['status'] == 'accepted' and row.get('review',{}).get('status') == 'passed', 'Baseline scope must be accepted first')
    require(row['assignment'].get('plan_partition',{}).get('mode') != 'group', 'A group cannot be the global partition baseline')
    snapshot = Snapshot(dict(row['result']['artifact_hashes']))
    for name in snapshot.hashes: snapshot.load(name)
    current = exchange.inspect_result(row['assignment'])
    require(current['artifact_hashes'] == row['result']['artifact_hashes'], 'Baseline artifacts changed')
    reviewed_results(state,baseline_request,current)
    baseline, _ = parse_message(snapshot.read(current['path']))
    require(baseline['agent'] == 'qa-test-plan-writer' and baseline['event'] in PLAN_EVENTS, 'Partition needs a common test-plan scope')
    require(isinstance(specification,dict) and type(specification.get('schema_version')) is int
            and specification['schema_version'] == 1, 'Invalid partition specification')
    require(set(specification) <= {'schema_version','plan_revision','integration_request_id','integration_criterion_ids','shared_context_paths','groups'},
            'Unknown partition specification fields')
    revision = exchange.required(specification.get('plan_revision'),'plan_revision')
    manifest = dict(schema_version=1,kind='test_plan_partition',task_id=state['task_id'],state_path=str(state_path),
        baseline_request_id=baseline_request,baseline_result_path=current['path'],baseline_artifact_base=row['assignment']['artifact_base'],
        base_revision=baseline['payload']['plan_revision'],plan_revision=revision,source_hashes=current['artifact_hashes'],
        baseline_assignment_sha256=digest(encoded(row['assignment'])),criterion_hashes={},
        shared_context_paths=[exchange.local_path(p,Path(current['path']).parent)
                              for p in ids(specification.get('shared_context_paths',[]),'shared_context_paths')],groups=[],
        integration=dict(request_id=exchange.required(specification.get('integration_request_id'),'integration_request_id'),
            criterion_ids=ids(specification.get('integration_criterion_ids',row['assignment']['criterion_ids']),'integration_criterion_ids'),
            **{key:str(output/'integration'/name) for key,name in [('result_path','result.json'),('plan_path','plan.md'),
                ('catalog_path','catalog.json'),('proof_path','partition-merge.json'),('payload_path','payload.json')]}))
    specs = specification.get('groups'); require(isinstance(specs,list) and bool(specs), 'Missing author groups')
    contexts = {}
    for spec in specs:
        require(isinstance(spec,dict), 'Group specification must be an object')
        require(set(spec) <= {'group_id','request_id','test_type','scenario_ids','depends_on','criterion_ids','context_paths'},
                'Unknown group specification fields')
        name = exchange.required(spec.get('group_id'),'group_id')
        require(re.fullmatch('[a-z][a-z0-9-]{0,47}',name), 'Invalid group directory')
        assigned = ids(spec.get('scenario_ids'),'scenario_ids',True)
        group = dict(group_id=name,request_id=exchange.required(spec.get('request_id'),'request_id'),test_type=spec.get('test_type'),
            scenario_ids=assigned,depends_on=ids(spec.get('depends_on',[]),'depends_on'),
            criterion_ids=ids(spec.get('criterion_ids',[]),'criterion_ids'),
            context_paths=[exchange.local_path(p,Path(current['path']).parent) for p in ids(spec.get('context_paths',[]),'context_paths')],
            requirement_ids=[r['requirement_id'] for r in baseline['payload']['requirement_coverage'] if set(r['scenario_ids']) & set(assigned)],
            **{key:str(output/name/file) for key,file in [('result_path','result.json'),('plan_path','plan.md'),
                ('catalog_path','catalog.json'),('context_path','context.json')]})
        context = encoded(group_context(manifest,group,baseline,snapshot)); group['context_sha256'] = digest(context)
        require(len(context) <= MAX_SOURCE_BYTES, 'Group context is too large; split its scope explicitly')
        contexts[name] = context; manifest['groups'].append(group)
    structural_manifest(manifest)
    wanted = {sid for r in baseline['payload']['requirement_coverage'] if r['status'] in ('planned','ready') for sid in r['scenario_ids']}
    require({sid for g in manifest['groups'] for sid in g['scenario_ids']} == wanted, 'Every selected scenario needs exactly one group')
    require(all(set(g['criterion_ids']) <= set(state.get('criteria',{})) for g in manifest['groups']), 'Unknown group task criteria')
    final_criteria = set(manifest['integration']['criterion_ids'])
    require(set(row['assignment']['criterion_ids']) <= final_criteria <= set(state.get('criteria',{})), 'Integration criteria lose the baseline or are unknown')
    needed_criteria = final_criteria | {cid for group in manifest['groups'] for cid in group['criterion_ids']}
    manifest['criterion_hashes'] = {cid:digest(encoded(state['criteria'][cid]['definition'])) for cid in sorted(needed_criteria)}
    require(not ({g['request_id'] for g in manifest['groups']} | {manifest['integration']['request_id']}) & set(state['requests']),
            'Partition request ID is already registered')
    snapshot.verify(); manifest_path = output/'partition.json'; manifest_raw = encoded(manifest)
    output.mkdir(parents=True,exist_ok=False)
    for group in manifest['groups']: write_once(Path(group['context_path']),contexts[group['group_id']])
    write_once(manifest_path,manifest_raw)
    by_name = {g['group_id']:g['request_id'] for g in manifest['groups']}
    for group in manifest['groups']:
        assignment = dict(task_id=state['task_id'],request_id=group['request_id'],agent='qa-test-plan-writer',
            stage='planning-'+group['group_id'],goal='Детализировать только назначенную группу '+group['group_id'],
            base_revision=manifest['base_revision'],plan_revision=revision,test_type=group['test_type'],
            acceptance_criteria=['Сохранить назначенные ID и связи требований','Понятные шаги/данные по первичным источникам',
                                 'Не менять общие решения и чужие файлы'],criterion_ids=group['criterion_ids'],
            scenario_ids=group['scenario_ids'],stop_condition='Отчёт своей группы или конкретный блокер',expected_artifact='QA JSON',
            result_path=group['result_path'],inputs=[str(manifest_path),group['context_path']],
            depends_on=[by_name[name] for name in group['depends_on']],
            plan_partition=dict(path=str(manifest_path),sha256=digest(manifest_raw),mode='group',group_id=group['group_id']))
        write_once(Path(group['result_path']).with_name('assignment.json'),encoded(assignment))
    final = manifest['integration']
    assignment = dict(task_id=state['task_id'],request_id=final['request_id'],agent='qa-test-plan-writer',stage='planning-integration',
        goal='Проверить совместимость групп и подготовить общий план без потери покрытия',base_revision=manifest['base_revision'],
        plan_revision=revision,acceptance_criteria=row['assignment']['acceptance_criteria'],criterion_ids=final['criterion_ids'],
        scenario_ids=sorted(wanted),stop_condition='Итоговый план для независимого ревью',expected_artifact='QA JSON',
        result_path=final['result_path'],inputs=[str(manifest_path),final['catalog_path'],final['proof_path']],
        depends_on=[g['request_id'] for g in manifest['groups']],plan_partition=dict(path=str(manifest_path),sha256=digest(manifest_raw),mode='integration'))
    write_once(Path(final['result_path']).with_name('assignment-template.json'),encoded(assignment))
    return dict(partition_path=str(manifest_path),partition_sha256=digest(manifest_raw),group_count=len(manifest['groups']))


def merge(path, selected=None):
    from build_test_plan_html import write_once
    manifest, _, sha = load(path)
    catalog,payload,proof = combined(manifest,sha,selected)
    load(path,sha)  # No source/group revision may change while composing the merge.
    for hashes in proof['group_result_hashes'].values():
        for name, expected in hashes.items():
            require(exchange.fingerprint(name) == expected, 'Group changed during merge')
    final = manifest['integration']
    assignment = file_json(Path(final['result_path']).with_name('assignment-template.json'))
    require(assignment.get('criterion_ids') == final['criterion_ids'] and assignment.get('plan_partition') ==
            dict(path=str(Path(path).resolve()),sha256=sha,mode='integration'), 'Integration assignment template changed')
    assignment['depends_on'] = [proof['group_request_ids'][group['group_id']] for group in manifest['groups']]
    for name,value in [('catalog_path',catalog),('proof_path',proof),('payload_path',payload)]:
        write_once(Path(final[name]),encoded(value))
    write_once(Path(final['result_path']).with_name('assignment.json'),encoded(assignment))
    return dict(status='merged',catalog_path=final['catalog_path'],payload_path=final['payload_path'],
                proof_path=final['proof_path'],case_count=len(catalog['test_cases']),final_content_review_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    create = sub.add_parser('build'); create.add_argument('--state',type=Path,required=True)
    create.add_argument('--baseline-request-id',required=True); create.add_argument('--groups',type=Path,required=True)
    create.add_argument('--output-dir',type=Path,required=True)
    for name in ('check','merge'):
        command = sub.add_parser(name); command.add_argument('--partition',type=Path,required=True)
        if name == 'merge': command.add_argument('--group-results',type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'build': result = build(args.state,args.baseline_request_id,file_json(args.groups),args.output_dir)
        elif args.command == 'merge': result = merge(args.partition, file_json(args.group_results) if args.group_results else None)
        else:
            manifest,_,sha = load(args.partition)
            result = dict(status='valid',partition_sha256=sha,group_count=len(manifest['groups']))
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (OSError,ValueError,UnicodeError,RecursionError,KeyError,TypeError,StopIteration) as exc:
        print('Test-plan partition error: '+str(exc)); return 2


if __name__ == '__main__':
    raise SystemExit(main())
