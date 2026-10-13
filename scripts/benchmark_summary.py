"""Command-derived metrics and calibrated comparison; no free-form numeric claims."""
from __future__ import annotations
from agent_runtime import assert_same_runtime

from collections import Counter
from decimal import Decimal
import itertools
import json
from pathlib import Path
import secrets
import statistics

from benchmark_grading import read, sha, check_pack_review, verify_pack
from compare_profile_skills import load_run
from evidence_language import text_errors
from run_agent_benchmark import terminal_envelope, usage_tokens


def collect_run(directory):
    root, run, suite, records = load_run(directory)
    if type(run.get('schema_version')) is not int or run['schema_version'] != 1 or run.get('mode') != 'reasoning_only' or run.get('measurement_kind') not in ('live', 'fixture'):
        raise ValueError('unrecognized run schema/mode')
    hashes = {str(root / 'run.json'): sha(root / 'run.json'), str(root / 'suite.json'): sha(root / 'suite.json')}
    for case in suite['cases']:
        path = root / case['prompt_file']; hashes[str(path)] = sha(path)
    counts, seconds, input_tokens, output_tokens, response_chars, issues = Counter(), [], [], [], [], []
    response_hashes = {}
    statuses = Counter()
    for (trial, cid), record in sorted(records.items()):
        directory = record['directory']
        for name in ('execution.json', 'raw.json', 'prompt.md', 'system.md', 'instructions.json'):
            path = directory / name; hashes[str(path)] = sha(path)
        raw = terminal_envelope((directory / 'raw.json').read_text())
        inp, out = usage_tokens(raw)
        input_tokens.append(inp); output_tokens.append(out)
        execution = read(directory / 'execution.json')
        if execution.get('status') not in ('captured', 'failed', 'timed_out', 'interrupted'):
            raise ValueError('unrecognized execution status')
        value = execution.get('seconds')
        if type(value) not in (int, float) or value < 0:
            raise ValueError('unrecognized execution duration')
        seconds.append(Decimal(str(value)))
        statuses[execution['status']] += 1
        counts.update(check['status'] for check in record['row']['checks'])
        path = directory / 'response.md'
        if path.exists():
            response = path.read_text()
            hashes[str(path)] = sha(path)
            response_hashes[f'{trial}/{cid}'] = sha(path)
            response_chars.append(len(response))
            found = text_errors(response)
            if found:
                issues.append(dict(trial=trial, case_id=cid, foreign_words=sum(e.startswith('foreign') for e in found),
                                   mixed_words=sum(e.startswith('mixed') for e in found), source_sha256=sha(path)))
        else:
            response_chars.append(0)
    for trial in range(1, run.get('repeats', 0) + 1):
        path = root / f'trial-{trial}/review.json'
        if path.exists(): hashes[str(path)] = sha(path)
        receipt = root / f'trial-{trial}/review-acceptance.json'
        if receipt.exists():
            hashes[str(receipt)] = sha(receipt)
            binding = read(receipt)
            review = Path(binding['review_path']); pack = Path(binding['pack_path'])
            hashes[str(review)] = sha(review); hashes[str(pack / 'manifest.json')] = sha(pack / 'manifest.json')
            for suffix in ('.execution.json', '.raw.json'):
                audit = review.with_suffix(review.suffix + suffix)
                if audit.exists(): hashes[str(audit)] = sha(audit)
            for relative in read(pack / 'manifest.json')['files']:
                hashes[str(pack / relative)] = sha(pack / relative)
    def measured(values):
        return dict(known=sum(v for v in values if v is not None), missing=sum(v is None for v in values),
                    total=sum(values) if values and all(v is not None for v in values) else None)
    total_checks = sum(len(case['checks']) for case in suite['cases']) * run['repeats']
    data = dict(run=str(root), suite_sha256=run['suite_sha256'], model_requested=run.get('model_requested'),
                measurement_kind=run.get('measurement_kind'), expected_checks=total_checks,
                observed_checks=sum(counts.values()), counts={s: counts[s] for s in ('pass', 'fail', 'not_checked')},
                execution_counts=dict(statuses), seconds=float(sum(seconds)), response_characters=sum(response_chars),
                input_tokens=measured(input_tokens), output_tokens=measured(output_tokens),
                writing_system_issues=issues, response_hashes=response_hashes,
                stage_closed=run['status'] == 'captured' and sum(counts.values()) == total_checks and counts['not_checked'] == 0,
                quality_passed=run['status'] == 'captured' and counts['pass'] == total_checks)
    data['summary_text'] = f"Оценок: {data['observed_checks']} из {total_checks}. pass: {counts['pass']}, fail: {counts['fail']}, not_checked: {counts['not_checked']}. Сумма длительностей захвата ответов: {data['seconds']} с. Символов в ответах: {data['response_characters']}."
    return data, hashes, run


