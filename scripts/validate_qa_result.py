#!/usr/bin/env python3
"""Validate a saved QA role result before passing it to another role."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qa_contract import parse_message, validate_message
from self_review import validate_self_review
from validate_test_cases import validate_catalog
from investigation_state import validate_review, validate_state
from test_plan import PLAN_EVENTS, validate_plan
from test_reporting import check_bundle
from stub_contract import STUB_EVENTS, STUB_PLAN_EVENT, read_stub_json, validate_stub_bundle, validate_stub_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument('--require-self-review', action='store_true', help='Require author review for a new result')
    parser.add_argument('--self-review-assignment', type=Path, help='Resolve review references against the coordinator assignment')
    parser.add_argument("--scenario-id", action="append", dest="scenario_ids", help="Assigned scenario ID; repeat for feature results or environment/data readiness")
    parser.add_argument("--investigation-state", type=Path, help="Check state links, local evidence and recorded coordinator assessment for an investigation result")
    parser.add_argument("--stub-assignment", type=Path, help="Coordinator-owned sbermock scope and probe expectations; required for stub artifacts")
    parser.add_argument('--impact-input', type=Path, help='Independent coordinator-owned scope for regression selection')
    parser.add_argument('--evidence-session-id', help='Coordinator session requiring a command-derived benchmark summary')
    parser.add_argument('--improvement-plan', type=Path, help='Independent expected reports/inputs and acceptance thresholds')
    args = parser.parse_args()
    if args.agent == "qa-feature-tester" and not args.scenario_ids:
        parser.error("qa-feature-tester validation requires at least one --scenario-id from the assignment")
    if args.investigation_state and args.agent != "qa-defect-investigator":
        parser.error("--investigation-state is only for qa-defect-investigator")
    if args.stub_assignment and args.agent != 'qa-stub-builder':
        parser.error('--stub-assignment is only for qa-stub-builder')
    if args.impact_input and args.agent != 'qa-impact-analyst':
        parser.error('--impact-input is only for qa-impact-analyst')
    try:
        message, _ = parse_message(args.input.read_text(encoding="utf-8"))
        errors = validate_message(message, args.task_id, args.request_id, args.agent, args.scenario_ids)
        if args.improvement_plan or 'improvement_reports' in message['payload']:
            if not args.improvement_plan:
                errors.append('improvement reports require independent --improvement-plan')
            elif args.evidence_session_id:
                errors.append('separate assignments are required for improvement and benchmark summaries')
            elif not errors:
                from improvement_acceptance import check_bundle as check_improvement_bundle
                check_improvement_bundle(message, args.input.parent, args.improvement_plan)
        if args.evidence_session_id or 'benchmark_summary_artifact' in message['payload']:
            if not args.evidence_session_id or not isinstance(message['payload'].get('benchmark_summary_artifact'), str):
                errors.append('benchmark summary requires artifact and independent --evidence-session-id')
            elif not errors:
                from benchmark_summary import check_summary
                evidence = check_summary(args.input.parent / message['payload']['benchmark_summary_artifact'], args.evidence_session_id)
                if message['summary'] != evidence['summary_text']:
                    errors.append('summary facts/numbers/verdict differ from command-derived summary_text')
                if message['event'] != 'blocked' and not evidence['baseline']['stage_closed']:
                    errors.append('benchmark stage cannot close with ungraded or failed execution inputs')
        if message['event'] == 'impact_ready':
            if not args.impact_input:
                errors.append('impact_ready requires --impact-input from the coordinator')
            elif not errors:
                from impact_analysis import check_impact_bundle
                check_impact_bundle(message, args.input.parent, args.impact_input)
        if args.require_self_review or args.self_review_assignment:
            assignment = json.loads(args.self_review_assignment.read_text(encoding='utf-8')) if args.self_review_assignment else None
            if assignment is not None and not isinstance(assignment, dict):
                raise ValueError('self-review assignment must be an object')
            review_errors = validate_self_review(message, assignment, required=args.require_self_review)
            errors.extend(error for error in review_errors if error not in errors)
        if not errors and 'combination_coverage' in message['payload']:
            from combination_coverage import load_bundles
            load_bundles(message['payload'], args.input.parent)
        if message['event'] == 'investigation_complete' and 'verification_artifact' in message['payload'] and not args.investigation_state:
            errors.append('verification_artifact requires --investigation-state; structure alone is insufficient')
        if message['event'] in {'environment_partial', 'data_ready', 'data_partial', STUB_PLAN_EVENT} | STUB_EVENTS and not args.scenario_ids:
            errors.append('readiness validation requires --scenario-id for every assigned scenario')
        if message['event'] in STUB_EVENTS and not args.stub_assignment:
            errors.append('stub artifacts require --stub-assignment from the coordinator')
        if not errors and message['event'] in STUB_EVENTS:
            errors.extend(validate_stub_bundle(message, args.input.parent, read_stub_json(args.stub_assignment)))
        if not errors and message['event'] == STUB_PLAN_EVENT:
            errors.extend(validate_stub_plan(message, args.input.parent))
        if not errors and args.investigation_state:
            state = json.loads(args.investigation_state.read_text(encoding='utf-8'))
            if message['event'] == 'investigation_complete':
                errors.extend(validate_review(message, state, args.investigation_state.parent))
            else:
                errors.extend(validate_state(state, args.task_id, args.investigation_state.parent))
                if isinstance(state, dict) and state.get('request_id') != args.request_id:
                    errors.append('state request_id does not match result')
        if not errors and message["event"] in PLAN_EVENTS:
            plan_path = Path(message["payload"]["plan_artifact"])
            if not plan_path.is_absolute():
                plan_path = args.input.parent / plan_path
            if not plan_path.is_file() or not plan_path.read_text(encoding="utf-8").strip():
                errors.append("plan_artifact must reference a non-empty local file")
        if not errors and message["event"] in {"plan_ready", "plan_partial"}:
            catalog_path = Path(message["payload"]["test_cases_artifact"])
            if not catalog_path.is_absolute():
                catalog_path = args.input.parent / catalog_path
            try:
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                catalog_errors = validate_catalog(catalog)
                errors.extend(f"test_cases_artifact: {error}" for error in catalog_errors)
                if not catalog_errors:
                    errors.extend(validate_plan(message["payload"], message["event"], catalog))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"test_cases_artifact cannot be read: {exc}")
        if not errors and message['event'] == 'test_report_ready':
            paths = []
            for key in ('report_artifact', 'summary_artifact'):
                path = Path(message['payload'][key])
                paths.append((args.input.parent / path).resolve() if not path.is_absolute() else path.resolve())
            if paths[0].name != 'report.json' or paths[1] != paths[0].with_name('report.md'):
                errors.append('test report must link report.json and report.md from the same snapshot')
            else:
                report = check_bundle(paths[0].parent)
                if report['task_id'] != args.task_id or report['report_revision'] != message['payload']['report_revision']:
                    errors.append('test report task/revision differs from message')
                if report['limitations'] != message['payload']['limitations']:
                    errors.append('test report limitations differ from message')
                artifacts = {(args.input.parent / Path(ref)).resolve() for ref in message['artifacts']
                             if not ref.startswith(('https://', 'http://'))}
                if not set(paths) <= artifacts:
                    errors.append('test report artifacts must include both snapshot files')
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        errors = [str(exc)]
    if errors:
        for error in errors:
            print(error)
        return 2
    print(f"valid {message['event']} from {args.agent}")
    if message['event'] in PLAN_EVENTS:
        print('Structure only: coordinator coverage and expected-behavior review is required; readiness does not mean acceptance or execution.')
    if message['event'] == 'analysis_ready':
        print('Structure and ID links only: coordinator source and expected-behavior review is required.')
    if message['event'] == 'impact_ready':
        print('Frozen sources and regression selection checked. Completeness and exclusions require independent review; no tests were executed.')
    if message['event'] == 'investigation_complete':
        print('Recorded coordinator assessment checked; content judgment is not automated.' if args.investigation_state else 'Structure only: coordinator evidence review is required before accepting the outcome.')
    if message['event'] == 'test_report_ready':
        print('Source hashes and computed coverage checked; report readiness does not mean full testing or release approval.')
    if message['event'] in STUB_EVENTS:
        print('Artifact links, recorded probes and declared readiness checked; live MCP availability and semantic judgment are separate.')
    if message['event'] == STUB_PLAN_EVENT:
        print('Draft intent checked; coordinator must assess scope and contract before mutation. No readiness or application changes are implied.')
    if 'self_review' in message:
        print('Author review structure and available references checked; independent content assessment is still required.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
