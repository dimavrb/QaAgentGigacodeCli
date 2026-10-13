#!/usr/bin/env python3
"""Validate the structure and naming of a QA test-case catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_catalog(catalog: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(catalog, dict):
        return ["catalog must be a JSON object"]
    if catalog.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    cases = catalog.get("test_cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["test_cases must be a non-empty list"]

    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"test_cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        scenario_id = case.get("scenario_id")
        if not _text(scenario_id):
            errors.append(f"{prefix}.scenario_id must be a non-empty string")
        elif scenario_id in seen_ids:
            errors.append(f"duplicate scenario_id: {scenario_id}")
        else:
            seen_ids.add(scenario_id)

        from combination_coverage import validate_case_parameters
        errors.extend(f'{prefix}: {error}' for error in validate_case_parameters(case))

        title = case.get("title")
        if not _text(title):
            errors.append(f"{prefix}.title must be a non-empty string")
        else:
            parts = title.split(" — ")
            if len(parts) != 3 or any(len(part.strip()) < 3 for part in parts):
                errors.append(f"{prefix}.title must have object — condition — expected result")
            if _text(scenario_id) and title.startswith(f"{scenario_id} "):
                errors.append(f"{prefix}.title must not repeat scenario_id")
            if title.startswith(("Тест ", "Проверка ")):
                errors.append(f"{prefix}.title must name the behavior, not the test")
            normalized = " ".join(title.casefold().split())
            if normalized in seen_titles:
                errors.append(f"duplicate title: {title}")
            seen_titles.add(normalized)

        if not _text(case.get("requirement_source")):
            errors.append(f"{prefix}.requirement_source must be a non-empty string")
        preconditions = case.get("preconditions")
        if not isinstance(preconditions, list) or any(not _text(item) for item in preconditions):
            errors.append(f"{prefix}.preconditions must be a list of non-empty strings")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{prefix}.steps must be a non-empty list")
            continue
        for step_index, step in enumerate(steps):
            step_prefix = f"{prefix}.steps[{step_index}]"
            if not isinstance(step, dict):
                errors.append(f"{step_prefix} must be an object")
                continue
            name = step.get("name")
            if not _text(name):
                errors.append(f"{step_prefix}.name must be a non-empty string")
            else:
                if not name[0].isupper() or name.endswith("."):
                    errors.append(f"{step_prefix}.name must start with a capital letter and have no final period")
                if name.startswith(("Шаг ", "Проверить, что", "Убедиться, что", "Выполнить тест")):
                    errors.append(f"{step_prefix}.name must describe a concrete action")
            if not _text(step.get("expected_result")):
                errors.append(f"{step_prefix}.expected_result must be a non-empty string")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON file with test cases")
    args = parser.parse_args()
    try:
        catalog = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read test cases: {exc}")
        return 2
    errors = validate_catalog(catalog)
    if errors:
        for error in errors:
            print(error)
        return 2
    print(f"valid {len(catalog['test_cases'])} test cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
