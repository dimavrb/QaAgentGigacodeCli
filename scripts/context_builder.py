"""Frozen role-specific instruction projections; no implicit truncation of evidence."""
import hashlib
import json
from pathlib import Path
import re

from qa_contract import ROLE_EVENTS
from read_test_data_guide import ROLE_SECTIONS, SECTIONS, selected_sections
from validate_test_cases import read_json, validate_catalog

ROOT = Path(__file__).resolve().parents[1]
QUALITY_ROLES = {'qa-analyst','qa-impact-analyst','qa-test-plan-writer','qa-feature-tester','qa-test-automation',
                 'qa-autotest-reviewer','qa-defect-investigator','qa-result-reviewer','qa-conclusion-reviewer'}
REVIEWERS = {'qa-result-reviewer','qa-autotest-reviewer','qa-conclusion-reviewer'}
CASE_ROLES = {'qa-test-plan-writer','qa-feature-tester','qa-test-automation','qa-autotest-reviewer'}
CASE_READERS = QUALITY_ROLES | {'qa-test-data-builder','qa-test-env-configurator'}
CASE_EXAMPLES = {'api': 'create.json', 'ui': 'create.json', 'kafka': 'persistence.json'}


def catalog_check(text, digest):
    """Describe structural validity of supplied JSON; never execute its contents."""
    if not text.lstrip().startswith(('{', '[')):
        return None
    try:
        data = read_json(text)
    except (ValueError, RecursionError) as exc:
        return dict(kind='json', sha256=digest, valid=False, errors=[str(exc)])
    if not isinstance(data, dict) or 'test_cases' not in data:
        return None
    errors = validate_catalog(data)
    return dict(kind='test_case_catalog', sha256=digest, valid=not errors,
                case_count=len(data['test_cases']) if isinstance(data.get('test_cases'), list) else None,
                errors=errors, validation='validate_test_cases.validate_catalog; structure only')


def sections(text):
    result = {'': []}
    current = ''
    for line in text.splitlines():
        if line.startswith('## '):
            current = line[3:].strip()
            if current in result:
                raise ValueError('Duplicate context heading: ' + current)
            result[current] = []
        result[current].append(line)
    return {name:'\n'.join(lines).strip() for name,lines in result.items()}


def select(text, names):
    parts = sections(text)
    if any(name not in parts for name in names):
        raise ValueError('Required context section missing: ' + ', '.join(n for n in names if n not in parts))
    return '\n\n'.join(parts[name] for name in names if parts[name]) + '\n'


def contract(text, role):
    names = ['Оболочка сообщения','События и обязательные данные','Частичное выполнение и готовность']
    if role == 'qa-analyst': names.append('Результат аналитика')
    if role == 'qa-impact-analyst': names.append('Результат импакт-анализа')
    if role == 'qa-test-plan-writer': names.append('Структура результатов тест-плана')
    body = select(text, names)
    # Keep role event rows and common types; role-specific paragraphs live in the role document.
    lines = []
    for line in body.splitlines():
        if line.startswith('| `') and not set(re.findall(r'`([^`]+)`',line.split('|')[1])) & ROLE_EVENTS[role]:
            continue
        if line.startswith(('Для `feature_test_complete`','Для `plan_ready`','Для `investigation_complete`')):
            continue
        lines.append(line)
    return '\n'.join(lines) + '\nРазрешённые события роли: ' + ', '.join(sorted(ROLE_EVENTS[role])) + '.\n'


def self_review(text, role):
    body = select(text, ['','Порядок','Четыре проверки','Формат'])
    lines = [line for line in body.splitlines() if not line.startswith('| `qa-') or line.startswith('| `' + role + '`')]
    return '\n'.join(lines) + '\n'


