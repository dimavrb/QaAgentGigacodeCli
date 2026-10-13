"""Parse and validate the compact QA message returned by a CLI run."""

from __future__ import annotations

import json
from typing import Any

from test_plan import PLAN_EVENTS, validate_plan
from analysis_contract import id_list, validate_analysis
from stub_contract import STUB_EVENTS, STUB_PLAN_EVENT, validate_stub_message
from self_review import validate_self_review
from impact_analysis import IMPACT_EVENT, validate_impact_payload


REQUIRED_FIELDS = ("task_id", "request_id", "agent", "event", "summary", "artifacts", "blockers", "payload")
REVIEW_FIELDS = (
    "scenario_id",
    "requirement_source",
    "base_revision",
    "current_revision",
    "changed_files",
    "diff_artifact",
    "run_result",
    "limitations",
)
AUTOMATION_EVENTS = {
    "ready_for_review", "automation_complete", "needs_analysis",
    "needs_environment", "needs_test_data", "blocked",
}
ROLE_EVENTS = {
    "qa-impact-analyst": {IMPACT_EVENT, "blocked"},
    "qa-conclusion-reviewer": {"review_passed", "changes_requested", "blocked"},
    "qa-result-reviewer": {"review_passed", "changes_requested", "blocked"},
    "qa-analyst": {"analysis_ready", "blocked"},
    "qa-test-plan-writer": {"coverage_ready", "plan_partial", "plan_ready", "needs_analysis", "blocked"},
    "qa-test-env-configurator": {"environment_ready", "environment_partial", "blocked"},
    "qa-test-data-builder": {"data_ready", "data_partial", "blocked"},
    "qa-stub-builder": STUB_EVENTS | {STUB_PLAN_EVENT, "needs_analysis", "needs_environment", "blocked"},
    "qa-defect-investigator": {"investigation_complete", "needs_analysis", "needs_environment", "needs_test_data", "blocked"},
    "qa-feature-tester": {"feature_test_complete", "needs_environment", "needs_test_data", "blocked"},
    "qa-test-automation": AUTOMATION_EVENTS,
    "qa-autotest-reviewer": {"review_passed", "changes_requested", "blocked"},
    "qa-jira-defect-creator": {"jira_defect_created", "jira_duplicate_found", "blocked"},
    "qa-test-report-writer": {"test_report_ready", "needs_analysis", "blocked"},
}
EVENT_FIELDS = {
    IMPACT_EVENT: {"impact_artifact": str, "impact_input": str, "input_sha256": str,
                   "base_revision": str, "target_revision": str, "run_scenario_ids": list,
                   "review_scenario_ids": list, "excluded_scenario_ids": list, "limitations": list},
    "coverage_ready": {"plan_artifact": str, "plan_revision": str, "requirement_coverage": list, "open_questions": list},
    "plan_partial": {"plan_artifact": str, "plan_revision": str, "test_cases_artifact": str, "requirement_coverage": list, "open_questions": list},
    "analysis_ready": {"requirements": list, "risk_scenarios": list, "blocking_questions": list, "assumptions": list, "coverage_gaps": list},
    "plan_ready": {"plan_artifact": str, "plan_revision": str, "test_cases_artifact": str, "requirement_coverage": list, "open_questions": list},
    "environment_ready": {"environment_id": str, "product_version": str, "verified_capabilities": list, "limitations": list},
    "environment_partial": {"environment_id": str, "product_version": str, "verified_capabilities": list, "limitations": list, "ready_scenarios": list, "blocked_scenarios": list},
    "data_ready": {"environment_id": str, "verified_properties": list, "cleanup": str, "scenario_ids": list},
    "data_partial": {"environment_id": str, "verified_properties": list, "cleanup": str, "scenario_ids": list, "blocked_scenarios": list},
    "stubs_ready": {"environment_id": str, "provider": str, "mcp_server": str, "project_id": str, "namespace": str,
                    "profile_artifact": str, "verification_artifact": str, "scenario_ids": list,
                    "blocked_scenarios": list, "cleanup": str, "limitations": list},
    "stubs_partial": {"environment_id": str, "provider": str, "mcp_server": str, "project_id": str, "namespace": str,
                      "profile_artifact": str, "verification_artifact": str, "scenario_ids": list,
                      "blocked_scenarios": list, "cleanup": str, "limitations": list},
    "stubs_configured": {"environment_id": str, "provider": str, "mcp_server": str, "project_id": str, "namespace": str,
                         "profile_artifact": str, "verification_artifact": str, "configured_scenarios": list,
                         "blocked_scenarios": list, "cleanup": str, "limitations": list},
    "stubs_plan_ready": {"environment_id": str, "provider": str, "mcp_server": str, "project_id": str, "namespace": str,
                         "assignment_artifact": str, "scenario_ids": list, "limitations": list},
    "investigation_complete": {"outcome": str, "expected_source": str, "actual_result": str, "attempts": list, "evidence": list, "limitations": list},
    "feature_test_complete": {"scenario_results": list, "uncovered_scenarios": list, "defects": list},
    "automation_complete": {"coverage_reference": str, "run_result": str, "reason_no_change": str},
    "review_passed": {"reviewed_revision": str, "findings": list, "checks": list, "limitations": list},
    "changes_requested": {"reviewed_revision": str, "findings": list, "checks": list, "limitations": list},
    "jira_defect_created": {"issue_key": str, "issue_url": str, "duplicate_check": str, "evidence": list},
    "jira_duplicate_found": {"issue_url": str, "duplicate_check": str},
    "test_report_ready": {"report_artifact": str, "summary_artifact": str, "report_revision": str, "limitations": list},
}
NONEMPTY_LISTS = {
    "coverage_ready": ("requirement_coverage",),
    "plan_partial": ("requirement_coverage",),
    "analysis_ready": ("requirements", "risk_scenarios"),
    "plan_ready": ("requirement_coverage",),
    "environment_ready": ("verified_capabilities",),
    "environment_partial": ("verified_capabilities", "ready_scenarios", "blocked_scenarios"),
    "data_ready": ("verified_properties", "scenario_ids"),
    "data_partial": ("verified_properties", "scenario_ids", "blocked_scenarios"),
    "investigation_complete": ("evidence",),
    "feature_test_complete": ("scenario_results",),
    "review_passed": ("checks",),
    "changes_requested": ("findings",),
    "jira_defect_created": ("evidence",),
}


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("```json") and value.endswith("```"):
            value = value[7:-3].strip()
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("QA result must be a JSON object")
    return value


