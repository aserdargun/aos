"""Opt-in public named-project lifecycle with real CPU-only owned services.

Run with AOS_LOCAL_APP_PROJECT_LIFECYCLE_TESTS=1. The installed offline frontend
builder stages only project-local assets. No images are built or pulled and no
models run. Missing reviewed local prerequisites skip explicitly.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from uuid import uuid4

import httpx

from aos.bounded_process import run_bounded
from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop import DOCKER
from aos.lifecycle import observe_process
from aos.local_app import LocalAppInstance, LocalAppState, private_read


@unittest.skipUnless(os.environ.get('AOS_LOCAL_APP_PROJECT_LIFECYCLE_TESTS') == '1',
                     'Public CPU project lifecycle requires AOS_LOCAL_APP_PROJECT_LIFECYCLE_TESTS=1')
class LocalAppProjectLifecycleTests(unittest.TestCase):
    def _remaining(self, *, cleanup=False):
        remaining = (self.deadline if cleanup else self.deadline - 35) - time.monotonic()
        if remaining <= 0:
            self.fail('Owned project lifecycle exhausted its bounded wall-clock budget')
        return remaining

    def _run(self, arguments, *, cleanup=False, timeout=20):
        return run_bounded(arguments, input=b'', env=self.environment,
            timeout=min(timeout, self._remaining(cleanup=cleanup)), max_output=262144)

    def _cli(self, command, *arguments, cleanup=False, token=False):
        result = self._run([str(REPO_ROOT / 'scripts/aos-v1'), command,
                           *self.instance.arguments(), *arguments], cleanup=cleanup, timeout=35)
        self.assertEqual(result.returncode, 0,
            'Public project ' + command + ' failed: ' + result.stderr.decode('utf-8', errors='replace')[:1024])
        if token:
            value = result.stdout.decode().strip()
            self.assertRegex(value, r'^[A-Za-z0-9_-]{43}$')
            return value
        return json.loads(result.stdout)

    def _state(self):
        path = self.instance.base / 'current.json'
        if not path.exists():
            return None
        state = LocalAppState.model_validate_json(private_read(path), strict=True)
        self.instance.check_state(state)
        self.assertTrue((self.instance.base / state.session).is_dir())
        return state

    @staticmethod
    def _default_snapshot():
        path = REPO_ROOT / 'data/local-app-v1/current.json'
        if not path.exists():
            return None
        raw = private_read(path)
        info = path.stat(follow_symlinks=False)
        return (info.st_dev, info.st_ino, info.st_mtime_ns, hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _shared_frontend_snapshot():
        root = REPO_ROOT / 'ui/dist'
        if not root.exists():
            return None
        result = []
        for path in sorted(root.rglob('*')):
            if path.is_file():
                info = path.stat(follow_symlinks=False)
                result.append((str(path.relative_to(root)), info.st_ino, info.st_mtime_ns,
                               hashlib.sha256(path.read_bytes()).hexdigest()))
        return result

    def _prerequisites(self):
        result = self._run([*DOCKER, 'info', '--format', '{{json .DefaultRuntime}}'], timeout=5)
        if result.returncode != 0 or json.loads(result.stdout) != 'runc':
            self.skipTest('CPU lifecycle blocked: accessible Docker with default runc runtime is required')
        if not (REPO_ROOT / 'ui/node_modules/vite/bin/vite.js').is_file() or not Path('/usr/bin/node').is_file():
            self.skipTest('CPU lifecycle blocked: installed offline Node/Vite frontend builder unavailable')
        prepared = self._cli('prepare-ui')
        self.assertEqual(prepared['status'], 'prepared')
        self.assertEqual(prepared['manager_scope'],
                         {'project': self.instance.project, 'port': self.instance.port})
        self.assertEqual(prepared['ui_root'], str(self.instance.base / 'ui'))
        self.assertRegex(prepared['source_sha256'], r'^[a-f0-9]{64}$')
        self.assertFalse(prepared['runtime_started'])
        self.assertFalse(prepared['execution_authorized'])
        result = self._run([str(REPO_ROOT / 'scripts/aos-v1'), 'doctor', '--fixture',
                           *self.instance.arguments()], timeout=25)
        if result.returncode != 0:
            try:
                report = json.loads(result.stdout)
                missing = ','.join(check['name'] for check in report['checks'] if not check['ok'])
            except (ValueError, KeyError, TypeError):
                missing = 'public fixture prerequisite report unavailable'
            self.skipTest('CPU lifecycle blocked by reviewed local prerequisites: ' + missing
                          + '; no image build, pull or admission bypass performed')
        report = json.loads(result.stdout)
        self.assertTrue(report['ready'])
        self.assertEqual(report['mode'], 'fixture')
        self.assertFalse(report['inference_verified'])

    def _container(self, runtime, state):
        result = self._run([*DOCKER, 'inspect', '--type', 'container', runtime['container_id']], timeout=5)
        self.assertEqual(result.returncode, 0)
        inspected, = json.loads(result.stdout)
        self.assertEqual(inspected['Id'], runtime['container_id'])
        self.assertEqual(inspected['Image'], runtime['image_id'])
        self.assertEqual(inspected['Config']['Labels']['com.aos.runtime'], runtime['runtime_id'])
        host = inspected['HostConfig']
        self.assertEqual(host['Runtime'], 'runc')
        self.assertEqual(host['NetworkMode'], 'none')
        self.assertFalse(host['Privileged'])
        self.assertFalse(host.get('Devices'))
        self.assertFalse(host.get('DeviceRequests'))
        self.assertFalse(host.get('DeviceCgroupRules'))
        self.assertTrue(host['ReadonlyRootfs'])
        self.assertIn('ALL', host['CapDrop'])
        mounts = inspected['Mounts']
        self.assertEqual(len(mounts), 1)
        self.assertEqual((mounts[0]['Type'], mounts[0]['Source'], mounts[0]['Destination']),
                         ('bind', str(self.instance.base / state.session / 'workspace'), '/workspace'))
        arguments = Path('/proc/' + str(state.backend.pid) + '/cmdline').read_bytes().split(b'\0')
        self.assertEqual(arguments[arguments.index(b'--engine') + 1], b'fixture')
        self.assertEqual(arguments[arguments.index(b'--vision-engine') + 1], b'fixture')
        self.assertNotIn(b'--prewarm-decider', arguments)
        self.assertNotIn(b'--reuse-decider', arguments)

    def _authenticated_readback(self, token, state):
        with httpx.Client(base_url=self.instance.origin, headers={'Origin': self.instance.origin},
                          trust_env=False, timeout=min(5, self._remaining())) as client:
            self.assertEqual(client.get('/api/session').json(), {'authenticated': False,
                'manager_scope': {'project': self.instance.project, 'port': self.instance.port},
                'manager_session': state.session})
            self.assertEqual(client.get('/api/tasks').status_code, 401)
            self.assertEqual(client.post('/api/login', json={'token': token}).status_code, 200)
            tasks = client.get('/api/tasks')
            self.assertEqual(tasks.status_code, 200)
            self.assertEqual(tasks.json()['manager_scope'],
                             {'project': self.instance.project, 'port': self.instance.port})
            self.assertEqual(tasks.json()['manager_session'], state.session)
            response = client.get('/api/state')
            self.assertEqual(response.status_code, 200)
            current = response.json()
            self.assertEqual(current['control']['status'], 'running')
            self.assertFalse(tasks.json()['busy'])
            runtime = current['runtime']
            self._container(runtime, state)
            return runtime

    def _stopped(self, state, container_id):
        self.assertEqual(self._cli('stop', '--expected-session', state.session)['phase'], 'stopped')
        confirmed = self._cli('stop', '--expected-session', state.session)
        self.assertEqual((confirmed['session'], confirmed['phase']), (state.session, 'stopped'))
        self._removed(state, container_id)

    def _browser_readback(self, token, *, parameter_project=False):
        if os.environ.get('AOS_LOCAL_APP_PROJECT_BROWSER_TESTS') != '1':
            return
        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(REPO_ROOT /
                'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'),
                args=['--disable-gpu'])
            try:
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                page.set_default_timeout(min(10000, int(self._remaining() * 1000)))
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.instance.url)
                self.assertEqual(page.title(), 'AOS · Control center')
                page.locator('input[name=token]').fill(token)
                page.locator('form').filter(has=page.locator('input[name=token]')).get_by_role('button').click()
                expect(page.get_by_test_id('development-source-delivery')).to_be_visible()
                expect(page.get_by_test_id('development-isolated-learning-project')).to_be_visible()
                expect(page.get_by_test_id('development-scientist-report-history')).to_be_visible()
                expect(page.locator('input[name=token]')).to_have_count(0)
                page.get_by_role('button', name='Türkçe', exact=True).click()
                expect(page.get_by_test_id('development-source-delivery')).to_contain_text('1 Ekim 2026')
                page.get_by_role('button', name='English', exact=True).click()
                expect(page.get_by_test_id('development-source-delivery')).to_contain_text('1 October 2026')
                if parameter_project:
                    page.get_by_role('button', name='Tasks', exact=True).click()
                    expect(page.get_by_test_id('owned-parameter-project')).to_contain_text('Source prepared; no task execution yet.')
                    expect(page.get_by_test_id('owned-parameter-project')).to_contain_text('not proof of a released skill')
                    page.get_by_role('button', name='Development', exact=True).click()
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.get_by_test_id('development-source-delivery')).to_be_visible()
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                screenshot = Path(tempfile.gettempdir()) / ('aos-project-browser-' + self.instance.project + '.png')
                page.screenshot(path=str(screenshot))
                self.assertEqual(errors, [])
                print('Actual GPU-disabled project browser evidence: ' + str(screenshot), flush=True)
            finally:
                browser.close()

    def _connector_readback(self):
        if os.environ.get('AOS_LOCAL_APP_PROJECT_CONNECTOR_TESTS') != '1':
            return
        directory = self.instance.base / 'connector-fixture'
        directory.mkdir(mode=0o700)
        status_file = directory / 'status.json'
        status_file.write_text(json.dumps(self._cli('status')))
        scripts = {
            'uname': 'printf "%s\\n" Darwin',
            'ssh': 'case " $* " in *" -O check "*) exit 1;; *" -O "*|*" -fNT "*) exit 0;; esac\n'
                   + 'cat "' + str(status_file) + '"',
            'open': 'printf "%s\\n" "$1" > "' + str(directory / 'opened-url') + '"'}
        for name, content in scripts.items():
            executable = directory / name
            executable.write_text('#!/bin/sh\nset -eu\n' + content + '\n')
            executable.chmod(0o700)
        environment = {**self.environment, 'PATH': str(directory) + os.pathsep + os.environ['PATH'],
            'AOS_HOST': 'synthetic-connector-host', 'AOS_PROJECT': self.instance.project,
            'AOS_PROJECT_PORT': str(self.instance.port)}
        environment.pop('AOS_REMOTE_DIR', None)
        result = run_bounded(['sh', str(REPO_ROOT / 'scripts/aos-connect-macos.sh')], input=b'',
            env=environment, timeout=min(15, self._remaining()), max_output=262144)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[:1024])
        self.assertEqual((directory / 'opened-url').read_text().strip(), self.instance.url)
        self.assertEqual(self._cli('status')['phase'], 'running')
        print('Connector identity evidence: actual CPU status/HTTP with synthetic SSH/macOS/open; '
              'not real tunnel or Mac acceptance', flush=True)

    def _removed(self, state, container_id):
        self.assertEqual(observe_process(state.supervisor), 'not_observed')
        self.assertEqual(observe_process(state.backend), 'not_observed')
        self.assertFalse((REPO_ROOT / 'runs' / state.token_name).exists())
        listed = self._run([*DOCKER, 'ps', '-aq', '--filter', 'id=' + container_id], timeout=5)
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(listed.stdout.strip(), b'')

    def test_public_parameter_projects_execute_two_cpu_applications_with_independent_receipts(self):
        from aos.owned_parameter_project_execution import OwnedParameterProjectExecutionReceipt

        for application, field in (('synthetic-crm-note', 'contact_name'),
                                   ('synthetic-inventory-note', 'item_code')):
            with self.subTest(application=application):
                self.deadline = time.monotonic() + 150
                self.environment = {**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src'),
                    'CUDA_VISIBLE_DEVICES': '', 'NVIDIA_VISIBLE_DEVICES': 'void', 'HF_HUB_OFFLINE': '1',
                    'HF_DATASETS_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'WANDB_MODE': 'disabled',
                    'PYTHONDONTWRITEBYTECODE': '1', 'npm_config_offline': 'true'}
                default_before = self._default_snapshot()
                shared_before = self._shared_frontend_snapshot()
                source_root = Path(tempfile.mkdtemp(prefix='aos-public-parameter-'))
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    source_port = listener.getsockname()[1]
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    ui_port = listener.getsockname()[1]
                    self.assertNotIn(ui_port, (8765, source_port))
                    self.instance = LocalAppInstance.for_project('cpu-parameter-' + uuid4().hex, ui_port)
                    self._prerequisites()
                request_file = source_root / 'request.json'
                request_file.write_text(canonical({'application_key': application, 'port': source_port,
                    'parameters': {'record-id': 'Synthetic public record', 'note-text': 'Synthetic public note'}}))
                request_file.chmod(0o600)
                project = source_root / 'project'
                command = [str(REPO_ROOT / 'scripts/aos-parameter-project')]
                common = ['--request', str(request_file), '--directory', str(project)]
                planned = self._run([*command, 'plan', *common], timeout=15)
                self.assertEqual(planned.returncode, 0, planned.stderr.decode()[:1024])
                plan = json.loads(planned.stdout)
                provisioned = self._run([*command, 'provision', *common,
                    '--confirm-request-sha256', plan['request_sha256'], '--human-confirmation', 'PROVISION'], timeout=15)
                self.assertEqual(provisioned.returncode, 0, provisioned.stderr.decode()[:1024])
                manifest_sha256 = json.loads(provisioned.stdout)['manifest_sha256']
                state = None
                try:
                    started = self._cli('start', '--owned-parameter-project-directory', str(project),
                        '--owned-parameter-project-manifest-sha256', manifest_sha256,
                        '--owned-parameter-project-engine', 'fixture')
                    self.assertEqual(started['phase'], 'running')
                    state = self._state()
                    self.assertTrue(state.owned_synthetic_parameter_project)
                    token = self._cli('token', token=True)
                    runtime = self._authenticated_readback(token, state)
                    self._browser_readback(token, parameter_project=True)
                    with httpx.Client(base_url=self.instance.origin, headers={'Origin': self.instance.origin},
                                      trust_env=False, timeout=min(5, self._remaining())) as client:
                        self.assertEqual(client.post('/api/login', json={'token': token}).status_code, 200)
                        tasks = client.get('/api/tasks').json()
                        self.assertEqual(tasks['owned_parameter_project_execution']['entries'], [])
                        self.assertFalse(tasks['real_model'])
                        control = client.get('/api/state').json()['control']
                        response = client.post('/api/tasks', json={'kind': 'browser_remote_form',
                            'lease_id': control['lease_id'], 'generation': control['generation']})
                        self.assertEqual(response.status_code, 200, response.text[:1024])
                        job_id = response.json()['job_id']
                        approvals = set()
                        while True:
                            self._remaining()
                            tasks = client.get('/api/tasks').json()
                            job = next(job for job in tasks['jobs'] if job['job_id'] == job_id)
                            approval = tasks['approval']
                            if approval is not None and approval['approval_id'] not in approvals:
                                self.assertEqual(approval['job_id'], job_id)
                                response = client.post('/api/approvals/' + approval['approval_id'],
                                    json={'action_sha256': approval['action_sha256'], 'accept': True})
                                self.assertEqual(response.status_code, 200, response.text[:1024])
                                approvals.add(approval['approval_id'])
                            if job['status'] in {'succeeded', 'failed', 'cancelled'} and not tasks['busy']:
                                break
                            time.sleep(.05)
                        self.assertEqual(job['status'], 'succeeded', str(job)[:1024])
                        self.assertEqual(len(approvals), 6)
                        entry, = tasks['owned_parameter_project_execution']['entries']
                        self.assertEqual(entry['status'], 'accepted_verified')
                        receipt_path = self.instance.base / state.session / 'owned-parameter-project-execution' / (entry['intent_sha256'] + '.accepted.json')
                        receipt = OwnedParameterProjectExecutionReceipt.model_validate_json(private_read(receipt_path), strict=True)
                        self.assertEqual(digest(receipt.model_dump(mode='json')), entry['receipt_sha256'])
                        self.assertEqual(receipt.observation.record,
                                         {field: 'Synthetic public record', 'note': 'Synthetic public note'})
                        self.assertEqual(receipt.observation.reported_post_count, 1)
                        self.assertFalse(receipt.native_model_verified)
                        self.assertFalse(receipt.gpu_release_verified)
                    self._stopped(state, runtime['container_id'])
                    with socket.socket() as listener:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind(('127.0.0.1', source_port))
                        listener.listen(1)
                    print('Actual public CPU parameter project evidence: ' + str(self.instance.base)
                          + '; private source ' + str(source_root), flush=True)
                finally:
                    current = self._state()
                    if current is not None and current.phase != 'stopped':
                        self._cli('stop', '--expected-session', current.session, cleanup=True)
                    self.assertEqual(self._default_snapshot(), default_before)
                    self.assertEqual(self._shared_frontend_snapshot(), shared_before)

    def test_public_fixture_start_login_stop_and_new_session_preserve_default_manager(self):
        self.deadline = time.monotonic() + 120
        self.environment = {**os.environ, 'PYTHONPATH': str(REPO_ROOT / 'src'),
            'CUDA_VISIBLE_DEVICES': '', 'NVIDIA_VISIBLE_DEVICES': 'void', 'HF_HUB_OFFLINE': '1',
            'HF_DATASETS_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'WANDB_MODE': 'disabled',
            'PYTHONDONTWRITEBYTECODE': '1', 'npm_config_offline': 'true'}
        shared_frontend = self._shared_frontend_snapshot()
        self.addCleanup(lambda: self.assertEqual(self._shared_frontend_snapshot(), shared_frontend))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
            self.assertNotEqual(port, 8765)
            self.instance = LocalAppInstance.for_project('cpu-lifecycle-' + uuid4().hex, port)
            self.assertFalse(self.instance.base.exists())
            self._prerequisites()
        default_before = self._default_snapshot()
        states = []
        try:
            first = self._cli('start', '--fixture')
            self.assertEqual(first['phase'], 'running')
            state = self._state()
            self.assertIsNotNone(state)
            states.append(state)
            first_token = self._cli('token', token=True)
            first_runtime = self._authenticated_readback(first_token, state)
            self._browser_readback(first_token)
            self._connector_readback()
            self.assertEqual(self._cli('start', '--fixture')['session'], state.session)
            stale = self._run([str(REPO_ROOT / 'scripts/aos-v1'), 'restart',
                              *self.instance.arguments(), '--expected-session', 'app-' + '0' * 32])
            self.assertNotEqual(stale.returncode, 0)
            self.assertEqual(self._state().session, state.session)
            self.assertEqual(observe_process(state.supervisor), 'same_process')
            with httpx.Client(base_url=self.instance.origin, headers={'Origin': self.instance.origin},
                              trust_env=False, timeout=min(5, self._remaining())) as client:
                self.assertEqual(client.post('/api/login', json={'token': first_token}).status_code, 200)
                desktop = client.get('/api/state').json()['control']['session_id']
                self.assertEqual(client.post('/api/restart/quiesce', json={'session_id': desktop}).status_code, 200)
            self.assertEqual(self._cli('release-restart', '--expected-session', state.session),
                             {'quiesced': False, 'session_id': desktop})
            restarted = self._cli('restart', '--expected-session', state.session)
            self.assertEqual(restarted['phase'], 'running')
            self.assertNotEqual(restarted['session'], state.session)
            self._removed(state, first_runtime['container_id'])
            state = self._state()
            states.append(state)
            restarted_token = self._cli('token', token=True)
            self.assertNotEqual(restarted_token, first_token)
            restarted_runtime = self._authenticated_readback(restarted_token, state)
            self._stopped(state, restarted_runtime['container_id'])
            second = self._cli('start', '--fixture')
            self.assertEqual(second['phase'], 'running')
            self.assertNotEqual(second['session'], first['session'])
            state = self._state()
            states.append(state)
            second_token = self._cli('token', token=True)
            self.assertNotEqual(first_token, second_token)
            second_runtime = self._authenticated_readback(second_token, state)
            self.assertNotEqual(first_runtime['runtime_id'], second_runtime['runtime_id'])
            self.assertNotEqual(first_runtime['container_id'], second_runtime['container_id'])
            self._stopped(state, second_runtime['container_id'])
            self.assertTrue((self.instance.base / states[0].session / 'store.sqlite').is_file())
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
                released.bind(('127.0.0.1', port))
            self.assertEqual(self._default_snapshot(), default_before)
        finally:
            state = self._state()
            if state is not None and state.phase in {'starting', 'running'}:
                self._cli('stop', '--expected-session', state.session, cleanup=True)
            self.assertEqual(self._default_snapshot(), default_before)
            print('Private CPU project lifecycle evidence:', self.instance.base, flush=True)


if __name__ == '__main__':
    unittest.main()
