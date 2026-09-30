import http.client
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import AOSFault, ErrorCode, REPO_ROOT
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.desktop_mcp_bundle import read_bundle, unpack_bundle
from test_desktop_mcp import worker_namespace


def guard_child(connection):
    namespace = worker_namespace()
    server = namespace['HTTPServer'](('127.0.0.1', 0), namespace['FixtureHandler'])
    try:
        evidence = namespace['install_fixture_tcp_guard'](server.server_port)
        connection.send(evidence)
    except BaseException as error:
        connection.send((type(error).__name__, str(error)))
    finally:
        server.server_close()
        connection.close()


class MCPRequestGuardTests(unittest.TestCase):
    def setUp(self):
        self.worker = worker_namespace()

    def test_fixture_tools_reject_standard_mode_or_unattested_mode(self):
        desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id=None)
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
        runtime.evidence = {}
        runtime.owned_desktop = Mock()
        with self.assertRaises(AOSFault) as caught:
            runtime.perform('browser.fixture.open', {})
        self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'), fixture_request_guard=True)
        runtime.evidence = {'fixture_request_guard': {'kind': 'landlock_tcp_connect_v1',
                                                      'fixture_port': 32123, 'verified': False}}
        runtime.owned_desktop = Mock()
        with self.assertRaises(AOSFault):
            runtime.perform('browser.fixture.open', {})

    def test_worker_rejects_fixture_without_guard(self):
        browser = self.worker['MCPBrowser']()
        browser.process = Mock()
        browser.tool = Mock()
        with self.assertRaises(self.worker['ProtocolError']):
            browser.fixture_open()
        browser.tool.assert_not_called()

    def test_pinned_fixture_port_conflict_fails_before_mcp_launch(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            browser = self.worker['MCPBrowser'](fixture_request_guard=True, fixture_port=port)
            with self.assertRaises(OSError):
                browser.start(Path('/unused'), '<html></html>')
            self.assertIsNone(browser.process)

    def test_pinned_port_requires_a_guarded_runtime(self):
        desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id=None)
        for options in ({'fixture_port': 32123},
                        {'fixture_request_guard': True, 'fixture_port': True},
                        {'fixture_request_guard': True, 'fixture_port': 80}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DesktopMCPBrowserRuntime(desktop, Path('synthetic'), **options)
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'), fixture_request_guard=True,
                                           staging_workflow=True, fixture_port=32123)
        self.assertEqual(runtime.fixture_port, 32123)

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the pinned MCP page gate')
    def test_page_route_gate_rejects_off_scope_before_continue(self):
        script = '''const source = process.argv[1];
const loaded = {exports: {}};
new Function('module', source)(loaded);
let handler;
const page = {route: async (pattern, callback) => {
  if (pattern !== '**/*') throw new Error('pattern');
  handler = callback;
}};
const decisions = [];
(async () => {
  await loaded.exports.default({page});
  for (const [method, url] of JSON.parse(process.argv[2])) {
    await handler({request: () => ({method: () => method, url: () => url}),
                   abort: async () => decisions.push('abort'),
                   continue: async () => decisions.push('continue')});
  }
  process.stdout.write(JSON.stringify(decisions));
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        origin = 'http://127.0.0.1:32123'
        cases = [('GET', origin + '/start'), ('GET', origin + '/details'),
                 ('GET', origin + '/start?next=details'), ('GET', 'http://other.invalid/start'),
                 ('POST', origin + '/start'), ('GET', origin + '/style.css')]
        result = subprocess.run(['node', '-e', script,
            self.worker['fixture_page_gate_source'](origin, False), json.dumps(cases)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ['continue', 'continue', 'abort', 'abort', 'abort', 'abort'])
        staging_cases = [('GET', origin + '/app'), ('GET', origin + '/draft'),
                         ('POST', origin + '/submit'), ('GET', origin + '/receipt'),
                         ('POST', origin + '/draft'), ('GET', origin + '/submit')]
        result = subprocess.run(['node', '-e', script,
            self.worker['fixture_page_gate_source'](origin, True), json.dumps(staging_cases)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ['continue', 'continue', 'continue', 'continue', 'abort', 'abort'])

    @unittest.skipUnless(shutil.which('node') and
                         (REPO_ROOT / 'models/desktop-mcp-v001/manifest.json').is_file() and
                         (REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell').is_file(),
                         'Pinned Node, MCP bundle and Chromium are required')
    def test_pinned_chromium_page_route_blocks_off_route_before_fixture_server(self):
        server = self.fixture_server()
        origin = f'http://127.0.0.1:{server.server_port}'
        script = '''const {chromium} = require(process.argv[1]);
