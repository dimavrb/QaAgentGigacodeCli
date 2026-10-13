#!/usr/bin/env python3
"""Compare independently reviewed, paired runs; never grade or promote skills automatically."""
from __future__ import annotations
from agent_runtime import assert_same_runtime

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from check_benchmark_review import load_suite, validate_review
from profile_skills import PROFILE_SKILLS
from run_agent_benchmark import ROLES, sha, terminal_response, usage_tokens


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load_run(directory, require_grading=True):
    directory = Path(directory).resolve()
    run = read(directory / 'run.json')
    suite, fingerprint = load_suite(directory / 'suite.json')
    if run.get('suite_sha256') != fingerprint:
        raise ValueError('Run suite or prompts changed')
    if run.get('status') == 'unavailable':
        if run.get('trials') != []:
            raise ValueError('Unavailable run must not contain trials')
        return directory, run, suite, {}
    if run.get('status') not in ('captured', 'failed', 'interrupted'):
        raise ValueError('Unsupported run status')
    repeats = run.get('repeats')
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError('Invalid repeats')
    expected = {(i, c['case_id']) for i in range(1, repeats + 1) for c in suite['cases']}
    manifest_rows = {}
    for row in run.get('trials', []):
        key = row['trial'], row['case_id']
        if key not in expected or key in manifest_rows:
            raise ValueError('Unknown or duplicate trial')
        manifest_rows[key] = row
    if set(manifest_rows) != expected:
        raise ValueError('All declared trials are required; no cherry-picking')
    observed_dirs = {p.name for p in directory.glob('trial-*') if p.is_dir()}
    if observed_dirs != {f'trial-{i}' for i in range(1, repeats + 1)}:
        raise ValueError('Trial directories differ from declared repeats')
    records = {}
    for trial in range(1, repeats + 1):
        base = directory / f'trial-{trial}'
        review = read(base / 'review.json')
        errors = validate_review(review, suite, fingerprint, base, base)
        if errors:
            raise ValueError('; '.join(errors))
        for row in review['cases']:
            cid = row['case_id']
            case_dir = base / cid
            execution = read(case_dir / 'execution.json')
            if execution != manifest_rows[trial, cid]:
                raise ValueError('Execution differs from manifest')
            raw = (case_dir / 'raw.json').read_bytes()
            prompt = (case_dir / 'prompt.md').read_bytes()
            case = next(c for c in suite['cases'] if c['case_id'] == cid)
            system = (case_dir / 'system.md').read_bytes()
            snapshots = read(case_dir / 'instructions.json')
            body = '\n\n'.join(f'Источник инструкций: {name}\n{text}' for name, text in snapshots.items())
            # Compare content actually supplied to CLI, not a separately editable snapshot.
            if not system.decode().startswith(body + '\n\nУсловия этого учебного reasoning benchmark'):
                raise ValueError('Instruction snapshots differ from supplied system prompt')
            if (sha(raw) != execution.get('raw_sha256') or sha(system) != execution.get('instructions_sha256')
                    or sha(prompt) != execution.get('prompt_sha256')
                    or prompt != (directory / case['prompt_file']).read_bytes()):
                raise ValueError('Saved execution inputs/outputs changed')
            checked = any(c['status'] != 'not_checked' for c in row['checks'])
            if execution.get('status') != 'captured':
                if checked or row.get('output_artifact') is not None:
                    raise ValueError('Failed capture cannot receive semantic grades')
            else:
                response, envelope = terminal_response(raw.decode())
                if (row.get('output_artifact') != f'{cid}/response.md'
                        or (case_dir / 'response.md').read_text() != response):
                    raise ValueError('Review must refer to the exact terminal response')
                tokens = usage_tokens(envelope)
                if (row.get('input_tokens'), row.get('output_tokens')) != tokens:
                    raise ValueError('Review usage differs from captured usage')
            if row.get('seconds') != execution.get('seconds'):
                raise ValueError('Review duration differs from captured duration')
            records[trial, cid] = dict(row=row, review=review, snapshots=snapshots, system=system.decode(),
                                      status=execution.get('status'), directory=case_dir)
        if require_grading and any(c['status'] != 'not_checked' for row in review['cases'] for c in row['checks']):
            from benchmark_grading import check_accepted_review
            check_accepted_review(directory, trial)
    return directory, run, suite, records


