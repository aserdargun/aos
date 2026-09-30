import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from aos import local_app
from aos.contracts import REPO_ROOT, canonical, digest
from aos.lifecycle import LifecycleBirth, LifecycleEvent, process_identity
from aos.workspace_identity import open_existing_workspace, workspace_identity


class LocalCleanExitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / 'local-app-v1'
        base_patch = patch('aos.local_app.BASE', self.base)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        local_app.prepare_base()
        self.state = local_app.LocalAppState(
            session='app-' + uuid4().hex, mode='fixture', phase='failed',
            supervisor=process_identity(os.getpid()), backend=process_identity(os.getpid()),
            started_at='synthetic')
        self.directory = self.base / self.state.session
        self.directory.mkdir(mode=0o700)
        self.workspace = self.directory / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.database = self.directory / 'store.sqlite'
        self.database.write_bytes(b'explicitly synthetic preserved database')
        self.database.chmod(0o600)
        self.token_path = REPO_ROOT / 'runs' / ('desktop-console-' + uuid4().hex[:16] + '.token')
        self.addCleanup(self.token_path.unlink, missing_ok=True)
        self.log = self.directory / 'backend.log'
        self.log.write_text('Local token file (0600): ' + str(self.token_path) + '\n')
        self.log.chmod(0o600)
        descriptor = open_existing_workspace(self.workspace)
        try:
            identity = workspace_identity(self.workspace, descriptor)
        finally:
            os.close(descriptor)
        self.birth = LifecycleBirth(
            runtime_id='desktop-' + uuid4().hex, container_name='aos-desktop-' + uuid4().hex[:20],
            image_id='sha256:' + 'd' * 64, source_sha256='e' * 64,
            workspace=identity, process=self.state.backend)
        self.container_id = 'c' * 64
        self.journals = self.directory / '.aos-lifecycle'
        self.journals.mkdir(mode=0o700)
        self.journal = self.journals / (self.birth.runtime_id + '.jsonl')
        self.write_journal()
        local_app.write_state(self.state)
        self.audit = self.base / ('.clean-exit-' + self.state.session + '.json')

    def write_journal(self, *, birth=None, stages=('intent', 'created', 'started', 'removed')):
        events = []
        for index, stage in enumerate(stages):
            events.append(LifecycleEvent(
                birth=birth or self.birth, sequence=index, stage=stage,
                container_id=self.container_id if index else None, recorded_at='synthetic',
                previous_sha256=digest(events[-1].model_dump()) if events else None))
        self.journal.write_text(''.join(canonical(event.model_dump()) + '\n' for event in events))
        self.journal.chmod(0o600)

    def absent_container(self):
        return subprocess.CompletedProcess(
            args=['docker'], returncode=1, stdout=b'',
            stderr=('Error response from daemon: No such container: ' + self.container_id).encode())

    def recover(self, *, observation='not_observed', inspection=None, socket_error=None):
        with (patch('aos.local_app.observe_process', return_value=observation),
              patch('aos.local_app.subprocess.run', return_value=inspection or self.absent_container()) as docker,
              patch('aos.local_app.socket.socket') as listener,
              patch('aos.local_app.signal_owned') as signal,
              patch('aos.local_app.start') as start):
            listener.return_value.__enter__.return_value.bind.side_effect = socket_error
            try:
                result = local_app.recover_clean_exit(expected_session=self.state.session)
            finally:
                signal.assert_not_called()
                start.assert_not_called()
        return result, docker

    def test_exact_clean_exit_retires_state_preserving_database_and_private_audit(self):
        database = self.database.read_bytes()
        journal = self.journal.read_bytes()
        result, docker = self.recover()
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(local_app.read_state().backend, self.state.backend)
        self.assertIsNone(local_app.read_state().token_name)
        self.assertEqual(self.database.read_bytes(), database)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(docker.call_args.args[0],
                         ['docker', 'inspect', '--type', 'container', '--format', '{{.Id}}', self.container_id])
        record = json.loads(self.audit.read_text())
        self.assertEqual(record['previous_state'], self.state.model_dump())
        self.assertEqual(record['reason'], 'verified_clean_exit')
        self.assertEqual(record['container_ids'], [self.container_id])
        self.assertEqual(record['retired_token_name'], self.token_path.name)
        self.assertEqual(self.audit.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'failed session'):
            self.recover()

    def test_running_starting_missing_backend_and_opt_in_states_rejected(self):
        for change in ({'phase': 'running'}, {'phase': 'starting'}, {'phase': 'stopped'},
                       {'backend': None}, {'synthetic_staging': True}, {'synthetic_learning': True},
                       {'mode': 'real', 'remote_entry_profile_sha256': 'a' * 64,
                        'remote_entry_task_sha256': 'b' * 64}):
            with self.subTest(change=change):
                changed = self.state.model_copy(update=change)
                local_app.write_state(changed)
                with self.assertRaises(ValueError):
                    self.recover()
                self.assertEqual(local_app.read_state(), changed)
                self.assertFalse(self.audit.exists())

    def test_live_reused_incomparable_or_unknown_processes_rejected(self):
        for observation in ('same_process', 'different_process', 'different_boot', 'incomparable', 'unavailable'):
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                self.recover(observation=observation)
        self.assertEqual(local_app.read_state(), self.state)
        self.assertFalse(self.audit.exists())

    def test_previous_boot_and_changed_session_rejected(self):
        changed = self.state.model_copy(update={'backend': self.state.backend.model_copy(
            update={'boot_id': '00000000-0000-0000-0000-000000000000'})})
        local_app.write_state(changed)
        with self.assertRaisesRegex(ValueError, 'this boot'):
            self.recover()
        with self.assertRaisesRegex(ValueError, 'session changed'):
            local_app.recover_clean_exit(expected_session='app-' + 'b' * 32)
        self.assertFalse(self.audit.exists())

    def test_existing_or_broken_symlink_token_not_deleted(self):
        self.token_path.write_text('synthetic, not a real credential')
        with self.assertRaisesRegex(ValueError, 'retired token'):
            self.recover()
        self.assertTrue(self.token_path.exists())
        self.token_path.unlink()
        self.token_path.symlink_to(self.directory / 'absent')
        with self.assertRaisesRegex(ValueError, 'retired token'):
            self.recover()
        self.assertTrue(self.token_path.is_symlink())
        self.assertFalse(self.audit.exists())

    def test_token_identity_ambiguity_foreign_path_and_state_mismatch_rejected(self):
        original = self.log.read_text()
        for content in ('missing\n', original * 2,
                        'Local token file (0600): /tmp/' + self.token_path.name + '\n'):
            with self.subTest(content=content):
                self.log.write_text(content)
                with self.assertRaises(ValueError):
                    self.recover()
        self.log.write_text(original)
        local_app.write_state(self.state.model_copy(update={'token_name': 'desktop-console-' + 'f' * 16 + '.token'}))
        with self.assertRaisesRegex(ValueError, 'retired token'):
            self.recover()
        self.assertFalse(self.audit.exists())

    def test_incomplete_foreign_process_or_workspace_journal_rejected(self):
        self.write_journal(stages=('intent', 'created', 'started'))
        with self.assertRaisesRegex(ValueError, 'removed lifecycle'):
            self.recover()
        for birth in (
                self.birth.model_copy(update={'process': self.birth.process.model_copy(update={'start_ticks': 1})}),
                self.birth.model_copy(update={'workspace': self.birth.workspace.model_copy(
                    update={'inode': self.birth.workspace.inode + 1})})):
            self.write_journal(birth=birth)
            with self.assertRaisesRegex(ValueError, 'removed lifecycle'):
                self.recover()
        self.assertEqual(local_app.read_state(), self.state)
        self.assertFalse(self.audit.exists())

    def test_docker_live_permission_failure_wrong_identity_or_empty_reply_rejected(self):
        for response in (
                subprocess.CompletedProcess([], 0, stdout=self.container_id.encode(), stderr=b''),
                subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'Cannot connect to Docker daemon'),
                subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'Permission denied'),
                subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'No such container: another'),
                subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'')):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, 'absence'):
                self.recover(inspection=response)
        self.assertEqual(local_app.read_state(), self.state)
        self.assertFalse(self.audit.exists())

    def test_busy_port_and_preexisting_audit_do_not_retire_session(self):
        with self.assertRaises(OSError):
            self.recover(socket_error=OSError('Address already in use'))
        self.audit.write_text('synthetic existing audit')
        with self.assertRaises(FileExistsError):
            self.recover()
        self.assertEqual(self.audit.read_text(), 'synthetic existing audit')
        self.assertEqual(local_app.read_state(), self.state)

    def test_docker_timeout_and_unsafe_journal_preserve_failed_state(self):
        with (patch('aos.local_app.observe_process', return_value='not_observed'),
              patch('aos.local_app.subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 10)),
              self.assertRaises(subprocess.TimeoutExpired)):
            local_app.recover_clean_exit()
        self.journal.chmod(0o644)
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual(local_app.read_state(), self.state)
        self.assertFalse(self.audit.exists())

    def test_state_and_journal_drift_during_inspection_rejected(self):
        def change_state(*arguments, **keywords):
            local_app.write_state(self.state.model_copy(update={'phase': 'starting'}))
            return self.absent_container()

        def change_journal(*arguments, **keywords):
            self.write_journal(stages=('intent', 'created', 'started'))
            return self.absent_container()

        def change_log(*arguments, **keywords):
            self.log.write_text(self.log.read_text() + 'changed\n')
            return self.absent_container()

        for mutation in (change_state, change_journal, change_log):
            with self.subTest(mutation=mutation):
                local_app.write_state(self.state)
                self.write_journal()
                with (patch('aos.local_app.observe_process', return_value='not_observed'),
                      patch('aos.local_app.subprocess.run', side_effect=mutation),
                      patch('aos.local_app.socket.socket'),
                      self.assertRaisesRegex(ValueError, 'session changed')):
                    local_app.recover_clean_exit()
                self.assertFalse(self.audit.exists())

    def test_cli_routes_explicit_command_without_start_or_reboot_recovery(self):
        with (patch('sys.argv', ['aos-v1', 'recover-clean-exit']),
              patch('aos.local_app.recover_clean_exit', return_value={'phase': 'stopped'}) as recover,
              patch('aos.local_app.start') as start,
              patch('aos.local_app.recover_reboot') as reboot,
              patch('sys.stdout')):
            local_app.main()
        recover.assert_called_once_with()
        start.assert_not_called()
        reboot.assert_not_called()
