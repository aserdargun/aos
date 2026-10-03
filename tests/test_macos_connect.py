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
    if os.environ.get('AOS_PROJECT'):
        start={'phase':'running','supervisor':'same_process','backend':'same_process',
               'manager_scope':{'project':os.environ['AOS_PROJECT'],'port':int(os.environ['AOS_PROJECT_PORT'])},
               'url':'http://127.0.0.1:'+os.environ['AOS_PROJECT_PORT']+'/ui/', 'session':'app-'+'a'*32}
        print(os.environ.get('CONNECT_TEST_START_JSON',json.dumps(start)))
    else:
        print('private synthetic start result')
    sys.exit(int(os.environ.get('CONNECT_TEST_REMOTE_FAIL','0')))
elif command=='curl':
    state=os.environ.get('CONNECT_TEST_READY','yes')
    if state=='fail':
        sys.exit(1)
    session={'authenticated':False,'local_auto_login':state=='yes'}
    if os.environ.get('AOS_PROJECT'):
        session={'authenticated':False,'manager_scope':{'project':os.environ['AOS_PROJECT'],
                 'port':int(os.environ['AOS_PROJECT_PORT'])},'manager_session':'app-'+'a'*32}
    print(os.environ.get('CONNECT_TEST_SESSION_JSON',json.dumps(session)))
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
        self.environment.pop('AOS_PROJECT', None)
        self.environment.pop('AOS_PROJECT_PORT', None)
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

    def named_environment(self):
        return {'AOS_PROJECT': 'example-project', 'AOS_PROJECT_PORT': '18765'}

    def named_start(self):
        return {'phase': 'running', 'supervisor': 'same_process', 'backend': 'same_process',
                'manager_scope': {'project': 'example-project', 'port': 18765},
                'url': 'http://127.0.0.1:18765/ui/', 'session': 'app-' + 'a' * 32}

    def named_session(self):
        return {'authenticated': False, 'manager_scope': {'project': 'example-project', 'port': 18765},
                'manager_session': 'app-' + 'a' * 32}

    def test_named_project_exact_port_scope_and_manual_login(self):
        for explicit_false in (False, True):
            with self.subTest(explicit_false=explicit_false):
                self.log.unlink(missing_ok=True)
                session = self.named_session()
                if explicit_false:
                    session['local_auto_login'] = False
                result = self.run_script(**self.named_environment(),
                                         CONNECT_TEST_SESSION_JSON=json.dumps(session))
                self.assertEqual(result.returncode, 0, result.stderr)
                forwards = [arguments for arguments in self.commands('ssh') if '-fNT' in arguments]
                self.assertIn('127.0.0.1:18765:127.0.0.1:18765', forwards[0])
                remote = [arguments for arguments in self.commands('ssh') if '-o' in arguments
                          and 'BatchMode=yes' in arguments]
                self.assertEqual(remote[0][-1], 'cd "$HOME"/aos && ./scripts/aos-v1 status '
                                 '--project example-project --project-port 18765')
                self.assertEqual(self.commands('open'), [['http://127.0.0.1:18765/ui/']])
                self.assertIn('--max-filesize', self.commands('curl')[0])
                self.assertIn('manual token login', result.stdout)
                self.assertIn('token --project example-project --project-port 18765', result.stdout)
                self.assertFalse(any(' token ' in argument for call in self.commands('ssh') for argument in call))
                self.assertFalse(any('aos-v1 start' in argument for call in self.commands('ssh') for argument in call))
                self.assert_cleanup()

    def test_invalid_project_pairs_fail_before_ssh(self):
        pairs = [('', '18765'), ('project', ''), ('UPPER', '18765'), ('-flag', '18765'),
                 ('unsafe;echo', '18765'), ('a' * 49, '18765'), ('valid', '8765'),
                 ('valid', '1023'), ('valid', '65536'), ('valid', 'true'),
                 ('valid', '018765'), ('valid', '+18765'), ('valid', '1' * 100)]
        for project, port in pairs:
            with self.subTest(project=project, port=port):
                result = self.run_script(AOS_PROJECT=project, AOS_PROJECT_PORT=port)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.environment.pop('AOS_PROJECT_PORT')
        self.assertEqual(self.run_script(AOS_PROJECT='valid').returncode, 2)
        self.environment.pop('AOS_PROJECT')
        self.assertEqual(self.run_script(AOS_PROJECT_PORT='18765').returncode, 2)
        self.assertEqual(self.commands('ssh'), [])

    def test_named_start_metadata_denies_before_http(self):
        invalid = ['not JSON', '{}', '[' + '0,' * 40000 + '0]',
                   '{"phase":"running","phase":"running"}', '{"extra":NaN}']
        for field, value in [('phase', 'stopped'), ('backend', 'not_observed'),
                             ('supervisor', 'not_observed'), ('session', 'invalid'),
                             ('url', 'http://127.0.0.1:8765/ui/'),
                             ('manager_scope', {'project': 'foreign', 'port': 18765}),
                             ('manager_scope', {'project': 'example-project', 'port': '18765'})]:
            invalid.append(json.dumps(self.named_start() | {field: value}))
        for content in invalid:
            with self.subTest(content=content[:80]):
                self.log.unlink(missing_ok=True)
                result = self.run_script(**self.named_environment(), CONNECT_TEST_START_JSON=content)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.commands('curl'), [])
                self.assertEqual(self.commands('open'), [])
                self.assertNotIn(content, result.stdout)
                self.assert_cleanup()

    def test_named_session_mismatch_replay_and_malformed_deny_browser(self):
        invalid = ['{}', 'not JSON', '{"authenticated":false,"authenticated":false}',
                   '{"extra":Infinity}']
        for field, value in [('authenticated', True), ('authenticated', 0),
                             ('local_auto_login', True), ('local_auto_login', 0),
                             ('manager_session', 'app-' + 'b' * 32),
                             ('manager_scope', {'project': 'foreign', 'port': 18765}),
                             ('manager_scope', {'project': 'example-project', 'port': 8765}),
                             ('manager_scope', {'project': 'example-project', 'port': True}),
                             ('manager_scope', {'project': 'example-project', 'port': 18765, 'extra': 1})]:
            invalid.append(json.dumps(self.named_session() | {field: value}))
        for content in invalid:
            with self.subTest(content=content):
                self.log.unlink(missing_ok=True)
                result = self.run_script(**self.named_environment(), CONNECT_TEST_SESSION_JSON=content)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.commands('open'), [])
                self.assert_cleanup()
