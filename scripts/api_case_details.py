"""Validate and display API details in manual cases; never execute requests.

Examples are documentation, not response matchers. Assertions remain in the
step's expected_result. No headers, bodies, credentials or defaults are added.
"""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any

MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_JSON_CHARACTERS = 1_000_000
HTTP_METHOD = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Z]+\Z")


def _utf8(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _atypical_text(value: str) -> bool:
    return (not value.strip() or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value))


def _diagnostic_name(value: str) -> str:
    if not _utf8(value) or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return json.dumps(value, ensure_ascii=True)
    return value


def _object_fields(value: Any, path: str, required: set[str],
                   optional: set[str], errors: list[str]) -> bool:
    if type(value) is not dict:
        errors.append(f"{path} must be an object")
        return False
    for field in sorted(required - value.keys()):
        errors.append(f"{path}.{field} is required")
    if any(type(field) is not str for field in value):
        errors.append(f"{path} must use string field names")
    if any(type(field) is str and not _utf8(field) for field in value):
        errors.append(f"{path} field names must be valid UTF-8 strings")
    for field in sorted(field for field in value
                        if type(field) is str and field not in required | optional):
        errors.append(f"{path}.{_diagnostic_name(field)} is not supported")
    return True


def _headers(value: Any, path: str, errors: list[str]) -> None:
    if type(value) is not dict:
        errors.append(f"{path} must be an object of string names and string values")
        return
    seen: set[str] = set()
    for name, text in value.items():
        if type(name) is not str or type(text) is not str:
            errors.append(f"{path} must contain only string names and string values")
            continue
        if not _utf8(name) or not _utf8(text):
            errors.append(f"{path} names and values must be valid UTF-8 strings")
            continue
        normalized = name.casefold()
        if normalized in seen:
            errors.append(f"{path} contains duplicate header names ignoring case: {_diagnostic_name(name)}")
        seen.add(normalized)


