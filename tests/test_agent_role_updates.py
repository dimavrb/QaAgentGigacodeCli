import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_agent_state as roles
import qa_start
from qa_files import project_lock


class ManagedRoleUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / 'проект'
        self.project.mkdir()
        self.package = self.project.parent / 'package'
        self.package.mkdir()
        self.vendor = {'qa-one.md': b'one v1\n', 'qa-two.md': b'two v1\n'}

    def sync(self, version='1.0.0', apply=True, **kwargs):
        with project_lock(self.project):
            return roles.sync_roles(self.project, self.package, version, self.vendor, apply=apply, **kwargs)

    def file(self, name):
        return self.project / roles.AGENTS / name

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}

    def test_new_vendor_replaces_unedited_role_and_keeps_baseline_and_backup(self):
        self.sync()
        self.vendor['qa-one.md'] = b'one v2\n'
        before = self.snapshot()
        preview = self.sync('1.0.1', apply=False)
        self.assertEqual(['.gigacode/agents/qa-one.md'], preview['planned_files'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual('installed', self.sync('1.0.1')['status'])
        self.assertEqual(b'one v2\n', self.file('qa-one.md').read_bytes())
        state = roles.load_state(self.project)
        self.assertEqual('1.0.1', state['package_version'])
        self.assertEqual(b'one v2\n', roles.blob(self.project, state['roles']['qa-one.md']['sha256']))
        self.assertEqual(b'one v1\n', (self.project / roles.DATA / 'backups' / state['last_transaction'] / 'qa-one.md').read_bytes())
        after = self.snapshot()
        self.assertEqual([], self.sync('1.0.1')['changed_files'])
        self.assertEqual(after, self.snapshot())

    def test_conflict_does_not_block_safe_update_or_new_role(self):
        self.sync()
        self.file('qa-one.md').write_bytes(b'local adaptation\n')
        self.vendor = {'qa-one.md': b'one v2\n', 'qa-two.md': b'two v2\n', 'qa-three.md': b'new\n'}
        result = self.sync('1.0.1')
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual('both_changed', result['conflict_details'][0]['reason'])
        self.assertEqual(b'local adaptation\n', self.file('qa-one.md').read_bytes())
        self.assertEqual(b'two v2\n', self.file('qa-two.md').read_bytes())
        self.assertEqual(b'new\n', self.file('qa-three.md').read_bytes())
        state = roles.load_state(self.project)
        self.assertEqual('1.0.0', state['package_version'])
        self.assertEqual('1.0.0', state['roles']['qa-one.md']['version'])
        self.assertEqual('1.0.1', state['roles']['qa-two.md']['version'])
        self.assertEqual([], self.sync('1.0.1')['changed_files'])

    def test_local_adaptation_survives_unrelated_vendor_release(self):
        self.sync()
        self.file('qa-one.md').write_bytes(b'local adaptation')
        self.vendor['qa-two.md'] = b'two v2'
        result = self.sync('1.0.1')
        self.assertEqual('installed', result['status'])
        self.assertEqual(['qa-one.md'], result['adapted_roles'])
        self.assertEqual(b'local adaptation', self.file('qa-one.md').read_bytes())

    def test_reviewed_merge_advances_vendor_baseline_without_replacing_merge(self):
        self.sync()
        self.vendor['qa-one.md'] = b'one v2'
        self.file('qa-one.md').write_bytes(b'reviewed merge')
        local_hash = roles.digest(b'reviewed merge')
        incoming_hash = roles.digest(b'one v2')
        self.assertEqual('needs_attention', self.sync('1.0.1')['status'])
        result = self.sync('1.0.1', accept_local={'qa-one.md': (local_hash, incoming_hash)})
        self.assertEqual('installed', result['status'])
        self.assertEqual(b'reviewed merge', self.file('qa-one.md').read_bytes())
        self.assertEqual(incoming_hash, roles.load_state(self.project)['roles']['qa-one.md']['sha256'])
        self.assertEqual('installed', self.sync('1.0.1')['status'])
        self.vendor['qa-one.md'] = b'one v3'
        self.assertEqual('both_changed', self.sync('1.0.2')['conflict_details'][0]['reason'])

    def test_changed_resolution_hashes_refuse_every_write(self):
        self.sync()
        self.vendor['qa-one.md'] = b'one v2'
        self.file('qa-one.md').write_bytes(b'local')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'hashes changed'):
            self.sync('1.0.1', accept_local={'qa-one.md': (roles.digest(b'wrong'), roles.digest(b'one v2'))})
        self.assertEqual(before, self.snapshot())

    def test_exact_manual_replacement_resolves_conflict(self):
        self.sync()
        self.vendor['qa-one.md'] = b'one v2'
        self.file('qa-one.md').write_bytes(b'local')
        self.sync('1.0.1')
        self.file('qa-one.md').write_bytes(b'one v2')
        self.assertEqual('installed', self.sync('1.0.1')['status'])

    def test_missing_managed_role_needs_explicit_restore(self):
        self.sync()
        self.file('qa-one.md').unlink()
        self.vendor['qa-one.md'] = b'one v2'
        self.assertEqual('deleted_locally', self.sync('1.0.1')['conflict_details'][0]['reason'])
        self.assertFalse(self.file('qa-one.md').exists())
        self.assertEqual('installed', self.sync('1.0.1', restore=['qa-one.md'])['status'])
        self.assertEqual(b'one v2', self.file('qa-one.md').read_bytes())

    def test_retired_role_is_archived_and_unrelated_file_is_preserved(self):
        self.sync()
        self.file('custom.md').write_text('Product role')
        del self.vendor['qa-one.md']
        result = self.sync('1.0.1')
        self.assertEqual('installed', result['status'])
        self.assertFalse(self.file('qa-one.md').exists())
        state = roles.load_state(self.project)
        self.assertNotIn('qa-one.md', state['roles'])
        self.assertEqual(b'one v1\n', (self.project / roles.DATA / 'backups' / state['last_transaction'] / 'qa-one.md').read_bytes())
        self.assertEqual('Product role', self.file('custom.md').read_text())

    def test_retired_local_adaptation_is_kept_until_moved_out_of_discovery(self):
        self.sync()
        self.file('qa-one.md').write_bytes(b'local')
        del self.vendor['qa-one.md']
        self.assertEqual('removed_upstream_with_local_changes', self.sync('1.0.1')['conflict_details'][0]['reason'])
        self.assertEqual(b'local', self.file('qa-one.md').read_bytes())
        self.file('qa-one.md').rename(self.project / 'saved-role.md')
        self.assertEqual('installed', self.sync('1.0.1')['status'])
        self.assertNotIn('qa-one.md', roles.load_state(self.project)['roles'])

    def test_legacy_identical_roles_are_adopted_and_unknown_edits_conflict(self):
        self.file('qa-one.md').parent.mkdir(parents=True)
        self.file('qa-one.md').write_bytes(self.vendor['qa-one.md'])
        self.file('qa-two.md').write_bytes(b'unknown old role')
        result = self.sync()
        self.assertEqual('unknown_baseline', result['conflict_details'][0]['reason'])
        self.assertIn('qa-one.md', roles.load_state(self.project)['roles'])
        self.assertNotIn('qa-two.md', roles.load_state(self.project)['roles'])
        self.assertEqual(b'unknown old role', self.file('qa-two.md').read_bytes())

    def test_managed_only_does_not_opt_in_and_flags_legacy_copies(self):
        self.assertEqual('unmanaged', self.sync(managed_only=True)['status'])
        self.assertEqual({}, self.snapshot())
        self.file('qa-one.md').parent.mkdir(parents=True)
        self.file('qa-one.md').write_bytes(self.vendor['qa-one.md'])
        result = self.sync(managed_only=True)
        self.assertEqual('needs_attention', result['status'])
        self.assertFalse((self.project / roles.STATE).exists())

    def test_crash_after_pending_before_first_role_can_resume(self):
        with patch.object(roles, 'publish_role', side_effect=OSError('fixture crash')):
            with self.assertRaises(OSError):
                self.sync()
        self.assertIsNotNone(roles.load_state(self.project)['pending'])
        before = self.snapshot()
        self.assertTrue(self.sync(apply=False)['pending'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual('installed', self.sync()['status'])
        self.assertIsNone(roles.load_state(self.project)['pending'])
        self.assertEqual(self.vendor['qa-two.md'], self.file('qa-two.md').read_bytes())

    def test_crash_after_replacement_before_registry_commit_replays_without_replacement(self):
        self.sync()
        self.vendor['qa-one.md'] = b'one v2'
        original = roles.publish_role
        def crash(project, op):
            result = original(project, op)
            raise OSError('crash after rename')
        with patch.object(roles, 'publish_role', side_effect=crash):
            with self.assertRaises(OSError):
                self.sync('1.0.1')
        self.assertEqual(b'one v2', self.file('qa-one.md').read_bytes())
        with patch.object(roles, 'publish_role', wraps=original) as publish:
            result = self.sync('1.0.1')
        self.assertEqual('installed', result['status'])
        self.assertEqual(0, publish.call_count)
        self.assertIsNone(roles.load_state(self.project)['pending'])

    def test_recovery_preserves_new_local_edit_and_updates_other_roles(self):
        self.sync()
        self.vendor = {'qa-one.md': b'one v2', 'qa-two.md': b'two v2'}
        with patch.object(roles, 'publish_role', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.sync('1.0.1')
        self.file('qa-one.md').write_bytes(b'new edit during crash')
        result = self.sync('1.0.1')
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual(b'new edit during crash', self.file('qa-one.md').read_bytes())
        self.assertEqual(b'two v2', self.file('qa-two.md').read_bytes())

    def test_corrupted_baseline_or_registry_never_replaces_roles(self):
        self.sync()
        state = roles.load_state(self.project)
        base = self.project / roles.DATA / 'blobs' / (state['roles']['qa-one.md']['sha256'] + '.md')
        base.write_text('corrupt')
        self.vendor['qa-one.md'] = b'one v2'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.sync('1.0.1')
        self.assertEqual(b'one v1\n', self.file('qa-one.md').read_bytes())

    def test_registry_cannot_name_a_path_outside_agents(self):
        self.sync()
        state = roles.load_state(self.project)
        state['roles']['../outside.md'] = state['roles']['qa-one.md']
        (self.project / roles.STATE).write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'baseline record'):
            self.sync()

    def test_symlinked_history_is_rejected(self):
        outside = self.project.parent / 'outside'
        outside.mkdir()
        (self.project / '.qa-agent').mkdir()
        (self.project / roles.DATA).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.sync()
        self.assertEqual([], list(outside.iterdir()))

    def test_launcher_does_not_start_with_conflicts_or_write_under_shared_lock(self):
        conflict = {'status': 'needs_attention', 'needs_sync': True, 'conflicts': ['qa-one.md']}
        with patch('qa_start.subprocess.run', return_value=subprocess.CompletedProcess([], 1, json.dumps(conflict), '')) as run:
            with self.assertRaisesRegex(ValueError, 'close active sessions'):
                qa_start.synchronize_project_agents(self.project, self.package, may_write=False)
            self.assertEqual(1, run.call_count)
            with self.assertRaisesRegex(ValueError, 'unresolved conflicts'):
                qa_start.synchronize_project_agents(self.project, self.package, may_write=True)

    def test_launcher_rejects_error_exit_disguised_as_ready(self):
        result = subprocess.CompletedProcess([], 1, json.dumps({'status': 'installed', 'needs_sync': False}), '')
        with patch('qa_start.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'invalid project role'):
                qa_start.synchronize_project_agents(self.project, self.package, may_write=True)

    def test_failed_intent_publication_does_not_modify_roles(self):
        original = roles.save_state
        def fail(project, state):
            if state['pending']:
                raise OSError('fixture before durable pending')
            return original(project, state)
        with patch.object(roles, 'save_state', side_effect=fail), self.assertRaises(OSError):
            self.sync()
        self.assertFalse(self.file('qa-one.md').exists())
        self.assertIsNone(roles.load_state(self.project))
        self.assertEqual('installed', self.sync()['status'])

    def test_rollback_uses_same_baseline_rules(self):
        old_vendor = dict(self.vendor)
        self.sync()
        self.vendor['qa-one.md'] = b'new vendor'
        self.sync('1.0.1')
        self.vendor = old_vendor
        self.assertEqual('installed', self.sync('1.0.0')['status'])
        self.assertEqual(b'one v1\n', self.file('qa-one.md').read_bytes())

    def test_retirement_can_resume_after_unlink_before_registry_commit(self):
        self.sync()
        del self.vendor['qa-one.md']
        original = roles.publish_role
        def crash(project, op):
            original(project, op)
            raise OSError('fixture crash after unlink')
        with patch.object(roles, 'publish_role', side_effect=crash), self.assertRaises(OSError):
            self.sync('1.0.1')
        self.assertFalse(self.file('qa-one.md').exists())
        self.assertEqual('installed', self.sync('1.0.1')['status'])
        self.assertNotIn('qa-one.md', roles.load_state(self.project)['roles'])

    def test_two_projects_keep_independent_history(self):
        self.sync()
        other = self.project.parent / 'other'
        other.mkdir()
        with project_lock(other):
            roles.sync_roles(other, self.package, '1.0.0', self.vendor, apply=True)
        self.file('qa-one.md').write_bytes(b'local')
        self.vendor['qa-one.md'] = b'one v2'
        self.assertEqual('needs_attention', self.sync('1.0.1')['status'])
        with project_lock(other):
            result = roles.sync_roles(other, self.package, '1.0.1', self.vendor, apply=True)
        self.assertEqual('installed', result['status'])
        self.assertEqual(b'one v2', (other / roles.AGENTS / 'qa-one.md').read_bytes())


if __name__ == '__main__':
    unittest.main()
