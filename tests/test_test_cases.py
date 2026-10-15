import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_test_cases import MAX_TEST_DATA_CHARACTERS, validate_catalog


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

    def test_schema_version_requires_integer_not_equal_boolean_or_float(self):
        for value in (True, False, 1.0, '1', None, 2):
            with self.subTest(value=value):
                self.catalog['schema_version'] = value
                self.assertTrue(any('schema_version' in error for error in validate_catalog(self.catalog)))
        self.catalog['schema_version'] = 1
        self.assertEqual([], validate_catalog(self.catalog))

    def test_cli_rejects_duplicate_keys_and_non_json_constants(self):
        raw = json.dumps(self.catalog)
        invalid = [raw.replace('"schema_version": 1', '"schema_version": 0, "schema_version": 1')]
        invalid += [raw.replace('"schema_version": 1', '"schema_version": 1, "extra": ' + value)
                    for value in ('NaN', 'Infinity', '-Infinity')]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'catalog.json'
            for text in invalid:
                with self.subTest(text=text[:100]):
                    source.write_text(text)
                    result = subprocess.run([sys.executable, str(ROOT/'scripts/validate_test_cases.py'),
                                             '--input', str(source)], capture_output=True, text=True)
                    self.assertEqual(2, result.returncode)
                    self.assertIn('Cannot read test cases:', result.stdout)
                    self.assertNotIn('Traceback', result.stderr)

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

    def test_optional_test_data_accepts_multiline_json_and_sql(self):
        step = self.catalog['test_cases'][0]['steps'][0]
        step['test_data'] = 'SQL:\nSELECT payload FROM events;\nОжидаемый payload:\n{\n  "quantity": 1\n}'
        self.assertEqual([], validate_catalog(self.catalog))
        del step['test_data']
        self.assertEqual([], validate_catalog(self.catalog))

    def test_test_data_rejects_empty_and_non_text_values(self):
        step = self.catalog['test_cases'][0]['steps'][0]
        for value in ('', ' \n\t', None, False, 0, [], {}):
            with self.subTest(value=value):
                step['test_data'] = value
                errors = validate_catalog(self.catalog)
                self.assertTrue(any('.test_data must be a non-empty string' in error for error in errors))

    def test_test_data_has_bounded_and_displayable_text(self):
        step = self.catalog['test_cases'][0]['steps'][0]
        step['test_data'] = 'x' * MAX_TEST_DATA_CHARACTERS
        self.assertEqual([], validate_catalog(self.catalog))
        for value, diagnostic in [('x' * (MAX_TEST_DATA_CHARACTERS + 1), 'character limit'),
                                  ('surrogate: \ud800', 'UTF-8'),
                                  ('raw NUL: \x00', 'visible escapes'),
                                  ('raw DEL: \x7f', 'visible escapes'),
                                  ('raw CR: \r', 'visible escapes')]:
            with self.subTest(diagnostic=diagnostic):
                step['test_data'] = value
                errors = validate_catalog(self.catalog)
                self.assertTrue(any('test_data' in error and diagnostic in error for error in errors))
        step['test_data'] = 'Visible JSON escape: {"value":"\\u0000"}\nColumn\tValue'
        self.assertEqual([], validate_catalog(self.catalog))

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

    def test_api_errors_and_deep_input_have_controlled_cli_failures(self):
        catalog = copy.deepcopy(self.catalog)
        step = catalog['test_cases'][0]['steps'][0]
        step['request'] = {'method': 'POST', 'path': '/orders', 'headers': {},
                           'body': {'kind': 'json', 'value': 'NESTED_PAYLOAD'}}
        step['response'] = {'status': True}
        invalid_status = json.dumps(catalog)
        step['response']['status'] = 201
        nested = '[' * 2000 + '0' + ']' * 2000
        deep_input = json.dumps(catalog).replace('"NESTED_PAYLOAD"', nested)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / 'cases'
            cases.mkdir()
            source = cases / 'catalog.json'
            for content in [invalid_status, deep_input]:
                source.write_text(content)
                commands = [
                    [sys.executable, str(ROOT / 'scripts/validate_test_cases.py'), '--input', str(source)],
                    [sys.executable, str(ROOT / 'scripts/build_manual_allure.py'), '--catalog-dir', str(cases),
                     '--namespace', 'synthetic.cli', '--output', str(root / 'results'),
                     '--manifest', str(root / 'manifest.json')]
                ]
                for command in commands:
                    result = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                self.assertFalse((root / 'results').exists())
                self.assertFalse((root / 'manifest.json').exists())


if __name__ == "__main__":
    unittest.main()
