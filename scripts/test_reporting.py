"""Assemble scoped test facts; never execute product actions or judge evidence content."""
from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def run_digest(run):
    return hashlib.sha256(canonical({key: value for key, value in run.items() if key != 'review'}).encode('utf-8')).hexdigest()


def timestamp(value):
    require(text(value), 'timestamp must be an ISO datetime with timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'timestamp must include timezone')
    return parsed


def strings(value, name, nonempty=False):
    require(isinstance(value, list) and all(text(v) for v in value), f'{name} must be a string list')
    require(len(value) == len(set(value)), f'{name} contains duplicate values')
    require(not nonempty or bool(value), f'{name} must not be empty')
    return value


class Sources:
    def __init__(self):
        self.files = {}
        self.remote = set()

    def reference(self, ref, base):
        require(text(ref), 'source reference must not be empty')
        if ref.startswith(('https://', 'http://')):
            self.remote.add(ref)
            return ref, False
        filename, marker, fragment = ref.partition('#')
        require(bool(filename), 'local source reference needs a file path')
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if path not in self.files:
            data = path.read_bytes()
            require(bool(data), f'empty source artifact: {path}')
            self.files[path] = data
        return str(path) + (marker + fragment if marker else ''), True

    def load(self, path):
        ref, _ = self.reference(str(Path(path).resolve()), Path.cwd())
        return json.loads(self.files[Path(ref)].decode('utf-8'))

    def digest(self, path):
        return hashlib.sha256(self.files[Path(path)]).hexdigest()

    def manifest(self):
        # Detect a source being changed while the report was assembled.
        for path, data in self.files.items():
            require(path.read_bytes() == data, f'source changed during assembly: {path}')
        return [{'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}
                for path, data in sorted(self.files.items())]


def validate_scope(scope):
    require(isinstance(scope, dict) and type(scope.get('schema_version')) is int
            and scope['schema_version'] == 1, 'scope schema_version must equal 1')
    canonical(scope)
    for key in ('task_id', 'scope_revision', 'feature'):
        require(text(scope.get(key)), f'scope.{key} must not be empty')
    require(scope.get('scope_kind') in ('synthetic', 'product'), 'scope_kind must be synthetic or product')
    indexes = {}
    for collection, id_key in (('configurations', 'configuration_id'), ('checks', 'check_id'),
                               ('business_cases', 'business_case_id')):
        items = scope.get(collection)
        require(isinstance(items, list) and 0 < len(items) <= 10000, f'invalid scope.{collection}')
        index = {}
        for item in items:
            require(isinstance(item, dict) and text(item.get(id_key)), f'{collection} needs {id_key}')
            require(item[id_key] not in index, f'duplicate {id_key}: {item[id_key]}')
            index[item[id_key]] = item
        indexes[collection] = index
    for config in scope['configurations']:
        for key in ('environment_id', 'product_version', 'requirements_revision'):
            require(text(config.get(key)), f'configuration needs {key}')
        for key in ('context', 'feature_flags'):
            require(isinstance(config.get(key), dict) and all(text(k) for k in config[key]), f'invalid {key}')
            require(all(v is not None for v in config[key].values()), f'planned {key} cannot contain unknown values')
    pairs = set()
    for check in scope['checks']:
        require(text(check.get('scenario_id')) and text(check.get('requirement_source')), 'check needs scenario_id and requirement_source')
        require(type(check.get('required')) is bool, 'check.required must be boolean')
        require(text(check.get('configuration_id')) and check['configuration_id'] in indexes['configurations'], 'unknown check configuration')
        if 'intent' in check:
            intent = check['intent']
            require(isinstance(intent, dict) and all(text(intent.get(key)) for key in
                    ('action', 'business_rule', 'expected_effect')),
                    'check.intent needs action, business_rule and expected_effect from requirement_source')
        pair = (check['scenario_id'], check['configuration_id'])
        require(pair not in pairs, 'duplicate scenario/configuration; reuse check_id across business cases')
        pairs.add(pair)
    used = set()
    for case in scope['business_cases']:
        require(text(case.get('title')) and text(case.get('source')), 'business case needs title and source')
        if 'business_goal' in case:
            require(text(case['business_goal']), 'business_goal must be a non-empty string from case.source')
        for identifier in strings(case.get('check_ids'), 'business case check_ids', True):
            require(identifier in indexes['checks'], f'unknown check_id: {identifier}')
            used.add(identifier)
        require(any(indexes['checks'][identifier]['required'] for identifier in case['check_ids']), 'business case needs an explicit required check')
    require(used == set(indexes['checks']), 'every check must belong to a business case')
    return indexes


def build_report(scope_path, runs_path, revision, generated_at=None):
    # Import here to keep the reporting artifact independent of the QA message envelope.
    from qa_contract import parse_message, validate_message

    scope_path, runs_path = Path(scope_path).resolve(), Path(runs_path).resolve()
    require(text(revision), 'report revision must not be empty')
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    cutoff = timestamp(generated_at)
    sources = Sources()
    scope, manifest = sources.load(scope_path), sources.load(runs_path)
    indexes = validate_scope(scope)
    require(isinstance(manifest, dict) and type(manifest.get('schema_version')) is int
            and manifest['schema_version'] == 1, 'runs schema_version must equal 1')
    canonical(manifest)
    require(manifest.get('task_id') == scope['task_id'], 'runs task_id differs from scope')
    runs = manifest.get('runs')
    require(isinstance(runs, list) and len(runs) <= 1000, 'runs must be a list of at most 1000 runs')
    check_sources = {check['check_id']: sources.reference(check['requirement_source'], scope_path.parent)[0]
                     for check in scope['checks']}
    pair_index = {(check['scenario_id'], check['configuration_id']): check for check in scope['checks']}
    business_sources = {case['business_case_id']: sources.reference(case['source'], scope_path.parent)[0]
                        for case in scope['business_cases']}
    indexed_runs, requests = {}, set()
    candidates = {identifier: [] for identifier in indexes['checks']}
    out_of_scope, defects, observed_results = [], [], 0
    for run in runs:
        require(isinstance(run, dict) and text(run.get('run_id')), 'run needs run_id')
        run_id = run['run_id']
        require(run_id not in indexed_runs, f'duplicate run_id: {run_id}')
        config_id = run.get('configuration_id')
        require(text(config_id) and config_id in indexes['configurations'], 'run has unknown configuration_id')
        observed = timestamp(run.get('observed_at'))
        require(observed <= cutoff, 'run observed_at is later than report time')
        assigned = strings(run.get('scenario_ids'), 'run.scenario_ids', True)
        result_ref, local = sources.reference(run.get('result_artifact'), runs_path.parent)
        require(local and '#' not in result_ref, 'result_artifact must be a local JSON file')
        message, _ = parse_message(sources.files[Path(result_ref)].decode('utf-8'))
        require(message.get('event') in ('feature_test_complete', 'blocked'), 'reporting needs feature execution results, not plans or generated tests')
        errors = validate_message(message, scope['task_id'], message.get('request_id'), 'qa-feature-tester', assigned)
        require(not errors, 'invalid execution result: ' + '; '.join(errors))
        request_id = message['request_id']
        require(text(request_id) and request_id not in requests, 'duplicate or empty request_id in runs')
        requests.add(request_id)
        review = run.get('review')
        require(isinstance(review, dict) and isinstance(review.get('status'), str)
                and review['status'] in ('pending', 'changes_requested', 'passed'), 'invalid run.review.status')
        review_ref = None
        assessment = None
        if review['status'] != 'pending':
            review_ref, review_local = sources.reference(review.get('source'), runs_path.parent)
            require(review_local and '#' not in review_ref, 'recorded review needs a local JSON source artifact')
            assessment = sources.load(review_ref)
            require(isinstance(assessment, dict) and type(assessment.get('schema_version')) is int
                    and assessment['schema_version'] == 1, 'invalid recorded review schema')
            require(assessment.get('task_id') == scope['task_id'] and assessment.get('request_id') == request_id,
                    'recorded review refers to another assignment')
            require(assessment.get('decision') == review['status'] and text(assessment.get('reviewer'))
                    and assessment['reviewer'] not in ('qa-feature-tester', 'qa-test-report-writer'), 'invalid recorded review decision/reviewer')
            require(assessment.get('result_sha256') == sources.digest(result_ref), 'review refers to a different result revision')
            require(assessment.get('execution_context_sha256') == run_digest(run), 'review refers to different execution metadata')
            checks = assessment.get('checks')
            require(isinstance(checks, dict), 'recorded review needs checks')
            for key in ('expected_source', 'actual_execution', 'configuration_context', 'evidence_support'):
                entry = checks.get(key)
                require(isinstance(entry, dict) and isinstance(entry.get('status'), str)
                        and entry['status'] in ('passed', 'failed', 'unknown') and text(entry.get('note')),
                        'invalid recorded review check: ' + key)
                require(review['status'] != 'passed' or entry['status'] == 'passed', 'passed review has unchecked/failed criteria')
        context = run.get('context', {})
        flags = run.get('feature_flags', {})
        require(isinstance(context, dict) and isinstance(flags, dict), 'run context and feature_flags must be objects')
        context_refs = [sources.reference(ref, runs_path.parent)
                        for ref in strings(run.get('configuration_evidence', []), 'configuration_evidence')]
        run_sources = {ref.partition('#')[0] for ref, local in context_refs if local}
        resolved_flags = {}
        for key, flag in flags.items():
            require(text(key) and isinstance(flag, dict) and isinstance(flag.get('status'), str)
                    and flag['status'] in ('confirmed', 'unknown', 'fallback'), 'invalid observed feature flag')
            if flag['status'] == 'unknown':
                require(flag.get('value') is None, 'unknown flag value must be null')
            else:
                require('value' in flag and flag['value'] is not None, 'observed flag needs value')
            flag_ref, flag_local = (None, False)
            if flag.get('source') is not None:
                flag_ref, flag_local = sources.reference(flag['source'], runs_path.parent)
            if flag['status'] == 'confirmed':
                require(flag_ref is not None, 'confirmed flag needs a source')
            if flag_local:
                run_sources.add(flag_ref.partition('#')[0])
            resolved_flags[key] = dict(flag, source=flag_ref, source_local=flag_local)
        parent = run.get('supersedes')
        require(parent is None or text(parent), 'supersedes must be a run_id or null')
        indexed_runs[run_id] = dict(run_id=run_id, configuration_id=config_id, observed_at=run['observed_at'],
                                   supersedes=parent, result_artifact=result_ref, request_id=request_id,
                                   review_status=review['status'], review_source=review_ref,
                                   requirements_revision=run.get('requirements_revision'), context=context,
                                   configuration_evidence=[ref for ref, _ in context_refs], feature_flags=resolved_flags,
                                   result_sha256=sources.digest(result_ref), execution_context_sha256=run_digest(run),
                                   observed_targets=[dict(environment_id=env, product_version=version) for env, version in sorted(
                                       {(r['environment_id'], r['product_version']) for r in message['payload']['scenario_results']})])
        target = indexes['configurations'][config_id]
        observed_results += len(message['payload']['scenario_results'])
        require(observed_results <= 50000, 'too many result rows; split the reporting scope')
        for result in message['payload']['scenario_results']:
            evidence_refs = [sources.reference(ref, Path(result_ref).parent) for ref in result['evidence']]
            evidence = [ref for ref, _ in evidence_refs]
            expected_source, requirement_local = sources.reference(result['expected_source'], Path(result_ref).parent)
            run_sources.update(ref.partition('#')[0] for ref, local in evidence_refs if local)
            if requirement_local:
                run_sources.add(expected_source.partition('#')[0])
            check = pair_index.get((result['scenario_id'], config_id))
            if check is None:
                out_of_scope.append(dict(run_id=run_id, result=dict(result, evidence=evidence)))
                continue
            mismatch, unknown = [], []
            for key in ('environment_id', 'product_version'):
                if result[key] != target[key]:
                    mismatch.append(key)
            if not text(run.get('requirements_revision')):
                unknown.append('requirements_revision')
            elif run['requirements_revision'] != target['requirements_revision']:
                mismatch.append('requirements_revision')
            if expected_source != check_sources[check['check_id']]:
                mismatch.append('requirement_source')
            for key, value in target['context'].items():
                if key not in context or context[key] is None:
                    unknown.append('context.' + key)
                elif canonical(context[key]) != canonical(value):
                    mismatch.append('context.' + key)
                if not context_refs or not all(local for _, local in context_refs):
                    unknown.append('context_evidence.' + key)
            for key, value in target['feature_flags'].items():
                flag = resolved_flags.get(key)
                if not flag or flag['status'] != 'confirmed' or not flag['source_local']:
                    unknown.append('feature_flags.' + key)
                elif canonical(flag['value']) != canonical(value):
                    mismatch.append('feature_flags.' + key)
            applicability = 'configuration_mismatch' if mismatch else ('configuration_unknown' if unknown else 'confirmed')
            candidates[check['check_id']].append(dict(run_id=run_id, status=result['status'],
                coverage_status=applicability, reasons=sorted(set(mismatch + unknown)),
                source_integrity='local_verified' if requirement_local and all(local for _, local in evidence_refs) else 'remote_unverified',
                review_status=review['status'], result_artifact=result_ref,
                result=dict(result, expected_source=expected_source, evidence=evidence)))
        fingerprints = {ref: sources.digest(ref) for ref in sorted(run_sources)}
        indexed_runs[run_id]['artifact_sha256'] = fingerprints
        if assessment is not None:
            recorded = assessment.get('artifact_sha256')
            require(isinstance(recorded, dict), 'recorded review needs artifact_sha256')
            normalized = {}
            for ref, expected_hash in recorded.items():
                resolved, local = sources.reference(ref, Path(review_ref).parent)
                require(local and '#' not in resolved and resolved not in normalized, 'invalid review artifact reference')
                require(expected_hash == sources.digest(resolved), 'reviewed evidence/source artifact changed')
                normalized[resolved] = expected_hash
            require(all(normalized.get(ref) == digest for ref, digest in fingerprints.items()), 'review does not cover source/evidence fingerprints')
        defects.extend({'run_id': run_id, 'result_artifact': result_ref, 'detail': defect}
                       for defect in message['payload']['defects'])
    ancestors = {}
    for run_id, run in indexed_runs.items():
        seen, current = set(), run
        while current['supersedes'] is not None:
            parent = current['supersedes']
            require(parent in indexed_runs and parent not in seen and parent != run_id, 'unknown or cyclic supersedes')
            earlier = indexed_runs[parent]
            require(earlier['configuration_id'] == current['configuration_id'], 'supersedes crosses configurations')
            require(timestamp(earlier['observed_at']) <= timestamp(current['observed_at']), 'supersedes reverses observation time')
            seen.add(parent)
            current = earlier
        ancestors[run_id] = seen
    rows = []
    for check in scope['checks']:
        history = candidates[check['check_id']]
        heads = [entry for entry in history if not any(entry['run_id'] in ancestors[other['run_id']] for other in history)]
        row = dict(check, requirement_source=check_sources[check['check_id']], history=history)
        row.update(selected_run_id=None, execution_status='not_run', coverage_status='not_run', review_status='pending', result=None, reasons=[], source_integrity='not_available')
        if len(heads) == 1:
            selected = heads[0]
            row.update(selected_run_id=selected['run_id'], execution_status=selected['status'],
                       coverage_status=selected['coverage_status'], review_status=selected['review_status'], result=selected['result'], reasons=selected['reasons'], source_integrity=selected['source_integrity'])
        elif len(heads) > 1:
            row.update(coverage_status='conflict')
        rows.append(row)
    row_index = {row['check_id']: row for row in rows}
    cases = []
    for case in scope['business_cases']:
        required = [row_index[c] for c in case['check_ids'] if row_index[c]['required']]
        if any(r['coverage_status'] == 'confirmed' and r['execution_status'] == 'failed' for r in required):
            status = 'failed'
        elif any(r['coverage_status'] != 'confirmed' or r['execution_status'] != 'passed' for r in required):
            status = 'incomplete'
        elif any(r['review_status'] != 'passed' or r['source_integrity'] != 'local_verified' for r in required):
            status = 'needs_review'
        else:
            status = 'passed'
        cases.append(dict(case, source=business_sources[case['business_case_id']], status=status))
    confirmed = [r for r in rows if r['coverage_status'] == 'confirmed']
    counts = dict(planned=len(rows), required=sum(r['required'] for r in rows),
                  executed=sum(r['execution_status'] in ('passed', 'failed') for r in rows),
                  target_executed=sum(r['execution_status'] in ('passed', 'failed') for r in confirmed),
                  passed=sum(r['execution_status'] == 'passed' for r in confirmed),
                  failed=sum(r['execution_status'] == 'failed' for r in confirmed),
                  accepted_passed=sum(r['execution_status'] == 'passed' and r['review_status'] == 'passed' and r['source_integrity'] == 'local_verified' for r in confirmed))
    limitations = []
    for row in rows:
        if row['coverage_status'] != 'confirmed':
            limitations.append(f"{row['check_id']}: {row['coverage_status']}")
        if row['execution_status'] in ('blocked', 'not_run'):
            limitations.append(f"{row['check_id']}: {row['execution_status']}")
        if row['selected_run_id'] and row['review_status'] != 'passed':
            limitations.append(f"{row['check_id']}: review {row['review_status']}")
        if row['source_integrity'] == 'remote_unverified':
            limitations.append(f"{row['check_id']}: source/evidence not captured locally")
    limitations.extend('remote source not retrieved: ' + ref for ref in sorted(sources.remote))
    if out_of_scope:
        limitations.append(f'{len(out_of_scope)} result(s) outside the declared scope; excluded from counters')
    selected_ids = {r['selected_run_id'] for r in rows}
    for defect in defects:
        defect['run_selected'] = defect['run_id'] in selected_ids
    return dict(schema_version=1, task_id=scope['task_id'], scope_kind=scope['scope_kind'], feature=scope['feature'],
                scope_revision=scope['scope_revision'], report_revision=revision, generated_at=generated_at,
                inputs=dict(scope=str(scope_path), runs=str(runs_path)), counts=counts, business_cases=cases,
                configurations=scope['configurations'], checks=rows, runs=list(indexed_runs.values()),
                out_of_scope=out_of_scope, defects=defects, limitations=list(dict.fromkeys(limitations)),
                remote_sources=sorted(sources.remote), sources=sources.manifest(), release_decision='not_assessed')


def cell(value):
    if isinstance(value, (dict, list, bool)) or value is None:
        value = canonical(value)
    value = html.escape(str(value)).replace('\\', '\\\\').replace('\n', ' ').replace('\r', ' ')
    for character in ('|', '`', '*', '_', '[', ']'):
        value = value.replace(character, '\\' + character)
    return value


def link(ref, label='Источник'):
    return '[' + cell(label) + '](<' + quote(ref, safe='/:#?=&%') + '>)'


def mapping(value, flags=False):
    labels = {'role': 'роль', 'channel': 'канал', 'browser': 'браузер', 'data_profile': 'данные',
              'integration_state': 'интеграции', 'platform': 'платформа', 'cohort': 'когорта'}
    parts = []
    for key, item in sorted(value.items()):
        if isinstance(item, dict):
            displayed = mapping(item, flags)
        elif flags and type(item) is bool:
            displayed = 'ON' if item else 'OFF'
        else:
            displayed = cell(item)
        parts.append(cell(key if flags else labels.get(key, key)) + '=' + displayed)
    return '; '.join(parts) or 'не заданы'


def render_business_details(report):
    """Keep planned intent distinct from executor observations and acceptance."""
    rows = {row['check_id']: row for row in report['checks']}
    configs = {config['configuration_id']: config for config in report['configurations']}
    statuses = dict(passed='Пройден и принят', failed='Обнаружено расхождение',
                    incomplete='Проверен не полностью', needs_review='Ожидает ревью')
    def describe_intent(intent):
        return ['- **Планируемое действие:** ' + cell(intent['action']),
                '- **Бизнес-правило из требования:** ' + cell(intent['business_rule']),
                '- **Ожидаемый бизнес-эффект:** ' + cell(intent['expected_effect'])]

    lines = ['## Что проверяли: функционал и бизнес', '',
             'Выводы ограничены записанными действиями и наблюдениями. Статус сценария не доказывает отдельную проверку каждого бизнес-правила или других функций.', '']
    for case in report['business_cases']:
        lines.extend([f"### {cell(case['title'])}", '',
                      '**Бизнес-цель:** ' + cell(case.get('business_goal', 'Не описана в исходных материалах.')), '',
                      '**Итог согласованного набора:** ' + statuses[case['status']] + '.', ''])
        intents = [rows[identifier].get('intent') for identifier in case['check_ids']]
        shared_intent = intents[0] if intents[0] and all(canonical(intent) == canonical(intents[0]) for intent in intents) else None
        if shared_intent:
            lines.extend(describe_intent(shared_intent) + [''])
        for identifier in case['check_ids']:
            row = rows[identifier]
            config = configs[row['configuration_id']]
            result = row['result']
            lines.extend(['**Целевые условия:** ' + mapping(config['context']) + '; тогглы: '
                          + mapping(config['feature_flags'], True) + f" ({cell(row['configuration_id'])}).", ''])
            intent = row.get('intent')
            if intent and not shared_intent:
                lines.extend(describe_intent(intent))
            elif not intent:
                lines.append('- **Планируемое действие и бизнес-правило:** отдельно не описаны; нельзя восстановить их из ID или статуса.')
            if result:
                if not intent or result['expected_result'] != intent['expected_effect']:
                    lines.append('- **Ожидание исполнителя:** ' + cell(result['expected_result']))
                if row['execution_status'] == 'not_run':
                    lines.extend(['', '**Действия, записанные исполнителем:** сценарий не запускался; запись шагов не считается выполнением.'])
                elif result['steps']:
                    lines.extend(['', '**Действия, записанные исполнителем:**', ''])
                    lines.extend(f"{i}. {cell(step)}" for i, step in enumerate(result['steps'], 1))
                else:
                    lines.extend(['', '**Действия, записанные исполнителем:** отсутствуют; выполнение действия не подтверждено.'])
                lines.extend(['', '**Наблюдение / причина остановки:** ' + cell(result['actual_result']), ''])
            elif row['coverage_status'] == 'conflict':
                lines.extend(['', '**Наблюдение:** конфликт повторов; актуальный результат не выбран.', ''])
            else:
                lines.extend(['', '**Наблюдение:** результата запуска нет; действие и бизнес-эффект не проверены.', ''])
            boundaries = []
            if result and row['execution_status'] in ('blocked', 'not_run'):
                boundaries.append('Сценарий не завершён; ожидаемый бизнес-эффект не подтверждён.')
            if row['coverage_status'] == 'configuration_mismatch':
                boundaries.append('Запись относится к другим условиям и не закрывает целевую конфигурацию.')
            elif row['coverage_status'] == 'configuration_unknown':
                boundaries.append('Фактические условия не подтверждены; запись не закрывает целевую конфигурацию.')
            if result and row['review_status'] != 'passed':
                boundaries.append('Содержательное принятие результата не завершено.')
            if row['source_integrity'] == 'remote_unverified':
                boundaries.append('Удалённые источники и доказательства не проверены сборщиком.')
            if not intent:
                boundaries.append('Отдельное бизнес-правило не описано в области проверки.')
            if boundaries:
                lines.extend(['**Граница вывода:** ' + ' '.join(boundaries), ''])
            lines.extend(['**Основания:** ' + link(row['requirement_source'], 'требование')
                          + ('; ' + ' '.join(link(ref, f'доказательство {i}') for i, ref in enumerate(result['evidence'], 1)) if result else '')
                          + f". Идентификаторы: {cell(row['check_id'])} / {cell(row['scenario_id'])}; "
                          + ('обязательная' if row['required'] else 'дополнительная') + ' проверка.', ''])
        lines.extend(['Источник бизнес-цели: ' + link(case['source']) + '.', ''])
    return lines


def render_markdown(report):
    counts = report['counts']
    case_titles = {identifier: case['title'] for case in report['business_cases'] for identifier in case['check_ids']}
    configs = {config['configuration_id']: config for config in report['configurations']}
    def readable_check(row):
        return cell(case_titles[row['check_id']]) + ' (' + mapping(configs[row['configuration_id']]['feature_flags'], True) + ')'
    completed = [row for row in report['checks'] if row['coverage_status'] == 'confirmed' and row['execution_status'] in ('passed', 'failed')]
    problems = [row for status in ('failed', 'blocked') for row in report['checks'] if row['execution_status'] == status and row['result']]
    gaps = [row for row in report['checks'] if row['coverage_status'] != 'confirmed' or row['execution_status'] in ('blocked', 'not_run') or row['review_status'] != 'passed' or row['source_integrity'] != 'local_verified']
    brief = 'Выполнены проверки: ' + '; '.join(readable_check(row) for row in completed[:3]) + (f"; ещё {len(completed) - 3}" if len(completed) > 3 else '') + '.' if completed else 'Выполненные проверки в целевых условиях не подтверждены.'
    problem = ('Расхождение или блокер: ' + cell(problems[0]['result']['actual_result']) + '.') if problems else 'В выбранных результатах нет записанного расхождения или блокировки.'
    gap = 'Не закрыты проверки: ' + '; '.join(readable_check(row) for row in gaps[:3]) + (f"; ещё {len(gaps) - 3}" if len(gaps) > 3 else '') + '.' if gaps else 'Все проверки согласованной области выполнены при целевых условиях и имеют записанное принятие.'
    lines = [f"# Отчёт: {cell(report['feature'])}", '',
             f"Задача {cell(report['task_id'])}; ревизия {cell(report['report_revision'])}; область {cell(report['scope_revision'])}.", '',
             '**Учебные синтетические данные; приложение не проверялось.**' if report['scope_kind'] == 'synthetic' else 'Данные выполненных проверок; содержательное принятие учитывается отдельно.', '',
             brief, problem, gap, '',
             f"В целевых условиях выполнено {counts['target_executed']} из {counts['planned']} проверок «сценарий × конфигурация».",
             f"Пройдено {counts['passed']}, с расхождением {counts['failed']}; пройдено и принято ревью {counts['accepted_passed']}.",
             f"Ограничений выполнения и принятия: {len(report['limitations'])}. Решение о выпуске не оценивалось.", '']
    lines.extend(render_business_details(report))
    lines.extend(['', 'Итог бизнес-кейса относится к обязательным проверкам; дополнительные проверки остаются в матрице.', '',
                  '## Покрытие сценариев и конфигураций', '',
                  '| Проверка | Сценарий / конфигурация | Обязательная | Выполнение | Условия | Ревью / источники |',
                  '| --- | --- | --- | --- | --- | --- |'])
    for row in report['checks']:
        execution_labels = dict(passed='Пройден', failed='Расхождение', blocked='Заблокирован', not_run='Не запускался')
        condition_labels = dict(confirmed='Подтверждены', configuration_mismatch='Не совпадают с целевыми',
                                configuration_unknown='Не подтверждены', conflict='Конфликт повторов', not_run='Не проверены')
        review_labels = dict(pending='Ожидает', changes_requested='Нужны исправления', passed='Принят')
        reason_labels = {'environment_id': 'стенд', 'product_version': 'сборка', 'requirements_revision': 'версия требований',
                         'requirement_source': 'источник ожидания', 'context.role': 'роль', 'context.channel': 'канал',
                         'context.browser': 'браузер', 'context.data_profile': 'профиль данных'}
        reasons = [reason_labels.get(reason, reason.replace('feature_flags.', 'тоггл ').replace('context_evidence.', 'источник контекста ')) for reason in row['reasons']]
        conditions = condition_labels[row['coverage_status']] + (': ' + ', '.join(reasons) if reasons else '')
        review = review_labels[row['review_status']] + ('; удалённые источники не проверены сборщиком' if row['source_integrity'] == 'remote_unverified' else '')
        lines.append(f"| {cell(row['check_id'])} | {cell(row['scenario_id'])} / {cell(row['configuration_id'])} | {'да' if row['required'] else 'нет'} | {execution_labels[row['execution_status']]} | {cell(conditions)} | {cell(review)} |")
    lines.extend(['', '## Целевые условия', ''])
    for config in report['configurations']:
        lines.extend([f"- **{cell(config['configuration_id'])}**: {cell(config['environment_id'])}, сборка {cell(config['product_version'])}, требования {cell(config['requirements_revision'])}; {mapping(config['context'])}; тогглы: {mapping(config['feature_flags'], True)}."])
    lines.extend(['', '## Наблюдавшиеся условия и тогглы', ''])
    for run in report['runs']:
        targets = ', '.join(cell(target['environment_id']) + ' / ' + cell(target['product_version']) for target in run['observed_targets'])
        lines.extend([f"- **{cell(run['run_id'])}**, {cell(run['observed_at'])}: {link(run['result_artifact'], 'результат')}; стенд/сборка {targets}; требования {cell(run['requirements_revision'])}; {mapping(run['context'])}."])
        for key, flag in run['feature_flags'].items():
            source = link(flag['source']) if flag['source'] else 'источник отсутствует'
            flag_status = {'confirmed': 'подтверждено', 'unknown': 'неизвестно', 'fallback': 'значение по умолчанию, применение варианта не подтверждено'}[flag['status']]
            lines.append(f"  - {mapping({key: flag.get('value')}, True)}; {flag_status}; {source}.")
    lines.extend(['', '## Расхождения из первичных отчётов', ''])
    for defect in report['defects']:
        detail = defect['detail']
        if isinstance(detail, dict):
            detail = next((detail[key] for key in ('summary', 'description', 'title') if text(detail.get(key))), 'Запись без краткого описания; подробности в первичном отчёте')
        lines.append(f"- {cell(defect['run_id'])} ({'прогон выбран хотя бы для одной проверки' if defect['run_selected'] else 'история'}): {cell(detail)}; {link(defect['result_artifact'])}.")
    if not report['defects']:
        lines.append('Отдельные записи дефектов не предоставлены; это не означает отсутствие расхождений или дефектов.')
    lines.extend(['', '## Ограничения и непроверенная область', ''])
    limitation_labels = {': not_run': ': не запускалась', ': blocked': ': заблокирована', ': configuration_unknown': ': условия не подтверждены',
                         ': configuration_mismatch': ': условия отличаются от целевых', ': conflict': ': конфликт повторов',
                         ': review pending': ': ожидает ревью', ': review changes_requested': ': замечания ревью',
                         ': source/evidence not captured locally': ': источники/доказательства не сохранены локально'}
    for value in report['limitations']:
        for original, translated in limitation_labels.items():
            value = value.replace(original, translated)
        lines.append('- ' + cell(value))
    if not report['limitations']:
        lines.append('Структурные ограничения в переданных материалах не обнаружены.')
    lines.extend(['', '## История и первичные материалы', ''])
    for row in report['checks']:
        history = ', '.join(f"{entry['run_id']}: {entry['status']} / {entry['coverage_status']}" for entry in row['history']) or 'нет запусков'
        lines.append(f"- {cell(row['check_id'])}: {cell(history)}; {link(row['requirement_source'], 'требование')}.")
    lines.extend(['', 'Хеши всех локальных входов и доказательств, результаты вне области и подробности повторов — в report.json.',
                  'Проверка файлов и связей не оценивает истинность их содержимого. Итог не разрешает выпуск.', ''])
    return '\n'.join(lines)


def check_bundle(directory):
    directory = Path(directory).resolve()
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    require(isinstance(report, dict) and isinstance(report.get('inputs'), dict), 'invalid report bundle')
    rebuilt = build_report(report['inputs']['scope'], report['inputs']['runs'], report['report_revision'], report['generated_at'])
    require(canonical(report) == canonical(rebuilt), 'report differs from sources or computed facts')
    require((directory / 'report.md').read_text(encoding='utf-8') == render_markdown(rebuilt), 'Markdown differs from verified report')
    return report
