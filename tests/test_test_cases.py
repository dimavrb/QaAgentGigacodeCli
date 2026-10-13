import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_test_cases import validate_catalog


class TestCaseStyleTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / "examples" / "test-cases.json").read_text(encoding="utf-8"))

    def test_example_is_valid(self):
        examples = [ROOT / "examples" / "test-cases.json"]
        examples.extend(sorted((ROOT / "examples" / "test-cases").rglob("*.json")))
        for example in examples:
            with self.subTest(example=example.relative_to(ROOT)):
                catalog = json.loads(example.read_text(encoding="utf-8"))
                self.assertEqual([], validate_catalog(catalog))

    def test_duplicate_id_and_title_are_rejected(self):
        duplicate = copy.deepcopy(self.catalog["test_cases"][0])
        self.catalog["test_cases"].append(duplicate)
        errors = validate_catalog(self.catalog)
        self.assertTrue(any("duplicate scenario_id" in error for error in errors))
        self.assertTrue(any("duplicate title" in error for error in errors))

    def test_case_title_requires_three_parts(self):
        self.catalog["test_cases"][0]["title"] = "Проверка фильтра"
        errors = validate_catalog(self.catalog)
        self.assertTrue(any("object — condition — expected result" in error for error in errors))

    def test_step_name_and_expected_result_are_separate(self):
        step = self.catalog["test_cases"][0]["steps"][0]
        step["name"] = "Проверить, что всё работает"
        step["expected_result"] = ""
        errors = validate_catalog(self.catalog)
        self.assertTrue(any("must describe a concrete action" in error for error in errors))
        self.assertTrue(any("expected_result must be a non-empty string" in error for error in errors))

    def test_plan_result_validates_referenced_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_file = root / "catalog.json"
            result_file = root / "result.json"
            result = {
                "task_id": "QA-1", "request_id": "QA-1-plan-1", "agent": "qa-test-plan-writer",
                "event": "plan_ready", "summary": "План готов", "artifacts": ["catalog.json"],
                "blockers": [],
                "payload": {"plan_artifact": "plan.md", "test_cases_artifact": "catalog.json", "plan_revision": "v1", "requirement_coverage": [{"requirement_id": "R1", "source": "spec", "scenario_ids": [case["scenario_id"] for case in self.catalog["test_cases"]], "priority": "high", "verification": "UI", "evidence": "screenshot", "status": "ready"}], "open_questions": []},
            }
            (root / "plan.md").write_text("План проверки", encoding="utf-8")
            result_file.write_text(json.dumps(result), encoding="utf-8")
            command = [sys.executable, str(ROOT / "scripts" / "validate_qa_result.py"),
                       "--input", str(result_file), "--task-id", "QA-1",
                       "--request-id", "QA-1-plan-1", "--agent", "qa-test-plan-writer"]
            catalog_file.write_text(json.dumps(self.catalog), encoding="utf-8")
            valid = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)
            self.catalog["test_cases"][0]["title"] = "Неудачное название"
            catalog_file.write_text(json.dumps(self.catalog), encoding="utf-8")
            invalid = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(2, invalid.returncode)
            self.assertIn("test_cases_artifact", invalid.stdout)


if __name__ == "__main__":
    unittest.main()