def _json_value(value: Any, path: str, errors: list[str]) -> None:
    """Bound an iterative traversal before using json.dumps for display."""
    pending = [(False, value, 0)]
    active: set[int] = set()
    nodes = 0
    while pending:
        leave, item, depth = pending.pop()
        if leave:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_JSON_NODES:
            errors.append(f"{path} exceeds the JSON node limit ({MAX_JSON_NODES})")
            return
        if depth > MAX_JSON_DEPTH:
            errors.append(f"{path} exceeds the JSON depth limit ({MAX_JSON_DEPTH})")
            return
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                errors.append(f"{path} contains a circular JSON value")
                return
            if nodes + len(item) > MAX_JSON_NODES:
                errors.append(f"{path} exceeds the JSON node limit ({MAX_JSON_NODES})")
                return
            if kind is dict and any(type(key) is not str for key in item):
                errors.append(f"{path} JSON object keys must be strings")
                return
            if kind is dict and any(not _utf8(key) for key in item):
                errors.append(f"{path} JSON object keys must be valid UTF-8 strings")
                return
            active.add(id(item))
            pending.append((True, item, depth))
            children = item.values() if kind is dict else item
            pending.extend((False, child, depth + 1) for child in children)
        elif kind is float:
            if not math.isfinite(item):
                errors.append(f"{path} must contain only finite JSON numbers")
                return
        elif kind is str and not _utf8(item):
            errors.append(f"{path} JSON strings must be valid UTF-8")
            return
        elif item is not None and kind not in (str, int, bool):
            errors.append(f"{path} must be a JSON value (object, array, string, number, boolean or null)")
            return
    try:
        # This also rejects oversized integer conversions on supported Python
        # versions. No global recursion or integer conversion limits are changed.
        serialized = _pretty(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        errors.append(f"{path} cannot be represented as JSON")
        return
    if len(serialized) > MAX_JSON_CHARACTERS:
        errors.append(f"{path} exceeds the JSON character limit ({MAX_JSON_CHARACTERS})")


def validate_api_details(step: Any) -> list[str]:
    """Return relative request/response diagnostics, accepting legacy steps."""
    errors: list[str] = []
    if type(step) is not dict:
        return ["step must be an object"]
    if ("request" in step or "response" in step) and type(step.get("name")) is str and not _utf8(step["name"]):
        errors.append("step.name must be a valid UTF-8 string for API details")
    if "request" in step:
        request = step["request"]
        if _object_fields(request, "request", {"method", "path", "headers", "body"}, set(), errors):
            if "method" in request and (type(request["method"]) is not str
                                        or not HTTP_METHOD.fullmatch(request["method"])):
                errors.append("request.method must be an uppercase HTTP token")
            if "path" in request and (type(request["path"]) is not str
                                      or not request["path"].startswith("/")):
                errors.append("request.path must start with /; put the base URL in preconditions")
            elif "path" in request and not _utf8(request["path"]):
                errors.append("request.path must be a valid UTF-8 string")
            if "headers" in request:
                _headers(request["headers"], "request.headers", errors)
            if "body" in request:
                body = request["body"]
                if type(body) is not dict:
                    errors.append("request.body must be an object")
                else:
                    kind = body.get("kind")
                    if kind == "none":
                        _object_fields(body, "request.body", {"kind"}, set(), errors)
                    elif kind in ("json", "text"):
                        _object_fields(body, "request.body", {"kind", "value"}, set(), errors)
                        if "value" in body:
                            if kind == "json":
                                _json_value(body["value"], "request.body.value", errors)
                            elif type(body["value"]) is not str:
                                errors.append("request.body.value must be a string for kind=text")
                            elif not _utf8(body["value"]):
                                errors.append("request.body.value must be a valid UTF-8 string for kind=text")
                    else:
                        errors.append("request.body.kind must be none, json or text")
    if "response" in step:
        response = step["response"]
        if _object_fields(response, "response", {"status"}, {"headers", "example"}, errors):
            if "status" in response and (type(response["status"]) is not int
                                         or not 100 <= response["status"] <= 599):
                errors.append("response.status must be an integer from 100 to 599")
            if "headers" in response:
                _headers(response["headers"], "response.headers", errors)
            if "example" in response:
                example = response["example"]
                if _object_fields(example, "response.example", {"scope", "json"}, set(), errors):
                    if "scope" in example and example["scope"] not in ("fragment", "exact"):
                        errors.append("response.example.scope must be fragment or exact")
                    if "json" in example:
                        _json_value(example["json"], "response.example.json", errors)
    return errors


def _pre(value: str) -> str:
    return "<pre>" + html.escape(value, quote=True) + "</pre>"


def _pretty(value: Any) -> str:
    # JSON escapes C0 controls itself, but leaves DEL literal with ensure_ascii=False.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).replace("\x7f", "\\u007f")


def render_api_test_data(step: Any) -> tuple[str, str]:
    """Return escaped HTML and readable text from the same validated API blocks.

    Both representations use request/response as their only source. Nothing is
    executed or substituted. A step without HTTP details returns two empty strings.
    """
    errors = validate_api_details(step)
    if errors:
        raise ValueError("; ".join(errors))
    if "request" not in step and "response" not in step:
        return "", ""
    blocks: list[tuple[str, str]] = []
    if "request" in step:
        request = step["request"]
        blocks.append(("h4", "Запрос"))
        if _atypical_text(request["path"]):
            blocks.extend([("p", "Метод и путь запроса (JSON). JSON-кавычки — представление строки, "
                            "не часть отправляемого пути."),
                           ("pre", _pretty({"method": request["method"], "path": request["path"]}))])
        else:
            blocks.append(("pre", f"{request['method']} {request['path']}"))
        blocks.extend([("p", "Заголовки запроса (JSON)"), ("pre", _pretty(request["headers"]))])
        body = request["body"]
        if body["kind"] == "none":
            blocks.append(("p", "Тело запроса: отсутствует"))
        elif body["kind"] == "json":
            blocks.extend([("p", "Тело запроса: JSON"), ("pre", _pretty(body["value"]))])
        elif _atypical_text(body["value"]):
            blocks.extend([("p", "Тело запроса: текст (JSON-строка). JSON-кавычки — представление строки, "
                            "не часть отправляемого тела."), ("pre", _pretty(body["value"]))])
        else:
            blocks.extend([("p", "Тело запроса: текст"), ("pre", body["value"])])
    if "response" in step:
        response = step["response"]
        blocks.extend([("h4", "Ответ"), ("p", f"HTTP {response['status']}")])
        if "headers" in response:
            blocks.extend([("p", "Заголовки ответа (JSON)"), ("pre", _pretty(response["headers"]))])
        if "example" in response:
            example = response["example"]
            label = ("Фрагмент JSON-ответа — иллюстрация значимой части"
                     if example["scope"] == "fragment" else
                     "Полный пример JSON-ответа — структурное равенство JSON; "
                     "порядок ключей объекта не важен, порядок элементов массива важен")
            blocks.extend([("p", label), ("pre", _pretty(example["json"]))])
    rendered = "".join(_pre(value) if tag == "pre" else
                       f"<{tag}>{html.escape(value, quote=True)}</{tag}>" for tag, value in blocks)
    return rendered, "\n".join(value for _, value in blocks)


def render_api_details_html(step: Any, step_number: int) -> str:
    """Render a legacy numbered API section from the shared test-data formatter."""
    rendered, _ = render_api_test_data(step)
    if not rendered:
        return ""
    if type(step_number) is not int or step_number < 1:
        raise ValueError("step_number must be a positive integer")
    if type(step.get("name")) is not str:
        raise ValueError("step.name must be a string")
    return (f"<section><h3>API: шаг {step_number} — {html.escape(step['name'], quote=True)}</h3>"
            + rendered + "<p>Условия проверки указаны в ожидаемом результате этого шага. "
            "Примеры JSON не исполняются автоматически.</p></section>")
