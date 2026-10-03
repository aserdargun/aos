"""Synthetic authority, process, Docker and kernel observations; no GPU runtime."""

from dataclasses import asdict
import fcntl
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos import shared_desktop_cleanup as cleanup
from aos.contracts import digest
from aos.lifecycle import LifecycleJournal, ProcessIdentity
from aos.session_binding import SessionRuntimeBinding
from aos.shared_desktop_host import SharedDesktopState, SharedServiceBinding, SharedTokenIdentity
from aos.workspace_identity import open_existing_workspace, workspace_identity


class SharedDesktopCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        (self.root / 'runs').mkdir(mode=0o700)
        self.process = ProcessIdentity(boot_id='00000000-0000-0000-0000-000000000001',
            pid=2147483646, start_ticks=100, pid_namespace=10, uid=os.getuid())
        self.service = SharedServiceBinding(invocation_id='a' * 32, process=self.process,
            control_group='/synthetic/swapp-aos-gpu-shared-desktop-default.service')
        descriptor = open_existing_workspace(self.workspace)
        try:
            identity = workspace_identity(self.workspace, descriptor)
            with patch('aos.lifecycle.process_identity', return_value=self.process):
                journal = LifecycleJournal(self.workspace, descriptor, 'desktop-' + 'b' * 32,
                    'sha256:' + 'c' * 64, 'd' * 64)
            journal.record('created', 'e' * 64)
            journal.record('started', 'e' * 64)
            journal.record('removed', 'e' * 64)
            birth = journal.birth
            self.journal = journal.path
            journal.close()
        finally:
            os.close(descriptor)
        self.state = SharedDesktopState(session='app-' + '1' * 32, url='http://127.0.0.1:8765/ui/',
            phase='uncertain', started_at='2026-10-03T00:00:00Z', plan_path=str(self.root / 'plan.json'),
            plan_sha256='1' * 64, activation_path=str(self.root / 'activation.json'), activation_sha256='2' * 64,
            provision_sha256='3' * 64, launch_intent_sha256='4' * 64, workspace=str(self.workspace),
            workspace_identity=identity, service_binding=self.service, token_name='desktop-console-' + '5' * 16 + '.token',
            token_identity=SharedTokenIdentity(device=1, inode=2, uid=os.getuid()),
            runtime_binding=SessionRuntimeBinding(session_id='desktop-session-' + '6' * 32,
                generation=1, birth=birth, container_id='e' * 64))
        self.daemon = cleanup.ReviewedDockerDaemon('SYNTHETIC-DAEMON', 1, 2)
        self.scope = Mock(return_value=None)
        self.clock = 100.0
        self.runner = Mock(side_effect=self.command)
        self.observer = cleanup.SharedDesktopCleanupObserver(self.daemon, verify_scope=self.scope,
            mount_namespace=os.stat('/proc/self/ns/mnt').st_ino,
            cgroup_namespace=os.stat('/proc/self/ns/cgroup').st_ino,
            runner=self.runner, clock=lambda: self.clock)
        self.unit = {'Id': self.service.unit, 'LoadState': 'loaded', 'ActiveState': 'inactive',
            'SubState': 'dead', 'MainPID': '0', 'InvocationID': self.service.invocation_id,
            'ControlGroup': self.service.control_group, 'Job': ''}
        self.listing = b''
        self.present_selector = None
        self.daemon_id = self.daemon.daemon_id
        self.socket = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_dev=1, st_ino=2)
        self.write_kernel('/proc/self/mountinfo', '1 0 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n')
        self.write_kernel('/sys/fs/cgroup/cgroup.procs', '')
        self.write_kernel('/sys/fs/cgroup/cgroup.controllers', 'cpu memory\n')
        self.write_kernel('/sys/fs/cgroup' + self.service.control_group + '/cgroup.procs', '')
        self.write_kernel('/sys/fs/cgroup' + self.service.control_group + '/cgroup.events', 'populated 0\n')
        real_open, real_stat = os.open, os.stat

        def synthetic_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).startswith(('/proc/', '/sys/fs/cgroup')):
                path = self.root / 'kernel' / str(path).lstrip('/')
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def synthetic_stat(path, *arguments, **options):
            if str(path) == '/var/run/docker.sock':
                return self.socket
            if str(path) == '/proc/' + str(self.process.pid):
                raise FileNotFoundError('Synthetic original process is absent')
            return real_stat(path, *arguments, **options)

        self.enterContext(patch.object(cleanup, 'REPO_ROOT', self.root))
        self.enterContext(patch.object(cleanup.os, 'open', side_effect=synthetic_open))
        self.enterContext(patch.object(cleanup.os, 'stat', side_effect=synthetic_stat))
        self.process_observer = self.enterContext(patch.object(cleanup, 'observe_process', return_value='not_observed'))

    def write_kernel(self, name, content):
        path = self.root / 'kernel' / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def command(self, arguments, **options):
        self.assertLessEqual(options['timeout'], 2)
        self.assertEqual(options['max_output'], 16384)
        if arguments[0] == '/usr/bin/systemctl':
            output = ''.join(name + '=' + value + '\n' for name, value in self.unit.items()).encode()
        elif arguments[3:] == ['info', '--format', '{{.ID}}']:
            output = (self.daemon_id + '\n').encode()
        else:
            self.assertEqual(arguments[3:7], ['container', 'ls', '--all', '--no-trunc'])
            selector = arguments[arguments.index('--filter') + 1]
            output = b'synthetic-conflict\n' if selector == self.present_selector else self.listing
        return subprocess.CompletedProcess(arguments, 0, output, b'')

    def observe(self):
        return self.observer.observe(self.state, deadline=109.0)

    def test_original_removed_resources_have_repeatable_non_authoritative_observation(self):
        original = self.state.model_dump(mode='json')
        observation = self.observe()
        self.assertEqual(observation.state_sha256, digest(original))
        self.assertEqual(self.state.model_dump(mode='json'), original)
        self.assertEqual(self.scope.call_count, 3)
        self.assertEqual(self.runner.call_count, 12)
        self.assertNotIn('cleanup_verified', asdict(observation))
        self.assertEqual(self.observe().evidence_sha256, observation.evidence_sha256)

    def test_default_scope_denies_before_any_physical_command(self):
        self.observer.verify_scope = cleanup._deny_scope
        with self.assertRaisesRegex(ValueError, 'scope are unavailable'):
            self.observe()
        self.runner.assert_not_called()

    def test_revoked_expired_or_unfenced_authority_denies_on_each_pass(self):
        for position in range(3):
            with self.subTest(position=position):
                self.scope.side_effect = [None] * position + [ValueError('Original scope revoked or spawn unfenced')]
                with self.assertRaisesRegex(ValueError, 'revoked'):
                    self.observe()

    def test_wrong_owner_generation_and_partial_target_deny(self):
        for update in ({'service_binding': None}, {'runtime_binding': None},
                       {'token_identity': None, 'token_name': None}, {'phase': 'starting'},
                       {'service_binding': self.service.model_copy(update={'process': self.process.model_copy(update={'start_ticks': 101})})}):
            state = self.state.model_copy(update=update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.observer.observe(state, deadline=109)
        self.runner.assert_not_called()

    def test_live_reused_incomparable_and_unknown_process_deny(self):
        for status in ('same_process', 'different_process', 'different_boot', 'incomparable', 'unavailable'):
            self.process_observer.return_value = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'process absence'):
                self.observe()
        self.runner.assert_not_called()

    def test_active_replaced_and_pending_job_unit_deny(self):
        original = dict(self.unit)
        for update in ({'MainPID': '123'}, {'InvocationID': 'f' * 32}, {'Job': '123/start'},
                       {'ActiveState': 'active', 'SubState': 'running'}, {'ControlGroup': '/other'}):
            self.unit = original | update
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, 'Original service'):
                self.observe()

    def test_descendant_or_recursive_population_denies(self):
        self.write_kernel('/sys/fs/cgroup' + self.service.control_group + '/child/cgroup.procs', '123\n')
        self.write_kernel('/sys/fs/cgroup' + self.service.control_group + '/child/cgroup.events', 'populated 0\n')
        with self.assertRaisesRegex(ValueError, 'contains processes'):
            self.observe()

    def test_wrong_daemon_or_socket_never_proves_container_absence(self):
        self.daemon_id = 'OTHER-DAEMON'
        with self.assertRaisesRegex(ValueError, 'daemon identity'):
            self.observe()
        self.daemon_id = self.daemon.daemon_id
        self.socket.st_ino = 3
        with self.assertRaisesRegex(ValueError, 'socket identity'):
            self.observe()

    def test_existing_container_token_and_symlink_deny(self):
        self.listing = ('e' * 64 + '\n').encode()
        with self.assertRaisesRegex(ValueError, 'container or conflicting'):
            self.observe()
        self.listing = b''
        token = self.root / 'runs' / self.state.token_name
        token.write_bytes(b'SYNTHETIC')
        with self.assertRaisesRegex(ValueError, 'token path'):
            self.observe()
        token.unlink()
        token.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'token path'):
            self.observe()

    def test_busy_workspace_and_missing_journal_deny(self):
        descriptor = open_existing_workspace(self.workspace)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.observe()
        finally:
            os.close(descriptor)
        self.journal.unlink()
        with self.assertRaises(FileNotFoundError):
            self.observe()

    def test_timeout_and_failed_command_do_not_return_observation(self):
        for deadline in (True, float('nan'), float('inf'), 99, 111):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                self.observer.observe(self.state, deadline=deadline)
        self.runner.side_effect = subprocess.TimeoutExpired('synthetic', 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.observe()
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess([], 1, b'', b'failed')
        with self.assertRaisesRegex(ValueError, 'command failed'):
            self.observe()

    def test_late_command_response_and_late_scope_response_deny(self):
        def late(*arguments, **options):
            self.clock = 110.0
            return self.command(*arguments, **options)
        self.runner.side_effect = late
        with self.assertRaisesRegex(ValueError, 'deadline expired'):
            self.observe()
        self.clock = 100.0
        self.scope.side_effect = lambda *arguments: setattr(self, 'clock', 110.0)
        with self.assertRaisesRegex(ValueError, 'deadline expired'):
            self.observe()

    def test_reappearance_after_first_sample_denies(self):
        def scope(*arguments):
            if self.scope.call_count == 2:
                self.listing = ('e' * 64 + '\n').encode()
        self.scope.side_effect = scope
        with self.assertRaisesRegex(ValueError, 'container or conflicting'):
            self.observe()

    def test_foreign_namespace_and_configuration_mutation_deny(self):
        self.observer.mount_namespace += 1
        with self.assertRaisesRegex(ValueError, 'namespace changed'):
            self.observe()
        self.observer.mount_namespace -= 1

        def mutate(*arguments):
            self.observer.cgroup_namespace += 1

        self.scope.side_effect = mutate
        with self.assertRaisesRegex(ValueError, 'environment changed'):
            self.observe()

    def test_collected_unit_requires_empty_original_cgroup_and_no_job(self):
        self.unit.update(LoadState='not-found', InvocationID='', ControlGroup='')
        self.assertIsNotNone(self.observe())
        self.write_kernel('/sys/fs/cgroup' + self.service.control_group + '/cgroup.events', 'populated 1\n')
        with self.assertRaisesRegex(ValueError, 'recursively populated'):
            self.observe()

    def test_scope_cannot_mutate_original_state_or_return_a_boolean(self):
        def mutate(state, deadline):
            state.service_binding.process.start_ticks += 1
        original = self.state.model_dump(mode='json')
        self.scope.side_effect = mutate
        with self.assertRaises(ValueError):
            self.observe()
        self.assertEqual(self.state.model_dump(mode='json'), original)
        self.scope.side_effect = None
        self.scope.return_value = True
        with self.assertRaisesRegex(ValueError, 'verify or raise'):
            self.observe()

    def test_crashed_writer_without_removed_event_still_requires_physical_absence(self):
        lines = self.journal.read_bytes().splitlines(keepends=True)
        self.journal.write_bytes(b''.join(lines[:-1]))
        self.assertIsNotNone(self.observe())
        self.listing = ('e' * 64 + '\n').encode()
        with self.assertRaisesRegex(ValueError, 'container or conflicting'):
            self.observe()

    def test_name_or_runtime_label_conflict_is_not_hidden_by_original_id_absence(self):
        birth = self.state.runtime_binding.birth
        for selector in ('name=^/' + birth.container_name + '$', 'label=com.aos.runtime=' + birth.runtime_id):
            self.present_selector = selector
            with self.subTest(selector=selector), self.assertRaisesRegex(ValueError, 'container or conflicting'):
                self.observe()

    def test_workspace_and_token_directory_replacement_deny(self):
        self.workspace.rename(self.root / 'retained-workspace')
        self.workspace.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'workspace identity changed'):
            self.observe()
        self.workspace.rmdir()
        (self.root / 'retained-workspace').rename(self.workspace)

        def replace_tokens(*arguments):
            if self.scope.call_count == 3:
                (self.root / 'runs').rename(self.root / 'retained-runs')
                (self.root / 'runs').mkdir(mode=0o700)

        self.scope.side_effect = replace_tokens
        with self.assertRaisesRegex(ValueError, 'Token directory changed'):
            self.observe()
