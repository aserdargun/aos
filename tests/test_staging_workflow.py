import http.client
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from aos.computer import SafetyPolicy
from aos.contracts import (AOSFault, Action, ErrorCode, Phase, REPO_ROOT, Settings,
                           STAGING_WORKFLOW_MESSAGE, STAGING_WORKFLOW_SCOPE, State, digest, identifier)
from aos.decision import FixtureDecisionEngine
from aos.desktop_tasks import DesktopScheduler
from aos.desktop_mcp_bundle import PACKAGES, prepare_bundle, read_bundle
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.local_navigation_admission import SyntheticStagingPin
from aos.reusable_decider import ReusableDeciderEngine
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from test_desktop_mcp import worker_namespace


class StagingWorkflowTests(unittest.TestCase):
    def test_identical_rerendered_submit_target_fails_before_mcp_click(self):
        worker = worker_namespace()
        browser = worker['MCPBrowser'](fixture_request_guard=True, staging_mode=True)
        browser.fixture_server = SimpleNamespace(server_port=18765, staging_submissions=0)
        snapshot_id, element_id, marker = 'a' * 32, 'b' * 32, 'c' * 32
        staging_snapshot = ({'page': 'draft', 'snapshot_id': snapshot_id,
                             'elements': [{'element_id': 'd' * 32}, {'element_id': element_id}],
                             'value': STAGING_WORKFLOW_MESSAGE}, ['textbox-ref', 'button-ref'], marker)
        browser.staging_snapshot = staging_snapshot
        browser.evaluate = Mock(return_value={
            'node_identity_matches': False,
            'controls_enabled': True,
            'page_ready': True,
            'url': 'http://127.0.0.1:18765/draft', 'title': 'Draft',
            'heading': 'Synthetic draft', 'links': [],
            'forms': [{'method': 'post', 'action': 'http://127.0.0.1:18765/submit'}],
            'inputs': [{'id': 'message', 'name': 'message', 'type': 'text', 'required': True}],
            'value': STAGING_WORKFLOW_MESSAGE, 'buttons': ['Save draft'],
            'receipt': '', 'other_controls': 0})
        browser.tool = Mock()
        self.assertEqual(browser.staging_act('browser.staging.submit', {
            'snapshot_id': snapshot_id, 'element_id': element_id}), {'error': 'UI_CHANGED'})
        browser.tool.assert_not_called()
        self.assertEqual(browser.fixture_server.staging_submissions, 0)
        self.assertIsNone(browser.staging_snapshot)

        browser.staging_snapshot = staging_snapshot
        browser.evaluate.return_value['node_identity_matches'] = True
        browser.evaluate.return_value['controls_enabled'] = False
        self.assertEqual(browser.staging_act('browser.staging.submit', {
            'snapshot_id': snapshot_id, 'element_id': element_id}), {'error': 'UI_CHANGED'})
        browser.tool.assert_not_called()
        self.assertEqual(browser.fixture_server.staging_submissions, 0)
        self.assertIsNone(browser.staging_snapshot)

        browser.evaluate.return_value['controls_enabled'] = True
        browser.evaluate.return_value['page_ready'] = False
        browser.staging_snapshot = staging_snapshot
        self.assertEqual(browser.staging_act('browser.staging.submit', {
            'snapshot_id': snapshot_id, 'element_id': element_id}), {'error': 'UI_CHANGED'})
        browser.tool.assert_not_called()
        self.assertEqual(browser.fixture_server.staging_submissions, 0)
        self.assertIsNone(browser.staging_snapshot)

        browser.evaluate.return_value['page_ready'] = True
        self.assertEqual(browser.staging_act('browser.staging.submit', {
            'snapshot_id': snapshot_id, 'element_id': element_id}), {'error': 'UI_CHANGED'})
        browser.tool.assert_not_called()

    def test_v9_upgrade_preserves_job_and_approval_and_limits_new_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'legacy.sqlite'
            connection = sqlite3.connect(database)
            self.addCleanup(connection.close)
            migrations = sorted((REPO_ROOT / 'database/migrations').glob('*.sql'))
            for migration in migrations[:9]:
                connection.executescript(migration.read_text())
            connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','image','AGENT','lease',0,'running','synthetic','synthetic')")
            connection.execute("INSERT INTO desktop_tasks VALUES('job','session',NULL,'browser_local_navigation','lease',0,'succeeded',0,'synthetic','synthetic',NULL)")
            connection.execute("INSERT INTO desktop_approvals VALUES('approval','job','{}',?,123,'consumed','synthetic','synthetic')", ('0' * 64,))
            connection.commit()
            connection.executescript(migrations[9].read_text())
            self.assertEqual(connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?', ('job',)).fetchone()[0],
                             'browser_local_navigation')
            self.assertEqual(connection.execute('SELECT status FROM desktop_approvals WHERE approval_id=?', ('approval',)).fetchone()[0],
                             'consumed')
            connection.execute("INSERT INTO desktop_tasks VALUES('staging','session',NULL,'browser_staging_workflow','lease',0,'queued',0,'synthetic','synthetic',NULL)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO desktop_tasks VALUES('arbitrary','session',NULL,'arbitrary_web','lease',0,'cancelled',0,'synthetic','synthetic',NULL)")
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 10)
            connection.executescript(migrations[10].read_text())
            self.assertEqual(connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 11)
            self.assertEqual(connection.execute('SELECT count(*) FROM desktop_web_profile_bindings').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT kind FROM desktop_tasks WHERE job_id=?', ('job',)).fetchone()[0],
                             'browser_local_navigation')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def state(self):
        return State(task_id='task', run_id='run', step_id='step', runtime_id='runtime',
                     deployment_id='fixture-decision-v1', owner_lease_id='lease',
                     task_kind='browser_staging_workflow', authorized_path=STAGING_WORKFLOW_SCOPE,
                     authorized_content=STAGING_WORKFLOW_MESSAGE, phase=Phase.EXECUTE)

    def action(self, tool='browser.staging.open', option='open_app', arguments=None):
        return Action(task_id='task', run_id='run', step_id='step', action_id=identifier('action'),
                      runtime_id='runtime', state_version=0, owner_lease_id='lease', tool=tool,
                      arguments={} if arguments is None else arguments, expected_effect='synthetic staging',
                      deadline=time.time() + 10,
                      idempotency_key=identifier('intent'), selected_option=option)

    def test_only_fixed_staging_tools_options_and_exact_value_are_authorized(self):
        state = self.state()
        reference = {'snapshot_id': 'a' * 32, 'element_id': 'b' * 32}
        for tool, option, arguments in (
                ('browser.staging.open', 'open_app', {}),
                ('browser.staging.follow', 'follow_draft', reference),
                ('browser.staging.fill', 'fill_message', {**reference, 'value': STAGING_WORKFLOW_MESSAGE}),
                ('browser.staging.submit', 'submit_draft', reference)):
            SafetyPolicy.check(self.action(tool, option, arguments), state, 'runtime')
        for tool, option, arguments in (
                ('browser.staging.open', 'open_app', {'url': 'http://127.0.0.1/other'}),
                ('browser.staging.follow', 'open_app', reference),
                ('browser.staging.fill', 'fill_message', {**reference, 'value': 'secret'}),
                ('browser.staging.submit', 'submit_draft', {**reference, 'redirect': 'https://example.invalid'}),
                ('browser.fixture.open', 'open_app', {})):
            with self.subTest(tool=tool, option=option), self.assertRaises(AOSFault):
                SafetyPolicy.check(self.action(tool, option, arguments), state, 'runtime')
        with self.assertRaises(AOSFault):
            SafetyPolicy.check(self.action(), state.model_copy(update={'authorized_path': 'aos://other'}), 'runtime')

    def test_fixture_server_exact_routes_body_redirect_and_one_submission(self):
        worker = worker_namespace()
        server = worker['HTTPServer'](('127.0.0.1', 0), worker['FixtureHandler'])
        server.staging_mode = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f'http://127.0.0.1:{server.server_port}'

        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        self.assertEqual(request('GET', '/receipt')[0], 404)
        self.assertEqual(request('GET', '/start')[0], 404)
        self.assertEqual(request('GET', '/app')[0], 200)
        self.assertEqual(request('GET', '/draft')[0], 200)
        body = worker['STAGING_FORM_BODY']
        headers = {'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'}
        for method, path, invalid_body, invalid_headers in (
                ('POST', '/other', body, headers),
                ('POST', '/submit', body + b'&extra=1', headers),
                ('POST', '/submit', body, {'Content-Type': 'application/x-www-form-urlencoded'}),
                ('POST', '/submit', body, {**headers, 'Host': 'example.invalid'}),
                ('POST', '/submit', body, {**headers, 'Content-Type': 'text/plain'}),
                ('GET', '/favicon.ico', None, {})):
            with self.subTest(path=path, headers=invalid_headers):
                status, response_headers, response_body = request(method, path, invalid_body, invalid_headers)
                self.assertEqual(status, 404)
                self.assertEqual(response_body, b'')
                self.assertNotIn('Location', response_headers)
        status, response_headers, response_body = request('POST', '/submit', body, headers)
        self.assertEqual((status, response_headers['Location'], response_body), (303, '/receipt', b''))
        self.assertEqual(request('POST', '/submit', body, headers)[0], 404)
        status, _response_headers, receipt = request('GET', '/receipt')
        self.assertEqual(status, 200)
        self.assertIn(b'Saved locally.', receipt)
        self.assertEqual(server.staging_submissions, 1)

    def test_staging_guard_rejects_other_worker_tools_before_open(self):
        worker = worker_namespace()
        browser = SimpleNamespace(staging_mode=True, staging_opened=False)
        for tool, arguments in (
                ('browser.fixture.open', {}), ('browser.fixture.snapshot', {}),
                ('browser.fill', {}), ('browser.submit', {}), ('browser.verify', {}),
                ('browser.staging.follow', {})):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                worker['dispatch'](browser, {'tool': tool, 'arguments': arguments})

    def test_scheduler_exposes_staging_only_with_pinned_explicit_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / 'node_modules'
            for package, version in PACKAGES.items():
                package_root = modules / package
                package_root.mkdir(parents=True)
                metadata = {'name': package, 'version': version}
                if package == '@playwright/mcp':
                    metadata['dependencies'] = {name: PACKAGES[name] for name in ('playwright', 'playwright-core')}
                (package_root / 'package.json').write_text(json.dumps(metadata))
            manifest = prepare_bundle(modules, root / 'bundle')
            controller = SimpleNamespace(store=SimpleNamespace())
            settings = Settings(workspace=root / 'workspace', database=root / 'store.sqlite')
            plain = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                     browser_manifest=Path('synthetic'), desktop_browser=True)
            self.assertNotIn('browser_staging_workflow', plain.kinds())
            staging = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                       browser_manifest=Path('synthetic'), desktop_browser=True,
                                       desktop_staging_mcp_manifest=manifest)
            self.assertIn('browser_staging_workflow', staging.kinds())
            pinned = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                      browser_manifest=Path('synthetic'), desktop_browser=True,
                                      desktop_staging_mcp_manifest=manifest,
                                      staging_fixture_port=32123)
            self.assertEqual(pinned.staging_fixture_port, 32123)
            for selected in (True, 80, 65536):
                with self.subTest(port=selected), self.assertRaises(ValueError):
                    DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                     browser_manifest=Path('synthetic'), desktop_browser=True,
                                     desktop_staging_mcp_manifest=manifest,
                                     staging_fixture_port=selected)
            with self.assertRaises(ValueError):
                DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                 browser_manifest=Path('synthetic'), desktop_browser=True,
                                 staging_fixture_port=32123)
            with self.assertRaises(AOSFault) as caught:
                staging._start('lease', 0, 'browser_staging_workflow', approve_all=True)
            self.assertEqual(caught.exception.code, ErrorCode.UNSAFE_ACTION)
            (manifest.parent / 'packages.zip').write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                 browser_manifest=Path('synthetic'), desktop_browser=True,
                                 desktop_staging_mcp_manifest=manifest)