const loaded = {exports: {}};
new Function('module', process.argv[2])(loaded);
(async () => {
  const browser = await chromium.launch({executablePath: process.argv[3], headless: true,
                                         args: ['--no-sandbox']});
  try {
    const page = await browser.newPage();
    await loaded.exports.default({page});
    const allowed = await page.goto(process.argv[4] + '/start');
    let blocked = false;
    try {await page.goto(process.argv[4] + '/outside');}
    catch (error) {blocked = error.message.includes('ERR_BLOCKED_BY_CLIENT');}
    process.stdout.write(JSON.stringify({status: allowed.status(), blocked}));
  } finally {await browser.close();}
})().catch(error => {process.stderr.write(error.message); process.exitCode = 1;});'''
        with tempfile.TemporaryDirectory() as directory:
            _pins, payload = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
            unpack_bundle(payload, Path(directory) / 'node_modules')
            command = ['node', '-e', script, str(Path(directory) / 'node_modules/playwright-core'),
                       self.worker['fixture_page_gate_source'](origin, False),
                       str(REPO_ROOT / 'models/playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'),
                       origin]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 200, 'blocked': True})
        self.assertEqual(server.fixture_requests, 1)

    def test_staging_proxy_requires_absolute_post_and_preserves_one_submission(self):
        server = self.fixture_server()
        server.staging_mode = True
        server.proxy_mode = True
        origin = f'http://127.0.0.1:{server.server_port}'
        body = self.worker['STAGING_FORM_BODY']
        headers = {'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'}
        self.assertEqual(self.fixture_request(server, 'GET', origin + '/app')[0], 200)
        self.assertEqual(self.fixture_request(server, 'GET', origin + '/draft')[0], 200)
        for path in ('/submit', origin + '/submit?extra=1', 'http://other.invalid/submit',
                     'https://127.0.0.1:' + str(server.server_port) + '/submit'):
            with self.subTest(path=path):
                status, response_headers, response_body = self.fixture_request(
                    server, 'POST', path, headers=headers, body=body)
                self.assertEqual(status, 404)
                self.assertEqual(response_body, b'')
                self.assertNotIn('location', {name.lower() for name, _value in response_headers})
        status, response_headers, response_body = self.fixture_request(
            server, 'POST', origin + '/submit', headers=headers, body=body)
        self.assertEqual(status, 303)
        self.assertEqual(dict(response_headers)['Location'], origin + '/receipt')
        self.assertEqual(response_body, b'')
        self.assertEqual(self.fixture_request(server, 'POST', origin + '/submit',
                                              headers=headers, body=body)[0], 404)
        self.assertEqual(self.fixture_request(server, 'GET', '/receipt')[0], 404)
        self.assertEqual(self.fixture_request(server, 'GET', origin + '/receipt')[0], 200)
        self.assertEqual(server.staging_submissions, 1)

    def test_invalid_port_and_unsupported_architecture_fail_before_syscall(self):
        for port in (0, 65536, True, '1234'):
            with self.subTest(port=port), self.assertRaises(self.worker['ProtocolError']):
                self.worker['install_fixture_tcp_guard'](port)
        with patch.object(self.worker['os'], 'uname', return_value=SimpleNamespace(machine='unsupported')):
            with self.assertRaises(self.worker['ProtocolError']):
                self.worker['install_fixture_tcp_guard'](1234)

    def fixture_server(self):
        server = self.worker['HTTPServer'](('127.0.0.1', 0), self.worker['FixtureHandler'])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def fixture_request(server, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def test_fixture_http_policy_allows_only_exact_get_routes_and_host(self):
        server = self.fixture_server()
        for route, expected in (('/start', b'Synthetic start'),
                                ('/details', b'Synthetic details')):
            status, headers, body = self.fixture_request(server, 'GET', route)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertNotIn('location', {name.lower() for name, value in headers})
        for method, path, headers, body in (
                ('POST', '/start', {}, b'x'), ('HEAD', '/start', {}, None),
                ('PUT', '/details', {}, b'x'), ('GET', '/redirect', {}, None),
                ('GET', '/start?next=/details', {}, None),
                ('GET', '/favicon.ico', {}, None), ('GET', '/style.css', {}, None),
                ('GET', f'http://127.0.0.1:{server.server_port}/start', {}, None),
                ('GET', '/start', {'Host': 'example.invalid'}, None),
                ('GET', '/start', {'Content-Length': '0'}, None),
                ('GET', '/start', {'Transfer-Encoding': 'chunked'}, None)):
            with self.subTest(method=method, path=path, headers=headers):
                status, response_headers, response_body = self.fixture_request(
                    server, method, path, headers, body)
                self.assertNotEqual(status, 200)
                self.assertEqual(response_body, b'')
                self.assertNotIn('location', {name.lower() for name, value in response_headers})

    def test_pinned_proxy_requires_absolute_exact_origin_and_route(self):
        server = self.fixture_server()
        server.proxy_mode = True
        origin = f'http://127.0.0.1:{server.server_port}'
        for path in (origin + '/start', origin + '/details'):
            with self.subTest(path=path):
                status, _headers, body = self.fixture_request(server, 'GET', path)
                self.assertEqual(status, 200)
                self.assertIn(b'Synthetic', body)
        for path in ('/start', origin + '/start?next=/details',
                     origin + '/favicon.ico', 'http://other.invalid/start',
                     'https://127.0.0.1:' + str(server.server_port) + '/start'):
            with self.subTest(path=path):
                status, _headers, body = self.fixture_request(server, 'GET', path)
                self.assertEqual(status, 404)
                self.assertEqual(body, b'')

    def test_fixture_http_policy_rejects_duplicate_host(self):
        server = self.fixture_server()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        try:
            connection.putrequest('GET', '/start', skip_host=True)
            host = f'127.0.0.1:{server.server_port}'
            connection.putheader('Host', host)
            connection.putheader('Host', host)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            self.assertEqual(response.read(), b'')
        finally:
            connection.close()

    def test_fixture_http_policy_closes_after_request_budget(self):
        server = self.fixture_server()
        for request_number in range(self.worker['FIXTURE_REQUEST_LIMIT']):
            with self.subTest(request_number=request_number):
                status, headers, body = self.fixture_request(server, 'GET', '/favicon.ico')
                self.assertEqual(status, 404)
                self.assertEqual(body, b'')
        self.assertEqual(server.fixture_requests, self.worker['FIXTURE_REQUEST_LIMIT'])
        self.assertEqual(getattr(server, 'fixture_response_bytes', 0), 0)
        with self.assertRaises(http.client.RemoteDisconnected):
            self.fixture_request(server, 'GET', '/start')

    def test_proxy_budget_stays_bounded_despite_background_connections(self):
        server = self.fixture_server()
        server.proxy_mode = True
        for request_number in range(self.worker['FIXTURE_PROXY_REQUEST_LIMIT']):
            with self.subTest(request_number=request_number):
                status, _headers, body = self.fixture_request(
                    server, 'GET', 'http://unapproved.invalid/background')
                self.assertEqual(status, 404)
                self.assertEqual(body, b'')
        self.assertEqual(server.fixture_requests, self.worker['FIXTURE_PROXY_REQUEST_LIMIT'])
        self.assertEqual(getattr(server, 'fixture_response_bytes', 0), 0)
        with self.assertRaises(http.client.RemoteDisconnected):
            self.fixture_request(server, 'GET', f'http://127.0.0.1:{server.server_port}/start')

    def test_fixture_http_policy_stops_serving_after_response_byte_budget(self):
        server = self.fixture_server()
        statuses = [self.fixture_request(server, 'GET', '/start')[0]
                    for request_number in range(self.worker['FIXTURE_REQUEST_LIMIT'])]
        self.assertIn(200, statuses)
        self.assertIn(429, statuses)
        self.assertLessEqual(server.fixture_response_bytes, self.worker['FIXTURE_RESPONSE_BYTE_LIMIT'])

    def test_incomplete_request_cannot_hold_fixture_server_indefinitely(self):
        server = self.fixture_server()
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=2) as stalled:
            stalled.settimeout(3)
            stalled.sendall(f'GET /start HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n'.encode())
            self.assertEqual(stalled.recv(1), b'')
        status, headers, body = self.fixture_request(server, 'GET', '/start')
        self.assertEqual(status, 200)
        self.assertIn(b'Synthetic start', body)
        self.assertEqual(server.fixture_requests, 2)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'Fixture Landlock syscall numbers target x86_64')
    def test_real_kernel_guard_allows_only_fixture_tcp_port_in_child(self):
        context = multiprocessing.get_context('fork')
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=guard_child, args=(sender,))
        child.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(5))
            evidence = receiver.recv()
            self.assertEqual(evidence['kind'], 'landlock_tcp_connect_v1')
            self.assertTrue(evidence['verified'])
            self.assertIs(type(evidence['fixture_port']), int)
        finally:
            receiver.close()
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
        self.assertEqual(child.exitcode, 0)


if __name__ == '__main__':
    unittest.main()
