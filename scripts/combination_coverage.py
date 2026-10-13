"""Concrete design coverage bindings. Semantic truth remains an independent review."""
import json
import re
from pathlib import Path


def text(value):
    return isinstance(value, str) and bool(value.strip())


def values_valid(values, factors):
    return (isinstance(values, dict) and set(values) == set(factors) and
            all(isinstance(v, str) and v in factors[k] for k, v in values.items()))


def validate_case_parameters(case):
    """Optional extension; ordinary case catalogs stay compatible."""
    errors = []
    if 'combination_values' in case:
        values = case['combination_values']
        if not isinstance(values, dict) or not values or any(not text(k) or not text(v) for k, v in values.items()):
            errors.append('combination_values needs named string values')
    if 'parameter_sets' in case:
        if 'combination_values' in case:
            errors.append('use combination_values or parameter_sets, not both')
        sets = case['parameter_sets']
        if not isinstance(sets, list) or not sets:
            return errors + ['parameter_sets must be a non-empty list']
        seen = set()
        for item in sets:
            if not isinstance(item, dict):
                errors.append('parameter set must be an object')
                continue
            identifier = item.get('parameter_set_id')
            if not text(identifier) or identifier in seen:
                errors.append('parameter_sets need unique parameter_set_id')
            else:
                seen.add(identifier)
            values = item.get('values')
            if not isinstance(values, dict) or not values or any(not text(k) or not text(v) for k, v in values.items()):
                errors.append('parameter set needs named string values')
                values = {}
            parameters = item.get('parameters', {})
            if (not isinstance(parameters, dict) or any(not text(k) or not text(v) for k, v in parameters.items())
                    or set(parameters) & set(values)):
                errors.append('parameters need string values and cannot override factors')
    return errors


def case_text(case, ref, expected=False):
    """Only concrete preparation/action or an observable step assertion can be cited."""
    if not isinstance(ref, str):
        raise ValueError('case reference must be a JSON pointer')
    condition = re.fullmatch(r'/preconditions/(0|[1-9][0-9]*)', ref)
    step = re.fullmatch(r'/steps/(0|[1-9][0-9]*)/(name|expected_result)', ref)
    if expected and step and step[2] == 'expected_result':
        return case['steps'][int(step[1])]['expected_result']
    if not expected and condition:
        return case['preconditions'][int(condition[1])]
    if not expected and step and step[2] == 'name':
        return case['steps'][int(step[1])]['name']
    raise ValueError('reference must point to preparation/action or expected_result, respectively')


def materialize(value, parameters):
    if '{{{' in value or '}}}' in value:
        raise ValueError('malformed parameter template')
    def replace(match):
        name = match[1]
        if name not in parameters:
            raise ValueError(f'unresolved parameter: {name}')
        return parameters[name]
    rendered = re.sub(r'\{\{([^{}]+)\}\}', replace, value)
    if '{{' in rendered or '}}' in rendered:
        raise ValueError('unresolved or malformed parameter template')
    return rendered


