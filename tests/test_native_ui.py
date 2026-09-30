import json
from contextlib import contextmanager, nullcontext, suppress
import os
from pathlib import Path
import subprocess
import secrets
import select
import shutil
import signal
import struct
import tempfile
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT
from aos.desktop import DOCKER
from test_desktop_task_ui import task_server


@contextmanager
def native_display(root):
    executable = Path(os.environ.get('AOS_XVFB', shutil.which('Xvfb') or REPO_ROOT / 'data/native-x11/Xvfb'))
    if not executable.is_file():
        raise AssertionError('Native acceptance requires an explicitly prepared Xvfb binary')
    authority = root / 'Xauthority'
    cookie = secrets.token_bytes(16)

    def authorize(number):
        fields = [b'', number.encode(), b'MIT-MAGIC-COOKIE-1', cookie]
        authority.write_bytes(struct.pack('!H', 65535) + b''.join(struct.pack('!H', len(field)) + field for field in fields))
        authority.chmod(0o600)

    authorize('0')
    reader, writer = os.pipe()
    environment = {**os.environ, 'LD_LIBRARY_PATH': str(executable.parent)}
    with (root / 'xvfb.log').open('w') as output:
        process = subprocess.Popen([str(executable), '-displayfd', str(writer), '-screen', '0', '1600x1100x24',
                                    '-nolisten', 'tcp', '-auth', str(authority)],
                                   env=environment, pass_fds=(writer,), stdout=output, stderr=output)
        os.close(writer)
        try:
            assert select.select([reader], [], [], 10)[0], 'Xvfb readiness timed out'
            number = os.read(reader, 64).decode().strip()
            assert number.isdigit(), (root / 'xvfb.log').read_text()
            authorize(number)
            yield {**os.environ, 'DISPLAY': ':' + number, 'XAUTHORITY': str(authority), 'GDK_BACKEND': 'x11', 'AOS_NATIVE_PRIVATE_DISPLAY': '1'}
        finally:
            os.close(reader)
            process.terminate()
            process.wait(timeout=10)
            authority.unlink(missing_ok=True)


@unittest.skipUnless(os.environ.get('AOS_NATIVE_UI_TESTS') == '1', 'Requires native Tauri build, X11, AT-SPI and Docker')
class NativeUITests(unittest.TestCase):
    def test_native_login_novnc_approval_pause_takeover_and_logout(self):
        self.run_acceptance('fixture')

    @unittest.skipUnless(os.environ.get('AOS_NATIVE_REAL_TASK_TESTS') == '1', 'Requires pinned real Decider/Bonsai and CUDA')
    def test_native_real_decider_bonsai_tasks_and_control(self):
        self.run_acceptance('decider')

    def run_acceptance(self, engine):
        binary = REPO_ROOT / 'ui/src-tauri/target/debug/aos-console'
        self.assertTrue(binary.is_file(), 'Build the native binary before enabling this test')
        directory = (nullcontext(tempfile.mkdtemp(prefix='native-real-', dir=REPO_ROOT / 'data')) if engine == 'decider'
                     else tempfile.TemporaryDirectory(prefix='native-ui-', dir=REPO_ROOT / 'data'))
        with directory as temporary:
            root = Path(temporary)
            with task_server(root, engine, ('--browser-tasks', '--vision-engine', 'bonsai' if engine == 'decider' else 'fixture'), port=8765) as (origin, token, client, server), native_display(root) as environment:
                container = client.get('/api/state').json()['runtime']['container_id']
                with subprocess.Popen(['/usr/bin/python', str(REPO_ROOT / 'tests/native_ui_checks.py'),
                                         '--binary', str(binary), '--log', str(root / 'native.log'), '--database', str(root / 'store.sqlite'),
                                         '--container', container, '--engine', engine],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=environment, start_new_session=True) as probe:
                    try:
                        output, errors = probe.communicate(input=token, timeout=360 if engine == 'decider' else 180)
                    finally:
                        with suppress(ProcessLookupError):
                            os.killpg(probe.pid, signal.SIGTERM)
                        try:
                            probe.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(probe.pid, signal.SIGKILL)
                            probe.wait(timeout=5)
                self.assertEqual(probe.returncode, 0, errors + '\n' + (root / 'native.log').read_text())
                self.assertEqual(json.loads(output)['result'], 'passed')
                self.assertEqual(json.loads(output)['engine'], engine)
                self.assertTrue(json.loads(output)['goal_plan_recovery'])
                self.assertTrue(json.loads(output)['compound_plan_read_only'])
                self.assertTrue(json.loads(output)['session_binding_stale_clear_restart'])
                self.assertEqual(json.loads(output)['native_bounded_tasks'], ['hello', 'browser_form', 'vision_canvas'])
                self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
                client.post('/api/login', json={'token': token}).raise_for_status()
                jobs = client.get('/api/tasks').json()['jobs']
                restarted_container = client.get('/api/state').json()['runtime']['container_id']
                self.assertEqual([job['status'] for job in jobs], ['succeeded', 'succeeded', 'succeeded', 'cancelled', 'cancelled'])
                self.assertEqual([job['kind'] for job in jobs[:3]], ['vision_canvas', 'browser_form', 'hello'])
                self.assertTrue(all(job['real_model'] == int(engine == 'decider') for job in jobs))
                native_log = (root / 'native.log').read_text()
                self.assertIn('AOS native UI page loaded', native_log)
                for line in native_log.splitlines():
                    if line == 'Xlib:  extension "GLX" missing on display "' + environment['DISPLAY'] + '".':
                        continue
                    self.assertIn(line, ['AOS native UI page loaded', '',
                                         'MESA-EGL: warning: DRI3 error: Could not get DRI3 device',
                                         'MESA-EGL: warning: Ensure your X server supports DRI3 to get accelerated rendering'])
                print(output, flush=True)
                if engine == 'decider':
                    print('Real native acceptance evidence: ' + str(root), flush=True)
            self.assertFalse(subprocess.check_output([*DOCKER, 'ps', '-a', '--filter', 'id=' + restarted_container, '--format', '{{.ID}}'], timeout=5))
