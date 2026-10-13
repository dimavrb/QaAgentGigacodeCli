"""Append-only operational incidents; recorded assessments are not semantic proof."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

CATEGORIES = {'requirements', 'verification', 'environment', 'test_data', 'tool',
              'context', 'coordination', 'retry', 'execution', 'unknown'}
SYMPTOMS = {'blocked_execution', 'tool_error', 'invalid_result', 'wrong_conclusion',
            'incomplete_coverage', 'repeated_action', 'lost_progress', 'timeout', 'other'}
STAGES = {'analysis', 'planning', 'environment', 'test_data', 'investigation',
          'feature_testing', 'automation', 'review', 'reporting', 'jira', 'orchestration'}
IMMUTABLE = ('task_id', 'request_id', 'agent', 'agent_version', 'stage', 'scope_kind')
REQUIRED = {'schema_version', 'event_id', 'incident_id', 'occurred_at', 'actor', *IMMUTABLE,
            'summary', 'symptom', 'impact', 'scenario_ids', 'context', 'evidence', 'cause', 'recovery'}
OPTIONAL = {'reopens_event_id', 'regression_case'}
MAX_BYTES = 64 * 1024 * 1024
MAX_LINE = 128 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def timestamp(value):
    require(text(value), 'occurred_at must be an ISO datetime')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'occurred_at must include timezone')
    return parsed


def file_digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def references(refs, base, stored=False):
    require(isinstance(refs, list), 'evidence must be a list')
    resolved = []
    for ref in refs:
        if stored:
            require(isinstance(ref, dict) and set(ref) == {'ref', 'sha256'}, 'invalid evidence record')
            value, checksum = ref['ref'], ref['sha256']
        else:
            value, checksum = ref, None
        require(text(value), 'empty evidence reference')
        if value.startswith(('https://', 'http://')):
            require(not stored or checksum is None, 'remote source must not claim a local hash')
        else:
            name, marker, fragment = value.partition('#')
            require(bool(name), 'local source needs a file path')
            path = Path(name).expanduser()
            if stored:
                require(path.is_absolute(), 'stored evidence needs an absolute path')
                require(isinstance(checksum, str) and len(checksum) == 64 and
                        all(c in '0123456789abcdef' for c in checksum), 'invalid evidence hash')
            else:
                path = (base / path).resolve()
                checksum = file_digest(path)
                value = str(path) + (marker + fragment if marker else '')
        resolved.append(dict(ref=value, sha256=checksum))
    require(len({item['ref'] for item in resolved}) == len(resolved), 'duplicate evidence reference')
    return resolved


def validate_entry(entry, base, stored=False):
    require(isinstance(entry, dict), 'entry must be an object')
    require(REQUIRED <= set(entry) and set(entry) <= REQUIRED | OPTIONAL, 'entry fields differ from journal schema')
    require(type(entry['schema_version']) is int and entry['schema_version'] == 1, 'schema_version must equal 1')
    for key in ('event_id', 'incident_id', 'actor', *IMMUTABLE, 'summary', 'symptom', 'impact'):
        require(text(entry[key]), key + ' must be a non-empty string')
    require(entry['stage'] in STAGES and entry['symptom'] in SYMPTOMS, 'unknown stage or symptom')
    require(entry['scope_kind'] in ('operational', 'synthetic'), 'unknown scope_kind')
    require(timestamp(entry['occurred_at']) <= datetime.now(timezone.utc), 'occurred_at is in the future')
    ids = entry['scenario_ids']
    require(isinstance(ids, list) and all(text(v) for v in ids) and len(set(ids)) == len(ids), 'invalid scenario_ids')
    require(isinstance(entry['context'], dict), 'context must be an object')
    result = dict(entry)
    result['evidence'] = references(entry['evidence'], base, stored)
    require(bool(result['evidence']), 'incident needs evidence of the symptom')
    cause = entry['cause']
    require(isinstance(cause, dict) and set(cause) == {'status', 'category', 'description', 'evidence'}, 'invalid cause fields')
    require(isinstance(cause['status'], str) and cause['status'] in ('unknown', 'suspected', 'confirmed'), 'invalid cause.status')
    require(isinstance(cause['category'], str) and cause['category'] in CATEGORIES, 'invalid cause.category')
    cause = dict(cause, evidence=references(cause['evidence'], base, stored))
    if cause['status'] == 'unknown':
        require(cause['category'] == 'unknown' and cause['description'] is None and not cause['evidence'], 'unknown cause cannot claim a diagnosis')
    else:
        require(cause['category'] != 'unknown' and text(cause['description']) and bool(cause['evidence']), 'diagnosis needs description and evidence')
    if cause['status'] == 'confirmed':
        require(entry['actor'] not in (entry['agent'], 'qa-runner'), 'cause confirmation needs a separate reviewer')
        require(any(item['sha256'] for item in cause['evidence']), 'confirmed cause needs captured local evidence')
    result['cause'] = cause
    recovery = entry['recovery']
    require(isinstance(recovery, dict) and set(recovery) == {'outcome', 'action', 'result', 'evidence'}, 'invalid recovery fields')
    require(isinstance(recovery['outcome'], str) and recovery['outcome'] in ('not_attempted', 'in_progress', 'failed', 'verified'), 'invalid recovery outcome')
    recovery = dict(recovery, evidence=references(recovery['evidence'], base, stored))
    if recovery['outcome'] == 'not_attempted':
        require(recovery['action'] is None and recovery['result'] is None and not recovery['evidence'], 'unattempted recovery must not claim actions/results')
    else:
        require(text(recovery['action']), 'attempted recovery needs an action')
        require(recovery['result'] is None or text(recovery['result']), 'invalid recovery result')
        if recovery['outcome'] in ('verified', 'failed'):
            require(text(recovery['result']) and bool(recovery['evidence']), 'completed recovery needs observed result and evidence')
    if recovery['outcome'] == 'verified':
        require(entry['actor'] != 'qa-runner' and any(item['sha256'] for item in recovery['evidence']), 'verified recovery needs local evidence; runner cannot verify it')
    result['recovery'] = recovery
    if 'reopens_event_id' in entry:
        require(text(entry['reopens_event_id']), 'reopens_event_id must identify the previous recovery')
    if 'regression_case' in entry:
        case = entry['regression_case']
        require(isinstance(case, dict) and set(case) == {'status', 'reason', 'evidence'}, 'invalid regression_case')
        require(isinstance(case['status'], str) and case['status'] in ('candidate', 'added', 'not_needed') and text(case['reason']), 'invalid regression_case status/reason')
        case = dict(case, evidence=references(case['evidence'], base, stored))
        require(case['status'] != 'added' or any(item['sha256'] for item in case['evidence']), 'added regression case needs a saved task')
        result['regression_case'] = case
    require(len(canonical(result).encode('utf-8')) <= MAX_LINE - 1024, 'entry too large; link artifacts instead')
    return result


def state(entry):
    outcome = entry['recovery']['outcome']
    return 'recovered' if outcome == 'verified' else ('recovering' if outcome == 'in_progress' else 'open')


def transition(previous, entry):
    if previous is None:
        require('reopens_event_id' not in entry, 'cannot reopen an unknown incident')
        return
    require(all(previous[key] == entry[key] for key in IMMUTABLE), 'incident assignment is immutable; use a new incident_id')
    require(timestamp(entry['occurred_at']) >= timestamp(previous['occurred_at']), 'incident update reverses time')
    if state(previous) == 'recovered' and state(entry) != 'recovered':
        require(entry.get('reopens_event_id') == previous['event_id'], 'reopening needs explicit reopens_event_id')
    else:
        require('reopens_event_id' not in entry, 'reopens_event_id is only valid for reopening a recovery')


@contextmanager
def locked(path, exclusive):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def decode(raw):
    require(len(raw) <= MAX_BYTES, 'journal too large; archive and start another period')
    require(not raw or raw.endswith(b'\n'), 'journal has an incomplete tail; preserve it and repair before appending')
    records, latest, seen, previous_hash = [], {}, set(), None
    for line in raw.splitlines():
        require(bool(line) and len(line) <= MAX_LINE, 'invalid journal line size')
        record = json.loads(line)
        require(isinstance(record, dict) and set(record) == {'sequence', 'recorded_at', 'previous_sha256', 'entry', 'sha256'}, 'invalid journal record')
        require(type(record['sequence']) is int and record['sequence'] == len(records) + 1, 'journal sequence differs')
        require(record['previous_sha256'] == previous_hash, 'journal chain differs')
        require(record['sha256'] == digest({k: v for k, v in record.items() if k != 'sha256'}), 'journal record hash differs')
        entry = validate_entry(record['entry'], Path.cwd(), stored=True)
        require(timestamp(record['recorded_at']) >= timestamp(entry['occurred_at']), 'record predates observation')
        require(entry['event_id'] not in seen, 'duplicate event_id in journal')
        transition(latest.get(entry['incident_id']), entry)
        latest[entry['incident_id']] = entry
        seen.add(entry['event_id'])
        records.append(record)
        previous_hash = record['sha256']
    return records


def read_journal(path):
    with locked(path, False) as path:
        require(path.stat().st_size <= MAX_BYTES, 'journal too large; archive and start another period')
        raw = path.read_bytes()
        return decode(raw), hashlib.sha256(raw).hexdigest()


def append_entry(path, entry, base=None):
    path = Path(path).expanduser().resolve()
    entry = validate_entry(entry, Path(base or Path.cwd()))
    protected = {str(path), str(path) + '.lock'}
    for refs in (entry['evidence'], entry['cause']['evidence'], entry['recovery']['evidence'], entry.get('regression_case', {}).get('evidence', [])):
        require(not any(item['ref'].partition('#')[0] in protected for item in refs), 'journal and lock cannot be their own evidence')
    with locked(path, True) as path:
        require(not path.exists() or path.stat().st_size <= MAX_BYTES, 'journal too large')
        raw = path.read_bytes() if path.exists() else b''
        records = decode(raw)
        for record in records:
            if record['entry']['event_id'] == entry['event_id']:
                require(canonical(record['entry']) == canonical(entry), 'event_id already exists with different content')
                return False
        previous = next((r['entry'] for r in reversed(records) if r['entry']['incident_id'] == entry['incident_id']), None)
        transition(previous, entry)
        record = dict(sequence=len(records) + 1, recorded_at=datetime.now(timezone.utc).isoformat(),
                      previous_sha256=records[-1]['sha256'] if records else None, entry=entry)
        record['sha256'] = digest(record)
        line = (canonical(record) + '\n').encode('utf-8')
        require(len(raw) + len(line) <= MAX_BYTES, 'journal too large')
        with path.open('ab') as output:
            output.write(line)
            output.flush()
            os.fsync(output.fileno())
        return True


def integrity(refs):
    issues = []
    for item in refs:
        if item['sha256'] is None:
            issues.append(dict(ref=item['ref'], status='remote_unverified'))
            continue
        try:
            if file_digest(Path(item['ref'].partition('#')[0])) != item['sha256']:
                issues.append(dict(ref=item['ref'], status='changed'))
        except OSError:
            issues.append(dict(ref=item['ref'], status='missing_or_unreadable'))
    return issues


def summarize(records, journal_sha256, scope_kind='operational'):
    latest = {}
    for record in records:
        entry = record['entry']
        if scope_kind == 'all' or entry['scope_kind'] == scope_kind:
            latest[entry['incident_id']] = entry
    statuses = dict(open=0, recovering=0, recovered=0)
    symptoms, confirmed, suspected, stages, incidents = {}, {}, {}, {}, []
    for identifier, entry in latest.items():
        cause, recovery = entry['cause'], entry['recovery']
        cause_issues, recovery_issues = integrity(cause['evidence']), integrity(recovery['evidence'])
        evidence_issues = integrity(entry['evidence'])
        status = state(entry)
        # A saved recovery assessment remains in history, but changed proof cannot show green.
        if status == 'recovered' and (recovery_issues or evidence_issues):
            status = 'recovery_unverifiable'
        statuses[status] = statuses.get(status, 0) + 1
        symptoms[entry['symptom']] = symptoms.get(entry['symptom'], 0) + 1
        stages[entry['stage']] = stages.get(entry['stage'], 0) + 1
        if cause['status'] == 'confirmed' and not cause_issues and not evidence_issues:
            confirmed[cause['category']] = confirmed.get(cause['category'], 0) + 1
        elif cause['status'] == 'suspected':
            suspected[cause['category']] = suspected.get(cause['category'], 0) + 1
        incidents.append(dict(entry, current_status=status, evidence_issues=evidence_issues,
                              cause_evidence_issues=cause_issues, recovery_evidence_issues=recovery_issues))
    order = lambda values: dict(sorted(values.items(), key=lambda pair: (-pair[1], pair[0])))
    return dict(schema_version=1, journal_sha256=journal_sha256, scope_kind=scope_kind,
                events=len(records), incidents=len(incidents), statuses=statuses,
                symptoms=order(symptoms), stages=order(stages), confirmed_causes_with_intact_evidence=order(confirmed),
                suspected_causes=order(suspected), incidents_detail=incidents)


def find_recoveries(records, journal_sha256, *, query='', agent=None, stage=None,
                    symptom=None, context=None, limit=5, scope_kind='operational'):
    """Retrieve candidate experience, never an authorization or diagnosis."""
    require(isinstance(query, str), 'query must be a string')
    require(type(limit) is int and 1 <= limit <= 20, 'limit must be between 1 and 20')
    require(scope_kind in ('operational', 'synthetic', 'all'), 'invalid scope_kind')
    require(agent is None or text(agent), 'agent must be a non-empty string')
    require(stage is None or stage in STAGES, 'unknown stage')
    require(symptom is None or symptom in SYMPTOMS, 'unknown symptom')
    require(context is None or isinstance(context, dict), 'context must be an object')
    context = {} if context is None else context
    require(all(text(key) and value is not None for key, value in context.items()),
            'search context needs named, known values; omit unknown conditions')
    terms = set(re.findall(r'\w+', query.casefold()))
    require(terms or agent or stage or symptom or context, 'find needs a query or filter')
    candidates = []
    skipped = dict(unresolved=0, evidence_unverifiable=0, context_mismatch=0)
    # The latest snapshot wins: a reopened incident must not offer its old recovery.
    latest = {}
    for record in records:
        entry = record['entry']
        latest[entry['incident_id']] = entry
    for entry in latest.values():
        if scope_kind != 'all' and entry['scope_kind'] != scope_kind:
            continue
        if any(value is not None and entry[key] != value for key, value in
               (('agent', agent), ('stage', stage), ('symptom', symptom))):
            continue
        searchable = ' '.join((entry['summary'], entry['symptom'], entry['cause']['description'] or '',
                               entry['recovery']['action'] or '', entry['recovery']['result'] or '')).casefold()
        if any(term not in searchable for term in terms):
            continue
        if entry['recovery']['outcome'] != 'verified':
            skipped['unresolved'] += 1
            continue
        if any(integrity(refs) for refs in (entry['evidence'], entry['cause']['evidence'], entry['recovery']['evidence'])):
            skipped['evidence_unverifiable'] += 1
            continue
        known = {key: value for key, value in entry['context'].items() if value is not None}
        if any(key in known and canonical(known[key]) != canonical(value) for key, value in context.items()):
            skipped['context_mismatch'] += 1
            continue
        missing = sorted(set(context) - set(known))
        comparison = 'not_checked' if not context else ('unknown' if missing else 'matched')
        candidate = {key: entry[key] for key in ('incident_id', 'event_id', 'task_id', 'request_id',
                     'occurred_at', 'agent', 'agent_version', 'stage', 'scope_kind', 'summary', 'symptom', 'context')}
        candidate.update(cause=entry['cause'], recovery=entry['recovery'], evidence=entry['evidence'],
                         context_match=comparison, unknown_context_keys=missing,
                         applicability_review_required=True)
        candidates.append(candidate)
    # Known matching conditions come first, then newest recovery. No confidence percentage.
    candidates.sort(key=lambda item: timestamp(item['occurred_at']), reverse=True)
    candidates.sort(key=lambda item: {'matched': 0, 'unknown': 1, 'not_checked': 2}[item['context_match']])
    return dict(schema_version=1, journal_sha256=journal_sha256, scope_kind=scope_kind,
                matches=candidates[:limit], total_matches=len(candidates), truncated=len(candidates) > limit,
                skipped=skipped, checked_at=datetime.now(timezone.utc).isoformat(),
                applicability_review_required=True)


LABELS = dict(requirements='Требования', verification='Проверка выводов', environment='Окружение',
              test_data='Тестовые данные', tool='Инструменты', context='Контекст', coordination='Передача между ролями',
              retry='Повторы', execution='Исполнение', unknown='Не установлена', blocked_execution='Выполнение заблокировано',
              tool_error='Ошибка инструмента/CLI', invalid_result='Невалидный ответ', wrong_conclusion='Неверный вывод',
              incomplete_coverage='Неполное покрытие', repeated_action='Повтор действия', lost_progress='Потеря состояния',
              timeout='Превышение времени', other='Другое', open='Открыт', recovering='Восстановление идёт',
              recovered='Восстановление подтверждено записью', recovery_unverifiable='Доказательства восстановления изменены/недоступны')


def escape(value):
    result = html.escape(str(value)).replace('\n', ' ').replace('\r', ' ').replace('\\', '\\\\')
    for char in ('|', '*', '_', '`', '[', ']'):
        result = result.replace(char, '\\' + char)
    return result


def render_summary(summary):
    lines = ['# Журнал сбоев QA-процесса', '',
             'Учебные и рабочие записи разделены; выбран набор: ' + escape(summary['scope_kind']) + '.', '',
             f"Инцидентов: {summary['incidents']}. Записей истории во всём журнале: {summary['events']}.",
             f"Открыто: {summary['statuses']['open']}; восстановление идёт: {summary['statuses']['recovering']}; "
             f"восстановление подтверждено записью: {summary['statuses']['recovered']}; "
             f"непроверяемое восстановление: {summary['statuses'].get('recovery_unverifiable', 0)}.",
             'Числа показывают зарегистрированные инциденты, а не вероятность сбоя среди всех запусков.', '',
             '## Наблюдаемые проблемы', '']
    lines.extend('- ' + LABELS[key] + ': ' + str(count) for key, count in summary['symptoms'].items())
    if not summary['symptoms']:
        lines.append('Инциденты выбранного набора не зарегистрированы.')
    lines.extend(['', '## Причины с записанным подтверждением и неизменными доказательствами', ''])
    lines.extend('- ' + LABELS[key] + ': ' + str(count) for key, count in summary['confirmed_causes_with_intact_evidence'].items())
    if not summary['confirmed_causes_with_intact_evidence']:
        lines.append('Такие причины пока не зарегистрированы.')
    if summary['suspected_causes']:
        lines.extend(['', '## Гипотезы о причинах — ещё не подтверждены', ''])
        lines.extend('- ' + LABELS[key] + ': ' + str(count) for key, count in summary['suspected_causes'].items())
    lines.extend(['', '## Инциденты и восстановление', ''])
    for entry in summary['incidents_detail']:
        lines.extend(['### ' + escape(entry['summary']), '',
                      '- **Влияние:** ' + escape(entry['impact']),
                      '- **Статус:** ' + LABELS[entry['current_status']],
                      '- **Причина:** ' + {'unknown': 'не установлена', 'suspected': 'гипотеза', 'confirmed': 'записано подтверждение'}[entry['cause']['status']]
                      + (' — ' + escape(entry['cause']['description']) if entry['cause']['description'] else ''),
                      '- **Восстановление:** ' + escape(entry['recovery']['result'] or entry['recovery']['action'] or 'не предпринималось'), ''])
        issues = entry['evidence_issues'] + entry['cause_evidence_issues'] + entry['recovery_evidence_issues']
        if issues:
            lines.append('**Ограничение доказательств:** ' + escape(', '.join(sorted({item['status'] for item in issues}))))
        refs = entry['evidence'] + entry['cause']['evidence'] + entry['recovery']['evidence']
        unique = list(dict.fromkeys(item['ref'] for item in refs))
        lines.extend(['**Основания:** ' + ' '.join(f"[{i}](<{quote(ref, safe='/:#?=&%')}>)" for i, ref in enumerate(unique, 1)),
                      'ID: ' + escape(entry['incident_id']) + '; задача ' + escape(entry['task_id']) + '; назначение ' + escape(entry['request_id'])
                      + '; этап ' + escape(entry['stage']) + '; агент ' + escape(entry['agent']) + '; версия ' + escape(entry['agent_version']) + '.', ''])
        if entry.get('regression_case'):
            case = entry['regression_case']
            case_status = {'candidate': 'кандидат', 'added': 'добавлено', 'not_needed': 'не требуется'}[case['status']]
            lines.extend(['Контрольное задание: ' + case_status + '; ' + escape(case['reason']) + '.', ''])
    lines.extend(['', 'Хеши защищают связи записей от случайного изменения, но не доказывают истинность вывода или личность проверяющего.', ''])
    return '\n'.join(lines)
