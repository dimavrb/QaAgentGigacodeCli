"""Blind semantic controls for the actual benchmark evaluator harness."""
import hashlib
import json
from pathlib import Path

from check_benchmark_review import verify_quote
from evidence_language import text_errors
from frozen_reports import Sources, rows, string, write_new, sha
from run_benchmark_grader import invoke_isolated, GRADING_SYSTEM
from run_agent_benchmark import terminal_envelope, terminal_response, usage_tokens


def public_cases(suite):
    result = []
    cases = rows(suite.get('cases'), 'control cases', 'case_id')
    if not cases: raise ValueError('empty controls')
    for cid, row in cases.items():
        for key in ('prompt', 'system', 'response'):
            string(row.get(key), cid + '.' + key)
        checks = rows(row.get('checks'), 'control checks', 'check_id')
        if not checks: raise ValueError('empty checks')
        for check in checks.values():
            string(check.get('expectation'), 'expectation')
            if check.get('gold_status') not in ('pass', 'fail'):
                raise ValueError('semantic controls need a definite expert verdict')
            string(check.get('gold_reason'), 'gold_reason')
            string(check.get('error_family'), 'error_family')
        if row.get('split') not in ('calibration', 'heldout'):
            raise ValueError('controls require a calibration/heldout split')
        result.append(dict(case_id=cid, prompt=row['prompt'], system=row['system'], response=row['response'],
            checks=[dict(check_id=c['check_id'], expectation=c['expectation']) for c in checks.values()]))
    return result


def prepare_controls(suite_path, output):
    sources = Sources(); suite = sources.load(suite_path, 'control suite')
    cases = public_cases(suite)
    # No gold labels, reasons, family names, split, source filenames or reviewer answers.
    package = dict(schema_version=1, cases=cases)
    sources.finish({}); write_new(output, package)
    return package


def run_controls(pack_path, output, *, cli=None, model=None, max_wall_time='5m', measurement_kind='live', runtime_config=None):
    if measurement_kind not in ('live', 'fixture'): raise ValueError('unknown measurement kind')
    pack_path, output = Path(pack_path).resolve(), Path(output).resolve()
    raw = output.with_suffix(output.suffix + '.raw.json')
    for path in (output, raw):
        if path.exists(): raise ValueError('control attempt already exists')
    sources = Sources(); package = sources.load(pack_path, 'public controls')
    if not isinstance(package.get('cases'), list) or not package['cases']:
        raise ValueError('empty public controls')
    allowed = {'case_id', 'prompt', 'system', 'response', 'checks'}
    if set(package) != {'schema_version', 'cases'} or any(set(c) != allowed for c in package['cases']):
        raise ValueError('private labels cannot be passed to the evaluator')
    for row in package['cases']:
        if any(set(c) != {'check_id', 'expectation'} for c in row['checks']):
            raise ValueError('private check labels cannot be passed to the evaluator')
    prompt = json.dumps(dict(cases=package['cases']), ensure_ascii=False)
    completed, duration = invoke_isolated(prompt, cli=cli, model=model, max_wall_time=max_wall_time, runtime_config=runtime_config)
    with raw.open('x', encoding='utf-8') as stream: stream.write(completed.stdout)
    envelope, grades, error = {}, None, None
    try:
        envelope = terminal_envelope(completed.stdout)
        if completed.returncode: raise ValueError('evaluator execution failed')
        response, _ = terminal_response(completed.stdout)
        grades = json.loads(response)
        if not isinstance(grades, dict) or set(grades) != {'cases'}:
            raise ValueError('unknown evaluator result schema')
    except (ValueError, TypeError, KeyError) as exc:
        error = str(exc)
    tokens = usage_tokens(envelope)
    result = sources.finish(dict(schema_version=1, measurement_kind=measurement_kind,
        model_requested=model, status='failed' if error else 'captured', error=error, exit_code=completed.returncode,
        pack_sha256=sha(pack_path), raw_path=str(raw), raw_sha256=sha(raw), grades=grades,
        duration_seconds=duration, input_tokens=tokens[0], output_tokens=tokens[1],
        system_sha256=hashlib.sha256(GRADING_SYSTEM.encode()).hexdigest(),
        tools='disabled', cwd='empty_temporary_directory'))
    write_new(output, result)
    return result


