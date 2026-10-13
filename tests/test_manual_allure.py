import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_manual_allure import build_bundle, identity, make_result


class ManualAllureTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / "examples/testops-jenkins/catalogs/orders.json").read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.catalog_dir = self.root / "cases"
        self.catalog_dir.mkdir()
        self.source = self.catalog_dir / "orders.json"
        self.write_catalog()

    def write_catalog(self):
        self.source.write_text(json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8")

    def build(self, output="results", **kwargs):
        return build_bundle(self.catalog_dir, self.root / output, "orders.manual", **kwargs)

    def test_manual_publication_preserves_content_without_claiming_execution(self):
        manifest = self.build()
        self.assertFalse(manifest["execution_performed"])
        self.assertEqual(2, manifest["case_count"])
        for case, record in zip(self.catalog["test_cases"], manifest["cases"]):
            result = json.loads((self.root / "results" / record["file"]).read_text())
            labels = {label["name"]: label["value"] for label in result["labels"]}
            self.assertEqual("true", labels["ALLURE_MANUAL"])
            self.assertEqual(case["scenario_id"], labels["scenario_id"])
            self.assertEqual(case["title"], result["name"])
            self.assertEqual("unknown", result["status"])
            self.assertNotIn("start", result)
            self.assertNotIn("stop", result)
            for condition in case["preconditions"]:
                self.assertIn(condition, result["descriptionHtml"])
            for original, step in zip(case["steps"], result["steps"]):
                self.assertEqual(original["name"], step["name"])
                self.assertEqual("unknown", step["status"])
                self.assertEqual("Ожидаемый результат: " + original["expected_result"], step["steps"][0]["name"])
                self.assertEqual("unknown", step["steps"][0]["status"])

    def test_identity_survives_rename_edit_and_file_move_but_uuid_is_new(self):
        first = self.build()
        self.catalog["test_cases"][0]["title"] = "Список заказов — выбран фильтр «Оплачен» — видны только оплаченные заказы"
        self.catalog["test_cases"][0]["steps"][0]["expected_result"] = "Новое ожидаемое поведение"
        self.write_catalog()
        self.source.rename(self.catalog_dir / "renamed.json")
        second = self.build("second")
        self.assertNotEqual(first["catalog_sha256"], second["catalog_sha256"])
        for before, after in zip(first["cases"], second["cases"]):
            self.assertEqual(before["test_case_id"], after["test_case_id"])
            self.assertEqual(before["full_name"], after["full_name"])
            self.assertNotEqual(before["file"], after["file"])

    def test_repeat_has_same_catalog_hash_and_separate_result_identity(self):
        first, second = self.build(), self.build("second")
        self.assertEqual(first["catalog_sha256"], second["catalog_sha256"])
        self.assertNotEqual(first["cases"][0]["file"], second["cases"][0]["file"])

    def test_namespace_and_scenario_boundaries_cannot_collide(self):
        self.assertNotEqual(identity("orders.manual", "TC-001"), identity("orders", "manual.TC-001"))
        self.assertEqual("orders.manual::TC-001", identity("orders.manual", "TC-001")[0])

    def test_duplicate_json_keys_are_rejected_before_writing_results(self):
        for old, replacement in (
            ('"schema_version": 1', '"schema_version": 99, "schema_version": 1'),
            ('"scenario_id": "EX-MANUAL-001"',
             '"scenario_id": "other", "scenario_id": "EX-MANUAL-001"'),
        ):
            with self.subTest(key=old):
                self.write_catalog()
                text = self.source.read_text()
                self.assertIn(old, text)
                self.source.write_text(text.replace(old, replacement, 1))
                with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                    self.build()
                self.assertFalse((self.root / "results").exists())

    def test_schema_version_requires_an_integer(self):
        for version in (True, 1.0):
            with self.subTest(version=version):
                self.catalog["schema_version"] = version
                self.write_catalog()
                with self.assertRaisesRegex(ValueError, "integer 1"):
                    self.build()
                self.assertFalse((self.root / "results").exists())

    def test_duplicates_across_files_fail_before_writing_results(self):
        (self.catalog_dir / "duplicate.json").write_text(json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError, "duplicate scenario_id"):
            self.build()
        self.assertFalse((self.root / "results").exists())

    def test_empty_invalid_and_unsupported_catalogs_fail(self):
        for change in (lambda c: c.update(test_cases=[]),
                       lambda c: c["test_cases"][0].update(parameters=[{"name": "browser", "value": "chrome"}]),
                       lambda c: c["test_cases"][0]["steps"][0].update(attachment="file.txt")):
            with self.subTest(change=change):
                original = copy.deepcopy(self.catalog)
                change(self.catalog)
                self.write_catalog()
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse((self.root / "results").exists())
                self.catalog = original
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "no JSON catalogs"):
            self.build()

    def test_existing_output_is_not_overwritten_or_appended(self):
        self.build()
        before = {path.name: path.read_bytes() for path in (self.root / "results").iterdir()}
        with self.assertRaisesRegex(ValueError, "new or empty"):
            self.build()
        after = {path.name: path.read_bytes() for path in (self.root / "results").iterdir()}
        self.assertEqual(before, after)

    def test_output_inside_catalog_and_symlinked_sources_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source"):
            build_bundle(self.catalog_dir, self.catalog_dir / "results", "orders.manual")
        (self.catalog_dir / "linked.json").symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            self.build()

    def test_symlinked_catalog_directories_cannot_be_silently_skipped(self):
        external = self.root / "external"
        external.mkdir()
        (external / "other.json").write_text(json.dumps(self.catalog))
        (self.catalog_dir / "linked-folder").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked"):
            self.build()
        self.assertFalse((self.root / "results").exists())
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinked catalog directory"):
            build_bundle(linked_root, self.root / "results", "orders.manual")

    def test_existing_ids_are_scoped_to_namespace_instance_and_project(self):
        mapping = {"schema_version": 1, "namespace": "orders.manual",
                   "endpoint": "https://testops.example.com", "project_id": "123",
                   "cases": {"EX-MANUAL-001": "1042"}}
        path = self.root / "mapping.json"
        path.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValueError, "target differs"):
            self.build(id_map_path=path)
        with self.assertRaisesRegex(ValueError, "target differs"):
            self.build(id_map_path=path, endpoint="https://other.example.com", project_id="123")
        with self.assertRaisesRegex(ValueError, "target differs"):
            self.build(id_map_path=path, endpoint="https://testops.example.com", project_id="456")
        manifest = self.build(id_map_path=path, endpoint="https://testops.example.com/", project_id="123")
        result = json.loads((self.root / "results" / manifest["cases"][0]["file"]).read_text())
        self.assertIn({"name": "ALLURE_ID", "value": "1042"}, result["labels"])

    def test_duplicate_and_missing_map_ids_are_rejected(self):
        mapping = {"schema_version": 1, "namespace": "orders.manual",
                   "endpoint": "https://testops.example.com", "project_id": "123",
                   "cases": {"EX-MANUAL-001": "1042", "EX-MANUAL-002": "1042"}}
        path = self.root / "mapping.json"
        path.write_text(json.dumps(mapping))
        args = dict(id_map_path=path, endpoint=mapping["endpoint"], project_id="123")
        with self.assertRaisesRegex(ValueError, "several scenarios"):
            self.build(**args)
        mapping["cases"] = {"UNKNOWN": "1042"}
        path.write_text(json.dumps(mapping))
        with self.assertRaisesRegex(ValueError, "absent from the catalog"):
            self.build(**args)

    def test_id_map_rejects_duplicate_keys_and_non_integer_version(self):
        mapping = {"schema_version": 1, "namespace": "orders.manual",
                   "endpoint": "https://testops.example.com", "project_id": "123",
                   "cases": {"EX-MANUAL-001": "1042"}}
        path = self.root / "mapping.json"
        args = dict(id_map_path=path, endpoint=mapping["endpoint"], project_id="123")
        path.write_text(json.dumps(mapping).replace('"EX-MANUAL-001": "1042"',
                         '"EX-MANUAL-001": "99", "EX-MANUAL-001": "1042"'))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self.build(**args)
        for version in (True, 1.0):
            with self.subTest(version=version):
                mapping["schema_version"] = version
                path.write_text(json.dumps(mapping))
                with self.assertRaisesRegex(ValueError, "schema_version=1"):
                    self.build(**args)
        self.assertFalse((self.root / "results").exists())

    def test_html_metadata_is_escaped(self):
        case = copy.deepcopy(self.catalog["test_cases"][0])
        case["preconditions"] = ["<script>bad()</script>"]
        case["requirement_source"] = '<a href="evil">source</a>'
        result = make_result(case, "orders.manual")
        self.assertNotIn("<script>", result["descriptionHtml"])
        self.assertNotIn('<a href="evil">', result["descriptionHtml"])
        self.assertIn("&lt;script&gt;", result["descriptionHtml"])

    def test_cli_writes_manifest_separately_and_does_not_modify_source(self):
        before = self.source.read_bytes()
        command = [sys.executable, str(ROOT / "scripts/build_manual_allure.py"),
                   "--catalog-dir", str(self.catalog_dir), "--namespace", "orders.manual",
                   "--output", str(self.root / "results"),
                   "--manifest", str(self.root / "manifest.json")]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(2, len(list((self.root / "results").glob("*-result.json"))))
        self.assertFalse((self.root / "results" / "manifest.json").exists())
        bad = command[:-1] + [str(self.root / "results" / "manifest.json")]
        completed = subprocess.run(bad, capture_output=True, text=True)
        self.assertEqual(2, completed.returncode)
        self.assertIn("outside", completed.stdout)


if __name__ == "__main__":
    unittest.main()
