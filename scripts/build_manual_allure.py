#!/usr/bin/env python3
"""Compile JSON manual-case catalogs into Allure results without executing tests."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from validate_test_cases import read_json, validate_catalog, validate_test_data
from api_case_details import render_api_test_data

CASE_FIELDS = {"scenario_id", "title", "requirement_source", "preconditions", "steps"}
STEP_FIELDS = {"name", "expected_result"}
API_STEP_FIELDS = {"request", "response"}
DATA_STEP_FIELDS = {"test_data"}


def read_cases(catalog_dir: Path) -> tuple[list[dict], list[dict]]:
    if catalog_dir.is_symlink():
        raise ValueError(f"{catalog_dir}: symlinked catalog directory is not supported")
    if not catalog_dir.is_dir():
        raise ValueError("catalog directory does not exist")
    entries = sorted(catalog_dir.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ValueError(f"{path}: symlinked catalog entry is not supported")
    paths = [path for path in entries if path.is_file() and path.suffix == ".json"]
    if not paths:
        raise ValueError("catalog directory contains no JSON catalogs")
    cases, sources = [], []
    for path in paths:
        data = path.read_bytes()
        catalog = read_json(data.decode("utf-8"))
        errors = validate_catalog(catalog)
        if errors:
            raise ValueError(f"{path}: " + "; ".join(errors))
        if set(catalog) != {"schema_version", "test_cases"}:
            raise ValueError(f"{path}: unsupported catalog fields")
        if type(catalog["schema_version"]) is not int:
            raise ValueError(f"{path}: schema_version must be the integer 1")
        for case in catalog["test_cases"]:
            if (set(case) != CASE_FIELDS
                    or any(not STEP_FIELDS <= set(step) <= STEP_FIELDS | API_STEP_FIELDS | DATA_STEP_FIELDS
                           for step in case["steps"])):
                raise ValueError(f"{path}: unsupported fields; do not silently discard metadata")
            if case["scenario_id"] != case["scenario_id"].strip():
                raise ValueError(f"{path}: scenario_id must not contain surrounding whitespace")
        cases.extend(catalog["test_cases"])
        sources.append({"path": path.relative_to(catalog_dir).as_posix(),
                        "sha256": hashlib.sha256(data).hexdigest()})
    errors = validate_catalog({"schema_version": 1, "test_cases": cases})
    if errors:
        raise ValueError("combined catalog: " + "; ".join(errors))
    return sorted(cases, key=lambda case: case["scenario_id"]), sources


def read_id_map(path: Path | None, namespace: str, endpoint: str | None = None,
                project_id: str | None = None) -> dict[str, str]:
    if path is None:
        return {}
    mapping = read_json(path.read_text(encoding="utf-8"))
    if (not isinstance(mapping, dict)
            or set(mapping) != {"schema_version", "namespace", "endpoint", "project_id", "cases"}
            or type(mapping["schema_version"]) is not int or mapping["schema_version"] != 1
            or mapping["namespace"] != namespace
            or not isinstance(mapping["cases"], dict)):
        raise ValueError("ID map must have schema_version=1, namespace, endpoint, project_id and cases")
    if (not endpoint or not project_id or mapping["endpoint"] != endpoint.rstrip("/")
            or mapping["project_id"] != project_id):
        raise ValueError("ID map target differs; supply its exact TestOps endpoint and project ID")
    ids = mapping["cases"]
    if any(not isinstance(key, str) or not key.strip()
           or not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
           for key, value in ids.items()):
        raise ValueError("ID map values must be existing positive TestOps IDs written as strings")
    if len(set(ids.values())) != len(ids):
        raise ValueError("ID map assigns one TestOps ID to several scenarios")
    return ids


def identity(namespace: str, scenario_id: str) -> tuple[str, str]:
    full_name = f"{namespace}::{scenario_id}"
    # Identity depends on the namespace and stable local ID, never title or file path.
    digest = hashlib.sha256(full_name.encode("utf-8")).hexdigest()
    return full_name, digest


def step_test_data(step: dict) -> tuple[str, str]:
    """Combine derived HTTP data and optional extra data without copying sources."""
    errors = validate_test_data(step)
    if errors:
        raise ValueError("; ".join(errors))
    rendered, text = render_api_test_data(step)
    if "test_data" in step:
        extra = step["test_data"]
        label = "Дополнительная тест-дата:" if rendered else ""
        rendered += (("<h4>" + label + "</h4>") if label else "")
        rendered += "<pre>" + html.escape(extra, quote=True) + "</pre>"
        text += ("\n\n" + label + "\n" if text else "") + extra
    return rendered, text


def render_step_table(steps: list[dict], data: list[tuple[str, str]] | None = None) -> str:
    """Keep actions, expectations and API/extra data in one escaped HTML table."""
    if data is None:
        data = [step_test_data(step) for step in steps]
    if not any(rendered for rendered, _ in data):
        return ""
    rows = []
    for number, (step, (rendered, _)) in enumerate(zip(steps, data), start=1):
        rows.append(f"<tr><td>{number}. {html.escape(step['name'], quote=True)}</td>"
                    f"<td><pre>{html.escape(step['expected_result'], quote=True)}</pre></td>"
                    f"<td>{rendered or '—'}</td></tr>")
    return ("<h3>Шаги</h3><table><thead><tr><th>Шаг</th><th>Ожидаемый результат</th>"
            "<th>Тест-дата</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def make_result(case: dict, namespace: str, allure_id: str | None = None) -> dict:
    full_name, case_id = identity(namespace, case["scenario_id"])
    labels = [{"name": "ALLURE_MANUAL", "value": "true"},
              {"name": "scenario_id", "value": case["scenario_id"]},
              {"name": "suite", "value": "Ручные кейсы"},
              {"name": "framework", "value": "qa-agent-manual-catalog"}]
    if allure_id:
        labels.append({"name": "ALLURE_ID", "value": allure_id})
    preconditions = "".join(f"<li>{html.escape(item)}</li>" for item in case["preconditions"])
    description = (f"<p>Источник: {html.escape(case['requirement_source'])}</p>"
                   f"<h3>Предусловия</h3><ul>{preconditions}</ul>"
                   "<p>Опубликовано описание. Ручное выполнение ещё не проводилось.</p>")
    data = [step_test_data(step) for step in case["steps"]]
    description += render_step_table(case["steps"], data)
    steps = []
    for step, (_, text) in zip(case["steps"], data):
        children = [{"name": "Ожидаемый результат: " + step["expected_result"],
                     "status": "unknown", "stage": "pending"}]
        if text:
            children.append({"name": "Тест-дата: " + text,
                             "status": "unknown", "stage": "pending"})
        steps.append({"name": step["name"], "status": "unknown", "stage": "pending", "steps": children})
    return {"uuid": str(uuid.uuid4()), "testCaseId": case_id, "historyId": case_id,
            "fullName": full_name, "name": case["title"], "descriptionHtml": description,
            "labels": labels, "status": "unknown", "stage": "pending", "steps": steps,
            "statusDetails": {"message": "Публикация ручного сценария; тест не выполнялся"}}


def build_bundle(catalog_dir: Path, output: Path, namespace: str,
                 id_map_path: Path | None = None, endpoint: str | None = None,
                 project_id: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", namespace):
        raise ValueError("namespace must be a stable non-empty ASCII project identifier")
    if output.resolve() == catalog_dir.resolve() or catalog_dir.resolve() in output.resolve().parents:
        raise ValueError("output must be outside the source catalog directory")
    cases, sources = read_cases(catalog_dir)
    ids = read_id_map(id_map_path, namespace, endpoint, project_id)
    unknown_ids = set(ids) - {case["scenario_id"] for case in cases}
    if unknown_ids:
        raise ValueError("ID map contains scenarios absent from the catalog: " + ", ".join(sorted(unknown_ids)))
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output must be a new or empty directory; old results must not be uploaded again")
    canonical = {"namespace": namespace, "cases": cases, "allure_ids": ids}
    catalog_hash = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
    manifest = {"schema_version": 1, "namespace": namespace, "catalog_sha256": catalog_hash,
                "case_count": len(cases), "sources": sources, "cases": [],
                "execution_performed": False,
                "expected_result_representation": "nested_step"}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="manual-allure-", dir=output.parent) as directory:
        staging = Path(directory) / "results"
        staging.mkdir()
        for case in cases:
            result = make_result(case, namespace, ids.get(case["scenario_id"]))
            filename = result["uuid"] + "-result.json"
            (staging / filename).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
            manifest["cases"].append({"scenario_id": case["scenario_id"],
                                      "full_name": result["fullName"],
                                      "test_case_id": result["testCaseId"],
                                      "allure_id": ids.get(case["scenario_id"]), "file": filename})
        if output.exists():
            output.rmdir()
        staging.rename(output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--id-map", type=Path, help="Existing TestOps IDs for this namespace/project")
    parser.add_argument("--endpoint", help="Required with --id-map; no network request is made")
    parser.add_argument("--project-id", help="Required with --id-map")
    parser.add_argument("--manifest", required=True, type=Path, help="Audit file outside the Allure directory")
    args = parser.parse_args()
    try:
        report = args.manifest.resolve()
        if report == args.output.resolve() or args.output.resolve() in report.parents:
            raise ValueError("manifest must be outside the Allure output directory")
        if report == args.catalog_dir.resolve() or args.catalog_dir.resolve() in report.parents:
            raise ValueError("manifest must not overwrite source catalogs")
        if args.id_map and report == args.id_map.resolve():
            raise ValueError("manifest must not overwrite the ID map")
        manifest = build_bundle(args.catalog_dir, args.output, args.namespace, args.id_map,
                                args.endpoint, args.project_id)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"Manual catalog error: {exc}")
        return 2
    print(f"Prepared {manifest['case_count']} manual cases; no tests executed")
    print(f"Catalog SHA256: {manifest['catalog_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
