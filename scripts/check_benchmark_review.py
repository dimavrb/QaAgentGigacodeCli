#!/usr/bin/env python3
"""Check a manual benchmark review; never infer semantic correctness from text."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / 'examples/benchmark/suite.json'


def load_suite(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    suite = json.loads(raw)
    if not isinstance(suite, dict) or type(suite.get('schema_version')) is not int or suite['schema_version'] != 1:
        raise ValueError('suite schema_version must be 1')
    if not isinstance(suite.get('cases'), list) or not suite['cases']:
        raise ValueError('suite needs cases')
    digest = hashlib.sha256(raw)
    seen = set()
    for case in suite['cases']:
        if not isinstance(case, dict) or not isinstance(case.get('case_id'), str) or not case['case_id'].strip() or case['case_id'] in seen:
            raise ValueError('suite case IDs must be nonempty and unique')
        seen.add(case['case_id'])
        if not isinstance(case.get('checks'), list) or not case['checks']:
            raise ValueError('case needs checks')
        check_ids = set()
        for check in case['checks']:
            if (not isinstance(check, dict) or not isinstance(check.get('check_id'), str) or not check['check_id'].strip()
                    or check['check_id'] in check_ids or not isinstance(check.get('expectation'), str) or not check['expectation'].strip()):
                raise ValueError('suite checks need unique IDs and expectations')
            check_ids.add(check['check_id'])
        if not isinstance(case.get('prompt_file'), str) or not case['prompt_file'].strip():
            raise ValueError('case needs prompt_file')
        prompt_path = Path(case['prompt_file'])
        if prompt_path.is_absolute() or '..' in prompt_path.parts:
            raise ValueError('prompt_file must stay inside suite directory')
        if not all(c.isalnum() or c in '-_' for c in case['case_id']):
            raise ValueError('case ID must be filesystem-safe')
        digest.update(case['prompt_file'].encode('utf-8'))
        digest.update((path.parent / case['prompt_file']).read_bytes())
    return suite, digest.hexdigest()


def make_template(suite: dict, fingerprint: str, version: str) -> dict:
    return dict(schema_version=1, suite_sha256=fingerprint, agent_version=version,
                model=None, configuration=None, reviewer=None,
                cases=[dict(case_id=case['case_id'], output_artifact=None,
                            output_sha256=None, seconds=None, input_tokens=None,
                            output_tokens=None,
                            checks=[dict(check_id=check['check_id'], status='not_checked',
                                         evidence='') for check in case['checks']])
                       for case in suite['cases']])


def verify_quote(evidence, response):
    """Check literal fragments; short-only evidence cannot pass vacuously."""
    normalize = lambda s: ' '.join(s.split())
    fragments = [normalize(part) for part in re.split(r'…|\.{3,}', evidence)]
    fragments = [part for part in fragments if len(part) > 12]
    if not fragments:
        return ['evidence needs at least one literal fragment longer than 12 characters']
    source = normalize(response)
    return ['evidence quote not found in corresponding response: ' + part for part in fragments if part not in source]


def validate_review(review: object, suite: dict, fingerprint: str, base: Path, quote_root: Path | None = None) -> list[str]:
    errors = []
    if not isinstance(review, dict):
        return ['review must be an object']
    if type(review.get('schema_version')) is not int or review['schema_version'] != 1:
        errors.append('schema_version must be 1')
    if review.get('suite_sha256') != fingerprint:
        errors.append('suite_sha256 differs from the suite and prompts')
    cases = review.get('cases')
    if not isinstance(cases, list):
        return errors + ['cases must be a list']
    expected = {case['case_id']: case for case in suite['cases']}
    seen = set()
    reviewed_any = False
    for index, case in enumerate(cases):
        prefix = f'cases[{index}]'
        if not isinstance(case, dict) or not isinstance(case.get('case_id'), str):
            errors.append(f'{prefix} must contain case_id')
            continue
        case_id = case['case_id']
        if case_id not in expected or case_id in seen:
            errors.append(f'{prefix}: unknown or duplicate case_id {case_id}')
            continue
        seen.add(case_id)
        for metric in ('seconds', 'input_tokens', 'output_tokens'):
            value = case.get(metric)
            valid = (value is None or
                     (type(value) in (int, float) and value >= 0 and
                      (type(value) is int or math.isfinite(value))))
            if metric.endswith('tokens') and value is not None:
                valid = valid and type(value) is int
            if not valid:
                errors.append(f'{prefix}.{metric} must be non-negative or null (tokens: integers)')
        checks = case.get('checks')
        if not isinstance(checks, list):
            errors.append(f'{prefix}.checks must be a list')
            continue
        expected_checks = {check['check_id'] for check in expected[case_id]['checks']}
        seen_checks = set()
        checked = False
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get('check_id'), str):
                errors.append(f'{prefix}: invalid check')
                continue
            check_id = check['check_id']
            if check_id not in expected_checks or check_id in seen_checks:
                errors.append(f'{prefix}: unknown or duplicate check_id {check_id}')
            seen_checks.add(check_id)
            status = check.get('status')
            if status not in ('pass', 'fail', 'not_checked'):
                errors.append(f'{prefix}.{check_id}: unknown status')
            if status in ('pass', 'fail'):
                checked = True
                evidence = check.get('evidence')
                if not isinstance(evidence, str) or not evidence.strip():
                    errors.append(f'{prefix}.{check_id}: reviewed check needs evidence')
                elif quote_root is not None:
                    try:
                        response = (quote_root / case_id / 'response.md').read_text(encoding='utf-8')
                        errors.extend(f'{prefix}.{check_id}: {e}' for e in verify_quote(evidence, response))
                    except OSError as exc:
                        errors.append(f'{prefix}.{check_id}: cannot read corresponding response: {exc}')
        if seen_checks != expected_checks:
            errors.append(f'{prefix}: checks differ from the required rubric')
        reviewed_any |= checked
        artifact = case.get('output_artifact')
        checksum = case.get('output_sha256')
        if checked or artifact is not None or checksum is not None:
            if not isinstance(artifact, str) or not artifact.strip():
                errors.append(f'{prefix}: reviewed case needs output_artifact')
                continue
            try:
                actual = hashlib.sha256((base / artifact).read_bytes()).hexdigest()
                if checksum != actual:
                    errors.append(f'{prefix}: output_sha256 differs from saved output')
            except OSError as exc:
                errors.append(f'{prefix}: cannot read output: {exc}')
    if seen != set(expected):
        errors.append('cases differ from the required suite')
    if reviewed_any:
        for field in ('agent_version', 'model', 'configuration', 'reviewer'):
            value = review.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f'reviewed results need {field}')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, default=SUITE, help='Suite snapshot with prompt files')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--init', type=Path, help='Create an unreviewed template; never overwrite')
    mode.add_argument('--input', type=Path, help='Check a completed or partial manual review')
    parser.add_argument('--verify-quotes', type=Path, help='Run root or trial-N directory for independent literal-quote audit')
    parser.add_argument('--grading-pack', type=Path, help='Frozen grading package; required for accepting graded reviews')
    args = parser.parse_args()
    try:
        suite, fingerprint = load_suite(args.suite)
        if args.init:
            review = make_template(suite, fingerprint, (ROOT / 'VERSION').read_text().strip())
            with args.init.open('x', encoding='utf-8') as output:
                json.dump(review, output, ensure_ascii=False, indent=2)
                output.write('\n')
            print(f'Unreviewed template: {args.init.resolve()}')
            return 0
        review = json.loads(args.input.read_text(encoding='utf-8'))
        if not isinstance(review, dict):
            raise ValueError('review must be a JSON object')
        quote_root = None
        if args.verify_quotes:
            quote_root = args.verify_quotes.resolve()
            if not (quote_root / 'response.md').is_file() and (quote_root / 'run.json').is_file():
                trial = review.get('grading_trial')
                if trial is None and re.fullmatch(r'trial-[1-9][0-9]*', args.input.parent.name):
                    trial = int(args.input.parent.name.split('-')[1])
                if type(trial) is not int or trial < 1:
                    raise ValueError('--verify-quotes at run root requires grading_trial or trial-N review location')
                quote_root = quote_root / f'trial-{trial}'
        errors = validate_review(review, suite, fingerprint, args.input.parent, quote_root)
        graded = isinstance(review.get('cases'), list) and any(isinstance(c, dict) and isinstance(c.get('checks'), list)
                  and any(isinstance(k, dict) and k.get('status') in ('pass', 'fail') for k in c['checks']) for c in review['cases'])
        if graded and not args.verify_quotes and not args.grading_pack:
            errors.append('graded review acceptance requires --grading-pack; use --verify-quotes for legacy audit only')
        if args.grading_pack and not errors:
            from benchmark_grading import check_pack_review
            check_pack_review(args.grading_pack, args.input.resolve())
        if errors:
            for error in errors:
                print(error)
            return 2
        statuses = [check['status'] for case in review['cases'] for check in case['checks']]
        counts = {status: statuses.count(status) for status in ('pass', 'fail', 'not_checked')}
        print('Manual review: ' + ', '.join(f'{key}={value}' for key, value in counts.items()))
        print('Literal-source/format gates do not establish semantic correctness; missing usage remains unknown.')
        if args.verify_quotes and not args.grading_pack:
            print('Legacy quote audit only; does not authorize stage completion or run comparison.')
        return 0 if counts['fail'] == counts['not_checked'] == 0 else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Benchmark review error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
