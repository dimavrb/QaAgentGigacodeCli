"""Structural checks for incremental test-plan results; no content approval."""
from __future__ import annotations

from typing import Any

PLAN_EVENTS = {"coverage_ready", "plan_partial", "plan_ready"}


def validate_plan(payload: dict[str, Any], event: str, catalog: dict | None = None) -> list[str]:
    errors: list[str] = []

    def string(value, path):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path} must be a non-empty string")
            return False
        return True

    def ids(value, path, nonempty=False):
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            errors.append(f"{path} must be a list of non-empty strings")
            return []
        if len(set(value)) != len(value):
            errors.append(f"{path} contains duplicate IDs")
        if nonempty and not value:
            errors.append(f"{path} must not be empty")
        return value

    string(payload.get("plan_revision"), "payload.plan_revision")
    coverage = payload.get("requirement_coverage")
    questions = payload.get("open_questions")
    requirements: set[str] = set()
    scenarios: set[str] = set()
    statuses: set[str] = set()
    blocked_requirements: set[str] = set()
    ready_requirements: set[str] = set()
    ready_scenarios: set[str] = set()
    if isinstance(coverage, list):
        for i, row in enumerate(coverage):
            path = f"payload.requirement_coverage[{i}]"
            if not isinstance(row, dict):
                errors.append(f"{path} must be an object")
                continue
            rid = row.get("requirement_id")
            if string(rid, f"{path}.requirement_id"):
                if rid in requirements:
                    errors.append(f"duplicate requirement_id: {rid}")
                requirements.add(rid)
            for field in ("source", "priority", "verification", "evidence"):
                string(row.get(field), f"{path}.{field}")
            case_ids = ids(row.get("scenario_ids"), f"{path}.scenario_ids")
            scenarios.update(case_ids)
            status = row.get("status")
            if not isinstance(status, str) or status not in {"planned", "ready", "blocked", "excluded"}:
                errors.append(f"{path}.status must be planned, ready, blocked or excluded")
                continue
            statuses.add(status)
            if status in {"planned", "ready"} and not case_ids:
                errors.append(f"{path} needs scenario_ids")
            if status in {"blocked", "excluded"}:
                string(row.get("reason"), f"{path}.reason")
            if status == "ready":
                ready_scenarios.update(case_ids)
                if isinstance(rid, str):
                    ready_requirements.add(rid)
            if status == "blocked" and isinstance(rid, str):
                blocked_requirements.add(rid)
            if event == "coverage_ready" and status == "ready":
                errors.append("coverage_ready cannot declare detailed cases ready")
    blocking_requirements: set[str] = set()
    question_ids: set[str] = set()
    if isinstance(questions, list):
        for i, question in enumerate(questions):
            path = f"payload.open_questions[{i}]"
            if not isinstance(question, dict):
                errors.append(f"{path} must be an object")
                continue
            qid = question.get("question_id")
            if string(qid, f"{path}.question_id"):
                if qid in question_ids:
                    errors.append(f"duplicate question_id: {qid}")
                question_ids.add(qid)
            string(question.get("question"), f"{path}.question")
            affected = ids(question.get("requirement_ids"), f"{path}.requirement_ids", True)
            affected_cases = ids(question.get("scenario_ids"), f"{path}.scenario_ids")
            if set(affected) - requirements:
                errors.append(f"{path} references unknown requirements")
            if set(affected_cases) - scenarios:
                errors.append(f"{path} references unknown scenarios")
            blocks = question.get("blocks")
            if not isinstance(blocks, str) or blocks not in {"planning", "execution", "none"}:
                errors.append(f"{path}.blocks must be planning, execution or none")
            status = question.get("status")
            if not isinstance(status, str) or status not in {"open", "resolved"}:
                errors.append(f"{path}.status must be open or resolved")
            if status == "resolved":
                for field in ("resolution", "decision_source"):
                    string(question.get(field), f"{path}.{field}")
            if status == "open" and blocks == "planning":
                blocking_requirements.update(affected)
                if set(affected) & ready_requirements or set(affected_cases) & ready_scenarios:
                    errors.append(f"{path} blocks ready coverage")
                if set(affected) - blocked_requirements:
                    errors.append(f"{path} planning questions require blocked coverage")
    if blocked_requirements - blocking_requirements:
        errors.append("blocked coverage needs an open planning question")
    if event == "plan_ready" and (statuses - {"ready", "excluded"} or blocking_requirements):
        errors.append("plan_ready cannot contain unfinished planning")
    if event == "plan_partial" and ("ready" not in statuses or "blocked" not in statuses or "planned" in statuses):
        errors.append("plan_partial needs ready and blocked coverage, without planned rows")
    if catalog is not None:
        catalog_ids = {case["scenario_id"] for case in catalog["test_cases"]}
        if ready_scenarios - catalog_ids:
            errors.append(f"ready coverage references missing catalog IDs: {sorted(ready_scenarios - catalog_ids)}")
        if catalog_ids - ready_scenarios:
            errors.append(f"catalog contains cases outside ready coverage: {sorted(catalog_ids - ready_scenarios)}")
    return errors