def profile(assignment, step):
    role = assignment['agent']
    mode = step.get('context_mode', 'compact')
    if mode not in ('compact','full'):
        raise ValueError('context_mode must be compact/full')
    extra=step.get('instruction_paths',[])
    if not isinstance(extra,list) or any(not isinstance(p,str) for p in extra):
        raise ValueError('instruction_paths must contain package paths')
    examples=[p for p in extra if p.startswith('examples/test-cases/') and p.endswith('.json')]
    scope=assignment.get('review_scope') or assignment.get('review_target',{}).get('scope')
    case_context=(role in CASE_ROLES or scope == 'test_cases'
                  or (role in CASE_READERS and (assignment.get('test_type') is not None or examples))
                  or (role in REVIEWERS and 'docs/TEST_CASE_STYLE.md' in extra))
    specs = {}
    def add(path, kind='full', names=None):
        specs[path] = dict(kind=kind, **({'sections':names} if names is not None else {}))
    add(f'.gigacode/agents/{role}.md')
    add('docs/CONTRACT.md','contract')
    add('docs/SELF_REVIEW.md','self_review')
    if role in QUALITY_ROLES: add('.gigacode/skills/qa-check-quality/SKILL.md')
    if role in ('qa-analyst','qa-test-plan-writer'):
        add('docs/TEST_PLAN.md','sections',['','Маршрут','Выбор сценариев до детализации','Смысловые дубли','Вопросы и частичная готовность','Адресные изменения и ревью','Проверка артефактов'])
        add('docs/TEST_DESIGN.md'); add('docs/TEST_CASE_STYLE.md'); add('docs/TEST_CASE_SELECTION.md')
    if role == 'qa-test-plan-writer':
        add('.gigacode/skills/qa-test-cases/SKILL.md'); add('docs/TEST_PLAN_HTML.md')
        add('docs/TEST_PLAN_PARTITION.md')
    if role == 'qa-test-env-configurator' or assignment.get('review_target', {}).get('scope') == 'environment':
        add('docs/ENVIRONMENT_READINESS.md')
    if role == 'qa-test-data-builder' or assignment.get('review_target', {}).get('scope') == 'data':
        add('docs/DATA_READINESS.md')
    if assignment.get('plan_partition'):
        add('docs/TEST_PLAN_PARTITION.md')
    if case_context:
        add('docs/TEST_CASE_REVIEW.md'); add('docs/TEST_CASE_STYLE.md')
        if role in ('qa-test-plan-writer','qa-result-reviewer'):
            add('docs/TEST_CASE_SELECTION.md'); add('.gigacode/skills/qa-test-cases/SKILL.md')
            add('docs/TEST_PLAN_HTML.md')
        test_type=assignment.get('test_type')
        if test_type is not None and (type(test_type) is not str or test_type not in CASE_EXAMPLES):
            raise ValueError('test_type must be api, ui or kafka for case context')
        types={test_type} if test_type else set()
        for path in examples:
            parts=path.split('/')
            if len(parts) != 4 or parts[2] not in CASE_EXAMPLES:
                raise ValueError('test-case examples must use examples/test-cases/<type>/<file>.json')
            types.add(parts[2])
            add(path,'case_example')
        for kind in sorted(types):
            add(f'examples/test-cases/{kind}/INDEX.md')
            if not any(p.startswith(f'examples/test-cases/{kind}/') for p in examples):
                add(f'examples/test-cases/{kind}/{CASE_EXAMPLES[kind]}','case_example')
    if role in ('qa-test-automation','qa-autotest-reviewer'):
        add('docs/AUTOTEST_QUALITY.md'); add('docs/AUTOTEST_NAMING.md'); add('docs/TEST_CASE_STYLE.md')
    if role in REVIEWERS:
        add('docs/CONTENT_REVIEW.md','sections',['','Назначение reviewer','Критерии приёмки'])
        if assignment.get('review_scope') == 'impact_selection' or assignment.get('review_target', {}).get('scope') == 'impact_selection':
            add('docs/IMPACT_ANALYSIS.md')
        if step.get('gateway_policy'): add('docs/REVIEW_EXECUTION.md','sections',['','Критерий запуска','Свежие доказательства'])
    if role == 'qa-conclusion-reviewer':
        add('docs/CONCLUSION_REVIEW.md','sections',['','Подключение к приёмке','Ответ и решения'])
    if assignment.get('review_discussion'):
        add('docs/REVIEW_DISCUSSION.md','sections',['','Раунд','Завершение','Независимость повторного ревью'])
    if role in ROLE_SECTIONS:
        extras=step.get('test_data_sections',[])
        if not isinstance(extras,list) or any(not isinstance(n,str) for n in extras):
            raise ValueError('test_data_sections must be section keys')
        names=selected_sections(role,extras)
        add('docs/TEST_DATA_CHECKLIST.md')
        add('docs/TEST_DATA.md','sections',[SECTIONS[n] for n in names])
    elif step.get('test_data_sections'):
        raise ValueError('test_data_sections requires a test-data role')
    if role == 'qa-stub-builder' or assignment.get('review_target', {}).get('scope') == 'stubs':
        add('docs/STUBS.md')
    if role == 'qa-impact-analyst':
        add('docs/IMPACT_ANALYSIS.md'); add('docs/QA_IMPROVEMENTS.md')
    if assignment.get('improvement_plan'): add('docs/QA_IMPROVEMENTS.md')
    if (assignment.get('evidence_session_id') or assignment.get('review_scope') == 'benchmark_evaluation'
            or assignment.get('review_target', {}).get('scope') == 'benchmark_evaluation'):
        add('docs/BENCHMARK_EVIDENCE_GATES.md')
    if role == 'qa-defect-investigator':
        add('docs/INVESTIGATION.md','sections',['','Назначение и сохранение','Продолжение','Принятие результата'])
        add('docs/DEFECT_VERIFICATION.md','sections',['','Когда включать','Порядок работы','Артефакт и проверка','Короткое сообщение пользователю'])
    if role == 'qa-feature-tester': add('docs/EXECUTION.md','sections',['','Общее условие вместо повторных попыток','Возобновление'])
    if role == 'qa-test-report-writer': add('docs/TEST_REPORTING.md')
    if role == 'qa-jira-defect-creator' or assignment.get('review_target', {}).get('scope') == 'jira_publication':
        add('docs/JIRA_PUBLICATION.md'); add('docs/ACTION_GATEWAY.md')
    if assignment.get('browser_pool_path') or assignment.get('test_type') == 'ui':
        add('.gigacode/skills/qa-browser-repro/SKILL.md'); add('docs/BROWSER_PROFILES.md')
    for path in extra:
        # Preserve declared extras; apply existing compact policy for known protocol documents.
        if path == 'docs/REVIEW_EXECUTION.md':
            add(path,'sections',['','Критерий запуска','Свежие доказательства'])
        elif path == 'docs/REVIEW_DISCUSSION.md':
            add(path,'sections',['','Раунд','Завершение','Независимость повторного ревью'])
        elif path not in specs: add(path)
    for path in specs:
        if not (ROOT/path).resolve().is_relative_to(ROOT):
            raise ValueError('Instruction path must stay inside package')
    if mode == 'full': specs={path:(spec if spec['kind'] == 'case_example' else dict(kind='full'))
                             for path,spec in specs.items()}
    # Fail before launching if a required section/schema changed.
    for path,spec in specs.items(): render((ROOT/path).read_text(), spec, role)
    return dict(schema_version=1,mode=mode,role=role,documents=specs)