def check_controls(suite_path, pack_path, review_path):
    sources = Sources(); suite = sources.load(suite_path, 'control suite')
    public = public_cases(suite)
    package = sources.load(pack_path, 'public controls')
    if package != dict(schema_version=1, cases=public):
        raise ValueError('public package differs from the gold suite')
    run = sources.load(review_path, 'evaluator capture')
    if run.get('measurement_kind') not in ('live', 'fixture'):
        raise ValueError('unknown control measurement kind')
    if run.get('pack_sha256') != sha(pack_path): raise ValueError('capture belongs to another package')
    raw_path = sources.reference(Path(review_path).resolve().parent,
        dict(path=run.get('raw_path'), sha256=run.get('raw_sha256')), 'evaluator raw')
    try:
        envelope = terminal_envelope(raw_path.read_text())
    except ValueError:
        if run.get('status') != 'failed': raise
        envelope = {}
    tokens = usage_tokens(envelope)
    if (run.get('input_tokens'), run.get('output_tokens')) != tokens:
        raise ValueError('capture usage differs from raw output')
    if run.get('status') == 'failed':
        return sources.finish(dict(schema_version=1, status='execution_failed', measurement_kind=run['measurement_kind'],
            input_tokens=tokens[0], output_tokens=tokens[1], promotion_eligible=False))
    if run.get('status') != 'captured' or run.get('exit_code') != 0:
        raise ValueError('unknown evaluator execution status')
    if run.get('system_sha256') != hashlib.sha256(GRADING_SYSTEM.encode()).hexdigest():
        raise ValueError('controls used a different evaluator instruction')
    if run.get('tools') != 'disabled' or run.get('cwd') != 'empty_temporary_directory':
        raise ValueError('evaluator isolation is missing')
    response, _ = terminal_response(raw_path.read_text())
    if json.loads(response) != run.get('grades'): raise ValueError('grades differ from primary raw output')
    grades = rows(run['grades'].get('cases'), 'grades', 'case_id')
    expected = {c['case_id']: c for c in suite['cases']}
    if grades.keys() != expected.keys(): raise ValueError('incomplete control cases')
    factors = []
    for cid, case in expected.items():
        checks = rows(grades[cid].get('checks'), 'grades.checks', 'check_id')
        gold = {c['check_id']: c for c in case['checks']}
        if checks.keys() != gold.keys(): raise ValueError('incomplete control checks')
        for key, check in checks.items():
            verdict = check.get('status')
            if verdict not in ('pass', 'fail', 'not_checked'): raise ValueError('unknown control verdict')
            evidence = string(check.get('evidence'), 'evidence')
            if verify_quote(evidence, case['response']): raise ValueError('control quote differs from primary response')
            note = string(check.get('note'), 'note')
            if text_errors(note): raise ValueError('invalid writing system in control note')
            target = gold[key]
            factors.append(dict(case_id=cid, check_id=key, split=case['split'], error_family=target['error_family'],
                gold_status=target['gold_status'], observed_status=verdict, correct=verdict == target['gold_status']))
    def counts(values):
        failures = sum(v['gold_status'] == 'fail' for v in values)
        found = sum(v['gold_status'] == 'fail' and v['observed_status'] == 'fail' for v in values)
        return dict(checks=len(values), correct=sum(v['correct'] for v in values),
            false_approvals=sum(v['gold_status'] == 'fail' and v['observed_status'] == 'pass' for v in values),
            false_blocks=sum(v['gold_status'] == 'pass' and v['observed_status'] == 'fail' for v in values),
            abstentions=sum(v['observed_status'] == 'not_checked' for v in values), missed_errors=failures-found,
            error_detection_recall=found/failures if failures else None)
    metrics = counts(factors)
    reviewed_by = suite.get('reviewed_by')
    if reviewed_by is not None: string(reviewed_by, 'reviewed_by')
    return sources.finish(dict(schema_version=1, status='scored', measurement_kind=run['measurement_kind'],
        gold_reviewed_by=reviewed_by, provisional_gold=reviewed_by is None,
        metrics=metrics, by_split={s: counts([v for v in factors if v['split'] == s]) for s in ('calibration', 'heldout')},
        factors=factors, input_tokens=tokens[0], output_tokens=tokens[1], promotion_eligible=False,
        limitations=['Совпадение с контрольной разметкой не доказывает правильность всех будущих оценок.']))
