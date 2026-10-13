import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from browser_pool import BrowserPool, resources


class BrowserPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.pool = BrowserPool(self.root / 'pool.json', clock=lambda: self.at)
        self.config = {'schema_version': 1, 'resources': [
            {'server': 'playwright-a', 'mode': 'isolated'},
            {'server': 'playwright-b', 'mode': 'isolated'},
            {'server': 'playwright-c', 'mode': 'isolated'}]}
        self.pool.init(self.config)

    def evidence(self, record, name='proof.json'):
        path = self.root / name
        path.write_text(json.dumps(record))
        return path

    def stopped(self, server, previous=None):
        return self.evidence(dict(server=server, previous_lease_id=previous, prior_owner_stopped=True,
                                  connections_quiesced=True, method='synthetic owner stopped', observed_at=self.at.isoformat()), 'stopped-' + server + '.json')

    def close_proof(self, item):
        return dict(server=item['server'], lease_id=item['lease']['lease_id'], close_id=item['close_id'],
                    scope='context', success=True, profile_released=True, method='synthetic context close',
                    observed_at=self.at.isoformat(), result={'closed': True})

    def finish(self, item):
        proof = self.evidence(self.close_proof(item), item['server'] + '-closed.json')
        return self.pool.finish_close(item['server'], item['lease']['lease_id'], proof)

    def adopt(self, server='playwright-a'):
        item = self.pool.begin_recovery(server, 'SETUP', 'SETUP-' + server, 'setup-owner', self.stopped(server))
        return self.finish(item)

    def acquire(self, owner='owner-1', server=None, request='UI-1'):
        return self.pool.acquire('QA-1', request, owner, server)

    def test_initial_unknown_profiles_cannot_be_assumed_free(self):
        self.assertTrue(all(i['status'] == 'quarantined' for i in self.pool.status()['resources']))
        with self.assertRaisesRegex(ValueError, 'no free profile'):
            self.acquire()

    def test_release_and_reacquire_gets_new_lease(self):
        self.adopt()
        owned = self.acquire()
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        self.assertEqual('free', self.finish(closing)['status'])
        new = self.acquire(owner='next-owner')
        self.assertNotEqual(owned['lease']['lease_id'], new['lease']['lease_id'])
        with self.assertRaisesRegex(ValueError, 'no longer belongs'):
            self.pool.begin_close(new['server'], owned['lease']['lease_id'])

    def test_retry_acquire_does_not_reserve_second_profile(self):
        self.adopt(); self.adopt('playwright-b')
        first = self.acquire()
        self.assertEqual(first, self.acquire())
        with self.assertRaisesRegex(ValueError, 'already has a reservation'):
            self.acquire(request='UI-2')
        with self.assertRaises(ValueError):
            self.acquire(server='playwright-b')

    def test_three_owners_queue_fourth_without_stealing(self):
        for server in ('playwright-a', 'playwright-b', 'playwright-c'):
            self.adopt(server)
        owned = [self.acquire(owner='owner-' + str(i)) for i in range(3)]
        self.assertEqual(3, len({i['server'] for i in owned}))
        with self.assertRaisesRegex(ValueError, 'no free profile'):
            self.acquire(owner='owner-4')

    def test_concurrent_processes_cannot_acquire_same_profile(self):
        self.adopt()
        commands = [[sys.executable, str(ROOT/'scripts/manage_browser_pool.py'), '--pool', str(self.pool.path),
                     'acquire', '--task-id', 'QA-RACE', '--request-id', str(i), '--owner-task-id', str(i)] for i in range(6)]
        processes = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in commands]
        outputs = [p.communicate(timeout=15) for p in processes]
        self.assertEqual([0, 2, 2, 2, 2, 2], sorted(p.returncode for p in processes), outputs)

    def test_ttl_never_makes_unknown_browser_free(self):
        self.adopt()
        owned = self.acquire()
        self.at += timedelta(seconds=301)
        with self.assertRaisesRegex(ValueError, 'not available'):
            self.pool.check(owned['server'], owned['lease']['lease_id'], heartbeat=True)
        self.assertEqual('quarantined', self.pool.status()['resources'][0]['status'])
        with self.assertRaisesRegex(ValueError, 'no free profile'):
            self.acquire(owner='next-owner')

    def test_heartbeat_renews_only_live_owner(self):
        self.adopt()
        owned = self.acquire()
        self.at += timedelta(seconds=100)
        renewed = self.pool.check(owned['server'], owned['lease']['lease_id'], heartbeat=True)
        self.assertGreater(renewed['lease']['expires_at'], owned['lease']['expires_at'])
        with self.assertRaises(ValueError):
            self.pool.check(owned['server'], 'wrong', heartbeat=True)

    def test_cleanup_phase_blocks_ui_and_repeated_begin_is_idempotent(self):
        self.adopt()
        owned = self.acquire()
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        self.assertEqual(closing, self.pool.begin_close(owned['server'], owned['lease']['lease_id']))
        with self.assertRaisesRegex(ValueError, 'not available'):
            self.pool.check(owned['server'], owned['lease']['lease_id'])

    def test_failed_tab_close_wrong_lease_and_stale_proof_do_not_release(self):
        self.adopt()
        owned = self.acquire()
        self.at += timedelta(seconds=10)
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        original = self.close_proof(closing)
        changes = [{'scope': 'tab'}, {'success': False}, {'profile_released': False}, {'close_id': 'old'},
                   {'lease_id': 'wrong'}, {'server': 'playwright-b'}, {'result': {}}, {'result': {'isError': True}},
                   {'observed_at': (self.at-timedelta(seconds=1)).isoformat()},
                   {'observed_at': (self.at+timedelta(minutes=1)).isoformat()}]
        for change in changes:
            with self.subTest(change=change):
                record = dict(original, **change)
                with self.assertRaises(ValueError):
                    self.pool.finish_close(owned['server'], owned['lease']['lease_id'], self.evidence(record))
                self.assertEqual('closing', self.pool.status()['resources'][0]['status'])

    def test_release_retry_after_lost_response_needs_no_second_mcp_action(self):
        self.adopt()
        owned = self.acquire()
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        proof = self.evidence(self.close_proof(closing))
        first = self.pool.finish_close(owned['server'], owned['lease']['lease_id'], proof)
        self.assertEqual(first, self.pool.finish_close(owned['server'], owned['lease']['lease_id'], proof))
        self.acquire(owner='next-owner')
        with self.assertRaises(ValueError):
            self.pool.finish_close(owned['server'], owned['lease']['lease_id'], proof)

    def test_cleanup_timeout_remains_quarantined(self):
        self.adopt()
        owned = self.acquire()
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        self.at += timedelta(seconds=301)
        with self.assertRaises(ValueError):
            self.finish(closing)
        self.assertEqual('quarantined', self.pool.status()['resources'][0]['status'])

    def test_recovery_requires_stopped_owner_and_current_generation(self):
        self.adopt()
        owned = self.acquire()
        with self.assertRaisesRegex(ValueError, 'never steal'):
            self.pool.begin_recovery(owned['server'], 'QA-2', 'R-2', 'coordinator', self.stopped(owned['server']))
        self.at += timedelta(seconds=301)
        self.pool.status()
        stale = self.stopped(owned['server'], 'wrong-lease')
        with self.assertRaisesRegex(ValueError, 'previous owner'):
            self.pool.begin_recovery(owned['server'], 'QA-2', 'R-2', 'coordinator', stale)
        proof = self.stopped(owned['server'], owned['lease']['lease_id'])
        recovered = self.pool.begin_recovery(owned['server'], 'QA-2', 'R-2', 'coordinator', proof)
        self.assertEqual(recovered, self.pool.begin_recovery(owned['server'], 'QA-2', 'R-2', 'coordinator', proof))
        with self.assertRaises(ValueError):
            self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        self.assertEqual('free', self.finish(recovered)['status'])

    def test_expired_owner_can_only_cleanup_not_resume_ui(self):
        self.adopt()
        owned = self.acquire()
        self.at += timedelta(seconds=301)
        self.pool.status()
        closing = self.pool.begin_close(owned['server'], owned['lease']['lease_id'])
        self.assertEqual('free', self.finish(closing)['status'])

    def test_registry_cannot_be_reinitialized_or_silently_reset_when_corrupt(self):
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.pool.init(self.config)
        self.pool.path.write_text('{bad')
        with self.assertRaises(ValueError):
            self.pool.status()
        self.assertEqual('{bad', self.pool.path.read_text())

    def test_free_flag_without_cleanup_receipt_fails_closed(self):
        state = json.loads(self.pool.path.read_text())
        state['resources'][0]['status'] = 'free'
        self.pool.path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'no cleanup receipt'):
            self.pool.status()

    def test_duplicate_persistent_directory_detects_symlink_alias(self):
        directory = self.root/'profile'
        directory.mkdir()
        alias = self.root/'alias'
        alias.symlink_to(directory, target_is_directory=True)
        config = {'schema_version': 1, 'resources': [
            {'server': 'playwright-a', 'mode': 'persistent', 'user_data_dir': str(directory)},
            {'server': 'playwright-b', 'mode': 'persistent', 'user_data_dir': str(alias)}]}
        with self.assertRaisesRegex(ValueError, 'cannot share'):
            resources(config)

    def test_bad_resource_configs_fail_before_registry_mutation(self):
        for entry in ({'server': 'a', 'mode': 'external'}, {'server': 'a', 'mode': 'persistent'},
                      {'server': 'a', 'mode': 'persistent', 'user_data_dir': 'relative'},
                      {'server': 'a', 'mode': 'persistent', 'user_data_dir': '/REPLACE_WITH_ACTUAL_PATH/a'},
                      {'server': 'a', 'mode': 'isolated', 'user_data_dir': str(self.root)}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                resources({'schema_version': 1, 'resources': [entry]})
        with self.assertRaises(ValueError):
            resources({'schema_version': 1, 'resources': [self.config['resources'][0]]*2})

    def test_cli_requires_shared_absolute_path_and_returns_clear_blocker(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/manage_browser_pool.py'), '--pool', 'relative.json', 'status'], capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('must be absolute', result.stderr)
        result = subprocess.run([sys.executable, str(ROOT/'scripts/manage_browser_pool.py'), '--pool', str(self.pool.path), 'acquire',
                                 '--task-id','QA-1','--request-id','R-1','--owner-task-id','owner'], capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('no free profile', result.stdout)


if __name__ == '__main__':
    unittest.main()
