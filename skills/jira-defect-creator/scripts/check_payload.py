#!/usr/bin/env python3
"""Check one Jira call against a measured profile. Never connects to Jira."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def timestamp(value):
    require(isinstance(value, str), 'timestamp must be a string')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Invalid('invalid timestamp') from exc
    require(result.tzinfo is not None, 'timestamp needs timezone')
    return result


def pointer_parts(pointer):
    require(isinstance(pointer, str) and pointer.startswith('/') and pointer != '/',
            'field path must be a nonempty JSON Pointer')
    require(not re.search(r'~(?![01])', pointer), 'invalid JSON Pointer escape')
    return tuple(part.replace('~1', '/').replace('~0', '~')
                 for part in pointer[1:].split('/'))


def lookup(arguments, parts):
    current = arguments
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def shape(value):
    # bool is a subclass of int in Python, but is not a JSON integer.
    return {str: 'string', int: 'integer', float: 'number', bool: 'boolean',
            dict: 'object', list: 'array', type(None): 'null'}.get(type(value))


def check(schema, plan, now=None):
    """Return a fingerprint, or raise Invalid. Facts/consent need external review."""
    now = now or datetime.now(timezone.utc)
    require(isinstance(schema, dict) and isinstance(plan, dict), 'expected JSON objects')
    require(type(schema.get('schema_version')) is int and schema['schema_version'] == 1
            and type(plan.get('schema_version')) is int and plan['schema_version'] == 1,
            'unsupported schema_version')
    for key in ('jira_base_url', 'project_key', 'connector'):
        require(isinstance(schema.get(key), str) and bool(schema[key].strip()),
                'missing profile identity: ' + key)
        require(plan.get(key) == schema[key], 'profile/plan identity mismatch: ' + key)
    url = urlsplit(schema['jira_base_url'])
    require(url.scheme == 'https' and bool(url.hostname) and url.username is None
            and url.password is None and not url.query and not url.fragment
            and not any(char.isspace() for char in schema['jira_base_url'])
            and not schema['jira_base_url'].endswith('/'), 'expected HTTPS Jira root without credentials')
    verified_at = timestamp(schema.get('verified_at'))
    require(verified_at <= now < timestamp(schema.get('expires_at')),
            'profile expired or from the future')
    require(schema.get('blockers') == [], 'unresolved profile blockers')
    operation = plan.get('operation')
    operations = schema.get('operations')
    require(isinstance(operation, str) and isinstance(operations, dict)
            and operation in operations, 'unknown operation')
    spec = operations[operation]
    require(isinstance(spec, dict) and isinstance(spec.get('tool'), str)
            and bool(spec['tool']), 'missing tool')
    require(plan.get('tool') == spec['tool'], 'wrong tool')
    fields = spec.get('fields')
    require(isinstance(fields, dict) and bool(fields), 'empty field allowlist')
    arguments = plan.get('arguments')
    require(isinstance(arguments, dict) and bool(arguments), 'empty arguments')
    paths = {pointer: pointer_parts(pointer) for pointer in fields}
    for name, path in paths.items():
        for other, other_path in paths.items():
            require(name == other or path[:len(other_path)] != other_path,
                    'overlapping field paths')

    def visit(value, path=()):
        if path in paths.values():
            return
        require(isinstance(value, dict) and bool(value), 'argument outside field allowlist')
        for key, child in value.items():
            visit(child, path + (key,))

    visit(arguments)
    roles = set()
    for pointer, field in fields.items():
        require(isinstance(field, dict), 'invalid field specification')
        require(type(field.get('required')) is bool, 'required must be measured boolean')
        require(type(field.get('writable')) is bool and type(field.get('auto')) is bool,
                'writable/auto must be explicit booleans')
        present, value = lookup(arguments, paths[pointer])
        require(present or not field['required'], 'missing required field: ' + pointer)
        if not present:
            continue
        require(field['writable'] and not field['auto'], 'nonwritable/AUTO field: ' + pointer)
        evidence = field.get('evidence', {})
        require(isinstance(evidence, dict) and evidence.get('status') == 'verified',
                'unverified field: ' + pointer)
        require(all(isinstance(evidence.get(key), str) and evidence[key].strip()
                    for key in ('method', 'source')), 'missing measurement source: ' + pointer)
        require(verified_at <= timestamp(evidence.get('observed_at')) <= now,
                'measurement outside current profile window: ' + pointer)
        require(shape(value) == field.get('type'), 'wrong JSON type: ' + pointer)
        if field.get('list_field') is True:
            require(isinstance(field.get('allowed_values'), list)
                    and bool(field['allowed_values']), 'missing exact options: ' + pointer)
        if 'allowed_values' in field:
            require(isinstance(field['allowed_values'], list), 'invalid allowed_values')
            require(any(canonical(value) == canonical(option) for option in field['allowed_values']),
                    'unmeasured value: ' + pointer)
        role = field.get('role')
        if role in ('project', 'issue_type'):
            require(isinstance(field.get('allowed_values'), list)
                    and len(field['allowed_values']) == 1,
                    'project/issue_type must be bound to one measured value')
        if role in ('summary', 'description', 'project', 'issue_type'):
            require(role not in roles, 'ambiguous field role: ' + role)
            roles.add(role)
        if role == 'summary':
            require(isinstance(value, str) and bool(value.strip()) and len(value) <= 255,
                    'summary must contain 1..255 characters')
        if role == 'description':
            require(bool(value) and (not isinstance(value, str) or bool(value.strip())),
                    'description must not be empty')
    if operation == 'create':
        require({'summary', 'description', 'project', 'issue_type'} <= roles,
                'create needs project, issue_type, summary and description roles')
    return hashlib.sha256(canonical({'schema': schema, 'plan': plan}).encode()).hexdigest()


def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result

    def invalid_constant(_):
        raise Invalid('nonfinite JSON number')

    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_pairs,
                      parse_constant=invalid_constant)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--expected-sha256', help='Compare with the previously reviewed fingerprint')
    args = parser.parse_args()
    try:
        fingerprint = check(read_json(args.schema), read_json(args.plan))
        require(args.expected_sha256 is None or args.expected_sha256 == fingerprint,
                'reviewed plan/profile changed')
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        # Do not echo arguments or raw API responses; they may contain secrets.
        print(json.dumps({'structural_check': 'blocked', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({'structural_check': 'passed', 'sha256': fingerprint,
                      'authorization': 'must_be_checked_separately'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