def check_bindings(model, report, catalog):
    errors = []
    factors = model['factors']
    rules = model.get('behavior_rules')
    if not isinstance(rules, list) or not rules:
        return ['ready coverage needs behavior_rules with sourced expectations']
    indexed_rules = {}
    for rule in rules:
        if not isinstance(rule, dict):
            errors.append('behavior rule must be an object')
            continue
        rid = rule.get('rule_id')
        conditions = rule.get('conditions')
        if (not text(rid) or rid in indexed_rules or not text(rule.get('requirement_id'))
                or rule.get('status') not in ('confirmed', 'inferred', 'unknown')
                or not isinstance(conditions, dict)
                or any(k not in factors or not isinstance(v, str) or v not in factors[k] for k, v in conditions.items())):
            errors.append('behavior rule needs unique ID, requirement, status and valid conditions')
            continue
        if rule['status'] == 'confirmed' and (not text(rule.get('source')) or not text(rule.get('expected_result'))):
            errors.append(f'{rid}: confirmed rule needs source and expected_result')
        indexed_rules[rid] = rule
    if errors:
        return errors
    cases = {case['scenario_id']: case for case in catalog['test_cases']}
    seen = set()
    exercised_rules = set()
    for row in report['rows']:
        prefix = row['combination_id']
        applicable = {rid for rid, rule in indexed_rules.items()
                      if all(row['values'][k] == v for k, v in rule['conditions'].items())}
        exercised_rules.update(applicable)
        if not applicable:
            errors.append(f'{prefix}: no applicable behavior rule')
        if any(indexed_rules[rid]['status'] != 'confirmed' for rid in applicable):
            errors.append(f'{prefix}: applicable expectation is inferred/unknown; keep coverage blocked')
        bindings = row.get('scenario_bindings')
        if not isinstance(bindings, list) or not bindings:
            errors.append(f'{prefix}: scenario_bindings are required; TC-ID alone does not prove coverage')
            continue
        linked, covered_rules = set(), set()
        for binding in bindings:
            try:
                if not isinstance(binding, dict) or not text(binding.get('binding_id')):
                    raise ValueError('binding needs binding_id')
                bid = binding['binding_id']
                if bid in seen:
                    raise ValueError('duplicate binding_id')
                seen.add(bid)
                case = cases.get(binding.get('scenario_id'))
                if case is None:
                    raise ValueError('binding needs an existing scenario_id')
                linked.add(case['scenario_id'])
                rid = binding.get('rule_id')
                if rid not in applicable:
                    raise ValueError('binding rule does not apply to row conditions')
                covered_rules.add(rid)
                pid = binding.get('parameter_set_id')
                if 'parameter_sets' in case:
                    selected = [p for p in case['parameter_sets'] if p['parameter_set_id'] == pid]
                    if len(selected) != 1:
                        raise ValueError('binding must select an existing parameter_set_id')
                    values = selected[0]['values']
                    parameters = dict(selected[0].get('parameters', {}), **values)
                else:
                    if pid is not None:
                        raise ValueError('static case cannot select parameter_set_id')
                    values = case.get('combination_values')
                    parameters = {}
                if not values_valid(values, factors) or values != row['values']:
                    raise ValueError('case/parameter values differ from the complete row assignment')
                for value in case['preconditions'] + [s[k] for s in case['steps'] for k in ('name', 'expected_result')]:
                    materialize(value, parameters)
                refs = binding.get('factor_refs')
                if not isinstance(refs, dict) or set(refs) != set(factors):
                    raise ValueError('factor_refs must cover every factor')
                for factor, ref in refs.items():
                    value = case_text(case, ref)
                    if pid is not None:
                        if '{{' + factor + '}}' not in value:
                            raise ValueError(f'{factor}: parameter is not used at its preparation/action reference')
                        materialize(value, parameters)
                    else:
                        # This is an explicit textual anchor, not a semantic oracle.
                        if not re.search(r'(?<![\w-])' + re.escape(values[factor]) + r'(?![\w-])', value):
                            raise ValueError(f'{factor}: value is absent from its preparation/action reference')
                        materialize(value, parameters)
                expected_refs = binding.get('expected_refs')
                if (not isinstance(expected_refs, list) or not expected_refs or
                        any(not text(ref) for ref in expected_refs) or len(set(expected_refs)) != len(expected_refs)):
                    raise ValueError('expected_refs needs unique step assertion pointers')
                for ref in expected_refs:
                    materialize(case_text(case, ref, expected=True), parameters)
            except (ValueError, IndexError, KeyError, TypeError) as exc:
                errors.append(f'{prefix}: {exc}')
        if linked != set(row['scenario_ids']):
            errors.append(f'{prefix}: scenario_ids differ from bound cases')
        if covered_rules != applicable:
            errors.append(f'{prefix}: bindings must cover every applicable behavior rule')
    if set(indexed_rules) - exercised_rules:
        errors.append(f'behavior rules have no covered row; add mandatory seeds or resolve scope: {sorted(set(indexed_rules) - exercised_rules)}')
    return errors