MCP_ENABLED = os.environ.get('AOS_DESKTOP_MCP_TESTS') == '1' and os.environ.get('AOS_DESKTOP_TESTS') == '1'


@unittest.skipUnless(MCP_ENABLED, 'Opt in to real owned Ubuntu/Chromium/MCP staging workflow')
class StagingWorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_visible_scheduler import VisibleSchedulerTests

        await VisibleSchedulerTests.asyncSetUp(self)
        self.scheduler.desktop_staging_mcp_manifest = REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'

    async def asyncTearDown(self):
        from test_visible_scheduler import VisibleSchedulerTests

        await VisibleSchedulerTests.asyncTearDown(self)

    async def approval(self, tool):
        from test_visible_scheduler import VisibleSchedulerTests

        return await VisibleSchedulerTests.approval(self, tool)

    def accept(self, pending):
        from test_visible_scheduler import VisibleSchedulerTests

        return VisibleSchedulerTests.accept(self, pending)

    def pinned_fixture_port(self):
        return int(self.desktop.docker(['exec', self.desktop.container_id, '/usr/bin/python3',
            '-c', 'import socket; connection=socket.socket(); connection.bind(("127.0.0.1",0)); '
                  'print(connection.getsockname()[1]); connection.close()']).decode().strip())

    def configure_synthetic_staging_pin(self):
        port = self.pinned_fixture_port()
        fixture = json.loads((REPO_ROOT / 'examples/synthetic_staging_pin.json').read_text())
        origin = f'http://127.0.0.1:{port}'
        fixture['profile']['entry_url'] = origin + '/app'
        fixture['profile']['allowed_origins'] = [origin]
        profile = WebApplicationProfile.model_validate(fixture['profile'])
        profiles = WebApplicationProfiles(self.root / 'profiles')
        checksum = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=checksum)
        bundle, _payload = read_bundle(REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
        candidate = DesktopMCPBrowserRuntime(self.desktop,
                                             REPO_ROOT / 'models/desktop-mcp-v001/manifest.json',
                                             fixture_request_guard=True, staging_workflow=True,
                                             fixture_port=port)
        pin = SyntheticStagingPin.model_validate({**fixture['pin'],
            'profile_sha256': checksum, 'fixture_port': port,
            'parent_runtime_id': self.desktop.runtime_id,
            'container_id': self.desktop.container_id,
            'image_id': self.desktop.pins['image_id'],
            'mcp_bundle_sha256': bundle['bundle_sha256'],
            'worker_sha256': hashlib.sha256(candidate.worker_source().encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(
                (REPO_ROOT / 'examples' / candidate.fixture_file).read_bytes()).hexdigest(),
            'runtime_digest': digest({**self.desktop.pins, 'mcp': bundle})})
        self.scheduler.staging_fixture_port = port
        self.scheduler.local_staging_profiles = profiles
        self.scheduler.local_staging_pin = pin
        return profiles, checksum, port

    async def test_four_manual_approvals_and_independent_receipt(self):
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        pending = await self.approval('browser.staging.open')
        for forbidden in ('browser.fixture.open', 'browser.fill', 'browser.submit'):
            with self.subTest(tool=forbidden), self.assertRaises(AOSFault):
                self.scheduler.active_runtime.perform(forbidden, {})
        self.accept(pending)
        for tool in ('browser.staging.follow', 'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.completed_runtime.valid_fixture_request_guard())
        self.assertEqual(self.scheduler.completed_runtime.perform('browser.staging.snapshot', {})['page'], 'receipt')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.staging.submit' AND status='ok'").fetchone()[0], 1)

    async def test_pinned_proxy_four_approvals_and_independent_receipt(self):
        port = self.pinned_fixture_port()
        self.scheduler.staging_fixture_port = port
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.completed_runtime.evidence['fixture_proxy_mode'])
        self.assertEqual(self.scheduler.completed_runtime.evidence['fixture_request_guard']['fixture_port'], port)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.staging.submit' AND status='ok'").fetchone()[0], 1)

    async def test_profile_pinned_proxy_four_approvals_and_independent_receipt(self):
        _profiles, checksum, port = self.configure_synthetic_staging_pin()
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler._local_staging_runtime_id, None)
        self.assertTrue(self.scheduler.completed_runtime.evidence['fixture_proxy_mode'])
        self.assertEqual(self.scheduler.completed_runtime.evidence['fixture_request_guard']['fixture_port'], port)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)
        binding = self.store.connection.execute(
            'SELECT * FROM desktop_web_profile_bindings WHERE job_id=?', (self.scheduler.job_id,)).fetchone()
        self.assertEqual(binding['run_id'], self.scheduler.status()['jobs'][0]['run_id'])
        self.assertEqual(binding['profile_sha256'], checksum)
        self.assertEqual(binding['task_key'], 'synthetic-staging-workflow')
        self.assertEqual(binding['pin_sha256'], digest(json.loads(binding['pin_json'])))
        self.assertEqual(binding['browser_runtime_id'], self.scheduler.completed_runtime.runtime_id)

    async def test_profile_change_before_approved_post_blocks_action(self):
        profiles, checksum, _port = self.configure_synthetic_staging_pin()
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow', 'browser.staging.fill'):
            self.accept(await self.approval(tool))
        pending = await self.approval('browser.staging.submit')
        (profiles.root / (checksum + '.json')).chmod(0o644)
        self.accept(pending)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.staging.submit' AND status='ok'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 3)
        self.assertEqual(self.store.connection.execute(
            'SELECT count(*) FROM desktop_web_profile_bindings WHERE job_id=?',
            (self.scheduler.job_id,)).fetchone()[0], 1)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider staging proxy opt-in')
    async def test_real_decider_pinned_proxy_staging_workflow(self):
        port = self.pinned_fixture_port()
        self.scheduler.staging_fixture_port = port
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertTrue(self.scheduler.completed_runtime.evidence['fixture_proxy_mode'])
        self.assertEqual(self.scheduler.completed_runtime.evidence['fixture_request_guard']['fixture_port'], port)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider profile-bound staging opt-in')
    async def test_real_decider_profile_pinned_proxy_staging_workflow(self):
        _profiles, checksum, port = self.configure_synthetic_staging_pin()
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertTrue(self.scheduler.completed_runtime.evidence['fixture_proxy_mode'])
        self.assertEqual(self.scheduler.completed_runtime.evidence['fixture_request_guard']['fixture_port'], port)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            'SELECT profile_sha256 FROM desktop_web_profile_bindings WHERE run_id=?',
            (self.scheduler.status()['jobs'][0]['run_id'],)).fetchone()[0], checksum)

    async def test_stale_submit_reference_fails_without_post_or_retry(self):
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow', 'browser.staging.fill'):
            self.accept(await self.approval(tool))
        pending = await self.approval('browser.staging.submit')
        runtime = self.scheduler.active_runtime
        fresh = runtime.perform('browser.staging.snapshot', {})
        self.assertNotEqual(fresh['snapshot_id'], pending['action']['arguments']['snapshot_id'])
        self.accept(pending)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM actions WHERE tool='browser.staging.submit' AND status='ok'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed'").fetchone()[0], 3)

    @unittest.skipUnless(os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1',
                         'Explicit real pinned Decider staging opt-in')
    async def test_real_decider_four_step_staging_workflow(self):
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertTrue(self.scheduler.status()['jobs'][0]['real_model'])
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system1' AND status='ok'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM model_calls WHERE role='system2'").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE status='consumed'").fetchone()[0], 4)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM verifications WHERE result='passed' AND expected_json=actual_json").fetchone()[0], 4)
