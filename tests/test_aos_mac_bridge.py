import asyncio
import unittest
from unittest.mock import patch

from scripts import aos_mac_bridge as bridge_module


class MacBridgeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = bridge_module.MacBridge('100.64.0.1', '100.64.0.2', 8766)

    def request(self, method='GET', target='/ui/', headers=''):
        return self.bridge.request((f'{method} {target} HTTP/1.1\r\nHost: 100.64.0.1:8766\r\n'
                                    + headers + '\r\n').encode())

    def test_host_origin_and_forwarded_headers_are_rebound(self):
        encoded, length, upgraded = self.request('POST', '/api/login/local',
            'Origin: http://100.64.0.1:8766\r\nContent-Length: 2\r\n'
            'Forwarded: for=other\r\nX-Forwarded-For: 127.0.0.1\r\n')
        self.assertIn(b'Host: 127.0.0.1:8765\r\n', encoded)
        self.assertIn(b'Origin: http://127.0.0.1:8765\r\n', encoded)
        self.assertNotIn(b'forwarded', encoded.lower())
        self.assertEqual(length, 2)
        self.assertFalse(upgraded)

    def test_csrf_smuggling_and_unbounded_requests_are_denied(self):
        for method, target, headers in [
            ('POST', '/api/login/local', ''),
            ('GET', '/ui/', 'Origin: https://other.invalid\r\n'),
            ('GET', '/ui/', 'Host: 127.0.0.1:8765\r\n'),
            ('GET', '//other.invalid/', ''),
            ('GET', '/ui/', 'Transfer-Encoding: chunked\r\n'),
            ('GET', '/ui/', 'Content-Length: 1\r\nContent-Length: 1\r\n'),
            ('GET', '/ui/', 'Content-Length: 2097153\r\n'),
            ('GET', '/ui/', ' malformed: folded\r\n'),
            ('GET', '/elsewhere', 'Origin: http://100.64.0.1:8766\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n'),
        ]:
            with self.subTest(method=method, target=target, headers=headers), self.assertRaises(bridge_module.BridgeDenied):
                self.request(method, target, headers)
        encoded, _, _ = self.request('GET', '/ui/', 'Connection: content-length\r\nContent-Length: 1\r\n')
        self.assertIn(b'Content-Length: 1\r\n', encoded)

    def test_only_exact_tailscale_peer_configuration_is_allowed(self):
        for bind, peer, port in [('0.0.0.0', '100.64.0.2', 8766), ('100.64.0.1', '127.0.0.1', 8766),
                                 ('100.64.0.1', '100.64.0.1', 8766), ('100.64.0.1', '100.64.0.2', 8765)]:
            with self.subTest(bind=bind, peer=peer, port=port), self.assertRaises(ValueError):
                bridge_module.MacBridge(bind, peer, port)


class MacBridgeSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = bridge_module.MacBridge('100.64.0.1', '100.64.0.2', 8766)
        self.bridge.peer_address = '127.0.0.1'
        self.headers = []
        self.upstream = await asyncio.start_server(self.upstream_handler, '127.0.0.1', 0)
        self.upstream_port = self.upstream.sockets[0].getsockname()[1]
        self.port_patch = patch.object(bridge_module, 'UPSTREAM_PORT', self.upstream_port)
        self.port_patch.start()
        self.server = await asyncio.start_server(self.bridge.handle, '127.0.0.1', 0, limit=bridge_module.HEADER_LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        self.upstream.close()
        await self.server.wait_closed()
        await self.upstream.wait_closed()
        for _attempt in range(100):
            if not self.bridge.active_connections:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.bridge.active_connections, 0)
        self.port_patch.stop()

    async def upstream_handler(self, reader, writer):
        try:
            headers = await reader.readuntil(b'\r\n\r\n')
            self.headers.append(headers)
            if b'/websockify' in headers:
                writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nfirst-frame')
                await writer.drain()
                content = await reader.readexactly(4)
                writer.write(content)
            else:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nSet-Cookie: synthetic=1; HttpOnly\r\n\r\nOK')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def connect(self, request):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(request)
        await writer.drain()
        return reader, writer

    async def test_http_preserves_cookie_and_does_not_forward_pipelined_second_request(self):
        reader, writer = await self.connect(b'GET /ui/ HTTP/1.1\r\nHost: 100.64.0.1:8766\r\n\r\n'
                                            b'POST /api/tasks HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n')
        response = await asyncio.wait_for(reader.read(), 3)
        self.assertIn(b'200 OK', response)
        self.assertIn(b'Set-Cookie: synthetic=1; HttpOnly', response)
        self.assertEqual(len(self.headers), 1)
        self.assertNotIn(b'/api/tasks', self.headers[0])
        writer.close()
        await writer.wait_closed()

    async def test_websocket_tunnel_preserves_buffered_first_frame_and_bidirectional_bytes(self):
        reader, writer = await self.connect(b'GET /websockify HTTP/1.1\r\nHost: 100.64.0.1:8766\r\n'
            b'Origin: http://100.64.0.1:8766\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n')
        headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        self.assertIn(b'101 Switching Protocols', headers)
        self.assertEqual(await reader.readexactly(11), b'first-frame')
        writer.write(b'ping')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 3), b'ping')
        self.assertIn(b'Origin: http://127.0.0.1:', self.headers[0])
        writer.close()
        await writer.wait_closed()

    async def test_foreign_peer_and_capacity_are_rejected_before_upstream(self):
        for peer, active, expected in [('100.64.0.2', 0, b'403'), ('127.0.0.1', 24, b'503')]:
            self.bridge.peer_address = peer
            self.bridge.active_connections = active
            reader, writer = await self.connect(b'')
            response = await asyncio.wait_for(reader.read(), 3)
            self.assertIn(expected, response)
            self.assertEqual(self.headers, [])
            writer.close()
            await writer.wait_closed()
        self.bridge.active_connections = 0


if __name__ == '__main__':
    unittest.main()
