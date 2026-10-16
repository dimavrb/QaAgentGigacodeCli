"""Local contract/process checks; no live model evaluation."""
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agent_trace


class TraceTests(unittest.TestCase):
    def test_parseable_record_without_newline_is_still_an_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trace.jsonl'
            path.write_text('{"schema_version":1}')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                agent_trace.append(path, agent_trace.context('QA-1'), 'runner_started')
            self.assertEqual('{"schema_version":1}', path.read_text())

    def test_success_failure_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trace.jsonl'
            ctx = agent_trace.context('QA-1', 'REQ-1', 'ATT-1', 'qa-analyst')
            agent_trace.append(path, ctx, 'runner_started')
            agent_trace.append(path, ctx, 'runner_finished', exit_code=0)
            agent_trace.append(path, ctx, 'runner_finished', exit_code=3)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([0, 3], [r['exit_code'] for r in records if 'exit_code' in r])
            with path.open('a') as stream:
                stream.write('{broken')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                agent_trace.append(path, ctx, 'runner_finished')
            self.assertEqual(before, path.read_bytes())
