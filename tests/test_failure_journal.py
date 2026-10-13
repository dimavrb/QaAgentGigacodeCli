"""Incident counting, provenance, append safety and recovery semantics."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from failure_journal import append_entry, find_recoveries, read_journal, render_summary, summarize

CLI = ROOT / 'scripts/manage_failure_journal.py'


class FailureJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        shutil.copytree(ROOT / 'examples/failure-journal', self.base / 'inputs')
        self.inputs = self.base / 'inputs'
        self.journal = self.base / 'failures.jsonl'

    def entry(self, name='01-open.json'):
        return json.loads((self.inputs / name).read_text())

    def append(self, entry):
        return append_entry(self.journal, entry, self.inputs)

    def summary(self, scope_kind='synthetic'):
        records, checksum = read_journal(self.journal)
        return summarize(records, checksum, scope_kind)

    def find(self, **kwargs):
        records, checksum = read_journal(self.journal)
        return find_recoveries(records, checksum, **{'scope_kind': 'synthetic', 'agent': 'qa-feature-tester', **kwargs})

    def test_find_verified_solution_preserves_origin_without_mutating_journal(self):
        self.append(self.entry())
        self.append(self.entry('02-recovered.json'))
        self.append(self.entry('03-hypothesis.json'))
        before = self.journal.read_bytes()
        result = self.find(query='отмена сервис')
        self.assertEqual(before, self.journal.read_bytes())
        self.assertEqual(1, result['total_matches'])
        match = result['matches'][0]
        self.assertEqual('DEMO-1-recovered', match['event_id'])
        self.assertEqual('verified', match['recovery']['outcome'])
        self.assertEqual('confirmed', match['cause']['status'])
        self.assertIs(True, match['applicability_review_required'])
        self.assertTrue(match['recovery']['evidence'][0]['sha256'])

    def test_find_excludes_changed_missing_and_remote_evidence(self):
        self.append(self.entry('02-recovered.json'))
        proof = self.inputs / 'recovery.txt'
        original = proof.read_bytes()
        proof.write_text('Изменённое доказательство')
        self.assertEqual([], self.find()['matches'])
        proof.unlink()
        self.assertEqual([], self.find()['matches'])
        proof.write_bytes(original)
        updated = self.entry('02-recovered.json')
        updated.update(event_id='remote-recovery', occurred_at='2020-01-01T12:00:00+00:00')
        updated['recovery']['evidence'].append('https://example.invalid/proof')
        self.append(updated)
        self.assertEqual(1, self.find()['skipped']['evidence_unverifiable'])

    def test_find_does_not_use_recovery_after_incident_reopened(self):
        self.append(self.entry('02-recovered.json'))
        reopened = self.entry()
        reopened.update(event_id='reopened', occurred_at='2020-01-01T12:00:00+00:00', reopens_event_id='DEMO-1-recovered')
        self.append(reopened)
        result = self.find()
        self.assertEqual([], result['matches'])
        self.assertEqual(1, result['skipped']['unresolved'])

    def test_find_compares_current_conditions_and_marks_unknowns(self):
        self.append(self.entry('02-recovered.json'))
        self.assertEqual([], self.find(context={'environment_id': 'another'})['matches'])
        match = self.find(context={'environment_id': 'synthetic'})['matches'][0]
        self.assertEqual('matched', match['context_match'])
        match = self.find(context={'model': 'new-model'})['matches'][0]
        self.assertEqual('unknown', match['context_match'])
        self.assertEqual(['model'], match['unknown_context_keys'])
        match = self.find()['matches'][0]
        self.assertEqual('not_checked', match['context_match'])
        self.assertIs(True, match['applicability_review_required'])

    def test_find_scope_and_query_are_explicit(self):
        self.append(self.entry('02-recovered.json'))
        records, checksum = read_journal(self.journal)
        self.assertEqual([], find_recoveries(records, checksum, agent='qa-feature-tester')['matches'])
        self.assertEqual([], self.find(query='отмена НЕСУЩЕСТВУЮЩЕЕ')['matches'])
        self.assertEqual([], self.find(stage='automation')['matches'])
        self.assertEqual([], self.find(symptom='invalid_result')['matches'])
        self.assertEqual(1, self.find(query='ОТМЕНА СЕРВИС')['total_matches'])
        with self.assertRaisesRegex(ValueError, 'query or filter'):
            find_recoveries(records, checksum)

    def test_find_keeps_unknown_cause_unknown(self):
        recovered = self.entry('02-recovered.json')
        recovered['cause'] = self.entry()['cause']
        self.append(recovered)
        match = self.find()['matches'][0]
        self.assertEqual('unknown', match['cause']['status'])
        self.assertIsNone(match['cause']['description'])
        self.assertEqual('verified', match['recovery']['outcome'])

    def test_find_excludes_changed_confirmed_cause_even_with_intact_recovery(self):
        self.append(self.entry('02-recovered.json'))
        (self.inputs / 'observations.txt').write_text('Изменённое основание причины')
        result = self.find()
        self.assertEqual([], result['matches'])
        self.assertEqual(1, result['skipped']['evidence_unverifiable'])

    def test_find_bounds_results_and_prioritizes_matching_context(self):
        for index in range(7):
            recovered = self.entry('02-recovered.json')
            recovered.update(event_id=f'recovered-{index}', incident_id=f'incident-{index}',
                             occurred_at=f'2020-01-01T{index + 10:02d}:00:00+00:00')
            if index > 0:
                recovered['context']['environment_id'] = None
            self.append(recovered)
        result = self.find(context={'environment_id': 'synthetic'})
        self.assertEqual(5, len(result['matches']))
        self.assertEqual(7, result['total_matches'])
        self.assertIs(True, result['truncated'])
        self.assertEqual('recovered-0', result['matches'][0]['event_id'])
        self.assertEqual('recovered-6', result['matches'][1]['event_id'])

    def test_find_rejects_invalid_limits_and_unknown_context_values(self):
        self.append(self.entry('02-recovered.json'))
        for kwargs in ({'limit': 0}, {'limit': 21}, {'limit': True}, {'context': []},
                       {'context': {'model': None}}, {'stage': 'invented'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.find(**kwargs)

    def test_find_cli_filters_context_and_returns_machine_readable_snapshot(self):
        self.append(self.entry('02-recovered.json'))
        context = self.base / 'current-context.json'
        context.write_text(json.dumps({'environment_id': 'synthetic'}))
        before = self.journal.read_bytes()
        command = [sys.executable, str(CLI), 'find', '--journal', str(self.journal),
                   '--scope-kind', 'synthetic', '--query', 'отмена сервис', '--context-file', str(context), '--limit', '2']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual('matched', snapshot['matches'][0]['context_match'])
        self.assertEqual(str(self.journal.resolve()), snapshot['journal'])
        self.assertEqual(before, self.journal.read_bytes())

    def test_find_cli_does_not_treat_corrupted_history_as_no_matches(self):
        self.journal.write_text('incomplete record')
        result = subprocess.run([sys.executable, str(CLI), 'find', '--journal', str(self.journal),
                                 '--agent', 'qa-feature-tester'], capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('Failure journal error:', result.stdout)

    def test_find_cli_requires_search_and_rejects_filters_on_other_commands(self):
        self.append(self.entry('02-recovered.json'))
        for arguments in (['find'], ['find', '--limit', '0', '--agent', 'qa-feature-tester'],
                          ['check', '--query', 'сервис']):
            result = subprocess.run([sys.executable, str(CLI), *arguments, '--journal', str(self.journal)],
                                    capture_output=True, text=True)
            self.assertEqual(2, result.returncode)

    def test_updates_and_multiple_affected_scenarios_are_one_incident(self):
        self.append(self.entry())
        self.append(self.entry('02-recovered.json'))
        self.append(self.entry('03-hypothesis.json'))
        summary = self.summary()
        self.assertEqual(3, summary['events'])
        self.assertEqual(2, summary['incidents'])
        self.assertEqual(1, summary['statuses']['recovered'])
        self.assertEqual(1, summary['statuses']['open'])
        self.assertEqual({'environment': 1}, summary['confirmed_causes_with_intact_evidence'])
        self.assertEqual({'requirements': 1}, summary['suspected_causes'])
        rendered = render_summary(summary)
        self.assertIn('не вероятность сбоя', rendered)
        self.assertIn('гипотеза', rendered)
        self.assertIn('исходный блокер снят', rendered)

    def test_same_event_is_idempotent_but_changed_content_cannot_replace_it(self):
        entry = self.entry()
        self.assertTrue(self.append(entry))
        original = self.journal.read_bytes()
        self.assertFalse(self.append(entry))
        self.assertEqual(original, self.journal.read_bytes())
        entry['impact'] = 'Другие последствия'
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.append(entry)
        self.assertEqual(original, self.journal.read_bytes())

    def test_assignment_is_immutable_and_revisions_preserve_previous_unknown_cause(self):
        entry = self.entry()
        self.append(entry)
        changed = self.entry('02-recovered.json')
        changed['request_id'] = 'other-request'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.append(changed)
        self.append(self.entry('02-recovered.json'))
        records, _ = read_journal(self.journal)
        self.assertEqual('unknown', records[0]['entry']['cause']['status'])
        self.assertEqual('confirmed', records[1]['entry']['cause']['status'])

    def test_verified_recovery_requires_local_proof_and_cannot_be_a_planned_retry(self):
        recovered = self.entry('02-recovered.json')
        for change in ('no-proof', 'remote-only', 'no-result', 'runner'):
            with self.subTest(change=change):
                value = copy.deepcopy(recovered)
                if change == 'no-proof':
                    value['recovery']['evidence'] = []
                elif change == 'remote-only':
                    value['recovery']['evidence'] = ['https://example.invalid/recovery']
                elif change == 'no-result':
                    value['recovery']['result'] = None
                else:
                    value['actor'] = 'qa-runner'
                    value['cause'] = self.entry()['cause']
                with self.assertRaises(ValueError):
                    self.append(value)
        attempted = self.entry()
        attempted['recovery'] = dict(outcome='in_progress', action='Перезапуск запланирован и начат', result=None, evidence=[])
        self.append(attempted)
        self.assertEqual(1, self.summary()['statuses']['recovering'])
        self.assertEqual(0, self.summary()['statuses']['recovered'])

    def test_confirmation_requires_independent_recorded_assessment_and_evidence(self):
        entry = self.entry('02-recovered.json')
        for change in ('self-review', 'empty-proof', 'unknown-diagnosis'):
            value = copy.deepcopy(entry)
            if change == 'self-review':
                value['actor'] = value['agent']
            elif change == 'empty-proof':
                value['cause']['evidence'] = []
            else:
                value['cause']['status'] = 'unknown'
            with self.assertRaises(ValueError):
                self.append(value)

    def test_changed_or_missing_evidence_does_not_show_accepted_cause_or_recovery(self):
        self.append(self.entry('02-recovered.json'))
        (self.inputs / 'recovery.txt').write_text('Changed after review')
        summary = self.summary()
        self.assertEqual(0, summary['statuses']['recovered'])
        self.assertEqual(1, summary['statuses']['recovery_unverifiable'])
        (self.inputs / 'observations.txt').unlink()
        summary = self.summary()
        self.assertEqual({}, summary['confirmed_causes_with_intact_evidence'])
        self.assertTrue(summary['incidents_detail'][0]['evidence_issues'])
        # Old inaccessible proof cannot stop unrelated new incident observations from being saved.
        evidence = self.inputs / 'new.txt'
        evidence.write_text('New symptom')
        new = self.entry()
        new.update(incident_id='NEW', event_id='NEW-event', evidence=['new.txt'])
        self.assertTrue(self.append(new))
        self.assertEqual(2, self.summary()['incidents'])

    def test_reopening_requires_explicit_reference_and_time_cannot_go_backwards(self):
        self.append(self.entry('02-recovered.json'))
        reopened = self.entry()
        reopened.update(event_id='reopened', occurred_at='2020-01-01T13:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'reopening'):
            self.append(reopened)
        reopened['reopens_event_id'] = 'DEMO-1-recovered'
        self.append(reopened)
        self.assertEqual(1, self.summary()['statuses']['open'])
        old = self.entry()
        old['event_id'] = 'late-edit'
        with self.assertRaisesRegex(ValueError, 'reverses time'):
            self.append(old)

    def test_tampered_history_and_incomplete_tail_reject_new_writes(self):
        self.append(self.entry())
        original = self.journal.read_bytes()
        record = json.loads(original)
        record['entry']['summary'] = 'Everything passed'
        self.journal.write_text(json.dumps(record) + '\n')
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            self.append(self.entry('03-hypothesis.json'))
        self.journal.write_bytes(original + b'{')
        with self.assertRaisesRegex(ValueError, 'incomplete tail'):
            self.append(self.entry('03-hypothesis.json'))
        self.assertEqual(original + b'{', self.journal.read_bytes())

    def test_synthetic_records_are_excluded_from_operational_statistics(self):
        self.append(self.entry())
        self.assertEqual(0, self.summary('operational')['incidents'])
        self.assertEqual(1, self.summary('all')['incidents'])

    def test_parallel_processes_do_not_lose_records_or_duplicate_sequences(self):
        files = []
        for index in range(6):
            entry = self.entry()
            entry.update(event_id=f'event-{index}', incident_id=f'incident-{index}')
            path = self.inputs / f'parallel-{index}.json'
            path.write_text(json.dumps(entry))
            files.append(path)
        def write(path):
            return subprocess.run([sys.executable, str(CLI), 'record', '--journal', str(self.journal), '--entry', str(path)], capture_output=True, text=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(write, files))
        for result in results:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        records, _ = read_journal(self.journal)
        self.assertEqual(list(range(1, 7)), [record['sequence'] for record in records])
        self.assertEqual(6, self.summary()['incidents'])

    def test_cli_preserves_existing_files_and_distinguishes_evidence_limitations(self):
        command = [sys.executable, str(CLI)]
        result = subprocess.run(command + ['init', '--journal', str(self.journal)], capture_output=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual(2, subprocess.run(command + ['init', '--journal', str(self.journal)], capture_output=True).returncode)
        self.append(self.entry())
        output = self.base / 'summary'
        arguments = ['summarize', '--journal', str(self.journal), '--scope-kind', 'synthetic', '--output-dir', str(output)]
        self.assertEqual(0, subprocess.run(command + arguments, capture_output=True).returncode)
        original = (output / 'summary.md').read_bytes()
        self.assertEqual(2, subprocess.run(command + arguments, capture_output=True).returncode)
        self.assertEqual(original, (output / 'summary.md').read_bytes())
        self.assertEqual(0, subprocess.run(command + ['check', '--journal', str(self.journal)], capture_output=True).returncode)
        (self.inputs / 'observations.txt').unlink()
        self.assertEqual(1, subprocess.run(command + ['check', '--journal', str(self.journal)], capture_output=True).returncode)

    def test_bad_types_unknown_fields_and_missing_sources_never_create_a_journal(self):
        for key, value in [('cause', []), ('symptom', 'invented'), ('context', []), ('scenario_ids', ['TC-1', 'TC-1']), ('evidence', ['missing.txt']), ('occurred_at', '2020-01-01'), ('schema_version', True)]:
            entry = self.entry()
            entry[key] = value
            with self.assertRaises((ValueError, OSError), msg=key):
                self.append(entry)
        entry = self.entry()
        entry['unrecognized'] = 'must not be silently ignored'
        with self.assertRaises(ValueError):
            self.append(entry)
        self.assertFalse(self.journal.exists())

    def test_regression_task_is_not_added_until_a_saved_artifact_exists(self):
        entry = self.entry()
        entry['regression_case'] = dict(status='added', reason='Regression coverage', evidence=[])
        with self.assertRaisesRegex(ValueError, 'saved task'):
            self.append(entry)
        entry['regression_case']['evidence'] = ['observations.txt']
        self.append(entry)
        self.assertEqual('added', self.summary()['incidents_detail'][0]['regression_case']['status'])
        # The validator checks existence; a person still checks whether this is an actual useful task.


if __name__ == '__main__':
    unittest.main()