def calibration(config):
    if not isinstance(config, dict) or config.get('schema_version') != 1:
        raise ValueError('unrecognized calibration schema')
    pack = Path(config['pack_path']).resolve()
    manifest, suite = verify_pack(pack)
    evaluations = config.get('evaluations')
    if not isinstance(evaluations, list) or len(evaluations) < 2:
        raise ValueError('dispersion requires at least two independent complete evaluations of fixed answers')
    values, scores, ids, review_paths, hashes = [], [], set(), set(), {str(pack / 'manifest.json'): sha(pack / 'manifest.json')}
    for relative in manifest['files']:
        hashes[str(pack / relative)] = sha(pack / relative)
    for evaluation in evaluations:
        if not isinstance(evaluation, dict) or not isinstance(evaluation.get('evaluation_id'), str) or not evaluation['evaluation_id']:
            raise ValueError('calibration needs explicit evaluation IDs')
        if evaluation['evaluation_id'] in ids: raise ValueError('duplicate calibration evaluation')
        ids.add(evaluation['evaluation_id'])
        paths = evaluation.get('review_paths')
        if not isinstance(paths, list) or len(paths) != manifest['repeats']:
            raise ValueError('calibration evaluation must cover every trial')
        verdicts, trials = {}, set()
        for ref in paths:
            path = Path(ref).resolve()
            if str(path) in review_paths: raise ValueError('calibration cannot reuse the same review')
            review_paths.add(str(path))
            review, _ = check_pack_review(pack, path)
            trial = review['grading_trial']
            if trial in trials: raise ValueError('duplicate calibration trial')
            trials.add(trial); hashes[str(path)] = sha(path)
            for case in review['cases']:
                for check in case['checks']:
                    if check['status'] not in ('pass', 'fail'):
                        raise ValueError('dispersion cannot be measured from unchecked factors')
                    verdicts[trial, case['case_id'], check['check_id']] = check['status']
        values.append(verdicts); scores.append(sum(v == 'pass' for v in verdicts.values()))
    if any(set(v) != set(values[0]) for v in values): raise ValueError('calibration factor sets differ')
    sigma = statistics.stdev(scores)
    disagreement = max(sum(a[key] != b[key] for key in a) for a, b in itertools.combinations(values, 2))
    threshold = max(disagreement, 2 * sigma)
    response_hashes = {f'{trial}/{case["case_id"]}': manifest['files'][f'trial-{trial}/{case["case_id"]}/response.md']
                       for trial in range(1, manifest['repeats'] + 1) for case in suite['cases']}
    return dict(schema_version=1, suite_sha256=manifest['suite_sha256'], total_checks=len(values[0]),
                evaluations=len(values), scores=scores, sigma_passes=sigma, max_disagreements=disagreement,
                threshold_passes=threshold, policy='max_observed_disagreements_or_two_sigma',
                response_hashes=response_hashes), hashes


