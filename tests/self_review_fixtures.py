"""Synthetic review for protocol tests; no model or product checks are claimed."""

def review_for(*, limited=False):
    return dict(schema_version=1, checks=[
        dict(id=cid, status='limited' if limited else 'passed',
             note='Синтетический результат проверяется только как протокол.',
             evidence=['assignment:/acceptance_criteria'] if cid == 'scope' else ['report:/payload'])
        for cid in ('scope', 'evidence', 'consistency', 'role')
    ], fixes=[], remaining_risks=['Учебное недостающее условие'] if limited else [])
