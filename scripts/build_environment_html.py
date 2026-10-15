#!/usr/bin/env python3
"""Offline HTML report for an accepted environment result, including blockers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

import task_exchange as exchange
from build_test_plan_html import MAX_SOURCE_BYTES, Snapshot, esc, require, write_once
from qa_contract import parse_message
from validate_test_cases import read_json

TEMPLATE = Path(__file__).with_name('environment_report.html')
EVENTS = {'environment_ready': 'Стенд готов', 'environment_partial': 'Стенд подготовлен частично',
          'blocked': 'Настройка заблокирована'}
STATUSES = {'passed': 'Выполнено', 'failed': 'Ошибка', 'blocked': 'Заблокировано', 'not_run': 'Не проверено'}


def accepted_bundle(state, rid):
    row = state['requests'].get(rid)
    require(row is not None and row['status'] == 'accepted', 'Environment report is not accepted')
    require(row['assignment']['agent'] == 'qa-test-env-configurator', 'Environment report requires the configurator role')
    require(row['result']['event'] in EVENTS, 'Unsupported environment result')
    exchange.check_blocking_reviews(state, rid)
    snapshot = Snapshot(dict(row['result']['artifact_hashes']))
    for path in snapshot.hashes:
        snapshot.load(path)
    exchange.check_inputs(row)
    exchange.check_answers(state, row['assignment']['questionnaire_ids'])
    exchange.assignment_sources(state, row['assignment'])
    current = exchange.inspect_result(row['assignment'])
    require(current['artifact_hashes'] == row['result']['artifact_hashes'], 'Accepted environment evidence changed')
    message, _ = parse_message(snapshot.read(current['path']))
    scope_path = row['assignment'].get('environment_assignment')
    require(scope_path or message['event'] == 'blocked', 'Admin-guide confirmation is missing from this historical report')
    scope = read_json(snapshot.read(scope_path)) if scope_path else None
    return dict(state=state, row=row, message=message, scope=scope, snapshot=snapshot, result_path=current['path'])


def link(path, destination, label=None):
    path = Path(path).resolve()
    href = quote(os.path.relpath(path, destination.parent), safe='/')
    return f'<a href="{esc(href)}">{esc(label if label is not None else path.name)}</a>'


def items(values):
    return '<ul>' + ''.join('<li>' + esc(value) + '</li>' for value in values) + '</ul>' if values else '<p class="muted">Не указаны.</p>'


def render(bundle, destination):
    message, scope, row = bundle['message'], bundle['scope'], bundle['row']
    payload = message['payload']
    points = {point['point_id']: point for point in scope['points']} if scope else {}
    checks = {check['point_id']: check for check in payload.get('guide_checks', [])}
    counts = {status: sum(check['status'] == status for check in checks.values()) for status in STATUSES}
    cards = []
    for pid, point in points.items():
        check = checks[pid]
        evidence = ''.join('<li>' + link(Path(row['assignment']['artifact_base']) / path, destination)
                           + '</li>' for path in check['evidence'])
        cards.append(f'<article class="point {esc(check["status"])}"><div class="point-title">'
            f'<h3>{esc(pid)}</h3><span class="badge {esc(check["status"])}">{STATUSES[check["status"]]}</span></div>'
            f'<p class="quote">{esc(point["quote"])}</p><div class="observations"><div><h4>Ожидаемый результат</h4>'
            f'<p>{esc(point["expected_result"])}</p></div><div><h4>Фактический результат</h4><p>{esc(check["actual_result"])}</p></div></div>'
            '<h4>Выполненные действия</h4>' + items(check['actions'])
            + f'<p class="muted">Наблюдение: {esc(check.get("observed_at") or "Не выполнялось")}</p>'
            + '<h4>Доказательства</h4>' + ('<ul>' + evidence + '</ul>' if evidence else '<p class="muted">Нет подтверждающего наблюдения.</p>')
            + '<p class="muted">Связанные сценарии: ' + esc(', '.join(point.get('scenario_ids', scope.get('scenario_ids', []))) or 'Весь стенд') + '</p></article>')
    questions = []
    for question in payload.get('open_questions', []):
        if question['status'] == 'resolved':
            continue
        label = 'Блокирует настройку' if question['blocking'] else 'Требует уточнения'
        questions.append('<article class="question"><h3>' + esc(question['question_id']) + ' · ' + esc(question['question'])
            + '</h3><p>' + label + '</p>' + ('<p>' + esc(question['context']) + '</p>' if question.get('context') else '')
            + '<p class="muted">Пункты: '
            + esc(', '.join(question.get('point_ids', [])) or 'Не указаны') + ' · Сценарии: '
            + esc(', '.join(question.get('scenario_ids', [])) or 'Не указаны') + '</p></article>')
    if questions:
        questions_html = ''.join(questions)
    elif 'open_questions' in payload:
        questions_html = '<p>Открытых вопросов нет.</p>'
    else:
        questions_html = '<p class="muted">Сведения об оставшихся вопросах не указаны в отчёте.</p>'
    guide = '<p class="muted">Админгайд не предоставлен. Полнота настройки не подтверждена.</p>'
    if scope:
        source = (Path(row['assignment']['environment_assignment']).parent / scope['admin_guide']['path']).resolve()
        guide = '<p>Источник: ' + link(source, destination) + '</p><details><summary>Открыть текст админгайда</summary><pre>'
        guide += esc(bundle['snapshot'].read(source)) + '</pre></details>'
    status_class = 'passed' if message['event'] == 'environment_ready' else 'blocked'
    values = dict(TITLE=esc(bundle['state']['title']), STATUS=EVENTS[message['event']], STATUS_CLASS=status_class,
        ENVIRONMENT=esc(payload.get('environment_id') or 'Не указан'), VERSION=esc(payload.get('product_version') or 'Не указана'),
        SUMMARY=esc(message['summary']), REQUEST=esc(message['request_id']), TASK=esc(message['task_id']),
        COUNT=str(len(points)) if scope else '—', PASSED=str(counts['passed']) if scope else '—',
        FAILED=str(counts['failed']) if scope else '—',
        UNFINISHED=str(counts['blocked'] + counts['not_run']) if scope else '—',
        QUESTIONS_COUNT=str(len(questions)) if 'open_questions' in payload else 'не указаны',
        POINTS=''.join(cards) or '<p>Пункты гайда не получены. Подтверждение выполнения отсутствует.</p>',
        QUESTIONS=questions_html, BLOCKERS=items(message['blockers']), LIMITATIONS=items(payload.get('limitations', [])),
        CAPABILITIES=items(payload.get('verified_capabilities', [])), GUIDE=guide,
        READY_SCENARIOS=esc(', '.join(scope.get('scenario_ids', []) if scope and message['event'] == 'environment_ready'
                                    else payload.get('ready_scenarios', [])) or 'Не выделены отдельным списком'),
        BLOCKED_SCENARIOS=esc(', '.join(scope.get('scenario_ids', []) if scope and message['event'] == 'blocked'
                                      else payload.get('blocked_scenarios', [])) or 'Не выделены отдельным списком'),
        DECISION=esc(row['review']['decision']), ACCEPTED_AT=esc(row['review']['reviewed_at']),
        JSON_LINK=link(bundle['result_path'], destination, 'Исходный JSON-отчёт'))
    return re.sub(r'\{\{([A-Z_]+)\}\}', lambda match: values[match[1]], TEMPLATE.read_text(encoding='utf-8'))


def publish(state_path, rid, output=None):
    def operation(state):
        row = state['requests'].get(rid)
        require(row is not None and row['status'] == 'accepted', 'Environment report is not accepted')
        require(row['assignment']['agent'] == 'qa-test-env-configurator', 'HTML report requires the configurator role')
        try:
            bundle = accepted_bundle(state, rid)
            destination = Path(output) if output is not None else Path(bundle['result_path']).with_suffix('.environment.html')
            require(not destination.is_symlink(), 'Symlinked HTML output is not supported')
            destination = destination.resolve()
            protected = set(bundle['snapshot'].hashes) | exchange.protected_paths(state_path, state)
            for request_id, other in state['requests'].items():
                protected.add(other['assignment']['result_path'])
                protected.update(other['input_hashes'])
                if request_id != rid and other.get('presentation', {}).get('path'):
                    protected.add(other['presentation']['path'])
            require(str(destination) not in protected, 'HTML output would overwrite a task artifact')
            document = render(bundle, destination).encode('utf-8')
            require(len(document) <= MAX_SOURCE_BYTES, 'Environment HTML exceeds the size limit')
            bundle['snapshot'].verify()
            write_once(destination, document)
            presentation = dict(status='ready', kind='environment_report', path=str(destination),
                sha256=hashlib.sha256(document).hexdigest(), source_sha256=bundle['snapshot'].hashes[bundle['result_path']],
                source_hashes=bundle['snapshot'].hashes)
        except (OSError, ValueError, UnicodeError, RecursionError, KeyError, TypeError) as exc:
            presentation = dict(status='needs_attention', error=str(exc))
        row['presentation'] = presentation
        return presentation
    return exchange.mutate(state_path, None, operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        result = publish(args.state, args.request_id, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['result']['status'] == 'ready' else 2
    except (OSError, ValueError, UnicodeError, RecursionError, KeyError, TypeError) as exc:
        print('Environment HTML error: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
