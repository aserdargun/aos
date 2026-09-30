import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aos.contracts import REPO_ROOT


FAKE_COMMAND = '''import json,os,sys
from pathlib import Path
command=Path(sys.argv[0]).name
arguments=sys.argv[1:]
with open(os.environ['CONNECT_TEST_LOG'],'a') as stream:
    stream.write(json.dumps([command,arguments])+'\\n')
if command=='uname':
    print(os.environ.get('CONNECT_TEST_OS','Darwin'))
elif command=='ssh':
    if '-fNT' in arguments:
        sys.exit(int(os.environ.get('CONNECT_TEST_FORWARD_FAIL','0')))
    if '-O' in arguments:
        sys.exit(1 if 'check' in arguments else 0)
    print('private synthetic start result')
    sys.exit(int(os.environ.get('CONNECT_TEST_REMOTE_FAIL','0')))
elif command=='curl':
    state=os.environ.get('CONNECT_TEST_READY','yes')
    if state=='fail':
        sys.exit(1)
    print(json.dumps({'authenticated':False,'local_auto_login':state=='yes'}))
elif command=='date':
    path=Path(os.environ['CONNECT_TEST_CLOCK'])
    count=int(path.read_text()) if path.exists() else 0
    path.write_text(str(count+1))
    print(count*15)
'''


class MacosConnectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.log = self.root / 'commands.jsonl'
        self.environment = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                                CONNECT_TEST_LOG=str(self.log), CONNECT_TEST_CLOCK=str(self.root / 'clock'))
        self.environment.pop('AOS_HOST', None)
        self.environment.pop('AOS_REMOTE_DIR', None)
        for command in ('uname', 'ssh', 'curl', 'open', 'sleep', 'date'):
            path = self.root / command
            path.write_text('#!' + sys.executable + '\n' + FAKE_COMMAND)
            path.chmod(0o700)
        self.script = REPO_ROOT / 'scripts/aos-connect-macos.sh'

    def tearDown(self):
        self.directory.cleanup()

    def run_script(self, *arguments, **environment):
        self.environment.update(environment)
        return subprocess.run(['sh', str(self.script), *arguments], env=self.environment,
                              capture_output=True, text=True, timeout=10)

    def commands(self, name):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [arguments for command, arguments in rows if command == name]

    def assert_cleanup(self):
        calls = self.commands('ssh')
        forward = next(arguments for arguments in calls if '-fNT' in arguments)
        socket = Path(forward[forward.index('-S') + 1])
        self.assertFalse(socket.parent.exists())
        if not self.environment.get('CONNECT_TEST_FORWARD_FAIL'):
            self.assertEqual(calls[-1], ['-S', str(socket), '-O', 'exit', forward[-1]])

    def test_success_one_forward_master_cookie_ready_browser_and_cleanup(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands('ssh')
        forwards = [arguments for arguments in calls if '-fNT' in arguments]
        self.assertEqual(len(forwards), 1)
        self.assertIn('127.0.0.1:8765:127.0.0.1:8765', forwards[0])
        for setting in ('ControlPersist=no', 'ExitOnForwardFailure=yes',
                        'ServerAliveInterval=15', 'ServerAliveCountMax=3'):
            self.assertIn(setting, forwards[0])
        self.assertEqual(forwards[0][-1], 'cachyos')
        remote = next(arguments for arguments in calls if arguments[-1].endswith('aos-v1 start'))
        self.assertIn('BatchMode=yes', remote)
        self.assertEqual(remote[-1], 'cd "$HOME"/aos && ./scripts/aos-v1 start')
        self.assertTrue(all(arguments[arguments.index('-S') + 1] == forwards[0][forwards[0].index('-S') + 1]
                            for arguments in calls))
        self.assertEqual(self.commands('open'), [['http://127.0.0.1:8765/ui/']])
        self.assertIn('--noproxy', self.commands('curl')[0])
        self.assertNotIn('private synthetic start result', result.stdout + result.stderr)
        self.assert_cleanup()

    def test_forward_failure_prevents_remote_start_and_browser(self):
        result = self.run_script(CONNECT_TEST_FORWARD_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('8765', result.stderr)
        self.assertEqual(len(self.commands('ssh')), 1)
        self.assertEqual(self.commands('open'), [])
        self.assertEqual(self.commands('curl'), [])
        self.assert_cleanup()

    def test_remote_failure_and_readiness_failure_never_open_browser(self):
        for environment in ({'CONNECT_TEST_REMOTE_FAIL': '1'}, {'CONNECT_TEST_READY': 'fail'},
                            {'CONNECT_TEST_READY': 'disabled'}):
            with self.subTest(environment=environment):
                self.environment.pop('CONNECT_TEST_REMOTE_FAIL', None)
                self.environment.pop('CONNECT_TEST_READY', None)
                self.log.unlink(missing_ok=True)
                (self.root / 'clock').unlink(missing_ok=True)
                result = self.run_script(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.commands('open'), [])
                self.assert_cleanup()
                if environment.get('CONNECT_TEST_READY') == 'disabled':
                    self.assertIn('safe idle restart', result.stderr)

    def test_alias_and_safe_absolute_path_are_passed_as_single_arguments(self):
        result = self.run_script('operator@configured-host', AOS_HOST='ignored', AOS_REMOTE_DIR='/srv/aos-work')
        self.assertEqual(result.returncode, 0, result.stderr)
        remote = next(arguments for arguments in self.commands('ssh')
                      if arguments[-1].endswith('aos-v1 start'))
        self.assertEqual(remote[-2:], ['operator@configured-host', "cd '/srv/aos-work' && ./scripts/aos-v1 start"])
        self.assert_cleanup()

    def test_unsafe_hosts_and_paths_rejected_before_ssh(self):
        for host in ('', '-oProxyCommand=evil', 'host;evil', 'host$(evil)', 'host name'):
            with self.subTest(host=host):
                self.assertEqual(self.run_script(host).returncode, 2)
        for path in ('', '-option', 'relative-other', '/srv/aos;evil', "/srv/aos'evil", '/srv/../aos'):
            with self.subTest(path=path):
                self.assertEqual(self.run_script(AOS_REMOTE_DIR=path).returncode, 2)
        self.assertEqual(self.commands('ssh'), [])

    def test_help_any_platform_and_darwin_guard(self):
        result = self.run_script('--help', CONNECT_TEST_OS='Linux')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Usage:', result.stdout)
        self.assertEqual(self.run_script().returncode, 2)
        self.assertEqual(self.commands('ssh'), [])
