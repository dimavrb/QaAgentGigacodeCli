#!/usr/bin/env python3
"""Publish an offline HTML view of an accepted, unchanged test-plan revision."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit

import task_exchange as exchange
from build_manual_allure import render_step_table
from content_review import check_required_reviews
from qa_contract import parse_message
from test_plan import validate_plan
from validate_test_cases import read_json, validate_catalog

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
TEMPLATE = Path(__file__).with_name('test_plan.html')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def esc(value):
    return html.escape(str(value), quote=True)


def anchor(kind, identifier):
    return kind + '-' + hashlib.sha256(identifier.encode('utf-8')).hexdigest()


def regular_bytes(path):
    """Do not block on FIFOs or follow a symlink supplied as a source/output."""
    path = Path(path)
    require(not path.is_symlink(), f'Symlink is not supported: {path}')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), f'Regular file required: {path}')
        require(info.st_size <= MAX_SOURCE_BYTES, f'Source is too large: {path}')
        data = stream.read(MAX_SOURCE_BYTES + 1)
    require(len(data) <= MAX_SOURCE_BYTES, f'Source is too large: {path}')
    return data


class Snapshot:
    def __init__(self, hashes):
        self.hashes = hashes
        self.files = {}

    def load(self, path):
        path = str(Path(path).resolve())
        require(path in self.hashes, f'Source was not included in the accepted revision: {path}')
        if path not in self.files:
            data = regular_bytes(path)
            require(hashlib.sha256(data).hexdigest() == self.hashes[path], f'Accepted source changed: {path}')
            require(sum(map(len, self.files.values())) + len(data) <= MAX_TOTAL_BYTES,
                    'Test-plan HTML source budget exceeded')
            self.files[path] = data
        return self.files[path]

    def read(self, path):
        return self.load(path).decode('utf-8')

    def verify(self):
        for path, data in self.files.items():
            require(regular_bytes(path) == data, f'Source changed while building HTML: {path}')


def remote_source(source):
    parsed = urlsplit(source)
    if not parsed.scheme and not source.startswith('//'):
        return False
    require(parsed.scheme in ('https', 'http') and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not any(ord(char) <= 32 or ord(char) == 127 for char in source),
            'Requirement source must use a safe HTTP(S) URL or a local file')
    return True


def markdown_section(text, fragment):
    """Resolve explicit heading anchors or line ranges; never guess missing text."""
    lines = text.splitlines(keepends=True)
    require(bool(text.strip()), 'Requirement source is empty')
    fragment = unquote(fragment)
    if not fragment:
        return text
    match = re.fullmatch(r'L(\d+)(?:-L(\d+))?', fragment)
    if match:
        first = int(match[1]); last = int(match[2] or match[1])
        require(1 <= first <= last <= len(lines), 'Requirement line range is outside the source')
        return ''.join(lines[first-1:last])
    headings = []; counts = {}; fence = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence:
            continue
        heading = re.match(r'^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$', line.rstrip('\r\n'))
        if heading:
            title = heading[2].strip()
            slug = re.sub(r'[^\w\- ]', '', title.casefold()).replace(' ', '-')
            count = counts.get(slug, 0); counts[slug] = count + 1
            headings.append((index, len(heading[1]), slug + (f'-{count}' if count else '')))
    for position, (start, level, slug) in enumerate(headings):
        if slug != fragment:
            continue
        stop = next((index for index, depth, _ in headings[position+1:] if depth <= level), len(lines))
        section = ''.join(lines[start:stop]).strip()
        require(bool(section), 'Requirement section is empty')
        return section
    raise ValueError(f'Requirement anchor #{fragment} was not found in the source')


def accepted_bundle(state, rid):
    row = state['requests'].get(rid)
    require(row is not None and row['status'] == 'accepted'
            and row.get('review', {}).get('status') == 'passed', 'Final content review has not accepted this plan')
    require(row['assignment']['agent'] == 'qa-test-plan-writer'
            and row['result']['event'] == 'plan_ready', 'HTML requires an accepted plan_ready result')
    require(row['assignment'].get('plan_partition', {}).get('mode') != 'group',
            'Group result cannot publish a final HTML plan; accept integration first')
    snapshot = Snapshot(dict(row['result']['artifact_hashes']))
    for path in snapshot.hashes:
        snapshot.load(path)
    exchange.check_inputs(row)
    exchange.check_answers(state, row['assignment']['questionnaire_ids'])
    exchange.assignment_sources(state, row['assignment'])
    result = exchange.inspect_result(row['assignment'])
    require(result['artifact_hashes'] == row['result']['artifact_hashes'], 'Accepted test-plan artifacts changed')
    results = {rid:result}; reviews = []
    for review_id, other in state['requests'].items():
        if (other['status'] != 'accepted'
                or other['assignment'].get('review_target', {}).get('request_id') != rid):
            continue
        require(other['result']['event'] == 'review_passed', 'Unresolved content review blocks the HTML view')
        for path, digest in other['result']['artifact_hashes'].items():
            require(path not in snapshot.hashes or snapshot.hashes[path] == digest, 'Reviewer uses another source revision')
            snapshot.hashes[path] = digest
            snapshot.load(path)
        current = exchange.inspect_result(other['assignment'])
        require(current['artifact_hashes'] == other['result']['artifact_hashes'], 'Accepted reviewer artifacts changed')
        results[review_id] = current
        reviews.append(review_id)
    for cid in row['assignment']['criterion_ids']:
        definition = state['criteria'][cid]['definition']
        if definition.get('required_reviews'):
            bundles = None
            if definition.get('kind') == 'combination_coverage':
                from combination_coverage import check_criterion
                bundles = check_criterion(definition, state, results)
            check_required_reviews(definition, state, results, selected_authors={rid}, combination_bundles=bundles)
        if definition.get('required_review_agent'):
            require(row['owner_task_id'] and any(state['requests'][key]['assignment']['agent'] == definition['required_review_agent']
                        and state['requests'][key]['owner_task_id']
                        and state['requests'][key]['owner_task_id'] != row['owner_task_id']
                        for key in reviews), 'Required independent reviewer is missing')
    result_path = row['result']['path']
    message, _ = parse_message(snapshot.read(result_path))
    base = Path(row['assignment']['artifact_base'])
    payload = message['payload']
    catalog = read_json(snapshot.read(base / payload['test_cases_artifact']))
    errors = validate_catalog(catalog) + validate_plan(payload, 'plan_ready', catalog)
    require(not errors, '; '.join(errors))
    plan = snapshot.read(base / payload['plan_artifact'])
    requirements = []
    for item in payload['requirement_coverage']:
        source = item['source']
        if remote_source(source):
            body = None; href = source
        else:
            filename, _, fragment = source.partition('#')
            require(bool(filename), 'Requirement source needs a local file path')
            body = markdown_section(snapshot.read(base / filename), fragment)
            href = '#' + anchor('requirement', item['requirement_id'])
        requirements.append(dict(item, body=body, href=href))
    return dict(state=state, row=row, message=message, catalog=catalog, plan=plan,
                requirements=requirements, reviews=reviews, snapshot=snapshot, result_path=result_path)


def requirement_link(item):
    external = ' target="_blank" rel="noopener noreferrer"' if item['body'] is None else ''
    return f'<a class="requirement-link" href="{esc(item["href"])}"{external}>{esc(item["requirement_id"])}</a>'


def step_table(steps):
    rendered = render_step_table(steps)
    if rendered:
        return rendered
    rows = ''.join(f'<tr><td>{index}. {esc(step["name"])}</td><td><pre>{esc(step["expected_result"])}</pre></td><td>—</td></tr>'
                   for index, step in enumerate(steps, 1))
    return '<h3>Шаги</h3><table><thead><tr><th>Шаг</th><th>Ожидаемый результат</th><th>Тест-дата</th></tr></thead><tbody>' + rows + '</tbody></table>'


def render(bundle):
    state = bundle['state']; row = bundle['row']; payload = bundle['message']['payload']
    requirements = bundle['requirements']; cases = bundle['catalog']['test_cases']
    coverage = []; requirement_cards = []; case_cards = []
    for item in requirements:
        rid = item['requirement_id']
        case_links = ' '.join(f'<a href="#{anchor("case", sid)}">{esc(sid)}</a>'
                              for sid in item['scenario_ids']) or '—'
        coverage.append(f'<tr><td>{requirement_link(item)}</td><td>{case_links}</td>'
                        f'<td>{esc(item["priority"])}</td><td>{esc(item["verification"])}</td>'
                        f'<td>{esc(item["evidence"])}</td><td>{esc(item["status"])}</td></tr>')
        body = ('<div class="document">' + esc(item['body']) + '</div>' if item['body'] is not None
                else '<p>Текст требования находится во внешнем источнике. Ссылка у кейса открывает его напрямую.</p>')
        reason = '<p>Основание: ' + esc(item['reason']) + '</p>' if item.get('reason') else ''
        requirement_cards.append(f'<article class="card" id="{anchor("requirement", rid)}"><h3>{esc(rid)}</h3>'
            f'<p class="muted">Источник: {esc(item["source"])}</p>{body}{reason}<p>Кейсы: {case_links}</p></article>')
    for case in cases:
        links = [item for item in requirements if case['scenario_id'] in item['scenario_ids'] and item['status'] == 'ready']
        require(bool(links), f'Case has no ready requirement link: {case["scenario_id"]}')
        label = ' '.join(requirement_link(item) for item in links)
        search = ' '.join([case['scenario_id'], case['title']] + [item['requirement_id'] for item in links])
        conditions = ''.join('<li>' + esc(value) + '</li>' for value in case['preconditions'])
        extra = {key:value for key,value in case.items() if key not in
                 ('scenario_id', 'title', 'requirement_source', 'preconditions', 'steps')}
        metadata = ('<h3>Дополнительные сведения сценария</h3><pre>'
                    + esc(json.dumps(extra, ensure_ascii=False, allow_nan=False, indent=2)) + '</pre>') if extra else ''
        step_extra = {str(index):{key:value for key,value in step.items() if key not in
                      ('name','expected_result','request','response','test_data')}
                      for index,step in enumerate(case['steps'], 1)}
        step_extra = {key:value for key,value in step_extra.items() if value}
        metadata += ('<h3>Дополнительные сведения шагов</h3><pre>'
                     + esc(json.dumps(step_extra, ensure_ascii=False, allow_nan=False, indent=2)) + '</pre>') if step_extra else ''
        case_cards.append(f'<article class="card case" id="{anchor("case", case["scenario_id"])}" data-search="{esc(search.casefold())}">'
            f'<div class="case-heading"><span class="badge">{esc(case["scenario_id"])}</span><h3>{esc(case["title"])}</h3></div>'
            f'<p class="case-requirements">Покрывает требования: {label}</p><details><summary>Предусловия и шаги</summary>'
            f'<p class="muted">Основание ожиданий: {esc(case["requirement_source"])}</p><h3>Предусловия</h3><ul>{conditions}</ul>'
            f'<div class="table-scroll">{step_table(case["steps"])}</div>{metadata}</details></article>')
    questions = []
    for item in payload['open_questions']:
        resolution = ('<p>Решение: ' + esc(item['resolution']) + '<br>Источник решения: ' + esc(item['decision_source']) + '</p>'
                      if item['status'] == 'resolved' else '')
        questions.append(f'<article class="card"><h3>{esc(item["question_id"])} · {esc(item["question"])}</h3>'
            f'<p>Статус: {esc(item["status"])} · Влияет на: {esc(item["blocks"])}</p>{resolution}</article>')
    criteria = ''.join('<li>' + esc(value) + '</li>' for value in row['assignment']['acceptance_criteria'])
    review_rows = '<p>' + esc(row['review']['decision']) + '</p><p class="muted">Принято: ' + esc(row['review']['reviewed_at']) + '</p>'
    for review_id in bundle['reviews']:
        review_row = state['requests'][review_id]
        review_rows += ('<p>' + esc(review_id) + ' · ' + esc(review_row['assignment']['agent']) + ' · '
                        + esc(review_row['assignment']['review_target']['scope']) + ': review_passed</p>')
    fingerprints = ''.join(f'<tr><td>{esc(path)}</td><td><code>{esc(digest)}</code></td></tr>'
                           for path,digest in sorted(bundle['snapshot'].hashes.items()))
    values = dict(TITLE=esc(state['title']), TASK=esc(state['task_id']), REVISION=esc(payload['plan_revision']),
                  REQUEST=esc(row['assignment']['request_id']), GOAL=esc(row['assignment']['goal']), CRITERIA=criteria,
                  PLAN=esc(bundle['plan']), CASE_COUNT=str(len(cases)), REQUIREMENT_COUNT=str(len(requirements)),
                  CASES=''.join(case_cards), REQUIREMENTS=''.join(requirement_cards), COVERAGE=''.join(coverage),
                  QUESTIONS=''.join(questions) or '<p>Вопросов нет.</p>', REVIEWS=review_rows, FINGERPRINTS=fingerprints)
    # Single substitution prevents source text resembling a template marker from becoming HTML.
    return re.sub(r'\{\{([A-Z_]+)\}\}', lambda match:values[match[1]], TEMPLATE.read_text(encoding='utf-8'))


def write_once(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        require(regular_bytes(path) == data, 'HTML output already exists with different content; use another path')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(state_path, rid, output=None):
    """Keep acceptance intact and record a visible failure of this derived view."""
    def operation(state):
        row = state['requests'].get(rid)
        require(row is not None and row['status'] == 'accepted', 'Plan is not accepted')
        try:
            bundle = accepted_bundle(state, rid)
            destination = Path(output) if output is not None else Path(bundle['result_path']).with_suffix('.plan.html')
            require(not destination.is_symlink(), 'Symlinked HTML output is not supported')
            destination = destination.resolve()
            protected = set(bundle['snapshot'].hashes) | exchange.protected_paths(state_path, state)
            protected |= {value['assignment']['result_path'] for value in state['requests'].values()}
            require(str(destination) not in protected, 'HTML output would overwrite a source or task artifact')
            document = render(bundle).encode('utf-8')
            require(len(document) <= MAX_SOURCE_BYTES, 'HTML output exceeds the size limit')
            bundle['snapshot'].verify()
            write_once(destination, document)
            presentation = dict(status='ready', path=str(destination), sha256=hashlib.sha256(document).hexdigest(),
                plan_revision=bundle['message']['payload']['plan_revision'], source_hashes=bundle['snapshot'].hashes,
                review_request_ids=bundle['reviews'])
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
        print('Test-plan HTML error: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