def bundle_specs(payload):
    specs = payload.get('combination_coverage', [])
    if not isinstance(specs, list):
        raise ValueError('combination_coverage must be a list')
    seen = set()
    for spec in specs:
        if (not isinstance(spec, dict) or not text(spec.get('bundle_id')) or spec['bundle_id'] in seen
                or any(not text(spec.get(k)) for k in ('model_artifact', 'combinations_artifact', 'catalog_artifact'))):
            raise ValueError('combination bundle needs unique bundle_id and three local artifact paths')
        seen.add(spec['bundle_id'])
    return specs


def local_file(ref, base):
    if ref.startswith(('http://', 'https://')):
        raise ValueError('combination artifact/source needs a local snapshot')
    path = Path(ref.split('#', 1)[0])
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_bundle(spec, base):
    from generate_combinations import check
    paths = {key: local_file(spec[key], base) for key in
             ('model_artifact', 'combinations_artifact', 'catalog_artifact')}
    data = {key: json.loads(path.read_text(encoding='utf-8')) for key, path in paths.items()}
    errors = check(data['model_artifact'], data['combinations_artifact'], catalog=data['catalog_artifact'])
    if errors:
        raise ValueError('combination coverage: ' + '; '.join(errors))
    # Sources are versioned with the bound result. A URL can be retained inside
    # the snapshot; a mutable URL by itself cannot be reviewed as exact bytes.
    sources = [r['source'] for r in data['model_artifact']['behavior_rules'] if r['status'] == 'confirmed']
    scope_sources = data['model_artifact'].get('scope_sources', [])
    if not isinstance(scope_sources, list):
        raise ValueError('scope_sources must be a list')
    sources += scope_sources
    for source in sources:
        if not text(source):
            raise ValueError('combination scope source must be a local reference')
        source_path = local_file(source, paths['model_artifact'].parent)
        if not source_path.is_file() or not source_path.read_text(encoding='utf-8').strip():
            raise ValueError(f'combination source is missing or empty: {source}')
        paths['source:' + source] = source_path
    return dict(spec=spec, paths=paths, model=data['model_artifact'], report=data['combinations_artifact'])


def load_bundles(payload, base):
    bundles = [load_bundle(spec, base) for spec in bundle_specs(payload)]
    if bundles and 'test_cases_artifact' in payload:
        catalog = local_file(payload['test_cases_artifact'], base)
        if any(b['paths']['catalog_artifact'] != catalog for b in bundles):
            raise ValueError('combination bundle must use the plan test_cases_artifact')
    return bundles


def binding_keys(bundle):
    return {(bundle['spec']['bundle_id'], b['binding_id']) for row in bundle['report']['rows']
            for b in row['scenario_bindings']}


def artifact_paths(payload, base):
    return {str(path) for bundle in load_bundles(payload, base) for path in bundle['paths'].values()}


def review_covers(payload, bundle):
    checks = [c for c in payload.get('combination_checks', []) if c['bundle_id'] == bundle['spec']['bundle_id']]
    return ({(c['bundle_id'], c['binding_id']) for c in checks} == binding_keys(bundle)
            and all(c['status'] == 'passed' for c in checks))


def check_criterion(definition, state, results):
    from qa_contract import parse_message
    from generate_combinations import fingerprint
    selected = {}
    for rid, result in results.items():
        if state['requests'][rid]['assignment'].get('review_target'):
            continue
        message, _ = parse_message(Path(result['path']).read_text(encoding='utf-8'))
        for bundle in load_bundles(message['payload'], Path(result['path']).parent):
            if bundle['spec']['bundle_id'] == definition['combination_bundle_id']:
                if fingerprint(bundle['model']) != definition['model_sha256']:
                    raise ValueError('Combination model differs from the agreed criterion scope')
                selected[rid] = bundle
    if len(selected) != 1:
        raise ValueError('combination criterion needs exactly one selected author bundle')
    return selected
