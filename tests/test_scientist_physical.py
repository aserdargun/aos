"""Synthetic CPU filesystem/command observations, never native GPU acceptance."""

from copy import deepcopy
from pathlib import Path
import os
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos import scientist_release_proof as physical
from aos.scientist_terminal import ScientistTerminalChild
from aos.scientist_transport import ScientistAdmissionError


class ScientistPhysicalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.boot = '00000000-0000-0000-0000-000000000001'
        self.gpu_uuid = 'GPU-00000000-0000-0000-0000-000000000001'
        self.unit = 'swapp-aos-gpu-turn-' + '1' * 32 + '.service'
        self.group = '/user.slice/swapp-gpu.slice/' + self.unit
        self.child = ScientistTerminalChild(unit=self.unit, invocation_id='2' * 32, pid=12345,
            start_ticks=99, boot_id=self.boot, control_group=self.group)
        self.terminal = SimpleNamespace(child_generation=self.child, recorded_boot_id=self.boot,
                                        original_principal=SimpleNamespace(uid=os.getuid()))
        self.original = {'request_id': '3' * 32, 'source': 'synthetic'}
        self.allocation = {'lease': {'fencing_token': 42}}
        self.drain = {'kind': 'physical_drain', 'never_started': False, 'late_start_fenced': True,
            'child_generation': self.child.model_dump(mode='json'),
            'observed_gpu_pids': [12345, 23456],
            'child_intent': {'unit': self.unit, 'nonce': '4' * 64}}
        self.source = Mock(side_effect=self.source_gate)
        self.current = Mock(return_value=None)
        self.provider = physical.ScientistPhysicalReleaseVerifier(gpu_uuid=self.gpu_uuid,
            verify_source=self.source, verify_current=self.current)
        self.write('/proc/sys/kernel/random/boot_id', self.boot + '\n')
        self.write('/proc/self/mountinfo', '1 0 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n')
        self.write('/sys/fs/cgroup/cgroup.procs', '')
        self.write('/sys/fs/cgroup/cgroup.controllers', 'cpu memory pids\n')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.procs', '')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.events', 'populated 0\nfrozen 0\n')
        real_open = os.open

        def synthetic_open(path, flags, mode=0o777, *, dir_fd=None):
            if str(path).startswith(('/proc/', '/sys/fs/cgroup')):
                path = self.root / str(path).lstrip('/')
            return real_open(path, flags, mode, dir_fd=dir_fd)

        self.opened = self.enterContext(patch.object(physical.os, 'open', side_effect=synthetic_open))
        self.enterContext(patch.object(self.provider, '_environment', return_value={'PATH': '/usr/bin:/bin'}))
        self.commands = self.enterContext(patch.object(physical, 'run_bounded', side_effect=self.command))
        self.unit_output = self.properties()
        self.process_output = b''
        self.memory_output = b'750, 16376\n'

    def write(self, path, value):
        destination = self.root / path.lstrip('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value, encoding='ascii')

    def properties(self, **updates):
        values = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0',
                  'InvocationID': self.child.invocation_id, 'ControlGroup': self.group}
        return ''.join(key + '=' + value + '\n' for key, value in (values | updates).items()).encode()

    def source_gate(self, original, terminal, allocation, drain, no_admission, gpu_uuid):
        if (original != self.original or allocation is None or allocation['lease']['fencing_token'] != 42
                or drain['child_intent']['nonce'] != '4' * 64 or gpu_uuid != self.gpu_uuid
                or drain['observed_gpu_pids'] != [12345, 23456]):
            raise ScientistAdmissionError('Synthetic retained source binding differs')

    def command(self, arguments, **options):
        self.assertEqual(options['input'], b'')
        self.assertLessEqual(options['timeout'], 2)
        self.assertEqual(options['max_output'], 16384)
        if arguments[0] == '/usr/bin/systemctl':
            self.assertEqual(arguments, ['/usr/bin/systemctl', '--user', 'show', self.unit,
                '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'])
            output = self.unit_output
        else:
            self.assertEqual(arguments[0:2], ['/usr/bin/nvidia-smi', '--id=' + self.gpu_uuid])
            self.assertEqual(arguments[-1], '--format=csv,noheader,nounits')
            if arguments[2] == '--query-compute-apps=pid,process_name,used_memory':
                output = self.process_output
            else:
                self.assertEqual(arguments[2], '--query-gpu=memory.used,memory.total')
                output = self.memory_output
        return subprocess.CompletedProcess(arguments, 0, output, b'')

    def verify(self, **changes):
        values = {'original': self.original, 'terminal': self.terminal, 'allocation': self.allocation,
                  'drain': self.drain, 'no_admission': None}
        return self.provider(**(values | changes))

    def enable_shared_lanes(self):
        self.provider = physical.ScientistPhysicalReleaseVerifier(gpu_uuid=self.gpu_uuid,
            verify_source=self.source, verify_current=self.current, allow_shared_lanes=True)
        self.enterContext(patch.object(self.provider, '_environment', return_value={'PATH': '/usr/bin:/bin'}))

    def test_exact_inactive_generation_and_empty_compute_list_succeed_with_display_memory(self):
        self.assertIsNone(self.verify())
        self.assertEqual(self.commands.call_count, 6)
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(self.current.call_count, 2)

    def test_shared_lanes_require_explicit_boolean_and_default_stays_quiet(self):
        for setting in (None, 'true', 1, 0, [], {}):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                physical.ScientistPhysicalReleaseVerifier(gpu_uuid=self.gpu_uuid, allow_shared_lanes=setting)
        self.process_output = b'999, synthetic-other-lane, 2048\n'
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.enable_shared_lanes()
        self.assertIsNone(self.verify())

    def test_source_callback_cannot_flip_public_device_or_lane_policy(self):
        self.process_output = b'999, synthetic-other-lane, 2048\n'
        for attribute, value in (('allow_shared_lanes', True),
                                 ('gpu_uuid', 'GPU-00000000-0000-0000-0000-000000000002')):
            def flip(*arguments):
                setattr(self.provider, attribute, value)
            self.source.side_effect = flip
            with self.subTest(attribute=attribute), self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.commands.assert_not_called()
        self.opened.assert_not_called()
        self.assertFalse(self.provider.allow_shared_lanes)
        self.assertEqual(self.provider.gpu_uuid, self.gpu_uuid)

    def test_source_callback_private_configuration_changes_deny_before_os(self):
        for attribute, value, original in (('_allow_shared_lanes', True, False),
                                           ('_gpu_uuid', 'GPU-00000000-0000-0000-0000-000000000002', self.gpu_uuid)):
            def flip(*arguments):
                setattr(self.provider, attribute, value)
            self.source.side_effect = flip
            with self.subTest(attribute=attribute), self.assertRaises(ScientistAdmissionError):
                self.verify()
            setattr(self.provider, attribute, original)
        self.commands.assert_not_called()
        self.opened.assert_not_called()

    def test_mid_observation_device_change_never_mixes_gpu_queries_and_denies(self):
        def flip_during_query(arguments, **options):
            result = self.command(arguments, **options)
            if arguments[0] == '/usr/bin/nvidia-smi':
                self.provider._gpu_uuid = 'GPU-00000000-0000-0000-0000-000000000002'
            return result
        self.commands.side_effect = flip_during_query
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.assertEqual(self.commands.call_count, 6)
        self.assertEqual(self.source.call_count, 1)

    def test_mid_observation_private_mode_change_cannot_relax_quiet_policy(self):
        def flip_during_query(arguments, **options):
            result = self.command(arguments, **options)
            if arguments[0] == '/usr/bin/nvidia-smi':
                self.provider._allow_shared_lanes = True
            return result
        self.process_output = b'999, synthetic-other-lane, 2048\n'
        self.commands.side_effect = flip_during_query
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.assertEqual(self.commands.call_count, 2)

    def test_shared_foreign_compute_contexts_succeed_without_mutation_commands(self):
        self.enable_shared_lanes()
        self.process_output = b'999, "synthetic, other lane", 2048\n1000, /usr/bin/kwin_wayland, 10\n'
        self.assertIsNone(self.verify())
        self.assertEqual(self.commands.call_count, 6)
        self.assertEqual(self.source.call_count, 2)

    def test_shared_original_child_descendant_and_reused_gpu_pid_deny(self):
        self.enable_shared_lanes()
        for pid in (12345, 23456):
            self.process_output = f'999, synthetic-other-lane, 20\n{pid}, synthetic-reused-or-owned, 2\n'.encode()
            with self.subTest(pid=pid), self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.source.side_effect = None
        self.source.return_value = None
        self.process_output = b'12345, synthetic-original-child, 2\n'
        with self.assertRaises(ScientistAdmissionError):
            self.verify(drain=self.drain | {'observed_gpu_pids': []})

    def test_retained_gpu_pid_list_provenance_is_required_before_queries(self):
        self.enable_shared_lanes()
        with self.assertRaises(ScientistAdmissionError):
            self.verify(drain=self.drain | {'observed_gpu_pids': []})
        self.commands.assert_not_called()
        self.opened.assert_not_called()

    def test_owned_pid_list_requires_bounded_unique_integer_array(self):
        self.source.side_effect = None
        self.source.return_value = None
        for pids in (None, (), '12345', [True], [1.0], ['1'], [0], [-1], [2**31],
                     [12345, 12345], list(range(1, 4098))):
            with self.subTest(pids=str(pids)[:50]), self.assertRaises(ScientistAdmissionError):
                self.verify(drain=self.drain | {'observed_gpu_pids': pids})
        self.commands.assert_not_called()
        self.opened.assert_not_called()

    def test_shared_malformed_and_duplicate_foreign_gpu_rows_deny(self):
        self.enable_shared_lanes()
        for output in (b'999, foreign, 2\n999, foreign, 2\n', b'0, foreign, 2\n',
                       b'2147483648, foreign, 2\n', b'1.0, foreign, 2\n', b'True, foreign, 2\n',
                       b'999, , 2\n', b'999, foreign, N/A\n', b'999, foreign, -1\n',
                       b'999, foreign, 1.0\n', b'999, foreign, 9007199254740992\n',
                       b'999, foreign, 2, extra\n', b'999, "unterminated, 2\n',
                       b'999, fore\x00ign, 2\n', b'999, foreign, 2\n\n'):
            self.process_output = output
            with self.subTest(output=output), self.assertRaises(ScientistAdmissionError):
                self.verify()

    def test_second_gpu_sample_denies_owned_reappearance_and_query_failure(self):
        self.enable_shared_lanes()
        for outcome in ('owned', 'query_error'):
            observations = 0

            def change_gpu(arguments, **options):
                nonlocal observations
                if '--query-compute-apps=pid,process_name,used_memory' in arguments:
                    observations += 1
                    if observations == 2:
                        if outcome == 'query_error':
                            return subprocess.CompletedProcess(arguments, 1, b'', b'')
                        self.process_output = b'23456, synthetic-reappeared-descendant, 2\n'
                return self.command(arguments, **options)

            self.process_output = b'999, synthetic-other-lane, 20\n'
            self.commands.side_effect = change_gpu
            with self.subTest(outcome=outcome), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assertEqual(observations, 2)

    def test_default_denied_and_each_missing_gate_deny_before_os_reads(self):
        for options in ({}, {'verify_source': self.source}, {'verify_current': self.current}):
            provider = physical.ScientistPhysicalReleaseVerifier(gpu_uuid=self.gpu_uuid, **options)
            with self.subTest(options=options), self.assertRaises(ScientistAdmissionError):
                provider(self.original, self.terminal, self.allocation, self.drain, None)
        self.opened.assert_not_called()
        self.commands.assert_not_called()

    def test_original_source_fence_nonce_and_gpu_binding_are_checked_before_reads(self):
        for field in ('source', 'fence', 'nonce'):
            original, allocation, drain = deepcopy((self.original, self.allocation, self.drain))
            if field == 'source':
                original['source'] = 'foreign'
            elif field == 'fence':
                allocation['lease']['fencing_token'] += 1
            else:
                drain['child_intent']['nonce'] = '5' * 64
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(original=original, allocation=allocation, drain=drain)
        self.opened.assert_not_called()
        self.commands.assert_not_called()

    def test_source_and_current_revocation_during_observation_deny(self):
        for gate in (self.source, self.current):
            gate.side_effect = [None, ScientistAdmissionError('Synthetic authority revoked')]
            with self.subTest(gate=gate), self.assertRaises(ScientistAdmissionError):
                self.verify()
            gate.side_effect = None
            gate.return_value = None

    def test_non_none_authority_acknowledgement_is_not_authority(self):
        self.source.side_effect = None
        self.source.return_value = True
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.opened.assert_not_called()
        self.commands.assert_not_called()

    def test_child_boot_unit_invocation_cgroup_and_activity_changes_deny(self):
        for updates in ({'InvocationID': 'f' * 32}, {'ControlGroup': self.group + '-other'},
                        {'ActiveState': 'active'}, {'MainPID': '12345'}, {'LoadState': 'error'}):
            self.unit_output = self.properties(**updates)
            with self.subTest(updates=updates), self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.unit_output = self.properties()
        self.write('/proc/sys/kernel/random/boot_id', '00000000-0000-0000-0000-000000000002\n')
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_live_child_and_reused_pid_deny(self):
        for ticks in (99, 100):
            self.write('/proc/12345/stat', '12345 (synthetic child) ' + ' '.join(['S'] + ['0'] * 18 + [str(ticks)]))
            with self.subTest(ticks=ticks), self.assertRaises(ScientistAdmissionError):
                self.verify()

    def test_recursive_child_process_and_populated_flag_deny(self):
        self.write('/sys/fs/cgroup' + self.group + '/descendant/cgroup.procs', '333\n')
        self.write('/sys/fs/cgroup' + self.group + '/descendant/cgroup.events', 'populated 0\n')
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.write('/sys/fs/cgroup' + self.group + '/descendant/cgroup.procs', '')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.events', 'populated 1\n')
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_cgroup_symlink_and_missing_retained_cgroup_deny(self):
        nested = self.root / ('sys/fs/cgroup' + self.group) / 'untrusted'
        nested.symlink_to(self.root)
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        nested.unlink()
        (self.root / ('sys/fs/cgroup' + self.group) / 'cgroup.procs').unlink()
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_collected_exact_unit_and_missing_cgroup_succeed_only_with_readable_cgroup2(self):
        self.unit_output = self.properties(LoadState='not-found', InvocationID='', ControlGroup='')
        group = self.root / ('sys/fs/cgroup' + self.group)
        (group / 'cgroup.procs').unlink()
        (group / 'cgroup.events').unlink()
        group.rmdir()
        self.assertIsNone(self.verify())
        for mount in ('', '1 0 0:28 / /sys/fs/cgroup rw - tmpfs tmpfs rw\n'):
            self.write('/proc/self/mountinfo', mount)
            with self.subTest(mount=mount), self.assertRaises(ScientistAdmissionError):
                self.verify()

    def test_collected_unit_does_not_hide_occupied_existing_cgroup(self):
        self.unit_output = self.properties(LoadState='not-found', InvocationID='', ControlGroup='')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.procs', '999\n')
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_any_compute_context_and_malformed_gpu_response_deny(self):
        for output in (b'12345, synthetic-model, 2048\n', b'999, /usr/bin/kwin_wayland, 10\n',
                       b'N/A\n', b'No devices were found\n'):
            self.process_output = output
            with self.subTest(output=output), self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.process_output = b''
        for output in (b'', b'N/A, N/A\n', b'0, 10\n0, 20\n', b'20, 10\n'):
            self.memory_output = output
            with self.subTest(output=output), self.assertRaises(ScientistAdmissionError):
                self.verify()

    def test_query_error_timeout_permission_and_output_overflow_deny(self):
        for error in (PermissionError(), subprocess.TimeoutExpired('synthetic', 2),
                      ValueError('process_output_limit')):
            self.commands.side_effect = error
            with self.subTest(error=error), self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.commands.side_effect = None
        for result in (subprocess.CompletedProcess([], 1, b'', b''),
                       subprocess.CompletedProcess([], 0, self.properties(), b'warning')):
            self.commands.return_value = result
            with self.subTest(result=result), self.assertRaises(ScientistAdmissionError):
                self.verify()

    def test_no_admission_and_never_started_are_not_inferred_from_idle_hardware(self):
        self.source.side_effect = None
        self.source.return_value = None
        for changes in ({'allocation': None, 'drain': None, 'no_admission': {}},
                        {'drain': self.drain | {'never_started': True}}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verify(**changes)
        self.opened.assert_not_called()
        self.commands.assert_not_called()

    def test_second_observation_rechecks_unit_and_cgroup(self):
        original_command = self.command

        def change_unit(arguments, **options):
            result = original_command(arguments, **options)
            if arguments[0] == '/usr/bin/nvidia-smi':
                self.unit_output = self.properties(InvocationID='f' * 32)
            return result

        self.commands.side_effect = change_unit
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.unit_output = self.properties()

        def populate(arguments, **options):
            result = original_command(arguments, **options)
            if arguments[0] == '/usr/bin/nvidia-smi':
                self.write('/sys/fs/cgroup' + self.group + '/cgroup.procs', '999\n')
            return result

        self.commands.side_effect = populate
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_gpu_identity_must_be_explicit_uuid(self):
        for identity in ('0', '', self.gpu_uuid + ',1', '--help'):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                physical.ScientistPhysicalReleaseVerifier(gpu_uuid=identity)

    def test_foreign_user_cannot_treat_hidden_proc_pid_as_absence(self):
        self.terminal.original_principal.uid += 1
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.opened.assert_not_called()
        self.commands.assert_not_called()

    def test_user_bus_requires_private_directory_and_owned_socket(self):
        uid = os.getuid()
        runtime = '/run/user/' + str(uid)
        environment = {'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + runtime + '/bus'}
        with patch.dict(os.environ, environment, clear=True):
            for directory_mode, bus_mode, bus_uid in (
                    (stat.S_IFDIR | 0o700, stat.S_IFSOCK | 0o600, uid),
                    (stat.S_IFDIR | 0o755, stat.S_IFSOCK | 0o600, uid),
                    (stat.S_IFLNK | 0o700, stat.S_IFSOCK | 0o600, uid),
                    (stat.S_IFDIR | 0o700, stat.S_IFLNK | 0o600, uid),
                    (stat.S_IFDIR | 0o700, stat.S_IFSOCK | 0o600, uid + 1)):
                info = [SimpleNamespace(st_mode=directory_mode, st_uid=uid),
                        SimpleNamespace(st_mode=bus_mode, st_uid=bus_uid)]
                with patch.object(Path, 'lstat', side_effect=info):
                    if directory_mode == stat.S_IFDIR | 0o700 and bus_mode == stat.S_IFSOCK | 0o600 and bus_uid == uid:
                        self.assertEqual(physical.ScientistPhysicalReleaseVerifier._environment()['XDG_RUNTIME_DIR'], runtime)
                    else:
                        with self.assertRaises(ScientistAdmissionError):
                            physical.ScientistPhysicalReleaseVerifier._environment()

    def test_proc_and_cgroup_read_permission_missing_event_and_oversize_deny(self):
        original_read = self.provider._read
        for target in ('/proc/12345/stat', 'cgroup.procs', 'cgroup.events'):
            def denied(path, **options):
                if path == target:
                    raise PermissionError('Synthetic denied kernel read')
                return original_read(path, **options)
            with patch.object(self.provider, '_read', side_effect=denied), self.subTest(target=target):
                with self.assertRaises(ScientistAdmissionError):
                    self.verify()
        self.unit_output = self.properties(LoadState='not-found', InvocationID='', ControlGroup='')
        events = self.root / ('sys/fs/cgroup' + self.group) / 'cgroup.events'
        events.unlink()
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.events', 'populated 0\n')
        self.write('/proc/self/mountinfo', 'a' * 262145)
        with self.assertRaises(ScientistAdmissionError):
            self.verify()

    def test_mountinfo_larger_than_16k_remains_bounded_and_supported(self):
        mounts = ''.join(f'{number} 0 0:1 / /synthetic-{number} rw - tmpfs tmpfs rw\n' for number in range(500))
        self.assertGreater(len(mounts), 16384)
        mounts += '1000 0 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n'
        self.write('/proc/self/mountinfo', mounts)
        self.assertIsNone(self.verify())

    def test_existing_release_verifier_invokes_concrete_observer_without_mutating_history(self):
        import test_scientist_release_proof as release_cases

        fixture = release_cases.ScientistReleaseProofTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.child = ScientistTerminalChild.model_validate(fixture.fixture.terminal['child_generation'])
        self.unit, self.group = self.child.unit, self.child.control_group
        self.unit_output = self.properties()
        self.write('/proc/sys/kernel/random/boot_id', self.child.boot_id + '\n')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.procs', '')
        self.write('/sys/fs/cgroup' + self.group + '/cgroup.events', 'populated 0\n')

        def retained_source(original, terminal, allocation, drain, no_admission, gpu_uuid):
            self.assertEqual(original, fixture.fixture.original)
            self.assertEqual(allocation, fixture.allocation)
            self.assertEqual(drain, fixture.drain)
            self.assertIsNone(no_admission)
            self.assertEqual(gpu_uuid, self.gpu_uuid)

        self.source.side_effect = retained_source
        before = fixture.fixture.rows()
        self.assertEqual(fixture.verify(verify_physical=self.provider).terminal_state, 'completed')
        self.assertEqual(fixture.fixture.rows(), before)
        self.assertEqual(self.commands.call_count, 6)
        self.process_output = b'999, synthetic-still-running-model, 2048\n'
        with self.assertRaises(ScientistAdmissionError):
            fixture.verify(verify_physical=self.provider)
        self.assertEqual(fixture.fixture.rows(), before)
        self.enable_shared_lanes()
        self.assertEqual(fixture.verify(verify_physical=self.provider).terminal_state, 'completed')
        self.assertEqual(fixture.fixture.rows(), before)
        self.process_output = f'{self.child.pid}, synthetic-owned-model, 2048\n'.encode()
        with self.assertRaises(ScientistAdmissionError):
            fixture.verify(verify_physical=self.provider)
        self.assertEqual(fixture.fixture.rows(), before)


if __name__ == '__main__':
    unittest.main()