def comparable(a, b):
    # Failed preflight has no executable fingerprint and cannot yield a measurement.
    # Preserve its diagnostic report; captured runs still require complete provenance.
    if a.get('status') != 'unavailable' and b.get('status') != 'unavailable':
        assert_same_runtime(a, b)
    for key in ('suite_sha256', 'agent_version', 'repeats', 'model_requested',
                'self_review', 'check_quality', 'memory', 'mode', 'max_wall_time', 'measurement_kind'):
        if key not in a or key not in b or a[key] != b[key]:
            raise ValueError(f'Conditions differ or are missing: {key}')
    if a.get('profile_skills') is not False or b.get('profile_skills') is not True:
        raise ValueError('Expected baseline profile_skills=off and candidate=on')
    if a.get('measurement_kind') not in ('live', 'fixture') or a.get('mode') != 'reasoning_only':
        raise ValueError('Unknown measurement mode')


def compare(baseline, candidate, calibration_path=None, session_id=None):
    ad, a, suite, ar = load_run(baseline)
    bd, b, _, br = load_run(candidate)
    comparable(a, b)
    roles_in_suite = {c.get('agent') or ROLES.get(c['case_id']) for c in suite['cases']}
    if not roles_in_suite.intersection(PROFILE_SKILLS):
        raise ValueError('Suite has no tasks for profile skills')
    if not roles_in_suite.difference(PROFILE_SKILLS):
        raise ValueError('Suite needs holdout tasks from other roles')
    report = dict(schema_version=1, baseline=str(ad), candidate=str(bd), suite_sha256=a['suite_sha256'],
                  measurement_kind=a['measurement_kind'], repeats=a['repeats'],
                  status='no_measurement', roles={}, limitations=[
                      'Independent semantic grades are supplied by the reviewer; this tool does not judge their truth.',
                      'Reasoning-only results do not prove live MCP/product execution or statistical significance.',
                      'No skill or default route is changed automatically.'])
    if a.get('status') == 'unavailable' or b.get('status') == 'unavailable':
        report['unavailable'] = {side: run.get('error') for side, run in [('baseline', a), ('candidate', b)]
                                 if run.get('status') == 'unavailable'}
        return report
    if set(ar) != set(br):
        raise ValueError('Trial sets differ')
    by_role = defaultdict(list)
    models, configurations = set(), set()
    incomplete = False
    candidate_hashes = defaultdict(set)
    for key in sorted(ar):
        trial, cid = key
        case = next(c for c in suite['cases'] if c['case_id'] == cid)
        role = case.get('agent') or ROLES.get(cid) or 'qa-coordinator'
        left, right = ar[key], br[key]
        expected_added = {PROFILE_SKILLS[role]} if role in PROFILE_SKILLS else set()
        ls, rs = left['snapshots'], right['snapshots']
        if (set(rs) - set(ls) != expected_added or set(ls) - set(rs)
                or any(ls[name] != rs[name] for name in ls)
                or any(name in ls for name in PROFILE_SKILLS.values())):
            raise ValueError(f'{cid}: A/B must change only the routed candidate skill')
        left_body = '\n\n'.join(f'Источник инструкций: {name}\n{text}' for name, text in ls.items())
        right_body = '\n\n'.join(f'Источник инструкций: {name}\n{text}' for name, text in rs.items())
        if left['system'][len(left_body):] != right['system'][len(right_body):]:
            raise ValueError('Benchmark overrides differ')
        for name in expected_added:
            candidate_hashes[name].add(sha(rs[name].encode()))
        for record, run in ((left, a), (right, b)):
            review = record['review']
            if any(c['status'] != 'not_checked' for c in record['row']['checks']):
                models.add(review['model'])
                configurations.add(review['configuration'].replace(';profile_skills=True;', ';profile_skills=AB;')
                                   .replace(';profile_skills=False;', ';profile_skills=AB;'))
            if record['status'] != 'captured':
                incomplete = True
        checks_left = {c['check_id']: c for c in left['row']['checks']}
        checks_right = {c['check_id']: c for c in right['row']['checks']}
        for check in case['checks']:
            lid = check['check_id']
            lc, rc = checks_left[lid], checks_right[lid]
            incomplete |= 'not_checked' in (lc['status'], rc['status'])
            by_role[role].append(dict(trial=trial, case_id=cid, check_id=lid, expectation=check['expectation'],
                                      baseline=lc['status'], candidate=rc['status'],
                                      baseline_evidence=lc['evidence'], candidate_evidence=rc['evidence']))
    if len(models) > 1 or len(configurations) > 1:
        raise ValueError('Reviewed actual models/provider configurations differ')
    if any(len(hashes) != 1 for hashes in candidate_hashes.values()):
        raise ValueError('Candidate instruction changed between trials')
    report['candidate_hashes'] = {name: next(iter(hashes)) for name, hashes in candidate_hashes.items()}
    report['model_reviewed'] = next(iter(models), None)
    holdout_regression = any(row['baseline'] == 'pass' and row['candidate'] == 'fail'
                             for role, rows in by_role.items() if role not in PROFILE_SKILLS for row in rows)
    for role, rows in by_role.items():
        counts = {arm: dict(Counter(row[arm] for row in rows)) for arm in ('baseline', 'candidate')}
        gains = [row for row in rows if row['baseline'] == 'fail' and row['candidate'] == 'pass']
        regressions = [row for row in rows if row['baseline'] == 'pass' and row['candidate'] == 'fail']
        failures = defaultdict(list)
        for row in rows:
            if row['baseline'] == 'fail':
                failures[row['case_id'], row['check_id']].append(row)
        weak_points = [dict(case_id=cid, check_id=check_id, failed_trials=len(items),
                           expectation=items[0]['expectation'], evidence=items)
                       for (cid, check_id), items in sorted(failures.items(), key=lambda item: -len(item[1]))]
        decision = 'holdout' if role not in PROFILE_SKILLS else 'no_demonstrated_gain'
        other_role_regression = any(r['baseline'] == 'pass' and r['candidate'] == 'fail'
                                    for other, other_rows in by_role.items() if other != role for r in other_rows)
        if role in PROFILE_SKILLS:
            if incomplete or a['repeats'] < 2:
                decision = 'pending_measurement'
            elif regressions or other_role_regression or any(row['candidate'] != 'pass' for row in rows):
                decision = 'revise_candidate'
            elif gains:
                decision = 'review_for_promotion' if a['measurement_kind'] == 'live' else 'fixture_only'
        # Cost is paired only where the entire case passed in both arms; null stays unknown.
        pairs = []
        for key in ar:
            case_rows = [r for r in rows if (r['trial'], r['case_id']) == key]
            if case_rows and all(r['baseline'] == r['candidate'] == 'pass' for r in case_rows):
                pairs.append((ar[key], br[key]))
        cost = dict(paired_passed_cases=len(pairs))
        for metric in ('seconds', 'input_tokens', 'output_tokens'):
            known = [p for p in pairs if all(r['row'].get(metric) is not None for r in p)]
            cost[metric] = dict(known_pairs=len(known), baseline_mean=None, candidate_mean=None)
            if known and len(known) == len(pairs):
                for arm, idx in (('baseline', 0), ('candidate', 1)):
                    cost[metric][arm + '_mean'] = sum(p[idx]['row'][metric] for p in known) / len(known)
        report['roles'][role] = dict(skill=PROFILE_SKILLS.get(role), decision=decision, counts=counts,
                                    improvements=gains, regressions=regressions, other_role_regression=other_role_regression,
                                    weak_points=weak_points, paired_cost=cost)
    report['status'] = ('pending_measurement' if incomplete else
                        'fixture_only' if a['measurement_kind'] == 'fixture' else 'measured')
    report['holdout_regression'] = holdout_regression
    if a['measurement_kind'] == 'live' and not incomplete:
        if calibration_path is None:
            report['status'] = 'pending_dispersion'
            for row in report['roles'].values():
                if row['decision'] == 'review_for_promotion': row['decision'] = 'pending_dispersion'
            report['limitations'].append('Measured evaluator dispersion is required before directional comparison or promotion.')
        else:
            from benchmark_summary import command_result
            gated = command_result(baseline, session_id, candidate, calibration_path, 'profile_skills')
            report['evidence_summary'] = gated
            threshold = gated['comparison']['threshold_passes']
            for row in report['roles'].values():
                if row['decision'] == 'review_for_promotion' and len(row['improvements']) <= threshold:
                    row['decision'] = 'no_demonstrated_gain'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='New report; never overwrite')
    parser.add_argument('--calibration', type=Path, help='Measured repeat-grading dispersion on fixed answers')
    parser.add_argument('--session-id', help='Coordinator evidence session; required with calibration')
    args = parser.parse_args()
    try:
        result = compare(args.baseline, args.candidate, args.calibration, args.session_id)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            with args.output.open('x', encoding='utf-8') as output:
                output.write(rendered)
        else:
            print(rendered, end='')
        return 0 if result['status'] == 'measured' else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Profile comparison error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