def parse_message(raw_output: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept legacy objects and the terminal result of a CLI JSON event array."""
    raw_output = raw_output.strip()
    if raw_output.startswith("```json") and raw_output.endswith("```"):
        raw_output = raw_output[7:-3].strip()
    value = json.loads(raw_output)
    if isinstance(value, list):
        if not value or not isinstance(value[-1], dict) or value[-1].get("type") != "result":
            raise ValueError("CLI event array must end with a result event")
        value = value[-1]
    outer = _as_object(value)
    if outer.get("is_error") is True:
        raise ValueError("CLI returned an error result")
    if outer.get("structured_result") is not None:
        return _as_object(outer["structured_result"]), outer
    if all(field in outer for field in REQUIRED_FIELDS):
        return outer, outer
    for key in ("response", "result"):
        if key in outer:
            return _as_object(outer[key]), outer
    raise ValueError("CLI output has no QA message in its response or result field")


def validate_message(
    message: dict[str, Any], task_id: str, request_id: str, agent: str,
    expected_scenario_ids: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in message:
            errors.append(f"missing {field}")
    if errors:
        return errors

    errors.extend(validate_self_review(message))

    for field, expected in (("task_id", task_id), ("request_id", request_id), ("agent", agent)):
        if message[field] != expected:
            errors.append(f"{field} must equal {expected!r}")
    for field in ("event", "summary"):
        if not isinstance(message[field], str) or not message[field].strip():
            errors.append(f"{field} must be a non-empty string")
    for field in ("artifacts", "blockers"):
        if not isinstance(message[field], list) or any(not isinstance(item, str) for item in message[field]):
            errors.append(f"{field} must be a list of strings")
    if not isinstance(message["payload"], dict):
        errors.append("payload must be an object")
        return errors

    event = message["event"]
    if not isinstance(event, str):
        return errors
    if agent not in ROLE_EVENTS:
        errors.append(f"unknown QA agent: {agent!r}")
    if isinstance(event, str) and agent in ROLE_EVENTS and event not in ROLE_EVENTS[agent]:
        errors.append(f"unexpected event for {agent}: {event!r}")

    if isinstance(event, str) and event in EVENT_FIELDS:
        for field, expected_type in EVENT_FIELDS[event].items():
            value = message["payload"].get(field)
            if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
                errors.append(f"payload.{field} must be {'a non-empty string' if expected_type is str else 'a list'}")
        for field in NONEMPTY_LISTS.get(event, ()):
            if isinstance(message["payload"].get(field), list) and not message["payload"][field]:
                errors.append(f"payload.{field} must not be empty")
    if event in {"data_ready", "data_partial"} and not (
        message["payload"].get("data_ids") or message["payload"].get("recipe")
    ):
        errors.append("data result needs payload.data_ids or payload.recipe")
    if event in PLAN_EVENTS:
        errors.extend(validate_plan(message["payload"], event))
    if 'combination_coverage' in message['payload']:
        from combination_coverage import bundle_specs
        try:
            bundle_specs(message['payload'])
        except ValueError as exc:
            errors.append(str(exc))
    if event == "analysis_ready":
        errors.extend(validate_analysis(message["payload"]))
    if event == IMPACT_EVENT:
        errors.extend(validate_impact_payload(message["payload"], expected_scenario_ids))
    if agent in ('qa-result-reviewer', 'qa-conclusion-reviewer') or agent == 'qa-autotest-reviewer' and 'target_request_id' in message['payload']:
        from content_review import validate_review
        errors.extend(validate_review(message['payload'], event))
        from conclusion_review import validate_checks
        errors.extend(validate_checks(message['payload'], event, required=agent == 'qa-conclusion-reviewer'))
        from review_discussion import validate_checks as validate_discussion
        errors.extend(validate_discussion(message['payload'], event))
    if event in STUB_EVENTS:
        errors.extend(validate_stub_message(message['payload'], event, expected_scenario_ids))
    if event == STUB_PLAN_EVENT:
        ids = id_list(message['payload'].get('scenario_ids'), 'payload.scenario_ids', errors)
        if not ids or expected_scenario_ids is not None and ids != set(expected_scenario_ids):
            errors.append('stub plan scenarios must exactly match assignment')
    if event in {"environment_partial", "data_ready", "data_partial"}:
        payload = message["payload"]
        ready_key = "ready_scenarios" if event == "environment_partial" else "scenario_ids"
        ready = id_list(payload.get(ready_key), f"payload.{ready_key}", errors)
        blocked = id_list(payload.get("blocked_scenarios"), "payload.blocked_scenarios", errors) if event.endswith("partial") else set()
        if ready is not None and blocked is not None:
            if ready & blocked:
                errors.append("ready and blocked scenario IDs must not overlap")
            if expected_scenario_ids is not None and ready | blocked != set(expected_scenario_ids):
                errors.append("readiness scenario IDs differ from assignment")
    if event in {"data_ready", "data_partial"}:
        data_ids = message["payload"].get("data_ids")
        recipe = message["payload"].get("recipe")
        if data_ids is not None and not isinstance(data_ids, list):
            errors.append("payload.data_ids must be a list")
        if recipe is not None and (not isinstance(recipe, str) or not recipe.strip()):
            errors.append("payload.recipe must be a non-empty string")
    if event == "investigation_complete":
        outcome = message["payload"].get("outcome")
        if not isinstance(outcome, str) or outcome not in {
            "defect_confirmed", "not_reproduced", "expected_behavior", "inconclusive"
        }:
            errors.append("payload.outcome has an unknown investigation result")
    if event == "changes_requested" and not message["payload"].get("findings"):
        errors.append("changes_requested needs findings")
    if event == "feature_test_complete" or (agent == "qa-feature-tester" and event == "blocked"):
        payload = message["payload"]
        if event == "blocked":
            for field in ("scenario_results", "uncovered_scenarios", "defects"):
                if not isinstance(payload.get(field), list):
                    errors.append(f"payload.{field} must be a list")
            if not payload.get("scenario_results"):
                errors.append("payload.scenario_results must not be empty")
        results = payload.get("scenario_results")
        if isinstance(results, list):
            seen: set[str] = set()
            incomplete: set[str] = set()
            for index, result in enumerate(results):
                prefix = f"payload.scenario_results[{index}]"
                if not isinstance(result, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                for field in (
                    "scenario_id", "environment_id", "product_version", "expected_source",
                    "expected_result", "actual_result",
                ):
                    value = result.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(f"{prefix}.{field} must be a non-empty string")
                scenario_id = result.get("scenario_id")
                if isinstance(scenario_id, str) and scenario_id.strip():
                    if scenario_id in seen:
                        errors.append(f"duplicate scenario_id: {scenario_id}")
                    seen.add(scenario_id)
                status = result.get("status")
                if event == "blocked" and status != "blocked":
                    errors.append(f"{prefix}.status must be blocked for a fully blocked assignment")
                if status not in ("passed", "failed", "blocked", "not_run"):
                    errors.append(f"{prefix}.status must be passed, failed, blocked or not_run")
                if status in ("blocked", "not_run") and isinstance(scenario_id, str):
                    incomplete.add(scenario_id)
                for field in ("steps", "evidence"):
                    value = result.get(field)
                    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                        errors.append(f"{prefix}.{field} must be a list of non-empty strings")
                    elif status in ("passed", "failed") and not value:
                        errors.append(f"{prefix}.{field} must not be empty for executed scenarios")
            uncovered = payload.get("uncovered_scenarios")
            id_list(uncovered, "payload.uncovered_scenarios", errors)
            if isinstance(uncovered, list):
                if any(not isinstance(item, str) or not item.strip() for item in uncovered):
                    errors.append("payload.uncovered_scenarios must list scenario IDs")
                elif set(uncovered) != incomplete:
                    errors.append("payload.uncovered_scenarios must match blocked and not_run results")
            if expected_scenario_ids is not None:
                expected = set(expected_scenario_ids)
                if seen != expected:
                    errors.append(f"scenario_results IDs differ from assignment: missing={sorted(expected - seen)}, extra={sorted(seen - expected)}")
            if event == "feature_test_complete" and results and all(isinstance(r, dict) and r.get("status") == "blocked" for r in results):
                errors.append("fully blocked feature assignment must use blocked")
    if event.startswith("needs_") and not message["payload"]:
        errors.append(f"{event} needs a concrete request in payload")

    if event == "ready_for_review":
        payload = message["payload"]
        for field in REVIEW_FIELDS:
            if field not in payload:
                errors.append(f"ready_for_review missing payload.{field}")
        if all(field in payload for field in REVIEW_FIELDS):
            for field in ("scenario_id", "requirement_source", "base_revision", "current_revision", "diff_artifact"):
                if not isinstance(payload[field], str) or not payload[field].strip():
                    errors.append(f"payload.{field} must be a non-empty string")
            for field in ("changed_files", "limitations"):
                if not isinstance(payload[field], list) or any(not isinstance(item, str) for item in payload[field]):
                    errors.append(f"payload.{field} must be a list of strings")
            if isinstance(payload["changed_files"], list) and not payload["changed_files"]:
                errors.append("payload.changed_files must list the reviewed changes")
            if not payload["run_result"]:
                errors.append("payload.run_result must describe the run or why it was unavailable")
    elif event == "blocked" and not message["blockers"]:
        errors.append("blocked result must list blockers")
    return errors
