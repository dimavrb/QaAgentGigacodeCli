"""Portable project installs: actual files, updates, recovery and local CLI."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_install as installer
import qa_project


class ProjectInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_source_files = staticmethod(installer.source_files)
        cls.info, cls.files = installer.source_files(ROOT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'приложение с пробелами'
        self.project.mkdir()
        self.payload = dict(self.files)
        self.release = dict(self.info)
        mocked = patch('qa_install.source_files', side_effect=lambda _: (self.release, self.payload))
        mocked.start(); self.addCleanup(mocked.stop)
        idle = patch('qa_install.ensure_idle')
        self.idle = idle.start(); self.addCleanup(idle.stop)

    def install(self, **kwargs):
        return installer.install(self.project, ROOT, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): p.read_bytes() for p in self.project.rglob('*')
                if p.is_file() and 'install.local' not in p.parts and p.name != 'session.local.lock'
                and '__pycache__' not in p.parts}

    def upgrade(self):
        self.release['package_version'] = '9.9.9'
        self.payload['VERSION'] = b'9.9.9\n'
        manifest = json.loads(self.payload['gigacode-extension.json'])
        manifest['version'] = '9.9.9'
        self.payload['gigacode-extension.json'] = installer.encoded(manifest)
        for name in ('.gigacode/agents/qa-analyst.md', 'agents/qa-analyst.md'):
            self.payload[name] += b'\nUpdated role instructions.\n'

    def test_preview_no_writes_and_install_preserves_unrelated_files(self):
        (self.project / 'GIGACODE.md').write_text('Product rules\n')
        target = self.project / '.gigacode/settings.json'; target.parent.mkdir()
        target.write_text('{"personal":true}')
        before = self.snapshot()
        preview = self.install()
        self.assertEqual('preview', preview['status'])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / '.qa-agent').exists())
        result = self.install(apply=True)
        self.assertEqual('installed', result['status'])
        self.assertEqual('{"personal":true}', target.read_text())
        self.assertTrue((self.project / 'GIGACODE.md').read_text().startswith('Product rules\n'))
        self.assertFalse((self.project / qa_project.CONFIG).exists())
        metadata = installer.metadata(self.project)
        self.assertEqual('.qa-agent/package', metadata['package_root'])
        self.assertNotIn(str(self.project), json.dumps(metadata))
        self.assertEqual(14, len(list((self.project / '.gigacode/agents').glob('*.md'))))
        self.assertTrue((self.project / '.gigacode/skills/jira-defect-creator/scripts/check_payload.py').is_file())
        self.assertFalse((self.project / installer.PACKAGE / 'scripts/qa_extension_mcp.py').exists())
        self.assertFalse(json.loads((self.project / installer.PACKAGE / 'gigacode-extension.json').read_text()).get('mcpServers'))
        before = self.snapshot()
        self.assertEqual('unchanged', self.install(apply=True)['status'])
        self.assertEqual(before, self.snapshot())

    def test_upgrade_changes_roles_skills_and_commands_together(self):
        self.install(apply=True)
        context = self.project / qa_project.CONTEXT; context.write_text('Product context')
        evidence = self.project / '.qa-agent/artifacts/evidence.txt'; evidence.parent.mkdir(parents=True)
        evidence.write_text('Historical evidence')
        self.upgrade()
        for prefix in ('.gigacode/', ''):
            self.payload[prefix + 'skills/jira-defect-creator/references/new.md'] = b'New reference'
            self.payload[prefix + 'commands/qa/tasks.md'] += b'\nUpdated command\n'
        self.assertEqual('installed', self.install(apply=True)['status'])
        self.assertEqual('9.9.9', installer.metadata(self.project)['package_version'])
        self.assertIn('Updated role', (self.project / '.gigacode/agents/qa-analyst.md').read_text())
        self.assertTrue((self.project / '.gigacode/skills/jira-defect-creator/references/new.md').is_file())
        self.assertEqual('Product context', context.read_text())
        self.assertEqual('Historical evidence', evidence.read_text())

    def test_one_conflict_prevents_all_updates(self):
        self.install(apply=True)
        (self.project / '.gigacode/agents/qa-analyst.md').write_text('Local role adaptation')
        before = self.snapshot()
        self.upgrade()
        result = self.install(apply=True)
        self.assertEqual('conflict', result['status'])
        self.assertEqual(['.gigacode/agents/qa-analyst.md'], result['conflicts'])
        self.assertEqual(before, self.snapshot())

    def test_memory_updates_preserve_product_rules_and_refuse_changed_qa_block(self):
        self.install(apply=True)
        memory = self.project / 'GIGACODE.md'
        memory.write_text('New product rule\n' + memory.read_text())
        installer.metadata(self.project)
        self.upgrade(); self.install(apply=True)
        self.assertTrue(memory.read_text().startswith('New product rule\n'))
        memory.write_text(memory.read_text().replace('QA-пакет проекта:', 'Edited QA instructions:'))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'changed locally'):
            self.install(apply=True)
        self.assertEqual(before, self.snapshot())
        with self.assertRaisesRegex(ValueError, 'managed QA block'):
            installer.metadata(self.project)

    def test_existing_managed_roles_migrate_using_verified_baselines(self):
        from qa_agent_state import sync_roles, STATE
        sync_roles(self.project, ROOT, '2.3.12', {'qa-analyst.md': b'Original legacy role'}, apply=True)
        history = (self.project / STATE).read_bytes()
        self.assertEqual('installed', self.install(apply=True)['status'])
        self.assertEqual(self.payload['.gigacode/agents/qa-analyst.md'],
                         (self.project / '.gigacode/agents/qa-analyst.md').read_bytes())
        self.assertEqual(history, (self.project / STATE).read_bytes())

    def test_locally_adapted_legacy_role_is_not_overwritten_on_migration(self):
        from qa_agent_state import sync_roles
        sync_roles(self.project, ROOT, '2.3.12', {'qa-analyst.md': b'Original legacy role'}, apply=True)
        (self.project / '.gigacode/agents/qa-analyst.md').write_text('Personal role adaptation')
        before = self.snapshot()
        self.assertEqual('conflict', self.install(apply=True)['status'])
        self.assertEqual(before, self.snapshot())

    def test_deleted_upstream_file_removed_only_if_unchanged(self):
        self.payload['docs/retired.md'] = b'Old documentation'
        self.install(apply=True)
        self.upgrade(); del self.payload['docs/retired.md']
        self.install(apply=True)
        self.assertFalse((self.project / installer.PACKAGE / 'docs/retired.md').exists())
        installer.metadata(self.project)

    def test_unknown_profile_collision_and_deleted_local_file_are_conflicts(self):
        path = self.project / '.gigacode/commands/qa.md'; path.parent.mkdir(parents=True)
        path.write_text('Existing user command')
        before = self.snapshot()
        self.assertIn('.gigacode/commands/qa.md', self.install(apply=True)['conflicts'])
        self.assertEqual(before, self.snapshot())
        path.unlink(); self.install(apply=True)
        path.unlink(); self.upgrade()
        self.assertIn('.gigacode/commands/qa.md', self.install(apply=True)['conflicts'])

    def test_cloned_checkout_resolves_paths_without_local_binding(self):
        self.install(apply=True)
        clone = self.base / 'cloned elsewhere'
        shutil.copytree(self.project, clone, ignore=shutil.ignore_patterns('install.local', '*.local.lock', '__pycache__'))
        self.assertEqual(str(clone / installer.PACKAGE), qa_project.read_binding(clone / qa_project.CONFIG, clone)['package_root'])
        result = subprocess.run([sys.executable, '-B', str(clone / installer.PACKAGE / 'scripts/qa_project.py'),
                                 'init', '--project-root', str(clone)], cwd=self.base,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(clone / installer.PACKAGE), json.loads(result.stdout)['package_root'])
        self.assertFalse((clone / qa_project.CONFIG).exists())

    def test_project_launcher_uses_local_package_and_holds_session_lock(self):
        self.install(apply=True)
        binaries = self.base / 'bin'; binaries.mkdir()
        cli = binaries / 'gigacode'
        cli.write_text('#!' + sys.executable + '''
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'.qa-agent/package/scripts'))
from qa_install import session_lock
blocked=False
try:
 with session_lock(Path.cwd(),exclusive=True): pass
except ValueError:
 blocked=True
print(json.dumps(dict(cwd=str(Path.cwd()),args=sys.argv[1:],update_blocked=blocked)))
''')
        cli.chmod(0o700)
        command = [sys.executable, '-B', str(self.project / installer.PACKAGE / 'scripts/qa_start.py'),
                   '--project-root', str(self.project), '--', '--fixture-option']
        result = subprocess.run(command, cwd=self.base, capture_output=True, text=True, timeout=15,
                                env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(dict(cwd=str(self.project), args=['--fixture-option'], update_blocked=True),
                         json.loads(result.stdout))

    def test_distribution_manifest_rejects_changed_source(self):
        source = self.base / 'release'; source.mkdir()
        for name, data in self.files.items():
            target = source / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        manifest = dict(version=self.info['package_version'], files=[dict(path=name, sha256=installer.digest(data))
                         for name, data in self.files.items()])
        (source / 'PACKAGE_MANIFEST.json').write_bytes(installer.encoded(manifest))
        (source / 'README.md').write_text('Changed after release')
        with self.assertRaisesRegex(ValueError, 'source differs'):
            self.real_source_files(source)

    def test_local_creation_and_nested_validator_use_installed_copy(self):
        self.install(apply=True)
        package = self.project / installer.PACKAGE
        criteria = self.project / 'criteria.json'
        criteria.write_text(json.dumps({'schema_version': 1, 'criteria': [{'criterion_id': 'C-1',
            'description': 'Check requirement', 'source': 'requirements.md', 'required': True}]}))
        command = [sys.executable, '-B', str(package / 'scripts/qa_task_create.py'), '--task-id', 'QA-1',
                   '--title', 'Local task', '--criteria', str(criteria)]
        for expected in ('created', 'existing'):
            result = subprocess.run(command, cwd=self.project, capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected, json.loads(result.stdout)['status'])
        # Submit a synthetic blocked report through the installed sibling validator.
        code = '''import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import task_exchange as e
state=Path(sys.argv[2])
assignment=dict(task_id='QA-1',request_id='r1',agent='qa-analyst',stage='analysis',goal='Analyse',
acceptance_criteria=['Requirement'],criterion_ids=['C-1'],stop_condition='Unknown',
expected_artifact='JSON',result_path=str(state.parent/'result.json'))
e.assign(state,assignment,state.parent,1)
handoff=e.prepare_handoff(state,'r1',2)
sys.path.insert(0,str(Path(sys.argv[1]).parent/'tests'))
from self_review_fixtures import review_for
report=dict(task_id='QA-1',request_id='r1',agent='qa-analyst',event='blocked',
summary='Synthetic missing requirement',artifacts=[],blockers=['Missing requirement'],
self_review=review_for(limited=True),payload={})
Path(assignment['result_path']).write_text(json.dumps(report))
run=subprocess.run
commands=[]
def checked(argv,**kwargs):
 assert Path(argv[1]).resolve().is_relative_to(Path(sys.argv[1]).resolve()), argv
 commands.append(argv)
 return run(argv,**kwargs)
e.subprocess.run=checked
e.submit_result(state,'r1',3)
assert commands and Path(commands[0][1]).name=='validate_qa_result.py'
e.review_result(state,'r1','Synthetic blocker checked',True,4)
print(json.dumps(dict(handoff=handoff,revision=e.read_state(state)['revision'])))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(package / 'scripts'),
                                 str(self.project / '.qa-agent-state/tasks/QA-1/task.json')],
                                cwd=self.project, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(str(package / 'docs/SELF_REVIEW.md'), data['handoff']['result']['self_review_guide'])
        self.assertEqual(5, data['revision'])

    def test_publication_error_rolls_back_entire_release(self):
        self.install(apply=True); before = self.snapshot(); self.upgrade()
        original = installer.publish
        counter = 0
        def fail_once(*args):
            nonlocal counter
            counter += 1
            if counter == 4:
                raise OSError('injected publication failure')
            return original(*args)
        with patch('qa_install.publish', side_effect=fail_once), self.assertRaisesRegex(OSError, 'injected'):
            self.install(apply=True)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / installer.PENDING).exists())
        installer.metadata(self.project)

    def test_abrupt_interruption_requires_recovery_and_preserves_history(self):
        self.install(apply=True); before = self.snapshot(); self.upgrade()
        original = installer.publish
        calls = []
        def stop(*args):
            calls.append(args[1])
            if len(calls) == 3:
                raise KeyboardInterrupt()
            return original(*args)
        with patch('qa_install.publish', side_effect=stop), self.assertRaises(KeyboardInterrupt):
            self.install(apply=True)
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            installer.binding(self.project)
        self.assertEqual('recovered', self.install(recovery=True, apply=True)['status'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual('installed', self.install(apply=True)['status'])

    def test_symlinked_profile_and_package_cannot_write_outside_project(self):
        outside = self.base / 'outside'; outside.mkdir()
        for relative in ('.gigacode', '.qa-agent/package'):
            path = self.project / relative; path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                self.install(apply=True)
            self.assertEqual([], list(outside.iterdir()))
            path.unlink()

    def test_manifest_cannot_claim_unrelated_or_escaping_files(self):
        self.install(apply=True)
        path = self.project / installer.INSTALL
        good = json.loads(path.read_text())
        for name in ('../../outside', '.env', '/tmp/file', '.gigacode/settings.json'):
            bad = deepcopy(good); bad['files'][name] = 'a' * 64; path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                installer.metadata(self.project, verify=False)

    def test_active_work_and_session_lock_prevent_update(self):
        self.install(apply=True); before = self.snapshot(); self.upgrade()
        self.idle.side_effect = ValueError('active plan')
        with self.assertRaisesRegex(ValueError, 'active plan'):
            self.install(apply=True)
        self.assertEqual(before, self.snapshot())
        self.idle.side_effect = None
        with installer.session_lock(self.project), self.assertRaisesRegex(ValueError, 'session/update'):
            self.install(apply=True)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
