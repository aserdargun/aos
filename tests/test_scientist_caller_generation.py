"""Synthetic proc/systemd observations only; no process launch or live observer."""

import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.scientist_admission_history import ScientistCallerGeneration
from aos.scientist_transport import SystemdCallerAuthenticator, ScientistAdmissionError, _caller_proc_text


class ScientistCallerGenerationTests(unittest.TestCase):
    def setUp(self):
        self.pid, self.uid = os.getpid(), os.getuid()
        self.parent = self.pid + 10000
        self.unit = 'swapp-aos-synthetic.service'
        self.group = '/user.slice/' + self.unit
        self.boot = '11111111-2222-3333-4444-555555555555'
        self.generation = ScientistCallerGeneration(pid=self.pid, uid=self.uid, start_ticks=80,
            boot_id=self.boot, unit=self.unit, invocation_id='a' * 32, control_group=self.group,
            parent_pid=self.parent, parent_start_ticks=40)
        self.properties = {'Id': self.unit, 'LoadState': 'loaded', 'ActiveState': 'active',
            'MainPID': str(self.parent), 'InvocationID': 'a' * 32, 'ControlGroup': self.group}
        self.runtime = '/run/user/' + str(self.uid)
        self.directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=self.uid, st_dev=1, st_ino=10)
        self.bus = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=self.uid, st_dev=1, st_ino=11)
        self.proc = {'/proc/sys/kernel/random/boot_id': self.boot + '\n'}
        self.proc.update({f'/proc/{pid}/stat': self.proc_stat(pid, ticks) for pid, ticks in ((self.pid, 80), (self.parent, 40))})
        self.proc.update({f'/proc/{pid}/cgroup': '0::' + self.group + '\n' for pid in (self.pid, self.parent)})
        self.output = None
        self.query = Mock(side_effect=self.query_result)

    @staticmethod
    def proc_stat(pid, start, *, kernel_parent=77):
        fields = ['S', str(kernel_parent)] + ['0'] * 18
        fields[19] = str(start)
        return f'{pid} (synthetic ) name) ' + ' '.join(fields) + '\n'

    def query_result(self, arguments, **options):
        self.assertEqual(arguments, ['/usr/bin/systemctl', '--user', 'show', self.unit,
            '--property=Id,LoadState,ActiveState,MainPID,InvocationID,ControlGroup', '--no-pager'])
        self.assertEqual(options['input'], b'')
        self.assertEqual(options['max_output'], 16384)
        self.assertGreater(options['timeout'], 0)
        self.assertLessEqual(options['timeout'], 2)
        self.assertEqual(options['env'], {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'XDG_RUNTIME_DIR': self.runtime,
                                         'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + self.runtime + '/bus'})
        raw = self.output if self.output is not None else ('\n'.join(key + '=' + value for key, value in self.properties.items()) + '\n').encode()
        return subprocess.CompletedProcess(arguments, 0, raw, b'')

    def authenticate(self, generation=None, **options):
        def metadata(path):
            self.assertIn(str(path), (self.runtime, self.runtime + '/bus'))
            return self.bus if path.name == 'bus' else self.directory
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.runtime,
                        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + self.runtime + '/bus',
                        'LD_PRELOAD': '/synthetic/not-forwarded'}), \
                patch('aos.scientist_transport._caller_proc_text', side_effect=lambda path: self.proc[path]), \
                patch.object(Path, 'lstat', autospec=True, side_effect=metadata), \
                patch('aos.scientist_transport.run_bounded', self.query):
            return SystemdCallerAuthenticator().authenticate(generation if generation is not None else self.generation, **options)

    def test_outer_deadline_caps_query_without_renewal(self):
        with patch('aos.scientist_transport.time.monotonic', return_value=10):
            self.assertEqual(self.authenticate(deadline=10.25), self.generation)
        self.assertEqual(self.query.call_args.kwargs['timeout'], 0.25)

    def test_still_current_preserves_outer_deadline(self):
        authenticator = SystemdCallerAuthenticator()
        with patch.object(authenticator, 'authenticate', return_value=self.generation) as query:
            self.assertTrue(authenticator.still_current(self.generation, deadline=10.25))
        query.assert_called_once_with(self.generation, deadline=10.25)

    def test_expired_or_invalid_deadline_denies_before_query(self):
        for deadline in [True, 'later', float('inf'), float('nan'), 10, 9]:
            with self.subTest(deadline=deadline), patch('aos.scientist_transport.time.monotonic', return_value=10):
                with self.assertRaises(ScientistAdmissionError):
                    self.authenticate(deadline=deadline)
        self.query.assert_not_called()

    def test_query_finishing_after_outer_deadline_is_rejected(self):
        with patch('aos.scientist_transport.time.monotonic', side_effect=[10, 10, 10.1, 10.3]):
            with self.assertRaises(ScientistAdmissionError):
                self.authenticate(deadline=10.25)

    def test_exact_non_main_caller_and_service_mainpid_are_bound_not_kernel_ppid(self):
        self.assertNotEqual(self.generation.parent_pid, 77)
        self.assertEqual(self.authenticate(), self.generation)
        self.query.assert_called_once()

    def test_service_mainpid_can_be_the_actual_calling_process(self):
        self.properties['MainPID'] = str(self.pid)
        generation = self.generation.model_copy(update={'parent_pid': self.pid, 'parent_start_ticks': 80})
        self.assertEqual(self.authenticate(generation), generation)

    def test_foreign_pid_uid_or_invalid_reviewed_unit_deny_before_query(self):
        for changes in ({'pid': self.pid + 1}, {'uid': self.uid + 1}, {'unit': 'foreign.service'},
                        {'unit': 'swapp-aos-user@template.service'}, {'parent_pid': 1}, {'start_ticks': 0}, {'pid': True}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.authenticate(self.generation.model_copy(update=changes))
        self.query.assert_not_called()

    def test_untyped_generation_is_denied(self):
        with self.assertRaises(ScientistAdmissionError): self.authenticate(self.generation.model_dump())
        self.query.assert_not_called()

    def test_descendant_or_foreign_cgroup_is_not_supported_by_current_producer(self):
        for pid in (self.pid, self.parent):
            for group in (self.group + '/child', self.group + '-foreign', '/foreign'):
                self.proc[f'/proc/{pid}/cgroup'] = '0::' + group + '\n'
                with self.subTest(pid=pid, group=group), self.assertRaises(ScientistAdmissionError): self.authenticate()
            self.proc[f'/proc/{pid}/cgroup'] = '0::' + self.group + '\n'
        self.query.assert_not_called()

    def test_start_ticks_boot_and_duplicate_unified_groups_deny(self):
        for path, changed in ((f'/proc/{self.pid}/stat', self.proc_stat(self.pid, 81)),
                              (f'/proc/{self.parent}/stat', self.proc_stat(self.parent, 41)),
                              ('/proc/sys/kernel/random/boot_id', '22222222-2222-3333-4444-555555555555\n'),
                              (f'/proc/{self.pid}/cgroup', '0::' + self.group + '\n0::' + self.group + '\n')):
            original = self.proc[path]
            self.proc[path] = changed
            with self.subTest(path=path), self.assertRaises(ScientistAdmissionError): self.authenticate()
            self.proc[path] = original
        self.query.assert_not_called()

    def test_alias_mainpid_invocation_load_and_active_state_deny(self):
        for field, value in (('Id', 'swapp-aos-alias.service'), ('MainPID', str(self.pid)),
                             ('InvocationID', 'b' * 32), ('LoadState', 'not-found'),
                             ('ActiveState', 'inactive'), ('ControlGroup', self.group + '/child')):
            original = self.properties[field]
            self.properties[field] = value
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError): self.authenticate()
            self.properties[field] = original

    def test_duplicate_missing_unknown_nonascii_and_oversized_properties_deny(self):
        correct = ('\n'.join(key + '=' + value for key, value in self.properties.items()) + '\n').encode()
        for raw in (correct + b'Id=' + self.unit.encode() + b'\n', correct + b'Extra=1\n',
                    correct.replace(b'LoadState=loaded\n', b''), correct + b'\xff', b'x' * 16385,
                    correct + b'\n', correct.replace(b'MainPID=', b'MainPID=0')):
            self.output = raw
            with self.subTest(raw=raw[:30]), self.assertRaises(ScientistAdmissionError): self.authenticate()

    def test_both_pid_generations_are_rechecked_after_systemd_query(self):
        for pid, ticks in ((self.pid, 81), (self.parent, 41)):
            original = self.proc[f'/proc/{pid}/stat']
            def mutate(arguments, **options):
                result = self.query_result(arguments, **options)
                self.proc[f'/proc/{pid}/stat'] = self.proc_stat(pid, ticks)
                return result
            self.query.side_effect = mutate
            with self.subTest(pid=pid), self.assertRaises(ScientistAdmissionError): self.authenticate()
            self.proc[f'/proc/{pid}/stat'] = original

    def test_runtime_directory_and_bus_must_be_private_owned_and_correct_types(self):
        for metadata, field, value in ((self.directory, 'st_mode', stat.S_IFDIR | 0o755),
                                      (self.directory, 'st_mode', stat.S_IFLNK | 0o700),
                                      (self.bus, 'st_mode', stat.S_IFREG | 0o600),
                                      (self.bus, 'st_uid', self.uid + 1)):
            original = getattr(metadata, field)
            setattr(metadata, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError): self.authenticate()
            setattr(metadata, field, original)
        self.query.assert_not_called()

    def test_bus_replacement_during_query_is_denied(self):
        def replace_bus(arguments, **options):
            result = self.query_result(arguments, **options)
            self.bus.st_ino += 1
            return result
        self.query.side_effect = replace_bus
        with self.assertRaises(ScientistAdmissionError): self.authenticate()

    def test_query_timeout_failure_stderr_and_elapsed_deadline_are_denied(self):
        for outcome in (subprocess.TimeoutExpired('synthetic', 2), ValueError('synthetic bound'),
                        subprocess.CompletedProcess([], 1, b'', b''), subprocess.CompletedProcess([], 0, b'', b'warning')):
            self.query.side_effect = outcome if isinstance(outcome, Exception) else lambda *args, **kwargs: outcome
            with self.subTest(outcome=outcome), self.assertRaises(ScientistAdmissionError): self.authenticate()
        self.query.side_effect = self.query_result
        with patch('aos.scientist_transport.time.monotonic', side_effect=[0, .01, 3]):
            with self.assertRaises(ScientistAdmissionError): self.authenticate()

    def test_proc_reader_bound_type_and_no_follow_flags(self):
        for raw, mode in ((b'x' * 16385, stat.S_IFREG), (b'\xff', stat.S_IFREG), (b'x', stat.S_IFIFO)):
            with self.subTest(raw=raw[:10], mode=mode), \
                    patch('aos.scientist_transport.os.open', return_value=99) as opened, \
                    patch('aos.scientist_transport.os.fstat', return_value=SimpleNamespace(st_mode=mode)), \
                    patch('aos.scientist_transport.os.read', return_value=raw) as read, \
                    patch('aos.scientist_transport.os.close') as close:
                with self.assertRaises((ScientistAdmissionError, UnicodeError)): _caller_proc_text('/proc/synthetic/stat')
                self.assertEqual(opened.call_args.args[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                if stat.S_ISREG(mode): read.assert_called_once_with(99, 16385)
                close.assert_called_once_with(99)

    def test_still_current_is_false_on_lost_generation(self):
        authenticator = SystemdCallerAuthenticator()
        with patch.object(authenticator, 'authenticate', return_value=self.generation):
            self.assertTrue(authenticator.still_current(self.generation))
        with patch.object(authenticator, 'authenticate', side_effect=ScientistAdmissionError('Synthetic revoked')):
            self.assertFalse(authenticator.still_current(self.generation))


if __name__ == '__main__':
    unittest.main()
