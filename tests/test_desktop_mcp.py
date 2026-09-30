import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from aos.browser import BROWSER_EXPECTED, BROWSER_VALUE
from aos.contracts import AOSFault, ErrorCode, REPO_ROOT, Settings, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop import DesktopRuntime
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.reusable_decider import ReusableDeciderEngine
from aos.desktop_mcp_bundle import (MAX_ARCHIVE, PACKAGES, inspect_bundle, member_name,
                                    PLAYWRIGHT_VERSION, prepare_bundle, read_bundle, unpack_bundle)
from aos.desktop_tasks import DesktopScheduler
from aos.local_navigation_admission import SyntheticNavigationPin, check_synthetic_navigation_runtime
from aos.learning_events import review_learning_events
from aos.learning_candidate import review_learning_candidates
from aos.site_page_evidence import review_site_page_evidence
from aos.site_knowledge import SiteKnowledgeStore, SitePageDraft
from aos.site_page_retrieval_bound import preview_local_bound_site_page_retrieval
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
import test_desktop_browser as browser_tests
import test_visible_scheduler as scheduler_tests


def worker_namespace():
    desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id=None)
    runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
    namespace = {'__name__': 'aos_mcp_unit_test'}
    exec(runtime.worker_source(), namespace)
    return namespace


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.packages = self.root / 'node_modules'
        for package, version in PACKAGES.items():
            directory = self.packages / package
            directory.mkdir(parents=True)
            metadata = {'name': package, 'version': version}
            if package == '@playwright/mcp':
                metadata['dependencies'] = {name: PACKAGES[name] for name in ('playwright', 'playwright-core')}
            (directory / 'package.json').write_text(json.dumps(metadata))

    def test_prepare_hash_roundtrip_private_unpack_and_explicit_new_output(self):
        manifest = prepare_bundle(self.packages, self.root / 'output')
        pins, payload = read_bundle(manifest)
        self.assertEqual(len(pins['bundle_sha256']), 64)
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest.parent.stat().st_mode), 0o700)
        unpack_bundle(payload, self.root / 'unpacked')
        self.assertEqual((self.root / 'unpacked/@playwright/mcp/package.json').read_bytes(),
                         (self.packages / '@playwright/mcp/package.json').read_bytes())
        with self.assertRaises(FileExistsError):
            prepare_bundle(self.packages, manifest.parent)

    def test_navigation_only_config_exposes_fourth_kind_without_switching_form(self):
        manifest = prepare_bundle(self.packages, self.root / 'output')
        controller = Mock()
        controller.store = Mock()
        scheduler = DesktopScheduler(controller, Settings(workspace=self.root / 'workspace',
                                                            database=self.root / 'store.sqlite'), Mock(),
                                   browser_manifest=Path('synthetic'), desktop_browser=True,
                                   desktop_navigation_mcp_manifest=manifest)
        self.assertIn('browser_local_navigation', scheduler.kinds())
        self.assertIsNone(scheduler.desktop_mcp_manifest)
        self.assertEqual(scheduler.desktop_navigation_mcp_manifest, manifest)
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, scheduler.settings, Mock(),
                             browser_manifest=Path('synthetic'), desktop_browser=True,
                             desktop_mcp_manifest=manifest, desktop_navigation_mcp_manifest=manifest)
        (manifest.parent / 'packages.zip').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, scheduler.settings, Mock(),
                             browser_manifest=Path('synthetic'), desktop_browser=True,
                             desktop_navigation_mcp_manifest=manifest)

    def test_tampered_hash_and_manifest_are_rejected(self):
        manifest = prepare_bundle(self.packages, self.root / 'output')
        archive = manifest.parent / 'packages.zip'
        original = archive.read_bytes()
        archive.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'hash differs'):
            read_bundle(manifest)
        archive.write_bytes(original)
        pins = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**pins, 'version': True}))
        with self.assertRaises(ValueError):
            read_bundle(manifest)

    def test_no_traversal_other_packages_or_ambiguous_paths(self):
        for name in ('../escape', '/playwright/file', 'playwright/../escape', 'playwright//file',
                     'playwright/./file', 'playwright\\file', 'unapproved/file', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                member_name(name)

    def test_symlink_in_source_rejected(self):
        link = self.packages / 'playwright/link'
        link.symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            prepare_bundle(self.packages, self.root / 'output')

    def test_wrong_version_and_dependency_set_rejected(self):
        metadata_path = self.packages / '@playwright/mcp/package.json'
        original = json.loads(metadata_path.read_text())
        for metadata in ({**original, 'version': '0.0.0'}, {**original, 'dependencies': {}}):
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                prepare_bundle(self.packages, self.root / 'output')

    def test_malformed_and_oversized_archives_rejected(self):
        for payload in (b'', b'not a zip', b'x' * (MAX_ARCHIVE + 1)):
            with self.assertRaises(ValueError):
                inspect_bundle(payload)

    def test_archive_links_and_duplicate_members_rejected(self):
        for mode, duplicate in ((stat.S_IFLNK, False), (stat.S_IFREG, True)):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                member = zipfile.ZipInfo('playwright/link')
                member.external_attr = (mode | 0o600) << 16
                archive.writestr(member, b'/etc/passwd')
                if duplicate:
                    with self.assertWarns(UserWarning):
                        archive.writestr(member, b'duplicate')
            with self.assertRaises(ValueError):
                inspect_bundle(buffer.getvalue())


class MCPAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = worker_namespace()

    def test_configuration_requires_visible_browser(self):
        with self.assertRaises(ValueError):
            DesktopScheduler(SimpleNamespace(), Settings(), FixtureDecisionEngine(),
                             desktop_mcp_manifest=Path('synthetic'))

    def test_unowned_parent_cannot_read_bundle_or_spawn(self):
        desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id=None)
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
        with patch('aos.desktop_mcp.read_bundle') as read, self.assertRaises(AOSFault):
            runtime.start()
        read.assert_not_called()

    def test_handshake_mismatch_closes_worker_without_fallback(self):
        desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id='a' * 64,
                       pins={'chromium_sha256': 'b' * 64})
        pins = {'bundle_sha256': 'c' * 64}
        evidence = {'ready': True, 'headed': True, 'display': ':99', 'chromium_sha256': 'b' * 64,
                    'network_namespace': 'synthetic-isolated', 'home_visible': False,
                    'docker_socket_visible': False, 'transport': 'playwright_mcp',
                    'bundle_sha256': 'c' * 64, 'server_version': PLAYWRIGHT_VERSION,
                    'staging_workflow': False, 'fixture_proxy_mode': False,
                    'page_request_gate': False,
                    'fixture_request_guard': None}
        for changes in ({'headed': False}, {'display': ':0'}, {'bundle_sha256': 'd' * 64},
                        {'server_version': 'other'}, {'transport': 'cdp'}, {'home_visible': True},
                        {'fixture_proxy_mode': True},
                        {'page_request_gate': True},
                        {'network_namespace': os.readlink('/proc/self/ns/net')}):
            runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
            runtime.owned_desktop = Mock()
            runtime.receive = Mock(return_value={**evidence, **changes})
            runtime.stop = Mock()
            with self.subTest(changes=changes), patch('aos.desktop_mcp.read_bundle', return_value=(pins, b'fixture')):
                with patch('aos.desktop_mcp.subprocess.Popen') as launch, self.assertRaises(AOSFault):
                    runtime.start()
                runtime.stop.assert_called_once()
                launch.assert_called_once()

    def test_arbitrary_mcp_tools_or_selectors_never_reach_browser(self):
        browser = Mock()
        for request in ({'tool': 'browser_evaluate', 'arguments': {'function': 'malicious'}},
                        {'tool': 'browser.navigate', 'arguments': {'url': 'https://example.invalid'}},
                        {'tool': 'browser.fill', 'arguments': {'selector': '#message', 'value': BROWSER_VALUE}},
                        {'tool': 'browser.verify', 'arguments': {'extra': True}}, None):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.worker['dispatch'](browser, request)
        browser.tool.assert_not_called()
        browser.evaluate.assert_not_called()

    def test_stale_check_has_no_mcp_input_effect(self):
        browser = Mock(references=['e1', 'e2'])
        browser.evaluate.return_value = {'error': 'UI_CHANGED'}
        request = {'tool': 'browser.submit', 'arguments': {
            'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}}
        actual = self.worker['dispatch'](browser, request)
        self.assertEqual(actual, {'error': 'UI_CHANGED'})
        self.assertIsNone(browser.references)
        self.assertEqual(self.worker['dispatch'](browser, request), {'error': 'UI_CHANGED'})
        browser.evaluate.assert_called_once()
        browser.tool.assert_not_called()

    def test_failed_freshness_evaluation_consumes_mcp_references(self):
        browser = Mock(references=['e1', 'e2'])
        browser.evaluate.side_effect = self.worker['ProtocolError']('MCP disconnected')
        request = {'tool': 'browser.submit', 'arguments': {
            'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}}
        with self.assertRaises(self.worker['ProtocolError']):
            self.worker['dispatch'](browser, request)
        self.assertIsNone(browser.references)
        self.assertEqual(self.worker['dispatch'](browser, request), {'error': 'UI_CHANGED'})
        browser.evaluate.assert_called_once()
        browser.tool.assert_not_called()

    def test_local_navigation_rejects_stale_reference_without_reuse(self):
        browser = self.worker['MCPBrowser'](fixture_request_guard=True)
        snapshot = {'page': 'start', 'snapshot_id': 'a' * 32,
                    'elements': [{'element_id': 'b' * 32}]}
        browser.fixture_snapshot = (snapshot, 'mcp-link')
        browser.fixture_state = Mock(side_effect=self.worker['ProtocolError']('Synthetic page changed'))
        browser.tool = Mock()
        reference = {'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}
        self.assertEqual(browser.fixture_follow(reference), {'error': 'UI_CHANGED'})
        self.assertIsNone(browser.fixture_snapshot)
        browser.fixture_state.reset_mock(side_effect=True)
        self.assertEqual(browser.fixture_follow(reference), {'error': 'UI_CHANGED'})
        browser.tool.assert_not_called()

    def test_fill_uses_observed_mcp_reference_and_consumes_it(self):
        browser = Mock(references=['e1', 'e2'])
        browser.evaluate.return_value = {'x': 5, 'y': 6}
        request = {'tool': 'browser.fill', 'arguments': {'snapshot_id': 'a' * 32,
                   'element_id': 'b' * 32, 'value': BROWSER_VALUE}}
        self.assertEqual(self.worker['dispatch'](browser, request), {'applied': True})
        browser.tool.assert_called_once_with('browser_fill_form', {'fields': [{
            'target': 'e1', 'name': 'Message', 'type': 'textbox', 'value': BROWSER_VALUE}]})
        self.assertIsNone(browser.references)
        self.assertEqual(self.worker['dispatch'](browser, request), {'error': 'UI_CHANGED'})
        self.assertEqual(browser.tool.call_count, 1)

    def test_bad_or_duplicate_accessibility_target_fails_closed(self):
        browser = Mock()
        for snapshot in ('', '- textbox "Message" [ref=e1]\n- textbox "Message" [ref=e2]'):
            browser.tool.return_value = snapshot
            with self.assertRaises(self.worker['ProtocolError']):
                self.worker['dispatch'](browser, {'tool': 'browser.observe', 'arguments': {}})
            self.assertIsNone(browser.references)
        browser.evaluate.assert_not_called()

    def test_mcp_error_and_unapproved_tool_fail_without_retry(self):
        browser = self.worker['MCPBrowser']()
        browser.request = Mock(return_value={'isError': True, 'content': []})
        with self.assertRaises(self.worker['ProtocolError']):
            browser.tool('browser_click', {'target': 'e1'})
        browser.request.assert_called_once()
        with self.assertRaises(self.worker['ProtocolError']):
            browser.tool('browser_run_code', {})
        browser.request.assert_called_once()

    def test_evaluation_rejects_missing_or_invalid_json(self):
        browser = self.worker['MCPBrowser']()
        for output in ('error', '### Result\nnot json'):
            browser.tool = Mock(return_value=output)
            with self.assertRaises(self.worker['ProtocolError']):
                browser.evaluate('() => true')

    def test_protocol_timeout_is_bounded_and_does_not_retry(self):
        browser = self.worker['MCPBrowser']()
        browser.process = Mock()
        with patch.object(self.worker['select'], 'select', return_value=([], [], [])), self.assertRaises(self.worker['ProtocolError']):
            browser.request('tools/list', {})
        browser.process.stdin.write.assert_called_once()

    def test_protocol_rejects_wrong_id_rpc_error_and_malformed_response(self):
        for response in (b'not json\n', b'[]\n', b'{"jsonrpc":"2.0","id":2,"result":{}}\n',
                         b'{"jsonrpc":"2.0","id":true,"result":{}}\n',
                         b'{"jsonrpc":"2.0","id":1,"error":{}}\n'):
            reader, writer = os.pipe()
            os.write(writer, response)
            os.close(writer)
            with os.fdopen(reader, 'rb') as output, io.BytesIO() as input_stream:
                browser = self.worker['MCPBrowser']()
                browser.process = SimpleNamespace(stdin=input_stream, stdout=output)
                with self.assertRaises(self.worker['ProtocolError']):
                    browser.request('tools/list', {})

    def test_local_fixture_probe_dispatch_is_separate_from_form(self):
        browser = Mock(fixture_opened=False)
        browser.fixture_open.return_value = {'page': 'start'}
        self.assertEqual(self.worker['dispatch'](browser, {'tool': 'browser.fixture.open', 'arguments': {}}),
                         {'page': 'start'})
        browser.fixture_open.assert_called_once_with()
        browser.fixture_opened = True
        browser.fixture_page = 'start'
        browser.fixture_observe.return_value = {'page': 'start'}
        self.assertEqual(self.worker['dispatch'](browser, {'tool': 'browser.fixture.snapshot', 'arguments': {}}),
                         {'page': 'start'})
        browser.fixture_observe.assert_called_once_with('start')
        with self.assertRaises(ValueError):
            self.worker['dispatch'](browser, {'tool': 'browser.observe', 'arguments': {}})
        browser.tool.assert_not_called()
        for request in ({'tool': 'browser.fixture.open', 'arguments': {'url': 'https://example.invalid'}},
                        {'tool': 'browser.fixture.snapshot', 'arguments': {'script': '1'}}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.worker['dispatch'](browser, request)

    def test_local_fixture_fresh_reference_is_one_use(self):
        browser = self.worker['MCPBrowser']()
        browser.fixture_server = SimpleNamespace(server_port=32123)
        browser.fixture_opened = True
        browser.evaluate = Mock(return_value={
            'url': 'http://127.0.0.1:32123/start', 'title': 'Start', 'heading': 'Synthetic start',
            'links': [{'text': 'Details', 'href': 'http://127.0.0.1:32123/details'}], 'other_controls': 0})
        browser.tool = Mock(return_value='- link "Details" [ref=e17]')
        observed = browser.fixture_observe('start')
        with self.assertRaises(ValueError):
            browser.fixture_follow({'url': 'file:///etc/passwd'})
        stale = {'snapshot_id': '0' * 32, 'element_id': observed['elements'][0]['element_id']}
        self.assertEqual(browser.fixture_follow(stale), {'error': 'UI_CHANGED'})
        browser.tool.assert_called_once_with('browser_snapshot', {})
        self.assertIsNone(browser.fixture_snapshot)
        observed = browser.fixture_observe('start')
        browser.fixture_observe = Mock(return_value={'page': 'details'})
        fresh = {'snapshot_id': observed['snapshot_id'], 'element_id': observed['elements'][0]['element_id']}
        self.assertEqual(browser.fixture_follow(fresh), {'page': 'details'})
        browser.tool.assert_called_with('browser_click', {'target': 'e17'})
        self.assertEqual(browser.fixture_follow(fresh), {'error': 'UI_CHANGED'})

    def test_local_fixture_origin_redirect_and_dom_change_fail_closed(self):
        browser = self.worker['MCPBrowser']()
        browser.fixture_server = SimpleNamespace(server_port=32123)
        expected = {'url': 'http://127.0.0.1:32123/start', 'title': 'Start',
                    'heading': 'Synthetic start',
                    'links': [{'text': 'Details', 'href': 'http://127.0.0.1:32123/details'}],
                    'other_controls': 0}
        for changes in ({'url': 'https://example.invalid/start'}, {'url': 'http://127.0.0.1:32123/redirect'},
                        {'links': [{'text': 'Details', 'href': 'file:///etc/passwd'}]},
                        {'other_controls': 1}):
            browser.evaluate = Mock(return_value={**expected, **changes})
            with self.subTest(changes=changes), self.assertRaises(self.worker['ProtocolError']):
                browser.fixture_state('start')

    def test_local_fixture_server_only_serves_exact_loopback_routes(self):
        server = self.worker['HTTPServer'](('127.0.0.1', 0), self.worker['FixtureHandler'])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f'http://127.0.0.1:{server.server_port}'
        with urlopen(origin + '/start', timeout=2) as response:
            self.assertIn(b'Synthetic start', response.read())
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with urlopen(origin + '/details', timeout=2) as response:
            self.assertIn(b'Local fixture only.', response.read())
        for request in (Request(origin + '/start', headers={'Host': 'example.invalid'}),
                        Request(origin + '/escape'), Request(origin + '/start?next=/details')):
            with self.subTest(url=request.full_url), self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=2)
            self.assertEqual(caught.exception.code, 404)
            caught.exception.close()

    def test_host_validates_typed_local_probe_output(self):
        desktop = Mock(manifest=Path('synthetic'), runtime_id='desktop-synthetic', container_id=None)
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
        good = {'page': 'start', 'heading': 'Synthetic start', 'snapshot_id': 'a' * 32,
                'elements': [{'element_id': 'b' * 32, 'role': 'link', 'label': 'Details'}]}
        self.assertEqual(runtime.validate_result('browser.fixture.open', good), good)
        for bad in ({**good, 'page': 'outside'}, {**good, 'heading': 'Changed'},
                    {**good, 'elements': [{'element_id': 'a' * 32, 'role': 'link', 'label': 'Details'}]},
                    {**good, 'url': 'https://example.invalid'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                runtime.validate_result('browser.fixture.open', bad)


MCP_ENABLED = os.environ.get('AOS_DESKTOP_MCP_TESTS') == '1' and os.environ.get('AOS_DESKTOP_TESTS') == '1'
MCP_MANIFEST = REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'


@unittest.skipUnless(MCP_ENABLED, 'Opt in to real Ubuntu Playwright MCP')
class MCPBrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='desktop-mcp-test-', dir=REPO_ROOT / 'data')
        self.addCleanup(self.temporary.cleanup)
        self.desktop = DesktopRuntime(Path(self.temporary.name) / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
        self.addCleanup(self.desktop.stop)
        self.desktop.start()
        self.runtime = DesktopMCPBrowserRuntime(self.desktop, MCP_MANIFEST)
        self.addCleanup(self.runtime.stop)
        self.runtime.start()

    test_visible_form = browser_tests.DesktopBrowserIntegrationTests.test_headed_form_operator_independent_verification_and_owned_cleanup
    test_stale_scope_and_one_submission = browser_tests.DesktopBrowserIntegrationTests.test_stale_and_unknown_ids_unauthorized_tools_and_one_submission

    def test_mcp_identity_and_process_cleanup_without_stopping_desktop(self):
        self.assertEqual(self.runtime.status()['browser_transport'], 'playwright_mcp')
        self.assertEqual(self.runtime.evidence['bundle_sha256'], read_bundle(MCP_MANIFEST)[0]['bundle_sha256'])
        processes = self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/ps', '-eo', 'args']).decode()
        self.assertIn('node_modules/@playwright/mcp/cli.js', processes)
        self.assertIn('--remote-debugging-pipe', processes)
        self.assertNotIn('--remote-debugging-port', processes)
        self.runtime.stop()
        processes = self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/ps', '-eo', 'args']).decode()
        self.assertNotIn('node_modules/@playwright/mcp/cli.js', processes)
        self.assertNotIn('--remote-debugging-pipe', processes)
        self.assertTrue(self.desktop.perform('probe', {})['display'])

    def test_crashed_mcp_is_not_retried_and_owned_chromium_is_reaped(self):
        script = '''import os,signal
from pathlib import Path
matches = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        parts = path.read_bytes().split(b'\\0')
        if len(parts) > 1 and parts[0] == b'/usr/local/bin/node' and parts[1].endswith(b'/node_modules/@playwright/mcp/cli.js'):
            matches.append(int(path.parent.name))
    except (FileNotFoundError, ProcessLookupError):
        pass
assert len(matches) == 1
os.kill(matches[0], signal.SIGKILL)
'''
        self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/python3', '-c', script])
        with self.assertRaises(AOSFault) as caught:
            self.runtime.perform('browser.observe', {})
        self.assertEqual(caught.exception.code, ErrorCode.RUNTIME_CRASH)
        self.runtime.stop()
        processes = self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/ps', '-eo', 'args']).decode()
        self.assertNotIn('node_modules/@playwright/mcp/cli.js', processes)
        self.assertNotIn('--remote-debugging-pipe', processes)

    def test_local_two_page_probe_uses_fresh_link_and_keeps_network_disabled(self):
        self.runtime.stop()
        self.runtime = DesktopMCPBrowserRuntime(self.desktop, MCP_MANIFEST, fixture_request_guard=True)
        self.addCleanup(self.runtime.stop)
        self.runtime.start()
        start = self.runtime.perform('browser.fixture.open', {})
        self.assertEqual((start['page'], start['heading']), ('start', 'Synthetic start'))
        with self.assertRaises(AOSFault) as stale:
            self.runtime.perform('browser.fixture.follow', {
                'snapshot_id': '0' * 32, 'element_id': start['elements'][0]['element_id']})
        self.assertEqual(stale.exception.code, ErrorCode.UI_CHANGED)
        fresh = self.runtime.perform('browser.fixture.snapshot', {})
        with self.assertRaises(AOSFault) as stale:
            self.runtime.perform('browser.fixture.follow', {
                'snapshot_id': start['snapshot_id'], 'element_id': start['elements'][0]['element_id']})
        self.assertEqual(stale.exception.code, ErrorCode.UI_CHANGED)
        with self.assertRaises(AOSFault) as stale:
            self.runtime.perform('browser.fixture.follow', {
                'snapshot_id': fresh['snapshot_id'], 'element_id': fresh['elements'][0]['element_id']})
        self.assertEqual(stale.exception.code, ErrorCode.UI_CHANGED)
        latest = self.runtime.perform('browser.fixture.snapshot', {})
        details = self.runtime.perform('browser.fixture.follow', {
            'snapshot_id': latest['snapshot_id'], 'element_id': latest['elements'][0]['element_id']})
        self.assertEqual((details['page'], details['heading'], details['elements']),
                         ('details', 'Synthetic details', []))
        self.assertEqual(self.runtime.perform('browser.fixture.snapshot', {})['page'], 'details')
        self.assertFalse(self.runtime.status()['network'])
        with self.assertRaises(AOSFault):
            self.runtime.perform('browser.fixture.open', {})
        self.assertTrue(self.desktop.perform('probe', {})['display'])

    def test_pinned_staging_post_traverses_same_chromium_proxy(self):
        self.runtime.stop()
        port = int(self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/python3',
            '-c', 'import socket; connection=socket.socket(); connection.bind(("127.0.0.1",0)); '
                  'print(connection.getsockname()[1]); connection.close()']).decode().strip())
        self.runtime = DesktopMCPBrowserRuntime(self.desktop, MCP_MANIFEST, fixture_request_guard=True,
                                                staging_workflow=True, fixture_port=port)
        self.addCleanup(self.runtime.stop)
        self.runtime.start()
        self.assertTrue(self.runtime.evidence['fixture_proxy_mode'])
        self.assertTrue(self.runtime.evidence['page_request_gate'])
        self.assertEqual(self.runtime.evidence['fixture_request_guard']['fixture_port'], port)
        app = self.runtime.perform('browser.staging.open', {})
        draft = self.runtime.perform('browser.staging.follow', {
            'snapshot_id': app['snapshot_id'], 'element_id': app['elements'][0]['element_id']})
        filled = self.runtime.perform('browser.staging.fill', {
            'snapshot_id': draft['snapshot_id'], 'element_id': draft['elements'][0]['element_id'],
            'value': 'Hello from the local agent.'})
        receipt = self.runtime.perform('browser.staging.submit', {
            'snapshot_id': filled['snapshot_id'], 'element_id': filled['elements'][1]['element_id']})
        self.assertEqual((receipt['page'], receipt['receipt'], receipt['submissions']),
                         ('receipt', 'Saved locally.', 1))
        self.assertFalse(self.runtime.status()['network'])


@unittest.skipUnless(MCP_ENABLED, 'Opt in to real Ubuntu MCP scheduler')
class MCPSchedulerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await scheduler_tests.VisibleSchedulerTests.asyncSetUp(self)
        self.scheduler.desktop_mcp_manifest = MCP_MANIFEST
        self.assertEqual(self.scheduler.status()['browser_transport'], 'playwright_mcp')

    asyncTearDown = scheduler_tests.VisibleSchedulerTests.asyncTearDown
    start = scheduler_tests.VisibleSchedulerTests.start
    approval = scheduler_tests.VisibleSchedulerTests.approval
    accept = scheduler_tests.VisibleSchedulerTests.accept
    complete_form = scheduler_tests.VisibleSchedulerTests.complete_form
    test_separate_approvals = scheduler_tests.VisibleSchedulerTests.test_two_approvals_real_desktop_and_retained_independent_result
    test_pause_resume = scheduler_tests.VisibleSchedulerTests.test_pause_retains_window_but_resume_requires_new_approval
    test_takeover = scheduler_tests.VisibleSchedulerTests.test_takeover_revokes_pending_input_and_closes_only_browser
    test_next_task_cleanup = scheduler_tests.VisibleSchedulerTests.test_next_task_releases_previous_visible_result
    test_real_decider = scheduler_tests.VisibleSchedulerTests.test_real_decider_visible_form_with_independent_verification
    test_cpu_ready_real_decider_baseline = scheduler_tests.VisibleSchedulerTests.test_real_decider_cpu_ready_form_baseline

    def configure_synthetic_navigation_pin(self):
        fixture = json.loads((REPO_ROOT / 'examples/synthetic_navigation_pin.json').read_text())
        with socket.socket() as available_port:
            available_port.bind(('127.0.0.1', 0))
            fixture_port = available_port.getsockname()[1]
        fixture['profile']['entry_url'] = f'http://127.0.0.1:{fixture_port}/start'
        fixture['profile']['allowed_origins'] = [f'http://127.0.0.1:{fixture_port}']
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        profiles = WebApplicationProfiles(self.root / 'profiles')
        report = profile_report(profile)
        profiles.register(profile, confirm_sha256=report.profile_sha256)
        bundle, _payload = read_bundle(MCP_MANIFEST)
        candidate = DesktopMCPBrowserRuntime(self.desktop, MCP_MANIFEST)
        pin = SyntheticNavigationPin.model_validate({**fixture['pin'],
            'profile_sha256': report.profile_sha256,
            'fixture_port': fixture_port,
            'parent_runtime_id': self.desktop.runtime_id,
            'container_id': self.desktop.container_id,
            'image_id': self.desktop.pins['image_id'],
            'mcp_bundle_sha256': bundle['bundle_sha256'],
            'worker_sha256': hashlib.sha256(candidate.worker_source().encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(
                (REPO_ROOT / 'examples' / candidate.fixture_file).read_bytes()).hexdigest(),
            'runtime_digest': digest({**self.desktop.pins, 'mcp': bundle})})
        self.scheduler.local_navigation_profiles = profiles
        self.scheduler.local_navigation_pin = pin

    async def test_explicit_task_scoped_approve_all(self):
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_form', approve_all=True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.verify', {}), BROWSER_EXPECTED)
        self.assertEqual(self.scheduler.completed_runtime.status()['browser_transport'], 'playwright_mcp')
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 2)

    async def test_navigation_only_transport_keeps_form_cdp_and_guards_two_page_task(self):
        self.scheduler.desktop_mcp_manifest = None
        self.scheduler.desktop_navigation_mcp_manifest = MCP_MANIFEST
        status = self.scheduler.status()
        self.assertEqual(status['browser_transport'], 'cdp')
        self.assertEqual(status['navigation_transport'], 'playwright_mcp')
        self.assertIn('browser_local_navigation', status['kinds'])
        state = self.controller.state()
        with self.assertRaises(AOSFault):
            self.scheduler.start(state['lease_id'], state['generation'], 'browser_local_navigation', approve_all=True)
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_local_navigation')
        for tool in ('browser.fixture.open', 'browser.fixture.follow'):
            pending = await self.approval(tool)
            self.accept(pending)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.completed_runtime.status()['browser_transport'], 'playwright_mcp')
        self.assertTrue(self.scheduler.completed_runtime.valid_fixture_request_guard())
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)

    async def test_local_navigation_has_two_explicit_approvals_and_independent_details(self):
        self.assertIn('browser_local_navigation', self.scheduler.kinds())
        state = self.controller.state()
        with self.assertRaises(AOSFault):
            self.scheduler.start(state['lease_id'], state['generation'], 'browser_local_navigation', approve_all=True)
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_local_navigation')
        opening = await self.approval('browser.fixture.open')
        self.assertEqual(opening['action']['arguments'], {})
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.accept(opening)
        following = await self.approval('browser.fixture.follow')
        self.assertEqual(set(following['action']['arguments']), {'snapshot_id', 'element_id'})
        self.assertNotEqual(opening['action_sha256'], following['action_sha256'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 1)
        self.accept(following)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.fixture.snapshot', {})['page'], 'details')
        self.assertEqual([row['tool'] for row in self.store.connection.execute(
            "SELECT tool FROM actions ORDER BY rowid")],
            ['browser.fixture.open', 'browser.fixture.snapshot', 'browser.fixture.follow', 'browser.fixture.snapshot'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    async def test_opt_in_synthetic_profile_pin_rechecks_real_owned_runtime(self):
        self.configure_synthetic_navigation_pin()
        self.start('browser_local_navigation')
        self.accept(await self.approval('browser.fixture.open'))
        self.accept(await self.approval('browser.fixture.follow'))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 2)
        report = check_synthetic_navigation_runtime(
            self.scheduler.local_navigation_profiles, self.scheduler.local_navigation_pin,
            self.scheduler.completed_runtime)
        self.assertTrue(report['profile_to_origin_bound'])
        self.assertIs(self.scheduler.completed_runtime.evidence['fixture_proxy_mode'], True)
        self.assertEqual(self.scheduler.completed_runtime.evidence['fixture_request_guard']['fixture_port'],
                         self.scheduler.local_navigation_pin.fixture_port)
        binding = self.store.connection.execute(
            'SELECT * FROM desktop_web_profile_bindings WHERE job_id=?', (self.scheduler.job_id,)).fetchone()
        self.assertEqual(binding['run_id'], self.scheduler.status()['jobs'][0]['run_id'])
        self.assertEqual(binding['profile_sha256'], self.scheduler.local_navigation_pin.profile_sha256)
        self.assertEqual(binding['task_key'], 'synthetic-local-navigation')
        self.assertEqual(binding['browser_runtime_id'], self.scheduler.completed_runtime.runtime_id)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit two-run pinned Decider local-bound page opt-in')
    async def test_two_profile_pinned_runs_yield_local_bound_page_candidate(self):
        self.configure_synthetic_navigation_pin()
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        selections = []
        for _run in range(2):
            self.start('browser_local_navigation')
            self.accept(await self.approval('browser.fixture.open'))
            self.accept(await self.approval('browser.fixture.follow'))
            await self.scheduler.task
            run_id = self.scheduler.status()['jobs'][0]['run_id']
            evidence = review_site_page_evidence(self.settings.database, run_id)
            selected = next(page for page in evidence['pages'] if page['page_key'] == 'start')
            selections.append((run_id, selected))
        self.assertEqual(selections[0][1]['page_fingerprint_sha256'],
                         selections[1][1]['page_fingerprint_sha256'])
        profile = self.scheduler.local_navigation_profiles.get(
            self.scheduler.local_navigation_pin.profile_sha256)
        page_fixture = json.loads((REPO_ROOT / 'examples/site_page_draft.json').read_text())['page']
        page = SitePageDraft.model_validate({**page_fixture,
            'profile_sha256': self.scheduler.local_navigation_pin.profile_sha256,
            'application_key': profile.application_key, 'tenant_key': profile.tenant_key,
            'account_role': profile.account_role, 'origin': profile.allowed_origins[0],
            'route_template': '/start', 'page_key': 'start', 'outgoing_page_keys': [],
            'page_fingerprint_sha256': selections[1][1]['page_fingerprint_sha256']})
        knowledge = SiteKnowledgeStore(self.root / 'knowledge', self.scheduler.local_navigation_profiles)
        checksum = digest(page.model_dump())
        knowledge.register(page, confirm_sha256=checksum)
        report = preview_local_bound_site_page_retrieval(
            self.settings.database, selections[0][0], selections[1][0],
            before_verification_id=selections[0][1]['source']['verification_id'],
            after_verification_id=selections[1][1]['source']['verification_id'],
            profiles=self.scheduler.local_navigation_profiles.root, store=knowledge.root,
            knowledge_sha256=checksum,
            selected_profile_sha256=self.scheduler.local_navigation_pin.profile_sha256)
        self.assertEqual(report['status'], 'candidate_local_profile_bound')
        self.assertTrue(report['profile_bound'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['collection_authorized'])
        self.assertFalse(report['training_ready'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider and synthetic identity gate opt-in')
    async def test_real_decider_with_opt_in_synthetic_profile_pin(self):
        self.configure_synthetic_navigation_pin()
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.start('browser_local_navigation')
        self.accept(await self.approval('browser.fixture.open'))
        self.accept(await self.approval('browser.fixture.follow'))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE status='ok' AND role='system1'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 2)

    async def test_local_navigation_pause_resume_requires_fresh_link_approval(self):
        self.start('browser_local_navigation')
        self.accept(await self.approval('browser.fixture.open'))
        old = await self.approval('browser.fixture.follow')
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertTrue(runtime.status()['running'])
        with self.assertRaises(AOSFault):
            self.accept(old)
        state = self.controller.control('resume')
        self.scheduler.resume(state['lease_id'], state['generation'])
        fresh = await self.approval('browser.fixture.follow')
        self.assertEqual(old['action']['run_id'], fresh['action']['run_id'])
        self.assertNotEqual(old['action_sha256'], fresh['action_sha256'])
        self.accept(fresh)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(runtime.perform('browser.fixture.snapshot', {})['page'], 'details')

    async def test_local_navigation_stale_link_fails_without_click_or_retry(self):
        self.start('browser_local_navigation')
        self.accept(await self.approval('browser.fixture.open'))
        following = await self.approval('browser.fixture.follow')
        runtime = self.scheduler.active_runtime
        newer = runtime.perform('browser.fixture.snapshot', {})
        self.assertNotEqual(newer['snapshot_id'], following['action']['arguments']['snapshot_id'])
        self.accept(following)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertFalse(runtime.status()['running'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.fixture.follow' AND status='ok'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.fixture.follow'").fetchone()[0], 1)

    async def test_local_navigation_takeover_revokes_pending_open_without_action(self):
        self.start('browser_local_navigation')
        pending = await self.approval('browser.fixture.open')
        runtime = self.scheduler.active_runtime
        self.controller.control('pause')
        await self.scheduler.cancel('take_control')
        self.controller.control('take-control')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'cancelled')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse(runtime.status()['running'])
        self.assertTrue(self.desktop.status()['running'])
        with self.assertRaises(AOSFault):
            self.accept(pending)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider navigation opt-in')
    async def test_real_decider_local_navigation_with_independent_verification(self):
        self.scheduler.desktop_mcp_manifest = None
        self.scheduler.desktop_navigation_mcp_manifest = MCP_MANIFEST
        self.assertEqual(self.scheduler.status()['browser_transport'], 'cdp')
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        self.start('browser_local_navigation')
        opening = await self.approval('browser.fixture.open')
        self.accept(opening)
        following = await self.approval('browser.fixture.follow')
        self.accept(following)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        decisions = self.store.connection.execute(
            'SELECT selected_option,confidence,policy_result FROM decisions ORDER BY rowid').fetchall()
        self.assertEqual([row['selected_option'] for row in decisions], ['open_start', 'follow_details'])
        self.assertTrue(all(row['confidence'] >= self.settings.execute_min
                            and row['policy_result'] == 'allow' for row in decisions))
        print('Real Decider navigation confidence:', [round(row['confidence'], 4) for row in decisions])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE status='ok' AND role='system1'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 2)
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.fixture.snapshot', {})['page'], 'details')
        run_id = self.scheduler.status()['jobs'][0]['run_id']
        review = review_learning_events(self.settings.database, run_id)
        self.assertEqual(review['event_count'], 2)
        self.assertTrue(all(event['role'] == 'system1' and event['verified_outcome']
                            and event['training_ready'] is False for event in review['events']))
        pages = review_site_page_evidence(self.settings.database, run_id)
        self.assertEqual([page['page_key'] for page in pages['pages']], ['details', 'start'])
        self.assertEqual(pages['page_count'], 2)
        self.assertTrue(all(page['reviewed'] is False and page['profile_bound'] is False
                            and page['training_ready'] is False for page in pages['pages']))
        gaps = review_learning_candidates(self.settings.database, run_id)
        self.assertEqual((gaps['system1_count'], gaps['system2_count']), (2, 0))
        self.assertTrue(all(row['has_independent_outcome'] and row['training_ready'] is False
                            for row in gaps['rows']))
