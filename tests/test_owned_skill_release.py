import tempfile
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, canonical, digest
from aos.owned_skill_release import (
    commit_owned_skill_selection, current_owned_skill_selection,
    list_owned_skill_release_families, load_owned_skill_release,
    make_owned_skill_release, persist_owned_skill_release,
    preview_owned_skill_selection, owned_skill_selection_chain)


class OwnedSkillReleaseTests(unittest.TestCase):
    def test_preview_status_exposes_exact_preview_pin_for_polling(self):
        from aos.desktop_tasks import DesktopScheduler

        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler._owned_candidate_execution = {
            'schema_version': '1.0', 'lifecycle': 'previewed', 'preview_sha256': 'a' * 64}
        self.assertEqual(scheduler._owned_candidate_execution_status()['preview_sha256'], 'a' * 64)
        scheduler._owned_candidate_execution['lifecycle'] = 'completed'
        self.assertNotIn('preview_sha256', scheduler._owned_candidate_execution_status())

    def test_first_publication_on_workspace_filesystem_sees_new_catalog_lock(self):
        with tempfile.TemporaryDirectory(prefix='release-filesystem-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            checksum, release = self.release(self.candidate('a'), 'a')
            persist_owned_skill_release(root, release, checksum)
            catalog = list_owned_skill_release_families(root)
            self.assertEqual(catalog[0]['releases'][0]['release_sha256'], checksum)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def candidate(self, suffix):
        skill = {
            'application_key': 'demo-app', 'tenant_key': 'demo-tenant',
            'account_role': 'demo-role', 'task_key': 'save-record',
            'page_draft_sha256': '1' * 64, 'skill_key': 'save_record',
        }
        return {
            'profile_sha256': '2' * 64, 'task_sha256': '3' * 64,
            'field_binding_sha256': '4' * 64, 'skill': skill,
            'recipe': {'skill_sha256': ('5' if suffix == 'a' else '6') * 64,
                       'steps': [{'step_key': 'open_entry', 'operation': suffix}]},
            'source_run_ref': '7' * 64, 'source_group_sha256': '8' * 64,
            'source_fingerprint_sha256': '9' * 64,
        }

    def release(self, candidate, suffix, parent=None):
        candidate_sha256 = digest(candidate)
        receipt = {
            'candidate_sha256': candidate_sha256,
            'source_run_ref': candidate['source_run_ref'],
            'source_invocation_sha256': 'd' * 64,
            'source_group_sha256': candidate['source_group_sha256'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'candidate_execution_sha256': suffix * 64,
        }
        return make_owned_skill_release(candidate, candidate_sha256,
                                        suffix * 64, receipt, parent)

    def test_release_revision_and_selection_rollback_are_append_only(self):
        candidate_a = self.candidate('a')
        release_a_hash, release_a = self.release(candidate_a, 'a')
        persist_owned_skill_release(self.root, release_a, release_a_hash)
        preview_a = preview_owned_skill_selection(self.root, release_a_hash, None, 'select')
        head_a = commit_owned_skill_selection(
            self.root, preview_a['selection'], preview_a['selection_sha256'], None)
        self.assertEqual(head_a['sequence'], 1)

        candidate_b = self.candidate('b')
        release_b_hash, release_b = self.release(candidate_b, 'b', release_a)
        persist_owned_skill_release(self.root, release_b, release_b_hash)
        self.assertEqual(load_owned_skill_release(self.root, release_b_hash), release_b)
        preview_b = preview_owned_skill_selection(
            self.root, release_b_hash, head_a['selection_sha256'], 'select')
        head_b = commit_owned_skill_selection(
            self.root, preview_b['selection'], preview_b['selection_sha256'],
            head_a['selection_sha256'])
        self.assertEqual(head_b['sequence'], 2)

        rollback = preview_owned_skill_selection(
            self.root, release_a_hash, head_b['selection_sha256'], 'rollback',
            'c' * 64)
        head_rollback = commit_owned_skill_selection(
            self.root, rollback['selection'], rollback['selection_sha256'],
            head_b['selection_sha256'])
        self.assertEqual(head_rollback['sequence'], 3)
        self.assertEqual(current_owned_skill_selection(
            self.root, release_a['family_sha256'])[1]['release_sha256'], release_a_hash)
        self.assertEqual(len(owned_skill_selection_chain(
            self.root, release_a['family_sha256'])), 3)
        inventory = list_owned_skill_release_families(self.root)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]['selected_release_sha256'], release_a_hash)

    def test_scheduler_previews_revision_two_selection_and_rollback(self):
        from aos.desktop_tasks import DesktopScheduler

        candidate_a = self.candidate('a')
        candidate_b = self.candidate('b')
        hash_a, release_a = self.release(candidate_a, 'a')
        receipt_b = {
            'candidate_sha256': digest(candidate_b),
            'source_run_ref': candidate_b['source_run_ref'],
            'source_invocation_sha256': 'd' * 64,
            'source_group_sha256': candidate_b['source_group_sha256'],
            'source_fingerprint_sha256': candidate_b['source_fingerprint_sha256'],
            'candidate_execution_sha256': 'b' * 64,
        }
        hash_b, release_b = make_owned_skill_release(
            candidate_b, digest(candidate_b), 'b' * 64, receipt_b,
            parent_release=release_a)
        persist_owned_skill_release(self.root, release_a, hash_a)
        selected_a = preview_owned_skill_selection(self.root, hash_a, None, 'select')
        commit_owned_skill_selection(self.root, selected_a['selection'],
                                     selected_a['selection_sha256'], None)

        class Session:
            directory = self.root

            def reinspect(self, _run_ref, _invocation_hash, candidate_hash):
                return ((candidate_a if candidate_hash == digest(candidate_a)
                         else candidate_b), {})

        receipts = {
            'a' * 64: {
                'candidate_sha256': digest(candidate_a),
                'source_run_ref': candidate_a['source_run_ref'],
                'source_invocation_sha256': 'd' * 64,
                'source_group_sha256': candidate_a['source_group_sha256'],
                'source_fingerprint_sha256': candidate_a['source_fingerprint_sha256'],
                'candidate_execution_sha256': 'a' * 64,
            },
            'b' * 64: receipt_b,
        }
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.owned_skill_planning = None
        scheduler.task = None
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.job_id = 'job-test'
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: None)))
        scheduler.remote_form_owned_candidate_session = Session()
        scheduler.inspect_owned_candidate_review = lambda checksum: {
            'status': 'accepted', 'receipt': receipts[checksum]}

        preview_release_b = scheduler.preview_owned_skill_release('b' * 64, hash_a)
        self.assertEqual(preview_release_b['release']['revision'], 2)
        self.assertEqual(preview_release_b['release']['parent_release_sha256'], hash_a)
        self.assertEqual(scheduler.publish_owned_skill_release(
            'b' * 64, hash_a, preview_release_b['release_sha256'])['status'], 'published')
        preview_b = scheduler.preview_owned_skill_selection(
            hash_b, selected_a['selection_sha256'], 'select')
        selected_b = scheduler.commit_owned_skill_selection(
            hash_b, selected_a['selection_sha256'], 'select',
            preview_b['selection_sha256'])
        scheduler._owned_release_rollback_evidence = lambda _session, _release: 'c' * 64
        rollback_a = scheduler.preview_owned_skill_selection(
            hash_a, selected_b['selection_sha256'], 'rollback')
        self.assertEqual(rollback_a['selection']['operation'], 'rollback')
        self.assertEqual(rollback_a['selection']['rollback_evidence_execution_sha256'],
                         'c' * 64)

    def test_selected_start_allows_rollback_release_before_ownership_check(self):
        from aos.contracts import AOSFault
        from aos.desktop_tasks import DesktopScheduler

        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.owned_skill_planning = None
        scheduler.task = None
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.job_id = 'job-test'
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_args: SimpleNamespace(fetchone=lambda: None)))
        scheduler._owned_form_lifecycle = 'audited'
        scheduler._owned_candidate_execution_consumed = set()
        scheduler._owned_selected_candidate_execution_consumed = set()
        scheduler._owned_candidate_execution_session_starts = 0
        scheduler._owned_selected_candidate_execution_session_starts = 0
        scheduler._owned_skill_reuse = None
        scheduler._owned_skill_reuse_admission_sha256 = None
        scheduler._restore_owned_candidate_replay_inventory = lambda *_args: None
        scheduler.prepare_owned_form_candidate_execution = lambda *_args: {
            'session': SimpleNamespace(directory=self.root)}
        scheduler._assert_candidate_review_for_execution = lambda *_args: None
        scheduler._assert_owned_release_selection = lambda *_args: (
            {'revision': 1, 'family_sha256': 'f' * 64},
            {'release_sha256': 'e' * 64})
        scheduler.controller = SimpleNamespace(state=lambda: {
            'owner': 'HUMAN', 'status': 'running', 'lease_id': 'lease-exact',
            'generation': 4})
        scheduler.closed = False
        scheduler._owned_candidate_execution_preview_payload = lambda *_args, **_kwargs: {
            'preview_sha256': 'd' * 64}
        with self.assertRaises(AOSFault):
            scheduler.start_owned_form_candidate_execution(
                candidate_sha256='a' * 64, source_run_ref='b' * 64,
                invocation_sha256='c' * 64, case_key='dev-alpha',
                development_value='alpha', preview_sha256='d' * 64,
                confirm_sha256='d' * 64, lease_id='lease-exact', generation=4,
                review_sha256='9' * 64, release_sha256='e' * 64,
                selection_sha256='8' * 64)

    def test_selection_cas_rejects_stale_head_and_orphaned_events(self):
        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        preview = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        commit_owned_skill_selection(self.root, preview['selection'],
                                      preview['selection_sha256'], None)
        with self.assertRaisesRegex(ValueError, 'head_stale'):
            preview_owned_skill_selection(self.root, release_hash, None, 'select')
        selections = self.root / 'owned-skill-release-catalog' / release['family_sha256'] / 'selections'
        orphan = selections / ('f' * 64)
        orphan.mkdir(mode=0o700)
        (orphan / 'selection.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'history_invalid'):
            current_owned_skill_selection(self.root, release['family_sha256'])

    def test_release_hash_and_parent_chain_are_revalidated(self):
        candidate_a = self.candidate('a')
        release_a_hash, release_a = self.release(candidate_a, 'a')
        persist_owned_skill_release(self.root, release_a, release_a_hash)
        candidate_b = self.candidate('b')
        release_b_hash, release_b = self.release(candidate_b, 'b', release_a)
        persist_owned_skill_release(self.root, release_b, release_b_hash)
        release_dir = (self.root / 'owned-skill-release-catalog'
                       / release_a['family_sha256'] / 'releases' / release_a_hash)
        release_path = release_dir / 'release.json'
        release_path.write_text('{}')
        with self.assertRaises(ValueError):
            load_owned_skill_release(self.root, release_b_hash)

    def test_release_admission_requires_unique_pinned_pre_action_observation(self):
        from aos.desktop_tasks import DesktopScheduler

        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        selected = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        commit_owned_skill_selection(self.root, selected['selection'],
                                     selected['selection_sha256'], None)
        run_id = 'run-owned-release'
        execution = {
            'release_sha256': release_hash,
            'selection_sha256': selected['selection_sha256'],
            'family_sha256': release['family_sha256'],
            'review_sha256': release['review_sha256'],
            'candidate_execution_sha256': 'a' * 64,
            'candidate_sha256': release['candidate_sha256'],
            'invocation_sha256': 'b' * 64,
            'run_id': run_id,
        }
        scheduler = DesktopScheduler.__new__(DesktopScheduler)
        scheduler.remote_form_owned_candidate_session = SimpleNamespace(directory=self.root)
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript('''
            CREATE TABLE observations (run_id TEXT, kind TEXT, action_id TEXT,
                step_id TEXT, payload_json TEXT, created_at TEXT);
            CREATE TABLE state_snapshots (run_id TEXT, state_version INTEGER,
                step_id TEXT, state_json TEXT, created_at TEXT);
            CREATE TABLE actions (run_id TEXT, created_at TEXT);
        ''')
        state0 = {'phase': 'CREATED', 'state_version': 0, 'owner': 'AGENT',
                  'task_kind': 'browser_remote_form', 'run_id': run_id,
                  'skill_invocation_sha256': execution['invocation_sha256'],
                  'task_id': 'task-1', 'step_id': 'step-1', 'runtime_id': 'runtime-1',
                  'deployment_id': 'deployment-1', 'owner_lease_id': 'lease-1'}
        state1 = {**state0, 'phase': 'OBSERVING', 'state_version': 1}
        expected = {
            'schema_version': '1.0', 'synthetic': True,
            'candidate_execution_sha256': execution['candidate_execution_sha256'],
            'candidate_sha256': execution['candidate_sha256'],
            'invocation_sha256': execution['invocation_sha256'],
            'review_sha256': execution['review_sha256'],
            'release_sha256': release_hash,
            'selection_sha256': selected['selection_sha256'],
            'family_sha256': release['family_sha256'],
            'selection_admission_verified': True,
            'activation_authorized': False, 'training_ready': False,
            'independent_held_out': False,
        }

        def set_rows(payload=expected, observation_times=('2026-01-01T00:00:01+00:00',),
                     action_time='2026-01-01T00:00:03+00:00'):
            connection.execute('DELETE FROM observations')
            connection.execute('DELETE FROM state_snapshots')
            connection.execute('DELETE FROM actions')
            connection.executemany(
                'INSERT INTO state_snapshots VALUES (?,?,?,?,?)',
                [(run_id, 0, 'step-1', canonical(state0), '2026-01-01T00:00:00+00:00'),
                 (run_id, 1, 'step-1', canonical(state1), '2026-01-01T00:00:02+00:00')])
            for when in observation_times:
                connection.execute('INSERT INTO observations VALUES (?,?,?,?,?,?)',
                    (run_id, 'skill.owned_release_selection_admission', None,
                     'step-1', canonical(payload), when))
            connection.execute('INSERT INTO actions VALUES (?,?)', (run_id, action_time))

        set_rows()
        self.assertTrue(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        set_rows(observation_times=())
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        set_rows(observation_times=('2026-01-01T00:00:01+00:00',) * 2)
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        wrong_pin = {**expected, 'selection_sha256': 'f' * 64}
        set_rows(payload=wrong_pin)
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        set_rows(observation_times=('2026-01-01T00:00:03+00:00',))
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        set_rows(action_time='2026-01-01T00:00:00.500000+00:00')
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        wrong_family = {**expected, 'family_sha256': 'f' * 64}
        set_rows(payload=wrong_family)
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        set_rows()
        connection.execute('DELETE FROM state_snapshots WHERE state_version=0')
        self.assertFalse(scheduler._candidate_release_admission_verified(
            execution, snapshot=connection))
        connection.close()

    def test_direct_selection_rejects_rewound_valid_head(self):
        candidate_a = self.candidate('a')
        release_a_hash, release_a = self.release(candidate_a, 'a')
        persist_owned_skill_release(self.root, release_a, release_a_hash)
        first = preview_owned_skill_selection(self.root, release_a_hash, None, 'select')
        commit_owned_skill_selection(self.root, first['selection'],
                                     first['selection_sha256'], None)
        candidate_b = self.candidate('b')
        release_b_hash, release_b = self.release(candidate_b, 'b', release_a)
        persist_owned_skill_release(self.root, release_b, release_b_hash)
        second = preview_owned_skill_selection(
            self.root, release_b_hash, first['selection_sha256'], 'select')
        commit_owned_skill_selection(self.root, second['selection'],
                                     second['selection_sha256'],
                                     first['selection_sha256'])
        family_dir = (self.root / 'owned-skill-release-catalog'
                      / release_a['family_sha256'])
        head_path = family_dir / 'head.json'
        head_path.write_text(canonical({
            'schema_version': '1.0', 'family_sha256': release_a['family_sha256'],
            'sequence': 1, 'selection_sha256': first['selection_sha256']}))
        with self.assertRaisesRegex(ValueError, 'history_invalid'):
            preview_owned_skill_selection(
                self.root, release_b_hash, first['selection_sha256'], 'select')

    def test_missing_catalog_lock_is_not_silently_recreated(self):
        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        lock = (self.root / 'owned-skill-release-catalog'
                / release['family_sha256'] / 'catalog.lock')
        lock.unlink()
        with self.assertRaises(ValueError):
            current_owned_skill_selection(self.root, release['family_sha256'])
        self.assertFalse(lock.exists())

    def test_invalid_release_inventory_does_not_change_selection_head(self):
        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        selected = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        commit_owned_skill_selection(self.root, selected['selection'],
                                     selected['selection_sha256'], None)
        family_dir = (self.root / 'owned-skill-release-catalog'
                      / release['family_sha256'])
        head_before = (family_dir / 'head.json').read_bytes()
        (family_dir / 'releases' / 'unknown').write_text('unexpected')
        with self.assertRaises(ValueError):
            preview_owned_skill_selection(self.root, release_hash,
                                          selected['selection_sha256'], 'select')
        self.assertEqual((family_dir / 'head.json').read_bytes(), head_before)

    def test_failed_head_publication_cleans_only_unpublished_event_and_can_retry(self):
        import aos.owned_skill_release as release_module

        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        selected = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        with patch.object(release_module, '_atomic_head_write',
                          side_effect=OSError('injected before replace')):
            with self.assertRaisesRegex(OSError, 'injected before replace'):
                commit_owned_skill_selection(self.root, selected['selection'],
                                             selected['selection_sha256'], None)
        self.assertEqual(current_owned_skill_selection(
            self.root, release['family_sha256']), (None, None))
        selections = (self.root / 'owned-skill-release-catalog'
                      / release['family_sha256'] / 'selections')
        self.assertEqual(list(selections.iterdir()), [])
        commit_owned_skill_selection(self.root, selected['selection'],
                                     selected['selection_sha256'], None)

    def test_head_fsync_failure_keeps_referenced_selection_event(self):
        import aos.owned_skill_release as release_module

        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        selected = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        original = release_module._atomic_head_write

        def publish_then_fail(family_fd, head):
            original(family_fd, head)
            raise OSError('injected after replace')

        with patch.object(release_module, '_atomic_head_write', publish_then_fail):
            with self.assertRaisesRegex(OSError, 'injected after replace'):
                commit_owned_skill_selection(self.root, selected['selection'],
                                             selected['selection_sha256'], None)
        head, event = current_owned_skill_selection(self.root, release['family_sha256'])
        self.assertEqual(head['selection_sha256'], selected['selection_sha256'])
        self.assertEqual(event, selected['selection'])

    def test_new_private_child_fsyncs_its_directory_entry(self):
        import aos.owned_skill_release as release_module

        child = self.root / 'private-child'
        child.mkdir(mode=0o700)
        child_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY)
        fsync_calls = []
        original_fsync = os.fsync

        def record_fsync(descriptor):
            fsync_calls.append(descriptor)
            return original_fsync(descriptor)

        try:
            with patch.object(release_module.os, 'fsync', record_fsync):
                release_module._write_new_private_child(child_fd, 'event.json', b'{"ok":true}')
            self.assertIn(child_fd, fsync_calls)
        finally:
            os.close(child_fd)

    def test_event_write_and_parent_fsync_failures_cleanup_unpublished_event(self):
        import aos.owned_skill_release as release_module

        original_fsync = os.fsync
        for mode in ('event-write', 'selections-fsync'):
            with self.subTest(mode=mode):
                root = self.root / mode
                root.mkdir(mode=0o700)
                candidate = self.candidate('a')
                release_hash, release = self.release(candidate, 'a')
                persist_owned_skill_release(root, release, release_hash)
                preview = preview_owned_skill_selection(root, release_hash, None, 'select')
                if mode == 'event-write':
                    patcher = patch.object(
                        release_module, '_write_new_private_child',
                        side_effect=OSError('injected event write failure'))
                else:
                    raised = []

                    def fail_selections_fsync(descriptor):
                        info = os.fstat(descriptor)
                        path = os.readlink(f'/proc/self/fd/{descriptor}')
                        if (not raised and os.path.isdir(path)
                                and Path(path).name == 'selections'):
                            raised.append(True)
                            raise OSError('injected selections fsync failure')
                        return original_fsync(descriptor)

                    patcher = patch.object(release_module.os, 'fsync',
                                           fail_selections_fsync)
                with patcher:
                    with self.assertRaisesRegex(OSError, 'injected'):
                        commit_owned_skill_selection(
                            root, preview['selection'], preview['selection_sha256'], None)
                self.assertEqual(current_owned_skill_selection(
                    root, release['family_sha256']), (None, None))
                selections = (root / 'owned-skill-release-catalog'
                              / release['family_sha256'] / 'selections')
                self.assertEqual(list(selections.iterdir()), [])
                commit_owned_skill_selection(
                    root, preview['selection'], preview['selection_sha256'], None)

    def test_competing_previews_cannot_both_commit(self):
        candidate = self.candidate('a')
        release_hash, release = self.release(candidate, 'a')
        persist_owned_skill_release(self.root, release, release_hash)
        first = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        second = preview_owned_skill_selection(self.root, release_hash, None, 'select')
        self.assertEqual(first['selection_sha256'], second['selection_sha256'])
        commit_owned_skill_selection(self.root, first['selection'],
                                     first['selection_sha256'], None)
        with self.assertRaisesRegex(ValueError, 'head_stale'):
            commit_owned_skill_selection(self.root, second['selection'],
                                         second['selection_sha256'], None)
        self.assertEqual(current_owned_skill_selection(
            self.root, release['family_sha256'])[0]['selection_sha256'],
            first['selection_sha256'])


if __name__ == '__main__':
    unittest.main()