def build_summary(run_directory, *, session_id, candidate=None, calibration_path=None, changed_dimension=None):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError('summary requires the coordinator session_id')
    left, hashes, left_run = collect_run(run_directory)
    result = dict(schema_version=1, kind='benchmark_evidence_summary', session_id=session_id, baseline=left,
                  comparison=None, source_hashes=hashes)
    if candidate is not None:
        right, right_hashes, right_run = collect_run(candidate)
        hashes.update(right_hashes)
        if not left['stage_closed'] or not right['stage_closed']:
            raise ValueError('comparison requires closed, file-validated evaluation stages')
        assert_same_runtime(left_run, right_run)
        permitted = {'model_requested', 'self_review', 'check_quality', 'profile_skills', 'agent_version'}
        if changed_dimension not in permitted:
            raise ValueError('comparison requires one declared changed_dimension')
        for key in ('suite_sha256', 'repeats', 'model_requested',
                    'self_review', 'check_quality', 'profile_skills', 'agent_version', 'memory', 'mode', 'max_wall_time', 'measurement_kind'):
            if key != changed_dimension and (key not in left_run or key not in right_run or left_run[key] != right_run[key]):
                raise ValueError('comparison conditions differ or are missing: ' + key)
        if calibration_path is None: raise ValueError('comparison requires measured evaluator dispersion')
        config_path = Path(calibration_path).resolve()
        calibrated, cal_hashes = calibration(read(config_path))
        hashes.update(cal_hashes); hashes[str(config_path)] = sha(config_path)
        if (calibrated['suite_sha256'] != left['suite_sha256'] or calibrated['total_checks'] != left['expected_checks']
                or calibrated['response_hashes'] not in (left['response_hashes'], right['response_hashes'])):
            raise ValueError('dispersion measured on different answers/rubric/scale')
        delta = right['counts']['pass'] - left['counts']['pass']
        detected = abs(delta) > calibrated['threshold_passes']
        verdict = ('candidate_higher' if delta > 0 else 'baseline_higher') if detected else 'no_detected_difference'
        result.update(candidate=right, calibration=calibrated,
                      comparison=dict(verdict=verdict, delta_passes=delta, difference_price_passes=abs(delta),
                                      difference_price_percent=100 * abs(delta) / left['expected_checks'],
                                      threshold_passes=calibrated['threshold_passes'],
                                      message=('Больше проходов у кандидата' if delta > 0 else 'Больше проходов у базового варианта')
                                              if detected else 'различий не обнаружено'))
    result['summary_text'] = left['summary_text']
    if result['comparison'] is not None:
        result['summary_text'] += ' ' + result['comparison']['message'] + f". Цена различия: {result['comparison']['difference_price_passes']} из {left['expected_checks']}."
    # Recheck all inputs before publishing numbers. No changed file is blessed by a new hash.
    if any(sha(path) != checksum for path, checksum in hashes.items()):
        raise ValueError('summary inputs changed during calculation')
    return result


def check_summary(path, session_id):
    saved = read(path)
    if not isinstance(saved, dict) or saved.get('kind') != 'benchmark_evidence_summary' or saved.get('session_id') != session_id:
        raise ValueError('unrecognized summary or different coordinator session')
    params = saved.get('command_inputs')
    if not isinstance(params, dict): raise ValueError('summary lacks command-derived inputs')
    expected = build_summary(**params, session_id=session_id)
    # The command nonce identifies this invocation; all factual fields are recomputed.
    command_id = saved.get('command_id')
    if not isinstance(command_id, str) or len(command_id) != 32:
        raise ValueError('summary lacks its command receipt')
    expected.update(command_inputs=params, command_id=command_id)
    if saved != expected: raise ValueError('summary contains unsupported numbers, verdicts or source hashes')
    return expected


def command_result(run_directory, session_id, candidate=None, calibration_path=None, changed_dimension=None):
    params = dict(run_directory=str(Path(run_directory).resolve()),
                  candidate=str(Path(candidate).resolve()) if candidate else None,
                  calibration_path=str(Path(calibration_path).resolve()) if calibration_path else None,
                  changed_dimension=changed_dimension)
    result = build_summary(**params, session_id=session_id)
    result.update(command_inputs=params, command_id=secrets.token_hex(16))
    return result
