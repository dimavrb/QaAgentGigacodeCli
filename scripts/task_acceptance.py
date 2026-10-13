"""Explicit task scope and coordinator assessments bound to accepted evidence."""
import copy
import re

from qa_contract import ROLE_EVENTS


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}: нужна непустая строка')
    return value.strip()


def criteria_packet(packet):
    if not isinstance(packet, dict) or type(packet.get('schema_version')) is not int or packet['schema_version'] != 1:
        raise ValueError('criteria: нужен объект schema_version=1')
    rows = packet.get('criteria')
    if not isinstance(rows, list) or not rows:
        raise ValueError('criteria: нужен непустой список')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('criterion: нужен объект')
        item = {key: text(row.get(key), key) for key in ('criterion_id', 'description', 'source')}
        cid = item['criterion_id']
        if cid in result:
            raise ValueError('criterion_id повторяется')
        if type(row.get('required')) is not bool:
            raise ValueError('required: нужен bool')
        item['required'] = row['required']
        kind = row.get('kind', 'judgment')
        if kind not in ('judgment', 'scenario_execution', 'combination_coverage'):
            raise ValueError('kind: judgment/scenario_execution/combination_coverage')
        if 'kind' in row:
            item['kind'] = kind
        if kind == 'scenario_execution':
            ids = row.get('scenario_ids')
            if (not isinstance(ids, list) or not ids or
                    any(not isinstance(sid, str) or not sid.strip() for sid in ids) or len(set(ids)) != len(ids)):
                raise ValueError('scenario_execution: нужны уникальные scenario_ids')
            item['scenario_ids'] = list(ids)
            context = row.get('execution_context', {})
            if (not isinstance(context, dict) or set(context) - {'environment_id', 'product_version'}
                    or any(not isinstance(v, str) or not v.strip() for v in context.values())):
                raise ValueError('execution_context: environment_id/product_version')
            item['execution_context'] = dict(context)
        elif 'scenario_ids' in row or 'execution_context' in row:
            raise ValueError('scenario_ids/execution_context требуют kind=scenario_execution')
        if kind == 'combination_coverage':
            item['combination_bundle_id'] = text(row.get('combination_bundle_id'), 'combination_bundle_id')
            if not re.fullmatch('[0-9a-f]{64}', str(row.get('model_sha256', ''))):
                raise ValueError('combination_coverage requires agreed model_sha256')
            item['model_sha256'] = row['model_sha256']
            if not row.get('required_reviews'):
                raise ValueError('combination_coverage requires independent required_reviews')
        elif 'combination_bundle_id' in row or 'model_sha256' in row:
            raise ValueError('combination_bundle_id/model_sha256 требуют kind=combination_coverage')
        if 'required_reviews' in row:
            slots = row['required_reviews']
            if not isinstance(slots, list) or not slots:
                raise ValueError('required_reviews: нужен непустой список')
            normalized, scopes = [], set()
            for slot in slots:
                if not isinstance(slot, dict) or slot.get('agent') not in ('qa-result-reviewer', 'qa-autotest-reviewer', 'qa-conclusion-reviewer'):
                    raise ValueError('required_reviews: неизвестная роль reviewer')
                scope = text(slot.get('scope'), 'review scope')
                if scope in scopes:
                    raise ValueError('review scope повторяется')
                scopes.add(scope)
                review = dict(scope=scope, agent=slot['agent'])
                if slot['agent'] == 'qa-conclusion-reviewer':
                    from conclusion_review import conclusion_paths
                    review['conclusions'] = conclusion_paths(slot.get('conclusions', ['/summary']))
                elif 'conclusions' in slot:
                    raise ValueError('conclusions require qa-conclusion-reviewer')
                if 'execution' in slot:
                    from review_execution import execution_specs
                    review['execution'] = execution_specs(slot['execution'])
                normalized.append(review)
            item['required_reviews'] = normalized
        reviewer = row.get('required_review_agent')
        if reviewer is not None:
            if reviewer not in ROLE_EVENTS:
                raise ValueError('required_review_agent: неизвестная роль')
            item['required_review_agent'] = reviewer
        result[cid] = dict(definition=item, assessment=None)
    return result


def check_completion(state, check_assessment):
    if state.get('acceptance_mode', 'legacy') == 'legacy':
        return
    rows = state.get('criteria', {})
    if not rows:
        raise ValueError('Нет согласованных критериев задачи')
    for cid, row in rows.items():
        assessment = row['assessment']
        if row['definition']['required'] and (
                not assessment or assessment['status'] not in ('verified', 'not_applicable')):
            raise ValueError(f'{cid}: обязательный критерий не принят')
        if assessment:
            check_assessment(state, row['definition'], copy.deepcopy(assessment), recheck=True)
