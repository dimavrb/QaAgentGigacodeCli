"""Synthetic local checks; no Jira integration or model evaluation."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / '.gigacode/skills/jira-defect-creator'
SCRIPT = SKILL / 'scripts/check_payload.py'
SPEC = importlib.util.spec_from_file_location('jira_skill_payload', SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class JiraDefectSkillTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        evidence = {'status': 'verified', 'observed_at': '2026-10-15T10:00:00Z',
                    'method': 'synthetic fixture', 'source': 'not live Jira'}

        def field(role, **extra):
            return dict(type='string', required=True, writable=True, auto=False,
                        role=role, evidence=deepcopy(evidence), **extra)

        self.fields = {
            '/project': field('project', allowed_values=['FIXTURE']),
            '/fields/issue_type': field('issue_type', allowed_values=['Synthetic defect']),
            '/fields/summary': field('summary'),
            '/fields/description': field('description'),
            '/fields/priority': field('priority', list_field=True,
                                      allowed_values=['Fixture / Important']),
        }
        self.schema = {
            'schema_version': 1, 'jira_base_url': 'https://jira.invalid/context',
            'project_key': 'FIXTURE', 'connector': 'synthetic-only',
            'verified_at': '2026-10-15T10:00:00Z', 'expires_at': '2026-10-15T13:00:00Z',
            'blockers': [], 'operations': {'create': {'tool': 'fixture_create',
                                                     'fields': self.fields}},
        }
        self.plan = {
            'schema_version': 1, 'jira_base_url': self.schema['jira_base_url'],
            'project_key': 'FIXTURE', 'connector': 'synthetic-only',
            'operation': 'create', 'tool': 'fixture_create',
            'arguments': {'project': 'FIXTURE', 'fields': {
                'issue_type': 'Synthetic defect', 'summary': 'Фильтр не сохраняет выбор',
                'description': 'Окружение\nУчебная сборка\nШаги\nВыбрать фильтр\n'
                               'Фактический результат\nВыбор сброшен\n'
                               'Ожидаемый результат\nВыбор сохранён',
                'priority': 'Fixture / Important',
            }},
        }

    def check(self):
        return CHECKER.check(self.schema, self.plan, now=self.now)

    def test_valid_plan_hash_binds_arguments_and_entire_profile(self):
        original = self.check()
        self.assertEqual(64, len(original))
        self.plan['arguments']['fields']['summary'] += ' после обновления'
        changed = self.check()
        self.assertNotEqual(original, changed)
        self.schema['measurement_note'] = 'new review'
        self.assertNotEqual(changed, self.check())

    def test_identity_is_scoped_by_instance_project_and_connector(self):
        for key in ('jira_base_url', 'project_key', 'connector'):
            with self.subTest(key=key):
                plan = deepcopy(self.plan)
                plan[key] += '-other'
                with self.assertRaisesRegex(CHECKER.Invalid, 'identity mismatch'):
                    CHECKER.check(self.schema, plan, now=self.now)

    def test_expired_future_and_naive_profiles_are_blocked(self):
        for key, value in [('expires_at', '2026-10-15T11:00:00Z'),
                           ('verified_at', '2026-10-16T11:00:00Z'),
                           ('verified_at', '2026-10-15T10:00:00')]:
            with self.subTest(key=key, value=value):
                schema = deepcopy(self.schema)
                schema[key] = value
                with self.assertRaises(CHECKER.Invalid):
                    CHECKER.check(schema, self.plan, now=self.now)

    def test_response_fields_and_unknown_nested_arguments_are_blocked(self):
        for location in ('root', 'nested'):
            plan = deepcopy(self.plan)
            target = plan['arguments'] if location == 'root' else plan['arguments']['fields']
            target['created_by'] = 'not a request field'
            with self.subTest(location=location), self.assertRaisesRegex(
                    CHECKER.Invalid, 'outside field allowlist'):
                CHECKER.check(self.schema, plan, now=self.now)

    def test_missing_required_and_auto_fields_are_blocked(self):
        del self.plan['arguments']['fields']['priority']
        with self.assertRaisesRegex(CHECKER.Invalid, 'missing required'):
            self.check()
        self.fields['/fields/priority']['required'] = False
        self.check()
        self.fields['/fields/summary']['auto'] = True
        with self.assertRaisesRegex(CHECKER.Invalid, 'AUTO'):
            self.check()

    def test_exact_option_keeps_spaces_and_json_shape(self):
        for value in ('Fixture/Important', {'value': 'Fixture / Important'}, 1):
            with self.subTest(value=value):
                self.plan['arguments']['fields']['priority'] = value
                with self.assertRaises(CHECKER.Invalid):
                    self.check()

    def test_observed_option_object_is_compared_as_a_whole(self):
        self.fields['/fields/priority'].update(type='object', allowed_values=[{'id': 'fixture-id'}])
        self.plan['arguments']['fields']['priority'] = {'id': 'fixture-id'}
        self.check()
        self.plan['arguments']['fields']['priority']['response_label'] = 'extra'
        with self.assertRaisesRegex(CHECKER.Invalid, 'unmeasured value'):
            self.check()

    def test_empty_and_long_summary_and_empty_description_are_blocked(self):
        for name, value in [('summary', ''), ('summary', ' '), ('summary', 'я' * 256),
                            ('description', ' ')]:
            with self.subTest(name=name, length=len(value)):
                plan = deepcopy(self.plan)
                plan['arguments']['fields'][name] = value
                with self.assertRaises(CHECKER.Invalid):
                    CHECKER.check(self.schema, plan, now=self.now)

    def test_unknown_and_indirect_evidence_do_not_become_verified(self):
        for status in ('unknown', 'indirect'):
            self.fields['/fields/summary']['evidence']['status'] = status
            with self.subTest(status=status), self.assertRaisesRegex(CHECKER.Invalid, 'unverified'):
                self.check()

    def test_unmeasured_required_and_open_blocker_stop_preflight(self):
        self.fields['/fields/summary']['required'] = None
        with self.assertRaisesRegex(CHECKER.Invalid, 'measured boolean'):
            self.check()
        self.fields['/fields/summary']['required'] = True
        self.schema['blockers'].append('Incomplete duplicate search')
        with self.assertRaisesRegex(CHECKER.Invalid, 'blockers'):
            self.check()

    def test_boolean_does_not_pass_as_integer(self):
        self.fields['/fields/priority'].update(type='integer', allowed_values=[1])
        self.plan['arguments']['fields']['priority'] = True
        with self.assertRaisesRegex(CHECKER.Invalid, 'wrong JSON type'):
            self.check()

    def test_old_measurement_cannot_be_refreshed_by_changing_only_profile_ttl(self):
        self.fields['/fields/summary']['evidence']['observed_at'] = '2025-10-15T10:00:00Z'
        with self.assertRaisesRegex(CHECKER.Invalid, 'profile window'):
            self.check()

    def test_credentials_and_query_are_not_allowed_in_saved_jira_root(self):
        for url in ('https://user:secret@jira.invalid', 'https://jira.invalid?token=secret',
                    'https:///no-host', 'https://jira.invalid#fragment'):
            with self.subTest(url=url):
                self.schema['jira_base_url'] = self.plan['jira_base_url'] = url
                with self.assertRaisesRegex(CHECKER.Invalid, 'HTTPS Jira root'):
                    self.check()

    def test_wrong_tool_operation_and_project_binding_are_blocked(self):
        self.plan['tool'] = 'other_create'
        with self.assertRaisesRegex(CHECKER.Invalid, 'wrong tool'):
            self.check()
        self.plan['tool'] = 'fixture_create'
        self.plan['arguments']['project'] = 'ANOTHER'
        with self.assertRaisesRegex(CHECKER.Invalid, 'unmeasured value'):
            self.check()
        self.plan['operation'] = 'unknown'
        with self.assertRaisesRegex(CHECKER.Invalid, 'unknown operation'):
            self.check()

    def test_overlapping_allowlist_cannot_bypass_child_checks(self):
        self.fields['/fields'] = deepcopy(self.fields['/fields/summary'])
        with self.assertRaisesRegex(CHECKER.Invalid, 'overlapping'):
            self.check()

    def test_pointer_escaping_is_supported_and_bad_escape_rejected(self):
        self.fields['/custom~1field~0name'] = deepcopy(self.fields['/fields/summary'])
        self.fields['/custom~1field~0name']['role'] = 'other'
        self.plan['arguments']['custom/field~name'] = 'measured'
        self.check()
        self.fields['/bad~escape'] = self.fields.pop('/custom~1field~0name')
        with self.assertRaisesRegex(CHECKER.Invalid, 'Pointer escape'):
            self.check()

    def test_cli_rejects_changed_fingerprint_and_does_not_send_request(self):
        with tempfile.TemporaryDirectory() as directory:
            schema_path, plan_path = (Path(directory) / name for name in ('schema.json', 'plan.json'))
            schema = deepcopy(self.schema)
            schema['verified_at'], schema['expires_at'] = '2020-01-01T00:00:00Z', '2100-01-01T00:00:00Z'
            schema_path.write_text(json.dumps(schema))
            plan_path.write_text(json.dumps(self.plan))
            command = [sys.executable, str(SCRIPT), '--schema', str(schema_path), '--plan', str(plan_path)]
            passed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, passed.returncode, passed.stdout + passed.stderr)
            output = json.loads(passed.stdout)
            self.assertEqual('must_be_checked_separately', output['authorization'])
            blocked = subprocess.run(command + ['--expected-sha256', '0' * 64],
                                     capture_output=True, text=True)
            self.assertEqual(2, blocked.returncode)
            self.assertIn('changed', json.loads(blocked.stdout)['error'])

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
                with self.subTest(raw=raw):
                    path.write_text(raw)
                    with self.assertRaises(CHECKER.Invalid):
                        CHECKER.read_json(path)


if __name__ == '__main__':
    unittest.main()
