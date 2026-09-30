import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import AOSFault, ErrorCode
from aos.desktop import DesktopRuntime
from aos.desktop_console import create_console


CONTAINER_ID = 'a' * 64
IMAGE_ID = 'sha256:' + 'b' * 64
SOURCE_SHA256 = 'c' * 64
RUNTIME_ID = 'desktop-test'
POINTER = b'X=640\nY=400\nSCREEN=0\nWINDOW=44040196\nWIDTH=1280\nHEIGHT=800\n'


def runtime_with_docker(*, pointer=POINTER, labels=None, running=True, image_id=IMAGE_ID):
    runtime = object.__new__(DesktopRuntime)
    runtime.runtime_id = RUNTIME_ID
    runtime.container_id = CONTAINER_ID
    runtime.pins = {'image_id': IMAGE_ID, 'source_sha256': SOURCE_SHA256}
    inspected = {'Id': CONTAINER_ID, 'Image': image_id, 'State': {'Running': running},
                 'Config': {'Labels': labels if labels is not None else {
                     'com.aos.runtime': RUNTIME_ID, 'com.aos.source-sha256': SOURCE_SHA256}}}

    def docker(arguments, *, timeout):
        if arguments == ['inspect', CONTAINER_ID]:
            return json.dumps([inspected]).encode()
        if arguments == ['exec', CONTAINER_ID, '/usr/bin/xdotool', 'getmouselocation', '--shell',
                         'getdisplaygeometry', '--shell']:
            return pointer
        raise AssertionError('Unexpected Docker command')

    runtime.docker = Mock(side_effect=docker)
    return runtime


class DesktopPointerRuntimeTests(unittest.TestCase):
    def test_fixed_owned_x11_probe_and_bounded_coordinates(self):
        runtime = runtime_with_docker()
        self.assertEqual(runtime.pointer_position(), {'x': 640, 'y': 400, 'width': 1280, 'height': 800})
        self.assertEqual(runtime.docker.call_count, 2)
        self.assertTrue(all(call.kwargs['timeout'] == 3 for call in runtime.docker.call_args_list))

    def test_owner_image_and_running_checks_precede_x11_probe(self):
        for changes in ({'labels': {'com.aos.runtime': 'different', 'com.aos.source-sha256': SOURCE_SHA256}},
                        {'image_id': 'sha256:' + 'd' * 64}, {'running': False}):
            with self.subTest(changes=changes):
                runtime = runtime_with_docker(**changes)
                with self.assertRaises(AOSFault):
                    runtime.pointer_position()
                runtime.docker.assert_called_once()

    def test_invalid_or_out_of_bounds_x11_data_fails_closed(self):
        for payload in (b'X=1280\nY=400\nSCREEN=0\nWINDOW=1\nWIDTH=1280\nHEIGHT=800\n',
                        b'X=-1\nY=0\nSCREEN=0\nWINDOW=1\nWIDTH=1280\nHEIGHT=800\n',
                        b'X=1\nY=2\nSCREEN=0\nWINDOW=1\nWIDTH=1280\n',
                        b'X=1\nX=2\nY=2\nSCREEN=0\nWINDOW=1\nWIDTH=1280\nHEIGHT=800\n',
                        b'X=1\nY=2\nSCREEN=0\nWINDOW=1\nWIDTH=1280\nHEIGHT=800\nSECRET=token\n',
                        b'X=' + b'1' * 140):
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(AOSFault):
                    runtime_with_docker(pointer=payload).pointer_position()


class DesktopPointerAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = runtime_with_docker()
        self.runtime.pointer_position = Mock(return_value={'x': 640, 'y': 400, 'width': 1280, 'height': 800})
        self.runtime.status = Mock(return_value={'running': True})
        self.state = {'session_id': 'session-test', 'runtime_id': RUNTIME_ID, 'generation': 0,
                      'status': 'running', 'owner': 'AGENT'}
        self.controller = SimpleNamespace(runtime=self.runtime, state=lambda: dict(self.state), session_id='session-test')
        self.origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', self.origin, Path('/tmp'))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=self.origin)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def login(self):
        self.assertEqual((await self.client.post('/api/login', json={'token': 'synthetic-token'},
                                                 headers={'Origin': self.origin})).status_code, 200)

    async def test_authentication_and_available_shape(self):
        self.assertEqual((await self.client.get('/api/desktop/pointer')).status_code, 401)
        await self.login()
        self.assertEqual((await self.client.get('/api/desktop/pointer', headers={'Host': 'evil.invalid'})).status_code, 403)
        self.assertEqual((await self.client.get('/api/desktop/pointer?container_id=other')).status_code, 400)
        self.assertTrue((await self.client.get('/api/state')).json()['runtime']['pointer_tracking'])
        response = await self.client.get('/api/desktop/pointer')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({key: response.json()[key] for key in ('available', 'x', 'y', 'width', 'height')},
                         {'available': True, 'x': 640, 'y': 400, 'width': 1280, 'height': 800})
        self.assertIn('sampled_at', response.json())
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('container_id', response.json())

    async def test_stopped_and_non_desktop_runtime_never_probe(self):
        await self.login()
        self.state['status'] = 'stopped'
        self.assertEqual((await self.client.get('/api/desktop/pointer')).json()['reason'], 'desktop_stopped')
        self.runtime.pointer_position.assert_not_called()
        self.state['status'] = 'running'
        self.controller.runtime = SimpleNamespace(runtime_id=RUNTIME_ID)
        self.assertEqual((await self.client.get('/api/desktop/pointer')).json()['reason'], 'runtime_unavailable')
        self.runtime.pointer_position.assert_not_called()

    async def test_runtime_change_or_probe_failure_never_returns_stale_coordinates(self):
        await self.login()
        self.runtime.pointer_position.side_effect = AOSFault(ErrorCode.UNSAFE_ACTION, 'not owned')
        self.assertEqual((await self.client.get('/api/desktop/pointer')).json()['reason'], 'runtime_changed')
        self.runtime.pointer_position.side_effect = AOSFault(ErrorCode.RUNTIME_CRASH, 'unavailable')
        self.assertEqual((await self.client.get('/api/desktop/pointer')).json()['reason'], 'pointer_unavailable')

        def move_generation():
            self.state['generation'] += 1
            return {'x': 640, 'y': 400, 'width': 1280, 'height': 800}

        self.runtime.pointer_position.side_effect = move_generation
        stale = (await self.client.get('/api/desktop/pointer')).json()
        self.assertEqual(stale['reason'], 'runtime_changed')
        self.assertNotIn('x', stale)


if __name__ == '__main__':
    unittest.main()
