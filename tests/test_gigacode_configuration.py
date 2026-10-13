"""Check capability boundaries without claiming to execute the CLI runtime."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def role_tools(role):
    header = (ROOT / '.gigacode/agents' / (role + '.md')).read_text().split('---', 2)[1]
    if '\ntools:\n' not in header:
        return None
    return [line.strip()[2:] for line in header.split('\ntools:\n', 1)[1].splitlines()
            if line.strip().startswith('- ')]


class CLIConfigurationTests(unittest.TestCase):
    def test_impact_role_has_local_analysis_tools_without_product_connectors(self):
        tools = role_tools('qa-impact-analyst')
        self.assertEqual({'read_file', 'glob', 'grep_search', 'write_file', 'edit', 'run_shell_command', 'skill'}, set(tools))
        self.assertEqual(len(tools), len(set(tools)))

    def test_reporting_role_has_artifact_tools_without_product_connectors(self):
        tools = role_tools('qa-test-report-writer')
        self.assertEqual({'read_file', 'glob', 'grep_search', 'write_file', 'edit', 'run_shell_command'}, set(tools))
        self.assertEqual(len(tools), len(set(tools)))

    def test_planning_and_code_roles_can_create_and_validate_artifacts_without_browser(self):
        for role in ['qa-analyst', 'qa-test-plan-writer', 'qa-test-automation', 'qa-autotest-reviewer']:
            with self.subTest(role=role):
                tools = role_tools(role)
                self.assertIsNotNone(tools)
                self.assertEqual(len(tools), len(set(tools)))
                self.assertTrue({'read_file', 'write_file', 'edit', 'run_shell_command', 'skill',
                                 'glob', 'grep_search', 'tool_search', 'tool_call'} <= set(tools))
                self.assertFalse(any(tool.startswith('mcp__') or 'browser' in tool for tool in tools))

    def test_infrastructure_roles_keep_inherited_tool_access(self):
        for role in ['qa-feature-tester', 'qa-defect-investigator', 'qa-test-env-configurator',
                     'qa-test-data-builder', 'qa-stub-builder', 'qa-jira-defect-creator']:
            with self.subTest(role=role):
                self.assertIsNone(role_tools(role))

    def test_portable_settings_only_ship_the_documented_mcp_configuration(self):
        settings = json.loads((ROOT / '.gigacode/settings.json').read_text())
        self.assertEqual({'mcpServers'}, set(settings))
        server = settings['mcpServers']['playwright']
        self.assertIn('.gigacode/playwright.config.json', server['args'])
        self.assertIn('browser_navigate', server['includeTools'])
        self.assertIn('browser_snapshot', server['includeTools'])
        self.assertNotIn('env', server)
        self.assertNotIn('trust', server)


if __name__ == '__main__':
    unittest.main()
