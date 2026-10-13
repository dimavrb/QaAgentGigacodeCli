import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qa_contract import parse_message, validate_message


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.message = {
            "task_id": "QA-1",
            "request_id": "QA-1-auto-1",
            "agent": "qa-test-automation",
            "event": "ready_for_review",
            "summary": "Тест добавлен",
            "artifacts": ["result.xml"],
            "blockers": [],
            "payload": {
                "scenario_id": "S-1",
                "requirement_source": "spec.md#filter",
                "base_revision": "abc",
                "current_revision": "def",
                "changed_files": ["tests/filter_test.py"],
                "diff_artifact": "changes.diff",
                "run_result": "1 passed",
                "limitations": [],
            },
        }

    def test_direct_and_wrapped_result(self):
        for raw in (json.dumps(self.message), json.dumps({"response": json.dumps(self.message)})):
            with self.subTest(raw=raw[:20]):
                parsed, _ = parse_message(raw)
                self.assertEqual([], validate_message(parsed, "QA-1", "QA-1-auto-1", "qa-test-automation"))

    def test_missing_review_evidence_is_rejected(self):
        del self.message["payload"]["run_result"]
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-test-automation")
        self.assertIn("ready_for_review missing payload.run_result", errors)

    def test_cli_event_array_uses_terminal_structured_result_and_usage(self):
        terminal = {
            "type": "result", "is_error": False,
            "structured_result": self.message,
            "result": "not the structured payload", "usage": {"input_tokens": 123},
        }
        parsed, envelope = parse_message(json.dumps([{"type": "assistant"}, terminal]))
        self.assertEqual(self.message, parsed)
        self.assertEqual({"input_tokens": 123}, envelope["usage"])

    def test_cli_event_array_supports_string_result_without_schema(self):
        terminal = {"type": "result", "structured_result": None, "result": json.dumps(self.message)}
        parsed, _ = parse_message(json.dumps([terminal]))
        self.assertEqual(self.message, parsed)

    def test_fenced_legacy_message_remains_supported(self):
        parsed, _ = parse_message("```json\n" + json.dumps(self.message) + "\n```")
        self.assertEqual(self.message, parsed)

    def test_cli_error_or_unfinished_event_array_cannot_reuse_previous_success(self):
        success = {"type": "result", "result": json.dumps(self.message)}
        for value in [[], [success, {"type": "assistant"}],
                      [success, dict(success, is_error=True)], dict(success, is_error=True)]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_message(json.dumps(value))

    def test_wrong_request_is_rejected(self):
        errors = validate_message(self.message, "QA-1", "QA-1-auto-2", "qa-test-automation")
        self.assertIn("request_id must equal 'QA-1-auto-2'", errors)

    def test_blocked_requires_blocker(self):
        self.message["event"] = "blocked"
        self.assertIn(
            "blocked result must list blockers",
            validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-test-automation"),
        )

    def test_invalid_event_type_is_reported(self):
        self.message["event"] = []
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-test-automation")
        self.assertIn("event must be a non-empty string", errors)

    def test_completion_contract_for_each_other_role(self):
        cases = {
            "qa-analyst": ("analysis_ready", json.loads((Path(__file__).resolve().parents[1] / "examples/analysis/result.json").read_text())["payload"]),
            "qa-test-plan-writer": ("plan_ready", {"plan_artifact": "plan.md", "plan_revision": "v1", "test_cases_artifact": "test-cases.json", "requirement_coverage": [{"requirement_id": "R1", "source": "spec", "scenario_ids": ["S1"], "priority": "high", "verification": "UI", "evidence": "screenshot", "status": "ready"}], "open_questions": []}),
            "qa-test-env-configurator": ("environment_ready", {"environment_id": "stage", "product_version": "1", "verified_capabilities": ["login"], "limitations": []}),
            "qa-test-data-builder": ("data_ready", {"environment_id": "stage", "data_ids": ["user1"], "verified_properties": ["active"], "cleanup": "delete user1", "scenario_ids": ["S1"]}),
            "qa-defect-investigator": ("investigation_complete", {"outcome": "defect_confirmed", "expected_source": "spec", "actual_result": "500", "attempts": ["once"], "evidence": ["log"], "limitations": []}),
            "qa-feature-tester": ("feature_test_complete", {"scenario_results": [{"scenario_id": "S1", "environment_id": "stage", "product_version": "1", "status": "passed", "steps": ["open page"], "expected_source": "spec", "expected_result": "page opens", "actual_result": "page opened", "evidence": ["screenshot.png"]}], "uncovered_scenarios": [], "defects": []}),
            "qa-autotest-reviewer": ("review_passed", {"reviewed_revision": "def", "findings": [], "checks": ["assert"], "limitations": []}),
            "qa-jira-defect-creator": ("jira_defect_created", {"issue_key": "QA-2", "issue_url": "https://jira.example/QA-2", "duplicate_check": "none", "evidence": ["log"]}),
            "qa-test-report-writer": ("test_report_ready", {"report_artifact": "report.json", "summary_artifact": "report.md", "report_revision": "v1", "limitations": []}),
        }
        for agent, (event, payload) in cases.items():
            with self.subTest(agent=agent):
                message = dict(self.message, agent=agent, event=event, payload=payload)
                self.assertEqual([], validate_message(message, "QA-1", "QA-1-auto-1", agent))
                message["payload"] = {}
                self.assertTrue(validate_message(message, "QA-1", "QA-1-auto-1", agent))

    def test_partial_result_names_ready_and_blocked_scenarios(self):
        message = dict(
            self.message,
            agent="qa-test-env-configurator",
            event="environment_partial",
            payload={
                "environment_id": "stage", "product_version": "1",
                "verified_capabilities": ["login"], "limitations": ["payment unavailable"],
                "ready_scenarios": ["S1"], "blocked_scenarios": ["S2"],
            },
        )
        self.assertEqual([], validate_message(message, "QA-1", "QA-1-auto-1", "qa-test-env-configurator"))
        del message["payload"]["blocked_scenarios"]
        self.assertIn(
            "payload.blocked_scenarios must be a list",
            validate_message(message, "QA-1", "QA-1-auto-1", "qa-test-env-configurator"),
        )

    def test_role_cannot_emit_another_roles_success_event(self):
        self.message["event"] = "jira_defect_created"
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-test-automation")
        self.assertIn("unexpected event for qa-test-automation: 'jira_defect_created'", errors)

    def test_investigation_outcome_with_wrong_type_returns_error(self):
        self.message.update(agent="qa-defect-investigator", event="investigation_complete")
        self.message["payload"] = {
            "outcome": [], "expected_source": "spec", "actual_result": "500",
            "attempts": [], "evidence": ["log"], "limitations": [],
        }
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-defect-investigator")
        self.assertIn("payload.outcome has an unknown investigation result", errors)

    def test_feature_results_match_assignment_and_have_evidence(self):
        self.message.update(agent="qa-feature-tester", event="feature_test_complete")
        self.message["payload"] = {
            "scenario_results": [{
                "scenario_id": "S1", "environment_id": "stage", "product_version": "1",
                "status": "passed", "steps": ["open page"], "expected_source": "spec",
                "expected_result": "page opens", "actual_result": "page opened", "evidence": ["screenshot.png"],
            }],
            "uncovered_scenarios": [], "defects": [],
        }
        self.assertEqual([], validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-feature-tester", ["S1"]))
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-feature-tester", ["S1", "S2"])
        self.assertTrue(any("missing=['S2']" in error for error in errors))
        self.message["payload"]["scenario_results"][0]["evidence"] = []
        errors = validate_message(self.message, "QA-1", "QA-1-auto-1", "qa-feature-tester", ["S1"])
        self.assertIn("payload.scenario_results[0].evidence must not be empty for executed scenarios", errors)


if __name__ == "__main__":
    unittest.main()
