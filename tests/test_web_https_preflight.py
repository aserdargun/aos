import contextlib
import asyncio
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx
import jsonschema
from pydantic import ValidationError

from aos.contracts import REPO_ROOT, canonical, digest
from aos.desktop_console import create_console
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import (WebRuntimePin, WebTaskContract, bind_web_task,
                                         plan_web_readonly_routes)
from aos.web_https_preflight import (_probe, _probe_bound, _public_addresses, main,
                                     _dns_slot, WebBoundHTTPSPreflightReport,
                                     WebHTTPSPreflightReport, WebHTTPSReadOnlyRouteReport,
                                     fetch_web_readonly_route)
from aos.web_static_assets import (MAX_STATIC_ASSET_BYTES, WebStaticAssetFetchReport,
                                   WebStaticAssetPlan,
                                   fetch_web_static_asset, plan_web_static_assets,
                                   verify_web_static_assets)
from aos.web_readonly_data import (WebReadOnlyDataBundlePlan,
                                   fetch_web_readonly_bundle_asset,
                                   fetch_web_readonly_data,
                                   plan_web_readonly_data_bundle,
                                   verify_web_readonly_data_bundle)


class EntryHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'<html>synthetic entry</html>'
    content_type = 'text/html; charset=utf-8'

    def do_GET(self):
        self.server.seen_paths.append(self.path)
        asset_response = getattr(self.server, 'asset_responses', {}).get(self.path)
        if asset_response is not None:
            status, content_type, body = asset_response
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass
            return
        if self.server.header_byte_delay:
            try:
                self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX-Slow: ')
                for _attempt in range(30):
                    self.connection.sendall(b'a')
                    time.sleep(self.server.header_byte_delay)
                self.connection.sendall(b'\r\nContent-Length: 0\r\n\r\n')
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                pass
            return
        self.send_response(self.status)
        self.send_header('Content-Type', self.content_type)
        self.send_header('Content-Length', str(len(self.body)))
        if self.status == 302:
            self.send_header('Location', 'https://other.invalid/')
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        pass


class WebHTTPSPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.console_assets = self.root / 'console-assets'
        self.console_assets.mkdir()
        self.host = 'www.aos-preflight.invalid'
        certificate = self.root / 'certificate.pem'
        key = self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-subj', '/CN=' + self.host,
                        '-addext', 'subjectAltName=DNS:' + self.host,
                        '-keyout', str(key), '-out', str(certificate)],
                       check=True, capture_output=True)
        self.tls_context = ssl.create_default_context(cafile=str(certificate))
        self.server = HTTPServer(('127.0.0.1', 0), EntryHandler)
        self.server.seen_paths = []
        self.server.header_byte_delay = 0
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(certificate), str(key))
        self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
        origin = f'https://{self.host}:{self.server.server_port}'
        self.profile = WebApplicationProfile.model_validate({**source,
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        self.checksum = profile_report(self.profile).profile_sha256
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profiles.register(self.profile, confirm_sha256=self.checksum)
        self.original_create_connection = socket.create_connection
        binding_fixture = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())
        task = WebTaskContract.model_validate({**binding_fixture['task'],
            'profile_sha256': self.checksum, 'entry_url': self.profile.entry_url,
            'allowed_origins': self.profile.allowed_origins})
        runtime = WebRuntimePin.model_validate(binding_fixture['runtime'])
        self.draft = bind_web_task(self.profiles, task, runtime)

    def probe(self, *, context=None):
        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            return _probe(self.profiles, self.checksum, self.checksum,
                          tls_context=context or self.tls_context)

    def probe_bound(self, draft=None, confirm=None):
        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            return _probe_bound(self.profiles, self.checksum, self.checksum,
                                draft or self.draft, confirm or self.draft.binding_sha256,
                                tls_context=self.tls_context)

    def test_exact_readonly_json_bundle_plan_and_host_fetch(self):
        origin = self.profile.allowed_origins[0]
        asset_url = origin + '/assets/app.js'
        data_url = origin + '/api/summary'
        self.server.asset_responses = {
            '/assets/app.js': (200, 'application/javascript', b'window.ready=true;'),
            '/api/summary': (200, 'application/json; charset=utf-8', b'{"status":"ready"}')}
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': asset_url, 'content_type': 'application/javascript'}],
            [{'url': data_url, 'content_type': 'application/json'}])
        self.assertEqual(WebReadOnlyDataBundlePlan.model_validate(plan.model_dump()), plan)
        self.assertEqual(verify_web_readonly_data_bundle(self.profiles, self.draft.task, plan), plan)
        plan_sha256 = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_readonly_data(self.profiles, self.draft.task, plan,
                                        '0' * 64, 0, tls_context=self.tls_context)
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_readonly_data(self.profiles, self.draft.task, plan,
                                        plan_sha256, True, tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, [])
            asset_report, asset = fetch_web_readonly_bundle_asset(
                self.profiles, self.draft.task, plan, plan_sha256, 0,
                tls_context=self.tls_context)
            data_report, data = fetch_web_readonly_data(
                self.profiles, self.draft.task, plan, plan_sha256, 0,
                tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, ['/assets/app.js', '/api/summary'])
            self.assertEqual(asset, b'window.ready=true;')
            self.assertEqual(data, b'{"status":"ready"}')
            self.assertEqual(asset_report.plan_sha256, plan_sha256)
            self.assertEqual(data_report.plan_sha256, plan_sha256)
            self.assertFalse(data_report.training_ready)
            self.assertNotIn('"status":"ready"', data_report.model_dump_json())
            self.server.asset_responses['/api/summary'] = (200, 'text/html', b'<html></html>')
            with self.assertRaisesRegex(ValueError, 'content_type_mismatch'):
                fetch_web_readonly_data(self.profiles, self.draft.task, plan,
                                        plan_sha256, 0, tls_context=self.tls_context)
            self.server.asset_responses['/api/summary'] = (200, 'application/json',
                                                           b'x' * 262145)
            with self.assertRaisesRegex(ValueError, 'response_length_limit'):
                fetch_web_readonly_data(self.profiles, self.draft.task, plan,
                                        plan_sha256, 0, tls_context=self.tls_context)
        for resource in (origin + '/api/summary?view=1&view=2',
                         'https://other.invalid/api/summary',
                         asset_url, self.profile.entry_url):
            with self.assertRaises(ValueError):
                plan_web_readonly_data_bundle(
                    self.profiles, self.draft.task,
                    [{'url': asset_url, 'content_type': 'application/javascript'}],
                    [{'url': resource, 'content_type': 'application/json'}])
        with self.assertRaises(ValueError):
            plan_web_readonly_data_bundle(
                self.profiles, self.draft.task,
                [{'url': asset_url, 'content_type': 'application/javascript'}],
                [{'url': data_url}, {'url': data_url}])

    def test_canonical_readonly_json_query_is_sent_exactly_once(self):
        origin = self.profile.allowed_origins[0]
        asset_url = origin + '/assets/app.js'
        data_url = origin + '/api/summary?view=compact&page=1'
        self.server.asset_responses = {
            '/api/summary?view=compact&page=1': (200, 'application/json', b'{"ok":true}')}
        plan = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task,
            [{'url': asset_url, 'content_type': 'application/javascript'}],
            [{'url': data_url}])
        plan_sha256 = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_readonly_data(self.profiles, self.draft.task, plan,
                                        '0' * 64, 0, tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, [])
            report, body = fetch_web_readonly_data(
                self.profiles, self.draft.task, plan, plan_sha256, 0,
                tls_context=self.tls_context)
        self.assertEqual(self.server.seen_paths, ['/api/summary?view=compact&page=1'])
        self.assertEqual(body, b'{"ok":true}')
        self.assertEqual(report.request_sha256, digest({'method': 'GET', 'url': data_url}))
        for invalid in ('?', '?view=1&view=2', '?view=1&', '?view=hello+world',
                        '?view=%2f', '?view=%GG', '?view=1#fragment',
                        '?view=1&' * 8 + 'extra=1'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                plan_web_readonly_data_bundle(
                    self.profiles, self.draft.task,
                    [{'url': asset_url, 'content_type': 'application/javascript'}],
                    [{'url': origin + '/api/summary' + invalid}])

    def test_exact_static_js_css_plan_and_host_fetch(self):
        asset_urls = [self.profile.allowed_origins[0] + '/assets/app.js',
                      self.profile.allowed_origins[0] + '/assets/site.css']
        self.server.asset_responses = {
            '/assets/app.js': (200, 'application/javascript; charset=utf-8',
                               b'document.title="Synthetic app";'),
            '/assets/site.css': (200, 'text/css', b'body{color:blue}')}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': asset_urls[0], 'content_type': 'application/javascript'},
            {'url': asset_urls[1], 'content_type': 'text/css'}])
        self.assertEqual(verify_web_static_assets(self.profiles, self.draft.task, plan), plan)
        plan_sha256 = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       '0' * 64, 0, tls_context=self.tls_context)
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       plan_sha256, True, tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, [])
            script_report, script = fetch_web_static_asset(
                self.profiles, self.draft.task, plan, plan_sha256, 0,
                tls_context=self.tls_context)
            style_report, style = fetch_web_static_asset(
                self.profiles, self.draft.task, plan, plan_sha256, 1,
                tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, ['/assets/app.js', '/assets/site.css'])
            self.assertEqual(script, b'document.title="Synthetic app";')
            self.assertEqual(style, b'body{color:blue}')
            self.assertEqual(script_report.content_type, 'application/javascript')
            self.assertEqual(script_report.plan_sha256, plan_sha256)
            self.assertEqual(style_report.asset_index, 1)
            self.assertFalse(script_report.browser_connected)
            self.assertFalse(script_report.execution_authorized)
            self.assertNotIn('Synthetic app', script_report.model_dump_json())
            self.server.asset_responses['/assets/app.js'] = (200, 'text/html', b'wrong mime')
            with self.assertRaisesRegex(ValueError, 'content_type_mismatch'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       plan_sha256, 0, tls_context=self.tls_context)
            self.server.asset_responses['/assets/app.js'] = (302, 'application/javascript', b'')
            with self.assertRaisesRegex(ValueError, 'not_plain_success'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       plan_sha256, 0, tls_context=self.tls_context)
            self.server.asset_responses['/assets/app.js'] = (
                200, 'application/javascript', b'x' * (MAX_STATIC_ASSET_BYTES + 1))
            with self.assertRaisesRegex(ValueError, 'length_limit'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       plan_sha256, 0, tls_context=self.tls_context)

    def test_static_asset_scope_rejects_cross_origin_duplicate_and_authority(self):
        origin = self.profile.allowed_origins[0]
        asset = {'url': origin + '/assets/app.js', 'content_type': 'application/javascript'}
        for invalid in ([asset, asset],
                        [{**asset, 'url': 'https://other.example.invalid/app.js'}],
                        [{**asset, 'url': origin + '/assets/app.js?version=1&version=2'}],
                        [{**asset, 'url': origin + '/assets/app.js?version=%0A'}],
                        [{**asset, 'url': self.profile.entry_url}],
                        [{**asset, 'url': self.profile.entry_url + '?v=1'}],
                        [{**asset, 'content_type': 'text/html'}]):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, ValidationError)):
                plan_web_static_assets(self.profiles, self.draft.task, invalid)
        plan = plan_web_static_assets(self.profiles, self.draft.task, [asset])
        with self.assertRaises(ValidationError):
            WebStaticAssetPlan.model_validate({**plan.model_dump(), 'execution_authorized': True})
        with self.assertRaises((ValueError, ValidationError)):
            verify_web_static_assets(self.profiles, self.draft.task,
                                     WebStaticAssetPlan.model_validate({**plan.model_dump(),
                                         'task_sha256': '0' * 64}))

    def test_exact_static_asset_queries_reach_host_and_bind_request_hash(self):
        origin = self.profile.allowed_origins[0]
        urls = [origin + '/assets/app.js?v=1&theme=dark',
                origin + '/assets/site.css?v=2',
                origin + '/assets/logo.png?v=3']
        bodies = [b'document.title="Ready";', b'body{color:blue}', b'\x89PNG\r\n\x1a\nsynthetic']
        types = ['application/javascript', 'text/css', 'image/png']
        self.server.asset_responses = {
            url.removeprefix(origin): (200, content_type, body)
            for url, content_type, body in zip(urls, types, bodies)}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': url, 'content_type': content_type}
            for url, content_type in zip(urls, types)])
        checksum = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       '0' * 64, 0, tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, [])
            for index, (url, body) in enumerate(zip(urls, bodies)):
                report, fetched = fetch_web_static_asset(
                    self.profiles, self.draft.task, plan, checksum, index,
                    tls_context=self.tls_context)
                self.assertEqual(fetched, body)
                self.assertEqual(report.request_sha256, digest({'method': 'GET', 'url': url}))
            self.assertEqual(self.server.seen_paths, [url.removeprefix(origin) for url in urls])

        bundle = plan_web_readonly_data_bundle(
            self.profiles, self.draft.task, [
                {'url': urls[0], 'content_type': types[0]}],
            [{'url': origin + '/api/summary?view=compact'}])
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            report, fetched = fetch_web_readonly_bundle_asset(
                self.profiles, self.draft.task, bundle, digest(bundle.model_dump()), 0,
                tls_context=self.tls_context)
        self.assertEqual(fetched, bodies[0])
        self.assertEqual(report.request_sha256, digest({'method': 'GET', 'url': urls[0]}))

    def test_exact_image_asset_plan_and_binary_host_fetch(self):
        origin = self.profile.allowed_origins[0]
        image = b'\x89PNG\r\n\x1a\nsynthetic-image-bytes'
        self.server.asset_responses = {
            '/assets/logo.png': (200, 'image/png', image)}
        plan = plan_web_static_assets(self.profiles, self.draft.task, [
            {'url': origin + '/assets/logo.png', 'content_type': 'image/png'}])
        checksum = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection):
            report, body = fetch_web_static_asset(
                self.profiles, self.draft.task, plan, checksum, 0,
                tls_context=self.tls_context)
            self.assertEqual(body, image)
            self.assertEqual(report.response_sha256, hashlib.sha256(image).hexdigest())
            self.assertEqual(report.content_type, 'image/png')
            self.assertEqual(self.server.seen_paths, ['/assets/logo.png'])
            self.server.asset_responses['/assets/logo.png'] = (200, 'image/svg+xml', image)
            with self.assertRaisesRegex(ValueError, 'content_type_mismatch'):
                fetch_web_static_asset(self.profiles, self.draft.task, plan,
                                       checksum, 0, tls_context=self.tls_context)
        with self.assertRaises(ValidationError):
            plan_web_static_assets(self.profiles, self.draft.task, [
                {'url': origin + '/assets/logo.svg', 'content_type': 'image/svg+xml'}])

    def test_static_asset_schemas_and_synthetic_binding(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_static_asset_plan.json').read_text())
        self.assertTrue(fixture['synthetic'])
        for model, name, part in ((WebStaticAssetPlan, 'web_static_asset_plan', 'plan'),
                                  (WebStaticAssetFetchReport, 'web_static_asset_fetch', 'report')):
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                             model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(fixture[part])
        self.assertEqual(fixture['report']['plan_sha256'], digest(fixture['plan']))
        self.assertEqual(fixture['report']['request_sha256'], digest({
            'method': 'GET', 'url': fixture['plan']['assets'][0]['url']}))
        self.assertNotIn('Synthetic app', json.dumps(fixture['report']))

    def test_static_image_asset_synthetic_fixture_binds_exact_plan(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_static_image_asset_plan.json').read_text())
        self.assertIs(fixture['synthetic'], True)
        self.assertEqual(fixture['plan']['assets'][0]['content_type'], 'image/png')
        plan = WebStaticAssetPlan.model_validate(fixture['plan'])
        report = WebStaticAssetFetchReport.model_validate(fixture['report'])
        self.assertEqual(report.plan_sha256, digest(plan.model_dump()))
        self.assertEqual(report.request_sha256, digest({
            'method': 'GET', 'url': plan.assets[0].url}))
        for name, value in (('web_static_asset_plan', fixture['plan']),
                            ('web_static_asset_fetch', fixture['report'])):
            schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator(schema).validate(value)

    def test_verified_tls_and_exact_entry_yield_contentless_report(self):
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        self.assertEqual(report.response_bytes, len(EntryHandler.body))
        self.assertTrue(report.tls_hostname_verified)
        self.assertFalse(report.browser_connected)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.collection_authorized)
        self.assertFalse(report.training_ready)
        self.assertNotIn('synthetic entry', report.model_dump_json())
        self.assertNotIn(self.host, report.model_dump_json())

    def test_single_entry_get_reports_only_static_html_counts(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        EntryHandler.body = (b'<html><form><input type="password" value="private-example">'
                             b'<script src="/app.js"></script><script src="https://cdn.example.invalid/app.js"></script>'
                             b'<link rel="stylesheet" href="/app.css"><img src="data:image/png;base64,AA">'
                             b'</form></html>')
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        self.assertEqual(report.entry_html_signals.model_dump(), {
            'form_count': 1, 'post_form_count': 0,
            'cross_origin_form_action_count': 0,
            'unclassified_form_action_count': 0,
            'meta_refresh_count': 0, 'cross_origin_meta_refresh_count': 0,
            'unclassified_meta_refresh_count': 0,
            'script_count': 2, 'stylesheet_count': 1, 'image_count': 1,
            'password_input_count': 1, 'cross_origin_resource_count': 1,
            'unclassified_resource_count': 1, 'static_html_only': True,
            'browser_or_account_verified': False})
        self.assertNotIn('private-example', report.model_dump_json())
        self.assertNotIn('cdn.example.invalid', report.model_dump_json())
        bound = self.probe_bound()
        self.assertEqual(bound.entry_html_signals, report.entry_html_signals)
        self.assertEqual(self.server.seen_paths, ['/entry', '/entry'])

    def test_form_method_and_action_scope_are_static_hints_only(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        EntryHandler.body = (b'<html><base href="https://other.invalid/">'
                             b'<script src="/relative.js"></script>'
                             b'<img src="https://other.invalid/icon.png">'
                             b'<img src="data:image/png;base64,AA">'
                             b'<form method="POST" action="/relative"><input type="text"></form>'
                             b'<form method="post" action="https://other.invalid/save"></form>'
                             b'<form method="GET" action="' + self.profile.allowed_origins[0].encode()
                             + b'/save"></form>'
                             b'<form method="POST" action="javascript:alert(1)"></form>'
                             b'<form method="POST"></form></html>')
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        self.assertEqual(report.entry_html_signals.form_count, 5)
        self.assertEqual(report.entry_html_signals.post_form_count, 4)
        self.assertEqual(report.entry_html_signals.cross_origin_form_action_count, 1)
        self.assertEqual(report.entry_html_signals.unclassified_form_action_count, 2)
        self.assertEqual(report.entry_html_signals.cross_origin_resource_count, 1)
        self.assertEqual(report.entry_html_signals.unclassified_resource_count, 2)
        self.assertFalse(report.browser_connected)
        self.assertNotIn('other.invalid', report.model_dump_json())

    def test_meta_refresh_scope_is_static_and_does_not_follow_navigation(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        EntryHandler.body = (b'<html><meta http-equiv="ReFrEsH" content="0; URL=/next?private=secret">'
                             b'<meta http-equiv=refresh content="1;url=\'https://other.invalid/landing\'">'
                             b'<meta http-equiv=refresh content="2; URL=//other.invalid/next">'
                             b'<meta http-equiv=refresh content="3;url=javascript:alert(1)">'
                             b'<meta http-equiv=refresh content="nope;url=/later">'
                             b'<meta http-equiv=refresh>'
                             b'<meta http-equiv=refresh content="0;url=\'https://other.invalid/missing">'
                             b'<meta http-equiv=refresh content="0;url='
                             + self.profile.allowed_origins[0].encode() + b'/same"></html>')
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        self.assertEqual(report.entry_html_signals.meta_refresh_count, 8)
        self.assertEqual(report.entry_html_signals.cross_origin_meta_refresh_count, 2)
        self.assertEqual(report.entry_html_signals.unclassified_meta_refresh_count, 4)
        self.assertNotIn('other.invalid', report.model_dump_json())
        self.assertNotIn('private=secret', report.model_dump_json())

    def test_meta_refresh_relative_reference_with_base_is_unclassified(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        EntryHandler.body = (b'<html><base href="https://other.invalid/">'
                             b'<meta http-equiv=refresh content="0;url=/relative">'
                             b'<meta http-equiv=refresh content="1;url='
                             + self.profile.allowed_origins[0].encode()
                             + b'/same"><meta http-equiv=refresh content="2;url=https://other.invalid/next">'
                             b'</html>')
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        self.assertEqual(report.entry_html_signals.meta_refresh_count, 3)
        self.assertEqual(report.entry_html_signals.cross_origin_meta_refresh_count, 1)
        self.assertEqual(report.entry_html_signals.unclassified_meta_refresh_count, 1)

    def test_html_reference_scope_normalizes_origin_and_rejects_ambiguous_authority(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        origin = self.profile.allowed_origins[0]
        same_origin_alias = origin.replace('https://', 'HTTPS://').replace(self.host, self.host.upper())
        EntryHandler.body = (b'<html><script src="' + same_origin_alias.encode() + b'/app.js"></script>'
                             b'<script src=""></script>'
                             b'<img src="\x00https://other.invalid/icon.png">'
                             b'<img src="https://other.invalid:bad/icon.png">'
                             b'<img src="https://other.invalid:0/icon.png">'
                             b'<form action="https://user:pass@other.invalid/save"></form>'
                             b'<form action="https://%65vil.invalid/save"></form>'
                             b'<form action="http://' + origin.removeprefix('https://').encode()
                             + b'/save"></form>'
                             b'<meta http-equiv=refresh content="0;url='
                             + same_origin_alias.encode() + b'/next">'
                             b'<meta http-equiv=refresh content="0;url=\x00https://other.invalid/next">'
                             b'</html>')
        report = self.probe()
        self.assertEqual(self.server.seen_paths, ['/entry'])
        signals = report.entry_html_signals
        self.assertEqual((signals.cross_origin_resource_count,
                          signals.unclassified_resource_count), (0, 4))
        self.assertEqual((signals.cross_origin_form_action_count,
                          signals.unclassified_form_action_count), (1, 2))
        self.assertEqual((signals.meta_refresh_count,
                          signals.cross_origin_meta_refresh_count,
                          signals.unclassified_meta_refresh_count), (2, 0, 1))
        self.assertNotIn('other.invalid', report.model_dump_json())
        self.assertNotIn('user:pass', report.model_dump_json())

    def test_non_utf8_entry_keeps_transport_but_no_html_claim(self):
        original = EntryHandler.body
        self.addCleanup(setattr, EntryHandler, 'body', original)
        EntryHandler.body = b'<html>\xff</html>'
        report = self.probe()
        self.assertIsNone(report.entry_html_signals)
        self.assertEqual(report.response_bytes, len(EntryHandler.body))

    def test_authenticated_console_entry_check_is_exact_and_one_attempt(self):
        origin = 'http://127.0.0.1:8765'
        controller = SimpleNamespace(session_id='test-preflight', runtime=None)
        app = create_console(controller, 'synthetic-token', origin, self.console_assets,
                             web_profiles_root=self.profiles.root)

        async def check():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=origin,
                    headers={'Origin': origin}) as client:
                selection = {'profile_sha256': self.checksum, 'confirm_sha256': self.checksum,
                             'entry_url': self.profile.entry_url, 'authorized_get': True}
                endpoint = '/api/web-applications/preflight'
                self.assertEqual((await client.post(endpoint, json=selection)).status_code, 401)
                self.assertEqual((await client.post('/api/login', json={
                    'token': 'synthetic-token'})).status_code, 200)
                self.assertEqual((await client.post(endpoint + '?retry=1',
                                                    json=selection)).status_code, 400)
                self.assertEqual((await client.post(endpoint, json={
                    **selection, 'authorized_get': False})).status_code, 400)
                self.assertEqual((await client.post(endpoint, json={
                    **selection, 'confirm_sha256': '0' * 64})).status_code, 400)
                self.assertEqual((await client.post(endpoint, json={
                    **selection, 'entry_url': self.profile.entry_url + '/other'})).status_code, 422)
                self.assertEqual((await client.post(endpoint, json={
                    **selection, 'profile_sha256': '0' * 64,
                    'confirm_sha256': '0' * 64})).status_code, 422)
                duplicate = ('{"profile_sha256":"%s","confirm_sha256":"%s",'
                             '"entry_url":"%s","authorized_get":true,"authorized_get":true}'
                             % (self.checksum, self.checksum, self.profile.entry_url))
                self.assertEqual((await client.post(endpoint, content=duplicate,
                    headers={'Content-Type': 'application/json'})).status_code, 400)
                self.assertEqual(self.server.seen_paths, [])
                response = await client.post(endpoint, json=selection)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['schema_version'], '1.3')
                self.assertEqual(response.json()['response_bytes'], len(EntryHandler.body))
                self.assertEqual(response.json()['entry_html_signals']['form_count'], 0)
                self.assertTrue(response.json()['entry_html_signals']['static_html_only'])
                self.assertFalse(response.json()['browser_connected'])
                self.assertFalse(response.json()['collection_authorized'])
                self.assertNotIn(self.host, response.text)
                self.assertNotIn('synthetic entry', response.text)
                self.assertEqual((await client.post(endpoint, json=selection)).status_code, 409)
                self.assertEqual(self.server.seen_paths, ['/entry'])

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection),
              patch('aos.desktop_console.probe_https_entry',
                    side_effect=lambda profiles, profile_sha256, confirmation: _probe(
                        profiles, profile_sha256, confirmation, tls_context=self.tls_context))):
            asyncio.run(check())

        failed_app = create_console(controller, 'synthetic-token', origin, self.console_assets,
                                    web_profiles_root=self.profiles.root)

        async def failed_check():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=failed_app), base_url=origin,
                    headers={'Origin': origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                selection = {'profile_sha256': self.checksum, 'confirm_sha256': self.checksum,
                             'entry_url': self.profile.entry_url, 'authorized_get': True}
                self.assertEqual((await client.post('/api/web-applications/preflight',
                                                    json=selection)).status_code, 409)
                self.assertEqual((await client.post('/api/web-applications/preflight',
                                                    json=selection)).status_code, 409)

        with patch('aos.desktop_console.probe_https_entry', side_effect=TimeoutError) as attempt:
            asyncio.run(failed_check())
            self.assertEqual(attempt.call_count, 1)

    def test_authenticated_console_entry_check_allows_one_in_flight_probe(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(SimpleNamespace(session_id='test-preflight', runtime=None),
                             'synthetic-token', origin, self.console_assets,
                             web_profiles_root=self.profiles.root)
        entered = threading.Event()
        release = threading.Event()

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        def delayed_probe(profiles, profile_sha256, confirmation):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('synthetic_probe_wait')
            return _probe(profiles, profile_sha256, confirmation, tls_context=self.tls_context)

        async def check():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=origin,
                    headers={'Origin': origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                selection = {'profile_sha256': self.checksum, 'confirm_sha256': self.checksum,
                             'entry_url': self.profile.entry_url, 'authorized_get': True}
                first = asyncio.create_task(client.post('/api/web-applications/preflight',
                                                        json=selection))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    second = await client.post('/api/web-applications/preflight', json=selection)
                    self.assertEqual(second.status_code, 429)
                finally:
                    release.set()
                self.assertEqual((await first).status_code, 200)
                self.assertEqual(self.server.seen_paths, ['/entry'])

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection),
              patch('aos.desktop_console.probe_https_entry', side_effect=delayed_probe)):
            asyncio.run(check())

    def test_bound_probe_requires_exact_task_profile_and_navigation(self):
        report = self.probe_bound()
        self.assertEqual(report.binding_sha256, self.draft.binding_sha256)
        self.assertEqual(report.task_sha256, self.draft.task_sha256)
        self.assertTrue(report.task_binding_verified)
        self.assertFalse(report.runtime_identity_verified)
        self.assertFalse(report.browser_connected)
        self.assertEqual(self.server.seen_paths, ['/entry'])
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            with self.assertRaisesRegex(ValueError, 'exact_binding_confirmation'):
                _probe_bound(self.profiles, self.checksum, self.checksum,
                             self.draft, '0' * 64)
            with self.assertRaisesRegex(ValueError, 'exact_binding_confirmation'):
                _probe_bound(self.profiles, self.checksum, self.checksum, {},
                             self.draft.binding_sha256)
            addresses.assert_not_called()
        tampered = self.draft.model_copy(update={'task_sha256': '0' * 64})
        click_only = WebTaskContract.model_validate({**self.draft.task.model_dump(),
            'tools': ['browser.click', 'browser.snapshot', 'browser.verify']})
        click_draft = bind_web_task(self.profiles, click_only, self.draft.runtime)
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            with self.assertRaises(ValueError):
                _probe_bound(self.profiles, self.checksum, self.checksum,
                             tampered, tampered.binding_sha256)
            with self.assertRaisesRegex(ValueError, 'outside_navigation_scope'):
                _probe_bound(self.profiles, self.checksum, self.checksum,
                             click_draft, click_draft.binding_sha256)
            with self.assertRaisesRegex(ValueError, 'outside_navigation_scope'):
                _probe_bound(self.profiles, '0' * 64, '0' * 64,
                             self.draft, self.draft.binding_sha256)
            addresses.assert_not_called()

    def test_exact_second_route_fetch_stays_on_confirmed_origin(self):
        route = self.profile.entry_url.replace('/entry', '/details')
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, route])
        plan_sha256 = digest(plan.model_dump())
        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            report, body = fetch_web_readonly_route(
                self.profiles, self.draft.task, plan, plan_sha256, 1,
                tls_context=self.tls_context)
        self.assertEqual(self.server.seen_paths, ['/details'])
        self.assertEqual(body, EntryHandler.body)
        self.assertEqual(report.plan_sha256, plan_sha256)
        self.assertEqual(report.request_sha256, digest({'method': 'GET', 'url': route}))
        self.assertFalse(report.browser_connected)
        self.assertFalse(report.execution_authorized)
        fixture = json.loads((REPO_ROOT / 'examples/web_readonly_route_plan.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_https_readonly_route.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         WebHTTPSReadOnlyRouteReport.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        self.assertFalse(WebHTTPSReadOnlyRouteReport.model_validate(fixture['report']).browser_connected)
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            for confirmation, index in [('0' * 64, 1), (plan_sha256, True),
                                        (plan_sha256, 2)]:
                with self.subTest(confirmation=confirmation, index=index), self.assertRaises(ValueError):
                    fetch_web_readonly_route(self.profiles, self.draft.task, plan,
                                             confirmation, index, tls_context=self.tls_context)
            addresses.assert_not_called()

    def test_exact_second_route_query_requires_plan_and_preserves_request_target(self):
        route = self.profile.allowed_origins[0] + '/details?view=compact&page=1'
        self.server.asset_responses = {
            '/details?view=compact&page=1': (200, 'text/html', b'<h1>Query details</h1>')}
        plan = plan_web_readonly_routes(self.profiles, self.draft.task,
                                        [self.profile.entry_url, route])
        plan_sha256 = digest(plan.model_dump())

        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with (patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']),
              patch('aos.web_https_preflight.socket.create_connection',
                    side_effect=local_connection)):
            with self.assertRaisesRegex(ValueError, 'exact_plan'):
                fetch_web_readonly_route(self.profiles, self.draft.task, plan,
                                         '0' * 64, 1, tls_context=self.tls_context)
            self.assertEqual(self.server.seen_paths, [])
            report, body = fetch_web_readonly_route(
                self.profiles, self.draft.task, plan, plan_sha256, 1,
                tls_context=self.tls_context)
        self.assertEqual(self.server.seen_paths, ['/details?view=compact&page=1'])
        self.assertEqual(body, b'<h1>Query details</h1>')
        self.assertEqual(report.request_sha256, digest({'method': 'GET', 'url': route}))

    def test_confirmation_and_profile_scope_fail_before_network(self):
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            with self.assertRaisesRegex(ValueError, 'exact_profile_confirmation'):
                _probe(self.profiles, self.checksum, '0' * 64)
            addresses.assert_not_called()
        local_source = json.loads((REPO_ROOT / 'examples/synthetic_navigation_pin.json').read_text())['profile']
        local_profile = WebApplicationProfile.model_validate(local_source)
        local_checksum = profile_report(local_profile).profile_sha256
        self.profiles.register(local_profile, confirm_sha256=local_checksum)
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            with self.assertRaisesRegex(ValueError, 'remote_profile_entry'):
                _probe(self.profiles, local_checksum, local_checksum)
            addresses.assert_not_called()

    def test_dns_rejects_private_or_mixed_answers(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))
        with patch('aos.web_https_preflight.socket.getaddrinfo', return_value=[public, private]):
            with self.assertRaisesRegex(ValueError, 'not_public'):
                _public_addresses(self.host, 443)
        with patch('aos.web_https_preflight.socket.getaddrinfo', return_value=[public]):
            self.assertEqual(_public_addresses(self.host, 443), ['8.8.8.8'])

    def test_dns_over_budget_never_starts_connection(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_addresses(_host, _port):
            calls.append(1)
            entered.set()
            release.wait(1)
            return ['8.8.8.8']

        try:
            with patch('aos.web_https_preflight.PROBE_TIMEOUT_SECONDS', 0.05), \
                    patch('aos.web_https_preflight._public_addresses', side_effect=slow_addresses), \
                    patch('aos.web_https_preflight.socket.create_connection') as connection:
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'https_probe_dns_deadline'):
                    _probe(self.profiles, self.checksum, self.checksum, tls_context=self.tls_context)
                self.assertTrue(entered.is_set())
                with self.assertRaisesRegex(TimeoutError, 'https_probe_dns_deadline'):
                    _probe(self.profiles, self.checksum, self.checksum, tls_context=self.tls_context)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(len(calls), 1)
                connection.assert_not_called()
        finally:
            release.set()
            self.assertTrue(_dns_slot.acquire(timeout=1))
            _dns_slot.release()

    def test_slow_header_is_stopped_by_absolute_deadline(self):
        self.server.header_byte_delay = 0.1
        started = time.monotonic()
        with patch('aos.web_https_preflight.PROBE_TIMEOUT_SECONDS', 0.2):
            with self.assertRaises(TimeoutError):
                self.probe()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.server.seen_paths, ['/entry'])

    def test_tls_hostname_failure_and_unverified_context_rejected(self):
        wrong = ssl.create_default_context(cafile=str(self.root / 'certificate.pem'))
        wrong.check_hostname = False
        with self.assertRaisesRegex(ValueError, 'verified_tls'):
            self.probe(context=wrong)
        wrong.check_hostname = True
        bad_profile = WebApplicationProfile.model_validate({**self.profile.model_dump(),
            'entry_url': self.profile.entry_url.replace(self.host, 'other.aos-preflight.invalid'),
            'allowed_origins': [self.profile.allowed_origins[0].replace(self.host, 'other.aos-preflight.invalid')]})
        bad_checksum = profile_report(bad_profile).profile_sha256
        self.profiles.register(bad_profile, confirm_sha256=bad_checksum)
        with patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection',
                      side_effect=lambda _address, timeout=None: self.original_create_connection(
                          ('127.0.0.1', self.server.server_port), timeout=timeout)):
            with self.assertRaises(ssl.SSLCertVerificationError):
                _probe(self.profiles, bad_checksum, bad_checksum, tls_context=self.tls_context)

    def test_redirect_and_non_html_rejected(self):
        original_status, original_type = EntryHandler.status, EntryHandler.content_type
        try:
            EntryHandler.status = 302
            with self.assertRaisesRegex(ValueError, 'plain_success'):
                self.probe()
            EntryHandler.status = 200
            EntryHandler.content_type = 'application/json'
            with self.assertRaisesRegex(ValueError, 'not_html'):
                self.probe()
        finally:
            EntryHandler.status, EntryHandler.content_type = original_status, original_type

    def test_oversized_response_rejected(self):
        original = EntryHandler.body
        try:
            EntryHandler.body = b'x' * 65537
            with self.assertRaisesRegex(ValueError, 'length_limit'):
                self.probe()
        finally:
            EntryHandler.body = original

    def test_cli_fails_closed_without_network(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with patch('aos.web_https_preflight._public_addresses') as addresses:
                with self.assertRaises(SystemExit) as exit_status:
                    main(['--profiles', str(self.profiles.root), '--profile-sha256', self.checksum,
                          '--confirm-sha256', '0' * 64])
                self.assertEqual(exit_status.exception.code, 1)
                addresses.assert_not_called()
        self.assertNotIn(self.host, output.getvalue())

    def test_bound_cli_requires_confirmation_and_emits_only_bound_hashes(self):
        binding_file = self.root / 'binding.json'
        binding_file.write_text(canonical(self.draft.model_dump()))
        options = ['--profiles', str(self.profiles.root), '--profile-sha256', self.checksum,
                   '--confirm-sha256', self.checksum, '--binding-file', str(binding_file)]
        with patch('aos.web_https_preflight._public_addresses') as addresses:
            with self.assertRaises(SystemExit):
                main(options)
            addresses.assert_not_called()
        output = io.StringIO()
        def local_connection(_address, timeout=None):
            return self.original_create_connection(('127.0.0.1', self.server.server_port),
                                                   timeout=timeout)

        with contextlib.redirect_stdout(output), \
                patch('aos.web_https_preflight._public_addresses', return_value=['8.8.8.8']), \
                patch('aos.web_https_preflight.socket.create_connection', side_effect=local_connection), \
                patch('aos.web_https_preflight.ssl.create_default_context', return_value=self.tls_context):
            main([*options, '--confirm-binding-sha256', self.draft.binding_sha256])
        report = WebBoundHTTPSPreflightReport.model_validate_json(output.getvalue())
        self.assertEqual(report.binding_sha256, self.draft.binding_sha256)
        self.assertNotIn(self.host, output.getvalue())
        self.assertNotIn('synthetic entry', output.getvalue())

    def test_synthetic_fixture_matches_report_contract(self):
        fixture = json.loads((REPO_ROOT / 'examples/web_https_preflight.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_https_preflight.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        WebHTTPSPreflightReport.model_validate(fixture['report'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'execution_authorized': True})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate({**fixture['report'],
                'entry_html_signals': {**fixture['report']['entry_html_signals'], 'form_count': -1}})
        bound_fixture = json.loads((REPO_ROOT / 'examples/web_bound_https_preflight.json').read_text())
        bound_schema = json.loads((REPO_ROOT / 'schemas/web_bound_https_preflight.schema.json').read_text())
        self.assertTrue(bound_fixture['synthetic'])
        jsonschema.Draft202012Validator(bound_schema).validate(bound_fixture['report'])
        WebBoundHTTPSPreflightReport.model_validate(bound_fixture['report'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(bound_schema).validate({**bound_fixture['report'],
                'runtime_identity_verified': True})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(bound_schema).validate({**bound_fixture['report'],
                'entry_html_signals': {**bound_fixture['report']['entry_html_signals'],
                                       'browser_or_account_verified': True}})


if __name__ == '__main__':
    unittest.main()
