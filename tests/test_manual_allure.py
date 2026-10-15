import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_manual_allure import build_bundle, identity, make_result, read_cases


class PreformattedText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'pre':
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'pre' and self.current is not None:
            self.blocks.append(''.join(self.current))
            self.current = None


class StepTableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = []
        elif tag == 'td':
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.row.append(''.join(self.cell))
            self.cell = None
        elif tag == 'tr' and self.row:
            self.rows.append(self.row)
            self.row = None


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
                self.assertEqual(1, len(step['steps']))
            self.assertNotIn('<table>', result['descriptionHtml'])

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

    def test_api_examples_preserve_requests_and_responses_in_bundle(self):
        source = ROOT / 'examples/test-cases/api'
        cases, _ = read_cases(source)
        manifest = build_bundle(source, self.root / 'api-results', 'synthetic.api')
        self.assertEqual(len(cases), manifest['case_count'])
        by_id = {case['scenario_id']: case for case in cases}
        for record in manifest['cases']:
            case = by_id[record['scenario_id']]
            result = json.loads((self.root / 'api-results' / record['file']).read_text())
            self.assertEqual(case['title'], result['name'])
            self.assertEqual(len(case['steps']), len(result['steps']))
            table = StepTableText()
            table.feed(result['descriptionHtml'])
            self.assertEqual(len(case['steps']), len(table.rows))
            for number, (original, step, row) in enumerate(zip(case['steps'], result['steps'], table.rows), start=1):
                self.assertEqual(original['name'], step['name'])
                self.assertEqual(f"{number}. {original['name']}", row[0])
                self.assertEqual(original['expected_result'], row[1])
                self.assertEqual('Ожидаемый результат: ' + original['expected_result'], step['steps'][0]['name'])
                if 'request' not in original and 'response' not in original:
                    self.assertEqual(original.get('test_data', '—'), row[2])
                    continue
                self.assertEqual(2, len(step['steps']))
                native = step['steps'][1]['name']
                self.assertTrue(native.startswith('Тест-дата: '))
                self.assertEqual('unknown', step['steps'][1]['status'])
                if 'request' in original:
                    request = original['request']
                    expected = [request['method'] + ' ' + request['path'],
                                json.dumps(request['headers'], ensure_ascii=False, indent=2)]
                    if request['body']['kind'] == 'none':
                        expected.append('Тело запроса: отсутствует')
                    elif request['body']['kind'] == 'json':
                        expected.append(json.dumps(request['body']['value'], ensure_ascii=False, indent=2))
                    for value in expected:
                        self.assertIn(value, row[2])
                        self.assertIn(value, native)
                if 'response' in original:
                    response = original['response']
                    expected = [f'HTTP {response["status"]}']
                    if 'headers' in response:
                        expected.append(json.dumps(response['headers'], ensure_ascii=False, indent=2))
                    if 'example' in response:
                        expected.append(json.dumps(response['example']['json'], ensure_ascii=False, indent=2))
                        expected.append('Фрагмент JSON-ответа' if response['example']['scope'] == 'fragment'
                                        else 'Полный пример JSON-ответа')
                    for value in expected:
                        self.assertIn(value, row[2])
                        self.assertIn(value, native)
                self.assertEqual('unknown', step['status'])
            self.assertNotIn('<section><h3>API:', result['descriptionHtml'])
            self.assertNotIn('start', result)
            self.assertNotIn('stop', result)

    def test_api_edits_change_catalog_hash_without_changing_identity(self):
        self.catalog = json.loads((ROOT / 'examples/test-cases/api/cancellation.json').read_text())
        self.write_catalog()
        before = self.build()
        self.catalog['test_cases'][0]['steps'][1]['request']['headers']['Authorization'] = 'Bearer {{OTHER_TOKEN}}'
        self.write_catalog()
        request_edit = self.build('request-edit')
        self.catalog['test_cases'][0]['steps'][1]['response']['example']['json']['error']['code'] = 'OTHER_CODE'
        self.write_catalog()
        response_edit = self.build('response-edit')
        self.assertEqual(3, len({row['catalog_sha256'] for row in [before, request_edit, response_edit]}))
        self.assertEqual(1, len({row['cases'][0]['test_case_id'] for row in [before, request_edit, response_edit]}))
        result = json.loads((self.root / 'response-edit' / response_edit['cases'][0]['file']).read_text())
        table = StepTableText()
        table.feed(result['descriptionHtml'])
        for data in (table.rows[1][2], result['steps'][1]['steps'][1]['name']):
            self.assertIn('{{OTHER_TOKEN}}', data)
            self.assertIn('OTHER_CODE', data)
            self.assertNotIn('ORDER_ACCESS_DENIED', data)

    def test_api_unknown_fields_and_invalid_types_fail_before_writing(self):
        source = json.loads((ROOT / 'examples/test-cases/api/cancellation.json').read_text())
        for mutate in [lambda step: step['request'].update(ignored='metadata'),
                       lambda step: step['response'].update(status=True),
                       lambda step: step['response']['example'].update(scope='unspecified')]:
            with self.subTest(mutate=mutate):
                self.catalog = copy.deepcopy(source)
                mutate(self.catalog['test_cases'][0]['steps'][1])
                self.write_catalog()
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse((self.root / 'results').exists())

    def test_api_data_is_escaped_in_description_without_changing_json_value(self):
        self.catalog = json.loads((ROOT / 'examples/test-cases/api/create.json').read_text())
        value = {'note': '<script>bad()</script>&"', 'line': 'first\nsecond'}
        self.catalog['test_cases'][0]['steps'][0]['request']['body']['value'] = value
        self.catalog['test_cases'][0]['steps'][0]['response']['example']['json'] = value
        self.write_catalog()
        manifest = self.build()
        result = json.loads((self.root / 'results' / manifest['cases'][0]['file']).read_text())
        self.assertNotIn('<script>', result['descriptionHtml'])
        self.assertIn('&lt;script&gt;', result['descriptionHtml'])
        parser = PreformattedText()
        parser.feed(result['descriptionHtml'])
        self.assertEqual(2, parser.blocks.count(json.dumps(value, ensure_ascii=False, indent=2)))

    def test_kafka_example_preserves_three_columns_and_step_data(self):
        source = ROOT / 'examples/test-cases/kafka'
        cases, _ = read_cases(source)
        manifest = build_bundle(source, self.root / 'kafka-results', 'synthetic.kafka')
        self.assertEqual(1, manifest['case_count'])
        self.assertFalse(manifest['execution_performed'])
        result = json.loads((self.root / 'kafka-results' / manifest['cases'][0]['file']).read_text())
        parser = StepTableText()
        parser.feed(result['descriptionHtml'])
        originals = cases[0]['steps']
        self.assertEqual(len(originals), len(parser.rows))
        for number, (original, row, published) in enumerate(zip(originals, parser.rows, result['steps']), start=1):
            self.assertEqual([f"{number}. {original['name']}", original['expected_result'], original['test_data']], row)
            self.assertEqual(original['name'], published['name'])
            self.assertEqual('Ожидаемый результат: ' + original['expected_result'], published['steps'][0]['name'])
            self.assertEqual('Тест-дата: ' + original['test_data'], published['steps'][1]['name'])
            self.assertEqual('unknown', published['steps'][1]['status'])
            self.assertEqual('pending', published['steps'][1]['stage'])
        self.assertNotIn('start', result)
        self.assertNotIn('stop', result)

    def test_ui_examples_preserve_three_columns_and_optional_step_data(self):
        source = ROOT / 'examples/test-cases/ui'
        cases, _ = read_cases(source)
        manifest = build_bundle(source, self.root / 'ui-results', 'synthetic.ui')
        self.assertEqual(5, manifest['case_count'])
        self.assertFalse(manifest['execution_performed'])
        by_id = {case['scenario_id']: case for case in cases}
        missing_data_seen = False
        for record in manifest['cases']:
            case = by_id[record['scenario_id']]
            result = json.loads((self.root / 'ui-results' / record['file']).read_text())
            table = StepTableText()
            table.feed(result['descriptionHtml'])
            self.assertEqual(len(case['steps']), len(table.rows))
            self.assertEqual(len(case['steps']), len(result['steps']))
            for number, (original, row, published) in enumerate(zip(case['steps'], table.rows, result['steps']), start=1):
                self.assertEqual([f"{number}. {original['name']}", original['expected_result'],
                                  original.get('test_data', '—')], row)
                self.assertEqual('Ожидаемый результат: ' + original['expected_result'], published['steps'][0]['name'])
                self.assertEqual('unknown', published['status'])
                if 'test_data' in original:
                    self.assertEqual(2, len(published['steps']))
                    self.assertEqual('Тест-дата: ' + original['test_data'], published['steps'][1]['name'])
                    self.assertEqual('unknown', published['steps'][1]['status'])
                    self.assertEqual('pending', published['steps'][1]['stage'])
                else:
                    missing_data_seen = True
                    self.assertEqual(1, len(published['steps']))
            self.assertNotIn('start', result)
            self.assertNotIn('stop', result)
        self.assertTrue(missing_data_seen)

    def test_test_data_is_optional_per_step_and_can_coexist_with_http_details(self):
        self.catalog = json.loads((ROOT / 'examples/test-cases/api/create.json').read_text())
        self.catalog['test_cases'][0]['steps'][0]['test_data'] = 'Фикстура: данные этого прогона'
        self.write_catalog()
        manifest = self.build()
        result = json.loads((self.root / 'results' / manifest['cases'][0]['file']).read_text())
        parser = StepTableText()
        parser.feed(result['descriptionHtml'])
        for original, row, step in zip(self.catalog['test_cases'][0]['steps'], parser.rows, result['steps']):
            if 'test_data' in original:
                self.assertIn(original['test_data'], row[2])
                self.assertIn(original['test_data'], step['steps'][1]['name'])
            has_data = any(field in original for field in ('request', 'response', 'test_data'))
            self.assertEqual(2 if has_data else 1, len(step['steps']))
        self.assertNotIn('API: шаг 1', result['descriptionHtml'])
        api = PreformattedText()
        api.feed(result['descriptionHtml'])
        body = self.catalog['test_cases'][0]['steps'][0]['request']['body']['value']
        self.assertIn(json.dumps(body, ensure_ascii=False, indent=2), api.blocks)

    def test_partial_http_details_generate_only_the_available_test_data(self):
        source = json.loads((ROOT / 'examples/test-cases/api/cancellation.json').read_text())['test_cases'][0]
        for absent in ('request', 'response'):
            with self.subTest(absent=absent):
                case = copy.deepcopy(source)
                del case['steps'][0][absent]
                result = make_result(case, 'synthetic.api')
                table = StepTableText()
                table.feed(result['descriptionHtml'])
                for data in (table.rows[0][2], result['steps'][0]['steps'][1]['name']):
                    if absent == 'request':
                        self.assertIn('HTTP 200', data)
                        self.assertNotIn('GET /api/orders/', data)
                    else:
                        self.assertIn('GET /api/orders/ORD-102', data)
                        self.assertNotIn('HTTP 200', data)

    def test_api_control_characters_are_unambiguous_in_table_and_native_data(self):
        self.catalog = json.loads((ROOT / 'examples/test-cases/api/create.json').read_text())
        step = self.catalog['test_cases'][0]['steps'][0]
        value = 'first\x00\nsecond'
        step['request']['body'] = {'kind': 'text', 'value': value}
        step['request']['headers']['X-Example'] = 'a\nInjected: b'
        self.write_catalog()
        manifest = self.build()
        result = json.loads((self.root / 'results' / manifest['cases'][0]['file']).read_text())
        table = StepTableText()
        table.feed(result['descriptionHtml'])
        for data in (table.rows[0][2], result['steps'][0]['steps'][1]['name']):
            self.assertIn(json.dumps(value, ensure_ascii=False), data)
            self.assertIn(json.dumps(step['request']['headers'], ensure_ascii=False, indent=2), data)
            self.assertNotIn('a\nInjected: b', data)
            self.assertNotIn('\x00', data)
            self.assertIn('не часть отправляемого тела', data)

    def test_test_data_is_escaped_and_preserved_without_interpreting_it(self):
        data = 'SQL:\nSELECT \'<script>bad()</script>&"\';\nJSON: {"id":"<run_id>"}\n\t{{TOKEN}}'
        self.catalog['test_cases'][0]['steps'][0]['test_data'] = data
        self.write_catalog()
        manifest = self.build()
        result = json.loads((self.root / 'results' / manifest['cases'][0]['file']).read_text())
        self.assertNotIn('<script>', result['descriptionHtml'])
        parser = StepTableText()
        parser.feed(result['descriptionHtml'])
        self.assertEqual(data, parser.rows[0][2])
        self.assertEqual('Тест-дата: ' + data, result['steps'][0]['steps'][1]['name'])

    def test_test_data_edits_change_hash_without_changing_case_identity(self):
        first = self.build()
        self.catalog['test_cases'][0]['steps'][0]['test_data'] = 'Key: order-<run_id>'
        self.write_catalog()
        added = self.build('added-data')
        self.catalog['test_cases'][0]['steps'][0]['test_data'] += '\nТело: {"quantity":1}'
        self.write_catalog()
        edited = self.build('edited-data')
        manifests = (first, added, edited)
        self.assertEqual(3, len({item['catalog_sha256'] for item in manifests}))
        self.assertEqual(1, len({item['cases'][0]['test_case_id'] for item in manifests}))

    def test_invalid_test_data_is_rejected_before_any_results_are_written(self):
        for value in ('', None, {'payload': {}}, '\ud800', '\x00'):
            with self.subTest(value=value):
                self.catalog['test_cases'][0]['steps'][0]['test_data'] = value
                self.source.write_text(json.dumps(self.catalog, ensure_ascii=True), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'test_data'):
                    self.build()
                self.assertFalse((self.root / 'results').exists())


if __name__ == "__main__":
    unittest.main()
