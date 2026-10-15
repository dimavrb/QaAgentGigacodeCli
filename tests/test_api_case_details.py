import copy
import html
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from api_case_details import (MAX_JSON_CHARACTERS, MAX_JSON_DEPTH,
                              MAX_JSON_NODES, render_api_details_html,
                              render_api_test_data, validate_api_details)


def api_step():
    return {"name": "Отправить запрос отмены чужого заказа",
            "expected_result": "HTTP 403, error.code=ORDER_ACCESS_DENIED; заказ остаётся NEW",
            "request": {"method": "POST", "path": "/api/orders/ORD-102/cancel",
                        "headers": {"Authorization": "Bearer {{TOKEN_A}}"},
                        "body": {"kind": "none"}},
            "response": {"status": 403, "example": {
                "scope": "fragment", "json": {"error": {"code": "ORDER_ACCESS_DENIED"}}}}}


class ApiCaseDetailsTests(unittest.TestCase):
    def test_legacy_step_and_documentation_only_calls_have_no_side_effects(self):
        legacy = {"name": "Открыть страницу", "expected_result": "Страница открыта"}
        step = api_step()
        before = copy.deepcopy(step)
        with patch("builtins.open", side_effect=AssertionError("file access")), \
                patch("subprocess.Popen", side_effect=AssertionError("subprocess")), \
                patch("socket.create_connection", side_effect=AssertionError("HTTP")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("HTTP")):
            self.assertEqual([], validate_api_details(legacy))
            self.assertEqual("", render_api_details_html(legacy, 1))
            self.assertEqual(("", ""), render_api_test_data(legacy))
            self.assertEqual([], validate_api_details(step))
            self.assertIn("{{TOKEN_A}}", render_api_details_html(step, 2))
        self.assertEqual(before, step)

    def test_shared_test_data_preserves_json_and_placeholders_in_html_and_text(self):
        step = api_step()
        value = {'note': '<script>bad()</script>&"', 'quantity': 1, 'flag': True}
        step['request']['body'] = {'kind': 'json', 'value': value}
        step['response']['example'] = {'scope': 'exact', 'json': value}
        before = copy.deepcopy(step)
        rendered, text = render_api_test_data(step)
        self.assertNotIn('<script>', rendered)
        self.assertIn('{{TOKEN_A}}', text)
        self.assertIn('HTTP 403', text)
        self.assertIn('Полный пример JSON-ответа', text)
        pretty = json.dumps(value, ensure_ascii=False, indent=2)
        self.assertEqual(2, text.count(pretty))
        blocks = [html.unescape(item) for item in re.findall(r'<pre>(.*?)</pre>', rendered, re.DOTALL)]
        self.assertEqual(2, blocks.count(pretty))
        self.assertEqual(before, step)

    def test_plain_test_data_distinguishes_no_body_null_and_exact_text(self):
        cases = [({'kind': 'none'}, 'Тело запроса: отсутствует'),
                 ({'kind': 'json', 'value': None}, 'Тело запроса: JSON\nnull'),
                 ({'kind': 'text', 'value': '{"quantity":'}, 'Тело запроса: текст\n{"quantity":'),
                 ({'kind': 'text', 'value': ''}, 'не часть отправляемого тела.\n""')]
        for body, expected in cases:
            with self.subTest(body=body):
                step = api_step()
                step.pop('response')
                step['request']['body'] = body
                _, text = render_api_test_data(step)
                self.assertIn(expected, text)
                self.assertNotIn('Content-Type', text)

    def test_shared_test_data_keeps_request_and_response_independent(self):
        for absent in ('request', 'response'):
            with self.subTest(absent=absent):
                step = api_step()
                step.pop(absent)
                rendered, text = render_api_test_data(step)
                if absent == 'response':
                    self.assertIn('POST /api/orders/ORD-102/cancel', text)
                    self.assertNotIn('HTTP 403', text)
                    self.assertNotIn('<h4>Ответ</h4>', rendered)
                else:
                    self.assertIn('HTTP 403', text)
                    self.assertNotIn('POST ', text)
                    self.assertNotIn('<h4>Запрос</h4>', rendered)

    def test_request_and_response_are_independent_optional_details(self):
        step = api_step()
        step.pop("response")
        self.assertEqual([], validate_api_details(step))
        self.assertNotIn("<h4>Ответ</h4>", render_api_details_html(step, 1))
        step = api_step()
        step.pop("request")
        self.assertEqual([], validate_api_details(step))
        self.assertIn("HTTP 403", render_api_details_html(step, 1))
        self.assertNotIn("<h4>Запрос</h4>", render_api_details_html(step, 1))

    def test_no_body_json_null_and_invalid_json_text_are_distinct(self):
        bodies = [("none", {"kind": "none"}),
                  ("null", {"kind": "json", "value": None}),
                  ("invalid", {"kind": "text", "value": '{"quantity":'})]
        rendered = {}
        for label, body in bodies:
            with self.subTest(label=label):
                step = api_step()
                step["request"]["body"] = body
                self.assertEqual([], validate_api_details(step))
                rendered[label] = render_api_details_html(step, 1)
        self.assertIn("Тело запроса: отсутствует", rendered["none"])
        self.assertIn("Тело запроса: JSON</p><pre>null</pre>", rendered["null"])
        self.assertIn("Тело запроса: текст</p><pre>{&quot;quantity&quot;:</pre>", rendered["invalid"])

    def test_all_json_value_shapes_are_preserved(self):
        for value in (None, True, False, 7, -1, 2.5, "строка", [], [1, None],
                      {}, {"object": ["a", {"flag": True}]}):
            with self.subTest(value=value):
                step = api_step()
                step["request"]["body"] = {"kind": "json", "value": value}
                step["response"]["example"]["json"] = value
                self.assertEqual([], validate_api_details(step))
                pretty = html.escape(json.dumps(value, ensure_ascii=False, indent=2), quote=True)
                self.assertEqual(2, render_api_details_html(step, 1).count(f"<pre>{pretty}</pre>"))

    def test_custom_uppercase_http_tokens_are_supported(self):
        for method in ("GET", "PATCH", "PROPFIND", "REPORT", "CUSTOM-METHOD", "M_SEARCH", "FOO+BAR"):
            with self.subTest(method=method):
                step = api_step()
                step["request"]["method"] = method
                self.assertEqual([], validate_api_details(step))
        for method in ("", "get", "Post", "GET POST", "GET\n", "GÉT", None, True, []):
            with self.subTest(method=method):
                step = api_step()
                step["request"]["method"] = method
                self.assertIn("request.method", " ".join(validate_api_details(step)))

    def test_request_requires_explicit_path_headers_and_body(self):
        for field in ("method", "path", "headers", "body"):
            with self.subTest(field=field):
                step = api_step()
                del step["request"][field]
                self.assertIn(f"request.{field} is required", validate_api_details(step))
        for path in ("https://example.test/api", "api/orders", "", None, 3, []):
            with self.subTest(path=path):
                step = api_step()
                step["request"]["path"] = path
                self.assertIn("request.path", " ".join(validate_api_details(step)))
        step = api_step()
        step["request"].update(path="/", headers={})
        self.assertEqual([], validate_api_details(step))
        self.assertNotIn("Content-Type", render_api_details_html(step, 1))

    def test_unknown_fields_cannot_be_silently_discarded(self):
        changes = (("request", lambda s: s["request"].update(timeout=10)),
                   ("request.body", lambda s: s["request"]["body"].update(value=None)),
                   ("response", lambda s: s["response"].update(matcher="exact")),
                   ("response.example", lambda s: s["response"]["example"].update(extra=True)))
        for prefix, mutate in changes:
            with self.subTest(prefix=prefix):
                step = api_step()
                mutate(step)
                self.assertIn(prefix, " ".join(validate_api_details(step)))
                with self.assertRaisesRegex(ValueError, "not supported"):
                    render_api_details_html(step, 1)

    def test_header_strings_and_case_insensitive_duplicates(self):
        for section in ("request", "response"):
            for headers in ([], None, {3: "a"}, {"Accept": None}, {"X": 3},
                            {"Authorization": "a", "authorization": "b"}):
                with self.subTest(section=section, headers=headers):
                    step = api_step()
                    step[section]["headers"] = headers
                    self.assertIn(section + ".headers", " ".join(validate_api_details(step)))
            step = api_step()
            step[section]["headers"] = {"Authorization": "", "X-Empty": ""}
            self.assertEqual([], validate_api_details(step))
            self.assertIn("&quot;X-Empty&quot;: &quot;&quot;", render_api_details_html(step, 1))

    def test_header_json_roundtrip_preserves_control_characters_and_boundaries(self):
        headers = {"X": "a\nInjected: b", "X-Colon": "a:b", "X-Empty": "",
                   "X-Control": "\r\t\x00\x7f", "X-Quoted": '"value" & <tag>'}
        step = api_step()
        step.pop("response")
        step["request"]["headers"] = headers
        self.assertEqual([], validate_api_details(step))
        rendered = render_api_details_html(step, 1)
        self.assertIn("Заголовки запроса (JSON)", rendered)
        self.assertNotIn("\x7f", rendered)
        self.assertIn("\\u007f", rendered)
        blocks = re.findall(r"<pre>(.*?)</pre>", rendered, re.DOTALL)
        self.assertEqual(headers, json.loads(html.unescape(blocks[1])))
        two_headers = api_step()
        two_headers.pop("response")
        two_headers["request"]["headers"] = {"X": "a", "Injected": "b"}
        two_blocks = re.findall(r"<pre>(.*?)</pre>", render_api_details_html(two_headers, 1), re.DOTALL)
        self.assertNotEqual(blocks[1], two_blocks[1])
        self.assertEqual({"X": "a", "Injected": "b"}, json.loads(html.unescape(two_blocks[1])))
        for value in ({}, {"X-Empty": ""}):
            step["request"]["headers"] = value
            blocks = re.findall(r"<pre>(.*?)</pre>", render_api_details_html(step, 1), re.DOTALL)
            self.assertEqual(value, json.loads(html.unescape(blocks[1])))
        step = api_step()
        step["response"]["headers"] = headers
        rendered = render_api_details_html(step, 1)
        self.assertIn("Заголовки ответа (JSON)", rendered)
        blocks = re.findall(r"<pre>(.*?)</pre>", rendered, re.DOTALL)
        self.assertEqual(headers, json.loads(html.unescape(blocks[2])))

    def test_path_and_text_controls_have_unambiguous_json_string_display(self):
        for path in ("/api/a\nInjected: b", "/api/\r\t\x00\x7f", "/api/orders "):
            with self.subTest(path=repr(path)):
                step = api_step()
                step.pop("response")
                step["request"]["path"] = path
                original = copy.deepcopy(step)
                rendered = render_api_details_html(step, 1)
                self.assertIn("Метод и путь запроса (JSON)", rendered)
                self.assertNotIn("\x7f", rendered)
                self.assertIn("JSON-кавычки — представление строки, не часть отправляемого пути", rendered)
                blocks = re.findall(r"<pre>(.*?)</pre>", rendered, re.DOTALL)
                self.assertEqual({"method": "POST", "path": path}, json.loads(html.unescape(blocks[0])))
                self.assertEqual(original, step)
        representations = {}
        for text in ("", " ", "   ", " leading", "trailing ", "a\nb", "\r\t\x00\x7f"):
            with self.subTest(text=repr(text)):
                step = api_step()
                step.pop("response")
                step["request"]["body"] = {"kind": "text", "value": text}
                original = copy.deepcopy(step)
                rendered = render_api_details_html(step, 1)
                self.assertIn("Тело запроса: текст (JSON-строка)", rendered)
                self.assertNotIn("\x7f", rendered)
                self.assertIn("JSON-кавычки — представление строки, не часть отправляемого тела", rendered)
                blocks = re.findall(r"<pre>(.*?)</pre>", rendered, re.DOTALL)
                self.assertEqual(text, json.loads(html.unescape(blocks[2])))
                representations[text] = blocks[2]
                self.assertEqual(original, step)
        self.assertEqual(len(representations), len(set(representations.values())))
        step = api_step()
        step["request"]["body"] = {"kind": "text", "value": "ordinary text"}
        rendered = render_api_details_html(step, 1)
        self.assertIn("<pre>POST /api/orders/ORD-102/cancel</pre>", rendered)
        self.assertIn("Тело запроса: текст</p><pre>ordinary text</pre>", rendered)
        self.assertNotIn("JSON-кавычки", rendered)

    def test_diagnostics_escape_unsafe_field_and_duplicate_header_names(self):
        for name in ("\ud800", "extra\nInjected: b", "extra\x00", "extra\x7f"):
            with self.subTest(name=repr(name)):
                step = api_step()
                step["request"][name] = "unsupported"
                errors = validate_api_details(step)
                unknown = [error for error in errors if "not supported" in error]
                self.assertEqual(1, len(unknown))
                self.assertIn(json.dumps(name, ensure_ascii=True), unknown[0])
                unknown[0].encode("utf-8")
                self.assertNotIn(name, unknown[0])
                with self.assertRaises(ValueError) as raised:
                    render_api_details_html(step, 1)
                str(raised.exception).encode("utf-8")
        step = api_step()
        step["request"]["headers"] = {"X\nHEADER": "a", "x\nheader": "b"}
        errors = validate_api_details(step)
        self.assertEqual(1, len(errors))
        self.assertIn('"x\\nheader"', errors[0])
        self.assertNotIn("\n", errors[0])

    def test_response_status_is_http_integer_not_bool(self):
        for status in (100, 200, 403, 599):
            step = api_step()
            step["response"]["status"] = status
            self.assertEqual([], validate_api_details(step))
        for status in (99, 600, -1, True, False, 200.0, "403", None, []):
            with self.subTest(status=status):
                step = api_step()
                step["response"]["status"] = status
                self.assertIn("response.status", " ".join(validate_api_details(step)))
        step = api_step()
        del step["response"]["status"]
        self.assertIn("response.status is required", validate_api_details(step))

    def test_request_body_shape_and_kind_have_controlled_errors(self):
        for body in (None, [], "data", {}, {"kind": None}, {"kind": []},
                     {"kind": "binary"}, {"kind": "json"},
                     {"kind": "text", "value": None},
                     {"kind": "text", "value": {"a": 1}}):
            with self.subTest(body=body):
                step = api_step()
                step["request"]["body"] = body
                self.assertIn("request.body", " ".join(validate_api_details(step)))
                with self.assertRaises(ValueError):
                    render_api_details_html(step, 1)

    def test_response_examples_require_explicit_scope_and_json(self):
        for example in (None, [], {}, {"scope": "fragment"}, {"json": {}},
                        {"scope": "subset", "json": {}},
                        {"scope": [], "json": {}}, {"scope": None, "json": {}}):
            with self.subTest(example=example):
                step = api_step()
                step["response"]["example"] = example
                self.assertIn("response.example", " ".join(validate_api_details(step)))

    def test_non_json_values_and_nonfinite_numbers_are_rejected(self):
        values = (float("nan"), float("inf"), float("-inf"), b"bytes", (1, 2), {1: "a"},
                  {"a": {1, 2}}, {"a": [float("nan")]}, object())
        for value in values:
            for location in ("request", "response"):
                with self.subTest(value=type(value).__name__, location=location):
                    step = api_step()
                    if location == "request":
                        step["request"]["body"] = {"kind": "json", "value": value}
                        prefix = "request.body.value"
                    else:
                        step["response"]["example"]["json"] = value
                        prefix = "response.example.json"
                    self.assertIn(prefix, " ".join(validate_api_details(step)))
                    with self.assertRaises(ValueError):
                        render_api_details_html(step, 1)

    def test_cycles_are_rejected_but_shared_acyclic_values_are_allowed(self):
        cycle_list = []
        cycle_list.append(cycle_list)
        cycle_dict = {}
        cycle_dict["self"] = cycle_dict
        for value in (cycle_list, cycle_dict):
            step = api_step()
            step["response"]["example"]["json"] = value
            self.assertIn("circular JSON", " ".join(validate_api_details(step)))
            with self.assertRaisesRegex(ValueError, "circular JSON"):
                render_api_details_html(step, 1)
        shared = {"quantity": 1}
        step["response"]["example"]["json"] = [shared, shared]
        self.assertEqual([], validate_api_details(step))
        self.assertEqual(2, render_api_details_html(step, 1).count("&quot;quantity&quot;: 1"))

    def test_depth_node_and_display_size_limits_are_controlled(self):
        step = api_step()
        value = None
        for _ in range(MAX_JSON_DEPTH):
            value = [value]
        step["response"]["example"]["json"] = value
        self.assertEqual([], validate_api_details(step))
        step["response"]["example"]["json"] = [value]
        self.assertIn("depth limit", " ".join(validate_api_details(step)))
        step["response"]["example"]["json"] = [None] * (MAX_JSON_NODES - 1)
        self.assertEqual([], validate_api_details(step))
        step["response"]["example"]["json"] = [None] * MAX_JSON_NODES
        self.assertIn("node limit", " ".join(validate_api_details(step)))
        step["response"]["example"]["json"] = "a" * MAX_JSON_CHARACTERS
        self.assertIn("character limit", " ".join(validate_api_details(step)))
        with self.assertRaisesRegex(ValueError, "character limit"):
            render_api_details_html(step, 1)

    def test_every_dynamic_display_value_is_html_escaped(self):
        step = api_step()
        step["name"] = '<script>name</script> " &'
        step["request"]["path"] = '/api/<img src=x onerror=alert(1)>?a="b"&x=1'
        step["request"]["headers"] = {"<b>header</b>": '<script>value</script> " &'}
        step["request"]["body"] = {"kind": "text", "value": '<svg onload="x">&'}
        step["response"]["headers"] = {"Content-Type": '<img src=x onerror="x">'}
        step["response"]["example"]["json"] = {"<key>": '</pre><script>json</script> " &'}
        rendered = render_api_details_html(step, 3)
        self.assertIn("API: шаг 3 — " + html.escape(step["name"], quote=True), rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img ", rendered)
        self.assertNotIn("<svg ", rendered)
        for value in (step["request"]["path"], step["request"]["body"]["value"]):
            self.assertIn(html.escape(value, quote=True), rendered)
        for headers in (step["request"]["headers"], step["response"]["headers"]):
            self.assertIn(html.escape(json.dumps(headers, ensure_ascii=False, indent=2), quote=True), rendered)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;json&lt;/script&gt;", rendered)

    def test_response_scope_is_visible_and_does_not_execute_assertions(self):
        step = api_step()
        self.assertIn("Фрагмент JSON-ответа", render_api_details_html(step, 1))
        step["response"]["example"]["scope"] = "exact"
        rendered = render_api_details_html(step, 1)
        self.assertIn("Полный пример JSON-ответа", rendered)
        self.assertIn("Условия проверки указаны в ожидаемом результате", rendered)
        self.assertIn("структурное равенство JSON", rendered)
        self.assertIn("порядок элементов массива важен", rendered)
        self.assertIn("не исполняются автоматически", rendered)
        self.assertNotIn("request_id", rendered)

    def test_surrogates_in_new_details_have_utf8_diagnostics(self):
        malformed = "\ud800"
        changes = (lambda s: s.update(name=malformed),
                   lambda s: s["request"].update(path="/" + malformed),
                   lambda s: s["request"].update(headers={"Accept": malformed}),
                   lambda s: s["request"].update(headers={malformed: "x"}),
                   lambda s: s["request"].update(body={"kind": "text", "value": malformed}),
                   lambda s: s["request"].update(body={"kind": "json", "value": [malformed]}),
                   lambda s: s["response"].update(headers={"X": malformed}),
                   lambda s: s["response"]["example"].update(json={malformed: "a"}),
                   lambda s: s["response"]["example"].update(json={"a": malformed}),
                   lambda s: s["request"].update({malformed: "unsupported"}))
        for mutate in changes:
            with self.subTest(mutate=mutate):
                step = api_step()
                mutate(step)
                errors = validate_api_details(step)
                self.assertIn("UTF-8", " ".join(errors))
                " ".join(errors).encode("utf-8")
                with self.assertRaisesRegex(ValueError, "UTF-8"):
                    render_api_details_html(step, 1)
        legacy = {"name": malformed, "expected_result": "Исторический кейс"}
        self.assertEqual([], validate_api_details(legacy))
        self.assertEqual("", render_api_details_html(legacy, 1))

    def test_malformed_library_arguments_return_controlled_errors(self):
        for step in (None, [], True, "step", {"request": None}, {"response": []},
                     {"request": {1: None}}, {"response": {1: None}}):
            with self.subTest(step=step):
                self.assertTrue(validate_api_details(step))
                with self.assertRaises(ValueError):
                    render_api_details_html(step, 1)
        for number in (None, True, 0, -1, 1.0, "1"):
            with self.subTest(number=number):
                with self.assertRaisesRegex(ValueError, "step_number"):
                    render_api_details_html(api_step(), number)
        step = api_step()
        step["name"] = None
        with self.assertRaisesRegex(ValueError, "step.name"):
            render_api_details_html(step, 1)


if __name__ == "__main__":
    unittest.main()
