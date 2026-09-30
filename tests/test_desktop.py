import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import AOSFault, ErrorCode, HELLO_CONTENT, HELLO_PATH, REPO_ROOT, Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.desktop_control import DesktopController
from aos.operator import Operator
from aos.storage import TrajectoryStore


class FakeDesktop:
    runtime_id = 'fixture-desktop'
    pins = {'image_id': 'sha256:' + '0' * 64}
    container_id = None

    def __init__(self):
        self.calls = []
        self.running = True
        self.actual = {'text': 'AOS desktop input'}

    def perform(self, tool, arguments):
        self.calls.append(tool)
        if tool == 'probe':
            return {'display': True, 'xfce': True, 'note': True, 'vnc': 'RFB fixture'}
        return self.actual if tool == 'read_note' else {'fixture': True}

    def stop(self):
        self.running = False

    def restart(self):
        self.running = True
        self.runtime_id = 'fixture-restarted'

    def status(self):
        return {'running': self.running, 'real_execution': False}


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = TrajectoryStore(Path(self.temporary.name) / 'desktop.sqlite')
        self.addCleanup(self.store.close)
        self.runtime = FakeDesktop()
        self.controller = DesktopController(self.store, self.runtime)

    def enqueue(self):
        state = self.controller.state()
        return self.controller.enqueue(state['lease_id'], state['generation'])

    def test_takeover_cancels_queue_and_requires_new_lease(self):
        previous = self.controller.state()
        queued = self.enqueue()
        self.assertEqual(self.controller.control('take-control')['owner'], 'HUMAN')
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        with self.assertRaises(AOSFault):
            self.enqueue()
        resumed = self.controller.control('return-control')
        self.assertEqual(resumed['owner'], 'AGENT')
        self.assertNotEqual(previous['lease_id'], resumed['lease_id'])
        self.assertEqual(self.runtime.calls, ['probe'])
        with self.assertRaises(AOSFault):
            self.controller.enqueue(previous['lease_id'], previous['generation'])
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_inputs').fetchone()[0], 'cancelled')

    def test_intent_precedes_input_and_completed_input_is_not_replayed(self):
        original = self.runtime.perform
        statuses = []

        def observed(tool, arguments):
            statuses.append(self.store.connection.execute('SELECT status FROM desktop_inputs').fetchone()[0])
            return original(tool, arguments)

        queued = self.enqueue()
        with patch.object(self.runtime, 'perform', side_effect=observed):
            self.assertEqual(self.controller.execute(queued), {'text': 'AOS desktop input'})
        self.assertEqual(statuses, ['running', 'running'])
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        self.assertEqual(self.runtime.calls, ['type_note', 'read_note'])

    def test_failure_and_mismatch_remain_uncertain_without_replay(self):
        queued = self.enqueue()
        self.runtime.actual = {'text': 'not the requested text'}
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        self.assertEqual(self.store.connection.execute('SELECT status FROM desktop_inputs').fetchone()[0], 'uncertain')
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        queued = self.enqueue()
        with patch.object(self.runtime, 'perform', side_effect=AOSFault(ErrorCode.RUNTIME_CRASH, 'Fixture lost ack')):
            with self.assertRaises(AOSFault):
                self.controller.execute(queued)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM desktop_inputs WHERE status='uncertain'").fetchone()[0], 2)

    def test_pause_stop_restart_and_fresh_probe_failure_are_fail_closed(self):
        queued = self.enqueue()
        self.assertEqual(self.controller.control('pause')['owner'], 'PAUSED')
        with self.assertRaises(AOSFault):
            self.controller.execute(queued)
        self.assertEqual(self.controller.control('stop')['status'], 'stopped')
        with self.assertRaises(AOSFault):
            self.controller.control('resume')
        restarted = self.controller.control('restart')
        self.assertEqual(restarted['runtime_id'], 'fixture-restarted')
        self.assertEqual(restarted['owner'], 'PAUSED')
        with patch.object(self.runtime, 'perform', side_effect=AOSFault(ErrorCode.RUNTIME_CRASH, 'Fixture offline')):
            with self.assertRaises(AOSFault):
                self.controller.control('resume')
        self.assertEqual(self.controller.state()['owner'], 'PAUSED')
        with patch.object(self.runtime, 'perform', return_value={'display': False}):
            with self.assertRaises(AOSFault):
                self.controller.control('resume')
        self.assertEqual(self.controller.state()['owner'], 'PAUSED')

    def test_reconcile_revokes_lease_cancels_queue_and_marks_inflight_uncertain(self):
        queued = self.enqueue()
        running = self.enqueue()
        previous = self.controller.state()
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_inputs SET status='running' WHERE input_id=?", (running,))
        self.store.reconcile()
        states = dict(self.store.connection.execute('SELECT input_id,status FROM desktop_inputs').fetchall())
        self.assertEqual(states, {queued: 'cancelled', running: 'uncertain'})
        self.assertNotEqual(previous['lease_id'], self.controller.state()['lease_id'])
        self.assertEqual(self.controller.state()['owner'], 'PAUSED')
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unpinned_or_source_mismatched_image_cannot_start(self):
        root = Path(self.temporary.name)
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'image_id': 'mutable-tag:latest'}))
        runtime = DesktopRuntime(root / 'workspace', manifest)
        with patch.object(runtime, 'docker') as docker, self.assertRaises(AOSFault):
            runtime.start()
        docker.assert_not_called()
        manifest.write_text(json.dumps({'image_id': 'sha256:' + '0' * 64, 'source_sha256': '0' * 64, 'source_files': {}}))
        with patch.object(runtime, 'docker', return_value=b'[{"Config":{"Labels":{}}}]') as docker, self.assertRaises(AOSFault):
            runtime.start()
        self.assertEqual(docker.call_count, 1)
        self.assertEqual(docker.call_args.args[0][:2], ['image', 'inspect'])
        self.assertIsNone(runtime.descriptor)

    def test_stop_refuses_foreign_runtime_container(self):
        runtime = DesktopRuntime(Path(self.temporary.name) / 'workspace', Path(self.temporary.name) / 'unused.json')
        runtime.container_id = 'a' * 64
        with patch.object(runtime, 'docker', return_value=b'[{"Config":{"Labels":{"com.aos.runtime":"another-owner"}}}]') as docker:
            with self.assertRaises(AOSFault):
                runtime.stop()
        self.assertEqual(docker.call_count, 1)
        self.assertEqual(docker.call_args.args[0][0], 'inspect')

    @unittest.skipUnless(importlib.util.find_spec('fastapi') and importlib.util.find_spec('httpx'), 'Install desktop extra')
    def test_http_auth_host_origin_bounded_body_and_ownership(self):
        import httpx
        from aos.desktop_console import create_console

        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'fixture-secret', origin, Path(self.temporary.name))

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                landing = await client.get('/')
                if (REPO_ROOT / 'ui/dist/index.html').is_file():
                    self.assertEqual(landing.status_code, 302)
                    self.assertEqual(landing.headers['location'], '/ui/')
                else:
                    self.assertEqual(landing.status_code, 200)
                legacy = await client.get('/legacy/')
                self.assertEqual(legacy.status_code, 200)
                self.assertIn('Yerel konsola giriş', legacy.text)
                self.assertEqual((await client.get('/api/state')).status_code, 401)
                self.assertEqual((await client.get('/novnc/core/rfb.js')).status_code, 401)
                self.assertEqual((await client.get('/', headers={'Host': 'attacker.invalid'})).status_code, 403)
                self.assertEqual((await client.post('/api/login', json={'token': 'fixture-secret'})).status_code, 403)
                client.headers['Origin'] = origin
                for value in ['wrong', 'şifre', [], None]:
                    self.assertEqual((await client.post('/api/login', json={'token': value})).status_code, 401)
                self.assertEqual((await client.post('/api/login', content='x' * 4097)).status_code, 413)
                self.assertEqual((await client.post('/api/login', content='[]')).status_code, 400)
                response = await client.post('/api/login', json={'token': 'fixture-secret'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('HttpOnly', response.headers['set-cookie'])
                self.assertIn('SameSite=strict', response.headers['set-cookie'])
                previous = (await client.get('/api/state')).json()['control']
                self.assertEqual((await client.post('/api/control', json={'command': []})).status_code, 400)
                self.assertEqual((await client.post('/api/input', json={'lease_id': previous['lease_id'], 'generation': True})).status_code, 400)
                queued = (await client.post('/api/input', json={'lease_id': previous['lease_id'], 'generation': previous['generation']})).json()['input_id']
                self.assertEqual((await client.post('/api/control', json={'command': 'take-control'})).status_code, 200)
                self.assertEqual((await client.post(f'/api/input/{queued}/execute', json={})).status_code, 409)
                self.assertEqual((await client.post('/api/control', json={'command': 'return-control'})).status_code, 200)
                self.assertEqual((await client.post('/api/logout', json={})).status_code, 200)
                self.assertEqual((await client.get('/api/state')).status_code, 401)
                self.assertEqual(self.controller.state()['owner'], 'PAUSED')

        asyncio.run(exercise())


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1', 'Opt in with AOS_DESKTOP_TESTS=1 after preparing image')
class DesktopIntegrationTests(unittest.TestCase):
    def test_real_desktop_lifecycle_files_office_input_and_isolation(self):
        with tempfile.TemporaryDirectory(prefix='desktop-test-', dir=REPO_ROOT / 'data') as temporary:
            root = Path(temporary)
            runtime = DesktopRuntime(root / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
            store = TrajectoryStore(root / 'desktop.sqlite')
            try:
                runtime.start()
                controller = DesktopController(store, runtime)
                probe = runtime.perform('probe', {})
                self.assertEqual(probe['uid'], os.getuid())
                self.assertEqual(probe['network_interfaces'], ['lo'])
                self.assertFalse(probe['host_home'])
                self.assertFalse(probe['docker_socket'])
                inspected = json.loads(runtime.docker(['inspect', runtime.container_id]))[0]
                self.assertEqual(inspected['HostConfig']['NetworkMode'], 'none')
                self.assertTrue(inspected['HostConfig']['ReadonlyRootfs'])
                self.assertTrue(inspected['HostConfig']['Init'])
                self.assertEqual(inspected['HostConfig']['CapDrop'], ['ALL'])
                self.assertFalse(inspected['HostConfig']['Privileged'])
                self.assertEqual(inspected['HostConfig']['PidsLimit'], 512)
                self.assertEqual(inspected['HostConfig']['Memory'], 3 * 1024 ** 3)
                self.assertEqual(inspected['HostConfig']['NanoCpus'], 2 * 10 ** 9)
                self.assertIn('no-new-privileges', inspected['HostConfig']['SecurityOpt'])
                self.assertFalse(inspected['HostConfig']['PortBindings'])
                mounts = [mount for mount in inspected['Mounts'] if mount['Type'] == 'bind']
                self.assertEqual([(mount['Source'], mount['Destination']) for mount in mounts], [(str(root / 'workspace'), '/workspace')])
                settings = Settings(workspace=root / 'workspace', database=root / 'desktop.sqlite')
                result = asyncio.run(Operator(settings, store, runtime, FixtureDecisionEngine()).hello())
                self.assertEqual(result['status'], 'succeeded')
                self.assertFalse(result['real_model'])
                self.assertEqual(runtime.read(HELLO_PATH), HELLO_CONTENT)
                with self.assertRaises(AOSFault):
                    runtime.write(HELLO_PATH, HELLO_CONTENT)
                self.assertEqual(runtime.read(HELLO_PATH), HELLO_CONTENT)
                original = root / 'workspace/original.txt'
                hello = root / 'workspace/hello.txt'
                hello.rename(original)
                hello.symlink_to(original)
                with self.assertRaises(AOSFault):
                    runtime.read(HELLO_PATH)
                hello.unlink()
                os.link(original, hello)
                with self.assertRaises(AOSFault):
                    runtime.read(HELLO_PATH)
                hello.unlink()
                original.rename(hello)
                self.assertIn('09308e6af1379087dd0ae56e8bbf85b96f3861fb4c91ef2393edb70229164032', runtime.checksum(HELLO_PATH)['stdout'])
                versions = runtime.perform('versions', {})
                self.assertTrue(all(versions.values()))
                self.assertEqual(runtime.perform('office_pdf', {})['actual'], 'AOS synthetic office acceptance.')
                windows = json.loads(runtime.docker(['exec', '-i', runtime.container_id, '/usr/bin/python3', '-'],
                                                    (REPO_ROOT / 'tests/desktop_apps_checks.py').read_bytes(), timeout=100))
                self.assertEqual(set(windows), {'chromium', 'codium', 'thunar', 'terminal', 'libreoffice', 'pdf_viewer'})
                self.assertTrue(all(windows.values()))
                state = controller.state()
                queued = controller.enqueue(state['lease_id'], state['generation'])
                controller.control('take-control')
                with self.assertRaises(AOSFault):
                    controller.execute(queued)
                self.assertEqual(runtime.perform('read_note', {}), {'text': ''})
                state = controller.control('return-control')
                queued = controller.enqueue(state['lease_id'], state['generation'])
                self.assertEqual(controller.execute(queued), {'text': 'AOS desktop input'})
                queued = controller.enqueue(state['lease_id'], state['generation'])
                self.assertEqual(controller.execute(queued), {'text': 'AOS desktop input'})
                with self.assertRaises(AOSFault):
                    runtime.perform('shell', {'command': 'id'})
                previous = runtime.container_id
                state = controller.control('restart')
                self.assertEqual(state['owner'], 'PAUSED')
                self.assertNotEqual(runtime.container_id, previous)
                self.assertEqual(runtime.read(HELLO_PATH), HELLO_CONTENT)
                self.assertEqual(runtime.perform('read_note', {}), {'text': ''})
                controller.control('stop')
                self.assertIsNone(runtime.container_id)
                self.assertTrue((root / 'workspace/hello.txt').is_file())
            finally:
                runtime.stop()
                store.close()
