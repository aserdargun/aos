from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.desktop_console import create_console


class LocalUiLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.origin = 'http://127.0.0.1:8765'
        self.controller = Mock()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.directory.cleanup()

    def client(self, enabled=False, peer='127.0.0.1', origin=None):
        origin = origin or self.origin
        app = create_console(self.controller, 'synthetic-token', origin, self.root,
                             web_profiles_root=self.root / 'profiles', local_ui_auto_login=enabled)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
                                   base_url=origin, headers={'Origin': origin})
        self.clients.append(client)
        return client

    async def test_default_keeps_token_login_and_existing_session_contract(self):
        client = self.client()
        self.assertEqual((await client.get('/api/session')).json(), {'authenticated': False})
        self.assertEqual((await client.post('/api/login/local', json={})).status_code, 403)
        self.assertEqual((await client.get('/api/retention')).status_code, 401)
        response = await client.post('/api/login', json={'token': 'synthetic-token'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await client.get('/api/session')).json(), {'authenticated': True})

    async def test_opt_in_bootstraps_same_cookie_without_any_controller_effect(self):
        for fetch_site in (None, 'same-origin', 'none'):
            client = self.client(True)
            self.assertEqual((await client.get('/api/session')).json(),
                             {'authenticated': False, 'local_auto_login': True})
            self.assertEqual((await client.get('/api/retention')).status_code, 401)
            headers = {'Sec-Fetch-Site': fetch_site} if fetch_site else {}
            response = await client.post('/api/login/local', json={}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'authenticated': True})
            cookie = response.headers['set-cookie']
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=strict', cookie)
            self.assertIn('Path=/', cookie)
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual((await client.get('/api/retention')).status_code, 200)
            self.assertEqual((await client.get('/api/session')).json(),
                             {'authenticated': True, 'local_auto_login': True})
        self.assertEqual(self.controller.mock_calls, [])

    async def test_host_origin_fetch_site_and_peer_fail_closed(self):
        cases = [({'Host': 'evil.example'}, '127.0.0.1'),
                 ({'Origin': 'http://evil.example'}, '127.0.0.1'),
                 ({'Origin': 'null'}, '127.0.0.1'),
                 ({'Sec-Fetch-Site': 'cross-site'}, '127.0.0.1'),
                 ({'Sec-Fetch-Site': 'same-site'}, '127.0.0.1'),
                 ({'X-Forwarded-For': '127.0.0.1'}, '192.0.2.1'),
                 ({}, 'localhost')]
        for headers, peer in cases:
            with self.subTest(headers=headers, peer=peer):
                client = self.client(True, peer=peer)
                response = await client.post('/api/login/local', json={}, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn('set-cookie', response.headers)
        client = self.client(True)
        del client.headers['Origin']
        self.assertEqual((await client.post('/api/login/local', json={})).status_code, 403)
        self.assertEqual(self.controller.mock_calls, [])

    async def test_exact_bounded_json_and_no_queries(self):
        client = self.client(True)
        cases = [('/api/login/local?consent=true', '{}', 'application/json', 400),
                 ('/api/login/local', '{"extra":true}', 'application/json', 400),
                 ('/api/login/local', '{"extra":1,"extra":2}', 'application/json', 400),
                 ('/api/login/local', '[]', 'application/json', 400),
                 ('/api/login/local', 'null', 'application/json', 400),
                 ('/api/login/local', '', 'application/json', 400),
                 ('/api/login/local', '{', 'application/json', 400),
                 ('/api/login/local', ' ' * 127 + '{}', 'application/json', 413),
                 ('/api/login/local', '{}', 'text/plain', 415)]
        for path, payload, content_type, status in cases:
            with self.subTest(path=path, payload=payload):
                response = await client.post(path, content=payload, headers={'Content-Type': content_type})
                self.assertEqual(response.status_code, status)
                self.assertNotIn('set-cookie', response.headers)
        self.assertEqual((await client.get('/api/session')).json()['authenticated'], False)
        self.assertEqual((await client.get('/api/login/local')).status_code, 405)
        self.assertEqual(self.controller.mock_calls, [])

    async def test_duplicate_boundary_headers_are_rejected(self):
        client = self.client(True)
        for name, first, second in (('Origin', self.origin, 'http://evil.example'),
                                     ('Host', '127.0.0.1:8765', 'evil.example'),
                                     ('Sec-Fetch-Site', 'same-origin', 'cross-site')):
            response = await client.post('/api/login/local', json={},
                                         headers=[(name, first), (name, second)])
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('set-cookie', response.headers)

    async def test_opt_in_rejects_noncanonical_or_external_origin(self):
        for origin in ('http://localhost:8765', 'https://127.0.0.1:8765', 'http://192.0.2.1:8765',
                       'http://127.0.0.1:8765/', 'http://127.0.0.1:8765?query=1',
                       'http://user@127.0.0.1:8765', 'http://127.0.0.1:65536'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.client(True, origin=origin)
