import gc
import hashlib
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import warnings

from aos.bounded_process import run_bounded


class BoundedProcessTests(unittest.TestCase):
    def run_child(self, source, *, input=b'', timeout=5, max_output=65536):
        return run_bounded([sys.executable, '-c', source], input=input, env={}, timeout=timeout, max_output=max_output)

    def test_bytes_input_stdout_stderr_and_completed_process(self):
        arguments = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b"error")']
        result = run_bounded(arguments, input=b'\x00synthetic\xff', env={})
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.args, arguments)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'\x00synthetic\xff')
        self.assertEqual(result.stderr, b'error')

    def test_nonblocking_input_and_both_output_streams_do_not_deadlock(self):
        payload = b'x' * 1048576
        source = ('import hashlib, sys; sys.stdout.buffer.write(b"a" * 131072); sys.stdout.flush(); '
                  'sys.stderr.buffer.write(b"b" * 131072); sys.stderr.flush(); '
                  'sys.stdout.write(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
        result = self.run_child(source, input=payload, max_output=262208)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'a' * 131072 + hashlib.sha256(payload).hexdigest().encode())
        self.assertEqual(result.stderr, b'b' * 131072)

    def test_combined_output_limit_includes_stderr_and_exact_boundary(self):
        result = self.run_child('import sys; sys.stdout.buffer.write(b"a" * 32768); sys.stderr.buffer.write(b"b" * 32768)')
        self.assertEqual(len(result.stdout) + len(result.stderr), 65536)
        for source in ('import sys; sys.stdout.buffer.write(b"a" * 65537)',
                       'import sys; sys.stderr.buffer.write(b"b" * 65537)',
                       'import sys; sys.stdout.buffer.write(b"a" * 32768); sys.stderr.buffer.write(b"b" * 32769)'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'process_output_limit'):
                self.run_child(source)

    def test_timeout_includes_closed_pipes_and_nonreading_stdin(self):
        for source, payload in (
            ('import time; time.sleep(20)', b'x' * 1048576),
            ('import os, time; os.close(0); os.close(1); os.close(2); time.sleep(20)', b''),
            ('import sys, time; sys.stdout.write("partial"); sys.stdout.flush(); time.sleep(20)', b''),
        ):
            started = time.monotonic()
            with self.subTest(source=source), self.assertRaises(subprocess.TimeoutExpired) as raised:
                self.run_child(source, input=payload, timeout=0.2)
            self.assertEqual(raised.exception.timeout, 0.2)
            self.assertLess(time.monotonic() - started, 5)

    def test_broken_stdin_pipe_preserves_child_exit_and_output(self):
        result = self.run_child('import os, sys; os.close(0); sys.stdout.write("closed"); sys.exit(7)', input=b'x' * 1048576)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b'closed')

    def test_nonzero_exit_is_returned_without_implicit_check(self):
        result = self.run_child('import sys; sys.stderr.write("synthetic failure"); sys.exit(9)')
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stderr, b'synthetic failure')
        with self.assertRaises(subprocess.CalledProcessError):
            result.check_returncode()

    def test_explicit_environment_and_arguments_are_not_shell_expanded(self):
        source = 'import os, sys; print(os.environ.get("AOS_BOUNDED_PARENT", "absent")); print(os.environ["AOS_BOUNDED_CHILD"]); print(sys.argv[1])'
        with patch.dict(os.environ, {'AOS_BOUNDED_PARENT': 'private'}):
            result = run_bounded([sys.executable, '-c', source, '$(echo injected); *'], input=b'',
                                 env={'AOS_BOUNDED_CHILD': 'synthetic'})
        self.assertEqual(result.stdout, b'absent\nsynthetic\n$(echo injected); *\n')

    def test_invalid_inputs_rejected_before_child_spawn(self):
        valid = {'arguments': [sys.executable, '-c', 'pass'], 'input': b'', 'env': {}, 'timeout': 1, 'max_output': 16}
        changes = [{'arguments': value} for value in ('sh -c true', [], [''], ['x', 1], ['x\x00'])]
        changes += [{'input': value} for value in ('text', bytearray(b'x'), b'x' * 1048577)]
        changes += [{'env': value} for value in (None, {'x': 1}, {'': 'x'}, {'a=b': 'x'}, {'x': '\x00'})]
        changes += [{'timeout': value} for value in (True, 0, -1, float('inf'), float('nan'), '1')]
        changes += [{'max_output': value} for value in (True, 0, -1, 1.5)]
        with patch('aos.bounded_process.subprocess.Popen') as spawn:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    run_bounded(**{**valid, **change})
            spawn.assert_not_called()

    def test_success_overflow_timeout_and_setup_error_reap_child_and_close_pipes(self):
        original_spawn = subprocess.Popen
        children = []

        def spawn(*arguments, **keywords):
            child = original_spawn(*arguments, **keywords)
            children.append(child)
            return child

        with patch('aos.bounded_process.subprocess.Popen', side_effect=spawn):
            self.run_child('print("done")')
            with self.assertRaisesRegex(ValueError, 'process_output_limit'):
                self.run_child('import sys, time; sys.stdout.buffer.write(b"x" * 65537); sys.stdout.flush(); time.sleep(20)')
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_child('import time; time.sleep(20)', timeout=0.2)
            with patch('aos.bounded_process.os.set_blocking', side_effect=OSError('synthetic setup failure')):
                with self.assertRaisesRegex(OSError, 'synthetic setup failure'):
                    self.run_child('import time; time.sleep(20)')
            with patch('aos.bounded_process.selectors.DefaultSelector.select', side_effect=OSError('synthetic selector failure')):
                with self.assertRaisesRegex(OSError, 'synthetic selector failure'):
                    self.run_child('import time; time.sleep(20)')
        self.assertEqual(len(children), 5)
        for child in children:
            self.assertIsNotNone(child.returncode)
            self.assertTrue(all(stream.closed for stream in (child.stdin, child.stdout, child.stderr)))
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_repeated_success_and_failure_leave_no_file_descriptors_or_resource_warnings(self):
        gc.collect()
        before = len(os.listdir('/proc/self/fd'))
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always', ResourceWarning)
            for _attempt in range(3):
                self.run_child('print("done")')
                with self.assertRaisesRegex(ValueError, 'process_output_limit'):
                    self.run_child('print("overflow")', max_output=1)
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.run_child('import time; time.sleep(20)', timeout=0.1)
            gc.collect()
        self.assertEqual([warning for warning in captured if issubclass(warning.category, ResourceWarning)], [])
        self.assertEqual(len(os.listdir('/proc/self/fd')), before)


if __name__ == '__main__':
    unittest.main()
