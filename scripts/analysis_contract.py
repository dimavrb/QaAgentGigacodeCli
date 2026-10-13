"""Validate analysis structure and links; source content needs human review."""

from typing import Any


def id_list(value: Any, prefix: str, errors: list[str]) -> set[str] | None:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        errors.append(f"{prefix} must be a list of non-empty string IDs")
        return None
    if len(value) != len(set(value)):
        errors.append(f"{prefix} must contain unique IDs")
    return set(value)


def validate_analysis(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    collections = {}
    for name, key, fields in (
        ("requirements", "requirement_id", ("description",)),
        ("risk_scenarios", "scenario_id", ("requirement_id", "risk", "priority_reason", "distinction")),
        ("blocking_questions", "question_id", ("question",)),
        ("assumptions", "assumption_id", ("description",)),
        ("coverage_gaps", "gap_id", ("description",)),
    ):
        items = payload.get(name)
        indexed = {}
        if isinstance(items, list):
            for i, item in enumerate(items):
                prefix = f"payload.{name}[{i}]"
                if not isinstance(item, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                for field in (key, *fields):
                    value = item.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(f"{prefix}.{field} must be a non-empty string")
                identifier = item.get(key)
                if isinstance(identifier, str) and identifier.strip():
                    if identifier in indexed:
                        errors.append(f"duplicate {key}: {identifier}")
                    indexed[identifier] = item
        collections[name] = indexed

    requirements = collections["requirements"]
    scenarios = collections["risk_scenarios"]
    for name, status_key, source_key, text_key in (
        ("requirements", "status", "source", "description"),
        ("risk_scenarios", "expectation_status", "expected_source", "expected_result"),
    ):
        for identifier, item in collections[name].items():
            prefix = f"payload.{name}.{identifier}"
            status = item.get(status_key)
            if status not in ("confirmed", "inferred", "unknown"):
                errors.append(f"{prefix}.{status_key} must be confirmed, inferred or unknown")
            if status == "unknown":
                if source_key not in item or item[source_key] is not None:
                    errors.append(f"{prefix}.{source_key} must be null for unknown behavior")
                if name == "risk_scenarios" and (text_key not in item or item[text_key] is not None):
                    errors.append(f"{prefix}.{text_key} must be null for unknown behavior")
            else:
                for field in (source_key, text_key):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        errors.append(f"{prefix}.{field} must be a non-empty string")

    blocked_planning = set()
    for name in ("blocking_questions", "assumptions", "coverage_gaps"):
        for identifier, item in collections[name].items():
            prefix = f"payload.{name}.{identifier}"
            req_ids = id_list(item.get("requirement_ids"), f"{prefix}.requirement_ids", errors)
            scenario_ids = id_list(item.get("scenario_ids"), f"{prefix}.scenario_ids", errors)
            if req_ids is not None and req_ids - requirements.keys():
                errors.append(f"{prefix} references unknown requirements")
            if scenario_ids is not None and scenario_ids - scenarios.keys():
                errors.append(f"{prefix} references unknown scenarios")
            if req_ids is not None and scenario_ids is not None and not (req_ids or scenario_ids):
                errors.append(f"{prefix} needs affected requirement or scenario IDs")
            if name == "blocking_questions":
                if item.get("blocks") not in ("planning", "execution"):
                    errors.append(f"{prefix}.blocks must be planning or execution")
                if not scenario_ids:
                    errors.append(f"{prefix} needs affected scenario IDs")
                if item.get("blocks") == "planning" and scenario_ids:
                    blocked_planning.update(scenario_ids)

    for identifier, item in scenarios.items():
        requirement_id = item.get("requirement_id")
        if not isinstance(requirement_id, str) or requirement_id not in requirements:
            errors.append(f"scenario {identifier} references unknown requirement")
        if item.get("priority") not in ("high", "medium", "low"):
            errors.append(f"scenario {identifier} priority must be high, medium or low")
        status = item.get("expectation_status")
        if status in ("unknown", "inferred") and identifier not in blocked_planning:
            errors.append(f"scenario {identifier} needs a blocking planning question for unconfirmed expectation")
        requirement = requirements.get(requirement_id, {}) if isinstance(requirement_id, str) else {}
        if status == "confirmed" and requirement.get("status") != "confirmed":
            errors.append(f"scenario {identifier} cannot confirm an unconfirmed requirement")

    if not isinstance(payload.get("test_plan_required"), bool):
        errors.append("payload.test_plan_required must be a boolean")
    if not isinstance(payload.get("test_plan_reason"), str) or not payload["test_plan_reason"].strip():
        errors.append("payload.test_plan_reason must be a non-empty string")
    return errors