def render(text, spec, role):
    kind=spec['kind']
    if kind == 'full': return text
    if kind == 'sections': return select(text,spec['sections'])
    if kind == 'contract': return contract(text,role)
    if kind == 'self_review': return self_review(text,role)
    if kind == 'case_example':
        checked=catalog_check(text,hashlib.sha256(text.encode('utf-8')).hexdigest())
        if not checked or checked['kind'] != 'test_case_catalog' or not checked['valid']:
            raise ValueError('Invalid test-case example: ' + '; '.join(checked['errors'] if checked else ['not a catalog']))
        return text
    raise ValueError('Unknown context projection')


def snapshots(step):
    import task_exchange as exchange
    frozen=step.get('context_profile')
    if frozen and exchange.fingerprint(Path(__file__)) != step['context_builder_sha256']:
        raise ValueError('Context builder changed; create a new plan')
    result={}
    for name,digest in step['instruction_hashes'].items():
        data=(ROOT/name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Managed instructions changed')
        text=data.decode('utf-8')
        result[name]=render(text,frozen['documents'][name],frozen['role']) if frozen else text
    return result


def correction_context(packet):
    """Full reviews remain in the source file; author receives all actionable failures."""
    findings=[]; checks=[]; limitations=[]; seen=set()
    for review in packet.get('reviews',[]) + packet.get('original_reviews',[]):
        payload=review['payload']
        limitations.extend(v for v in payload.get('limitations',[]) if v not in limitations)
        for name,target in [('findings',findings),('checks',checks),('conclusion_checks',checks),('execution_checks',checks),('combination_checks',checks)]:
            for item in payload.get(name,[]):
                if name != 'findings' and item.get('status') == 'passed': continue
                marker=(name,json.dumps(item,sort_keys=True,ensure_ascii=False))
                if marker not in seen:
                    seen.add(marker);target.append(dict(kind=name,**item))
    return dict(schema_version=1,target_request_id=packet['target_request_id'],target_sha256=packet['target_sha256'],
                findings=findings,unresolved_checks=checks,limitations=limitations,instruction=packet['instruction'])


def assemble(handoff, actions, expected_hashes=None):
    import task_exchange as exchange
    partition_scope = None
    if handoff['assignment'].get('plan_partition'):
        from partition_test_plan import load
        binding = handoff['assignment']['plan_partition']
        manifest, _, _ = load(binding['path'], binding['sha256'])
        partition_scope = dict(mode=binding['mode'], base_revision=manifest['base_revision'],
                               plan_revision=manifest['plan_revision'], group_id=binding.get('group_id'))
    inputs={}; checks={}; input_hashes={}; content_index={}; excluded=set(handoff['assignment'].get('blind_review_exclusions',[]))
    for name in handoff['assignment']['inputs']:
        data=Path(name).read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        input_hashes[name]=digest
        if expected_hashes and name in expected_hashes and digest != expected_hashes[name]:
            raise ValueError('Context input changed: ' + name)
        if name in excluded:
            inputs[name]=dict(unavailable='prior reviewer feedback excluded from independent assessment',sha256=digest);continue
        try: text=data.decode('utf-8')
        except UnicodeError:
            inputs[name]=dict(unavailable='binary input; use an authorized observer or report a limitation',sha256=digest);continue
        if name == handoff['assignment'].get('correction_feedback'):
            inputs[name]=dict(projection='correction_feedback',source_sha256=digest,content=correction_context(json.loads(text)))
            continue
        checked=catalog_check(text,digest)
        if checked:
            checks[name]=checked
        if text in content_index:
            inputs[name]=dict(input_ref=content_index[text],sha256=digest)
        else:
            inputs[name]=text;content_index[text]=name
    def content(path):
        if path in inputs: return dict(input_ref=path,sha256=input_hashes[path])
        return exchange.read_json(path)
    sources={s['request_id']:content(s['result_path']) for s in handoff.get('sources',[])}
    answers={s['questionnaire_id']:content(s['answers_path']) for s in handoff.get('answer_sources',[])}
    completed={aid:{'tool':row['tool'],'status':row['status'],'result':row.get('result')} for aid,row in actions.items()}
    return dict(handoff=handoff,input_contents=inputs,catalog_validations=checks,
                **({'partition_scope':partition_scope} if partition_scope else {}),
                source_reports=sources,applied_answers=answers,recorded_actions=completed)
