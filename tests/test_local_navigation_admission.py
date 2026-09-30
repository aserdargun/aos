import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import AOSFault, REPO_ROOT, Settings, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.desktop_tasks import DesktopScheduler, LeasedGateway
from aos.local_navigation_admission import (
    SyntheticNavigationPin, check_synthetic_navigation_preflight,
    check_synthetic_navigation_profile, check_synthetic_navigation_runtime)
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report


FIXTURE = json.loads((REPO_ROOT / 'examples/synthetic_navigation_pin.json').read_text())


class SyntheticNavigationAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(FIXTURE['profile'])
        self.profile_sha256 = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha256)
        self.bundle = {'bundle_sha256': 'd' * 64}
        self.desktop_pins = {'image_id': 'sha256:' + 'c' * 64,
                             'chromium_sha256': '2' * 64}
        self.desktop = Mock(runtime_id='desktop-' + 'a' * 32,
                            container_id='b' * 64, manifest=Path('synthetic'), pins=self.desktop_pins)
        self.desktop.status.return_value = {
            'kind': 'docker_xfce', 'running': True, 'network': False,
            'runtime_id': self.desktop.runtime_id, 'container_id': self.desktop.container_id,
            'image_id': self.desktop_pins['image_id'],
            'workspace_identity': {'synthetic': True}, 'lifecycle_ref': 'synthetic'}
        self.runtime = DesktopMCPBrowserRuntime(self.desktop, self.root / 'mcp.json',
                                                fixture_request_guard=True, fixture_port=32123)
        self.runtime.owned_desktop = Mock()
        self.runtime.pins = {**self.desktop_pins, 'mcp': self.bundle}
        self.runtime.process = Mock()
        self.runtime.process.poll.return_value = None
        self.runtime.status = Mock(return_value={
            'kind': 'docker_chromium_mcp', 'browser_transport': 'playwright_mcp',
            'staging_workflow': False,
            'runtime_id': self.runtime.runtime_id, 'parent_runtime_id': self.desktop.runtime_id,
            'container_id': self.desktop.container_id, 'image_id': self.desktop_pins['image_id'],
            'worker_sha256': hashlib.sha256(self.runtime.worker_source().encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(
                (REPO_ROOT / 'examples' / self.runtime.fixture_file).read_bytes()).hexdigest(),
            'runtime_digest': digest(self.runtime.pins), 'network': False, 'running': True,
            'real_execution': True, 'desktop': True,
            'browser_display': 'desktop', 'fixture_request_guard': True,
            'isolation': {'ready': True, 'headed': True, 'display': ':99',
                          'transport': 'playwright_mcp', 'bundle_sha256': self.bundle['bundle_sha256'],
                          'staging_workflow': False,
                          'server_version': '1.64.0-alpha-1789764292000',
                          'network_namespace': 'synthetic-isolated',
                          'chromium_sha256': self.desktop_pins['chromium_sha256'],
                          'home_visible': False, 'docker_socket_visible': False,
                          'fixture_proxy_mode': True,
                          'page_request_gate': True,
                          'fixture_request_guard': {'kind': 'landlock_tcp_connect_v1',
                                                    'fixture_port': 32123, 'verified': True}}})
        self.runtime.evidence = self.runtime.status.return_value['isolation']
        self.pin = SyntheticNavigationPin.model_validate({**FIXTURE['pin'],
            'profile_sha256': self.profile_sha256,
            'worker_sha256': self.runtime.status()['worker_sha256'],
            'fixture_sha256': self.runtime.status()['fixture_sha256'],
            'runtime_digest': digest(self.runtime.pins)})

    def check_runtime(self):
        with patch('aos.local_navigation_admission.read_bundle', return_value=(self.bundle, b'synthetic')):
            return check_synthetic_navigation_runtime(self.profiles, self.pin, self.runtime)

    def test_canonical_schema_fixture_and_authority_boundary(self):
        self.assertTrue(FIXTURE['synthetic'])
        self.assertEqual(self.profile_sha256, FIXTURE['pin']['profile_sha256'])
        schema = json.loads((REPO_ROOT / 'schemas/synthetic_navigation_pin.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SyntheticNavigationPin.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(FIXTURE['pin'])
        self.assertFalse(self.pin.collection_authorized)
        for changes in ({'network_mode': 'bridge'}, {'collection_authorized': True},
                        {'collection_authorized': 0}, {'task_key': 'other-task'},
                        {'fixture_port': True}, {'fixture_port': '32123'}, {'fixture_port': 80}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SyntheticNavigationPin.model_validate({**self.pin.model_dump(), **changes})

    def test_profile_scope_exact_and_remote_or_wrong_origin_rejected(self):
        self.assertEqual(check_synthetic_navigation_profile(self.profiles, self.pin), self.profile)
        with self.assertRaises((ValueError, FileNotFoundError)):
            check_synthetic_navigation_profile(self.profiles, SyntheticNavigationPin.model_validate(
                {**self.pin.model_dump(), 'profile_sha256': '0' * 64}))
        for profile_changes in (
                {'task_keys': ['other-task']},
                {'entry_url': 'http://localhost/start', 'allowed_origins': ['http://localhost']},
                {'entry_url': 'http://127.0.0.1/other',
                 'allowed_origins': ['http://127.0.0.1']},
                {'entry_url': 'http://127.0.0.1:32124/start',
                 'allowed_origins': ['http://127.0.0.1:32124']},
                {'entry_url': 'https://crm.example.invalid/start',
                 'allowed_origins': ['https://crm.example.invalid'], 'environment': 'staging'},
        ):
            with self.subTest(profile_changes=profile_changes):
                changed = WebApplicationProfile.model_validate({**self.profile.model_dump(), **profile_changes})
                checksum = profile_report(changed).profile_sha256
                self.profiles.register(changed, confirm_sha256=checksum)
                selected = SyntheticNavigationPin.model_validate(
                    {**self.pin.model_dump(), 'profile_sha256': checksum})
                with self.assertRaises(ValueError):
                    check_synthetic_navigation_profile(self.profiles, selected)

    def test_runtime_match_is_only_synthetic_preflight_and_fails_closed_on_drift(self):
        report = self.check_runtime()
        self.assertEqual(report['status'], 'synthetic_runtime_match_only')
        self.assertEqual(report['browser_runtime_id'], self.runtime.runtime_id)
        self.assertTrue(report['profile_to_origin_bound'])
        self.assertFalse(report['account_role_verified'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['collection_authorized'])
        for field, value in (('parent_runtime_id', 'desktop-' + 'f' * 32),
                             ('container_id', 'f' * 64), ('image_id', 'sha256:' + 'f' * 64),
                             ('runtime_digest', 'f' * 64), ('network', True),
                             ('running', False), ('worker_sha256', 'f' * 64)):
            with self.subTest(field=field):
                original = self.runtime.status.return_value[field]
                self.runtime.status.return_value[field] = value
                with self.assertRaises(ValueError):
                    self.check_runtime()
                self.runtime.status.return_value[field] = original
        self.runtime.owned_desktop.side_effect = ValueError('ownership changed')
        with self.assertRaises(ValueError):
            self.check_runtime()
        self.runtime.owned_desktop.side_effect = None
        self.runtime.process.poll.return_value = 1
        with self.assertRaises(ValueError):
            self.check_runtime()

    def test_request_guard_attestation_is_required_not_just_selected(self):
        isolation = self.runtime.evidence
        guard = isolation['fixture_request_guard']
        for changed in (None, {}, {'kind': 'landlock_tcp_connect_v1',
                                   'fixture_port': 32123, 'verified': False},
                        {'kind': 'landlock_tcp_connect_v1',
                         'fixture_port': True, 'verified': True}):
            with self.subTest(changed=changed):
                isolation['fixture_request_guard'] = changed
                with self.assertRaises(ValueError):
                    self.check_runtime()
        isolation['fixture_request_guard'] = guard
        isolation['fixture_request_guard'] = {**guard, 'fixture_port': 32124}
        with self.assertRaises(ValueError):
            self.check_runtime()
        isolation['fixture_request_guard'] = guard
        isolation['fixture_proxy_mode'] = False
        with self.assertRaises(ValueError):
            self.check_runtime()
        isolation['fixture_proxy_mode'] = True
        isolation['page_request_gate'] = False
        with self.assertRaises(ValueError):
            self.check_runtime()
        isolation['page_request_gate'] = True
        self.runtime.status.return_value['fixture_request_guard'] = False
        with self.assertRaises(ValueError):
            self.check_runtime()

    def test_preflight_checks_selected_pins_before_launch(self):
        candidate = Mock(fixture_file=self.runtime.fixture_file)
        candidate.worker_source.return_value = self.runtime.worker_source()
        candidate.owned_desktop.return_value = None
        with (patch('aos.local_navigation_admission.DesktopMCPBrowserRuntime',
                    return_value=candidate) as constructor,
              patch('aos.local_navigation_admission.read_bundle',
                    return_value=(self.bundle, b'synthetic'))):
            check_synthetic_navigation_preflight(self.profiles, self.pin, self.desktop, self.root / 'mcp.json')
            constructor.assert_called_once_with(self.desktop, self.root / 'mcp.json',
                                                fixture_request_guard=True, fixture_port=32123)
            self.assertEqual(candidate.owned_desktop.call_count, 2)
            changed = SyntheticNavigationPin.model_validate({**self.pin.model_dump(),
                                                               'mcp_bundle_sha256': '0' * 64})
            with self.assertRaises(ValueError):
                check_synthetic_navigation_preflight(self.profiles, changed, self.desktop,
                                                     self.root / 'mcp.json')

    def test_scheduler_opt_in_rejects_preflight_before_job_and_rechecks_gateway(self):
        controller = SimpleNamespace(runtime=self.desktop, session_id='session',
                                     state=lambda: {'owner': 'AGENT', 'status': 'running',
                                                    'lease_id': 'lease', 'generation': 0,
                                                    'runtime_id': self.desktop.runtime_id})
        controller.store = Mock()
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, Settings(workspace=self.root / 'workspace',
                                                  database=self.root / 'store.sqlite'),
                             FixtureDecisionEngine(), browser_manifest=Path('synthetic'),
                             desktop_browser=True, desktop_mcp_manifest=self.root / 'mcp.json',
                             local_navigation_pin=self.pin)
        scheduler = DesktopScheduler(controller, Settings(workspace=self.root / 'workspace',
                                                           database=self.root / 'store.sqlite'),
                                     FixtureDecisionEngine(),
                                     browser_manifest=Path('synthetic'), desktop_browser=True,
                                     desktop_mcp_manifest=self.root / 'mcp.json',
                                     local_navigation_profiles=self.profiles,
                                     local_navigation_pin=self.pin)
        scheduler.store.connection.execute.return_value.fetchone.return_value = None
        with patch('aos.local_navigation_admission.check_synthetic_navigation_preflight',
                   side_effect=ValueError('wrong runtime')):
            with self.assertRaises(AOSFault):
                scheduler.start('lease', 0, 'browser_local_navigation')
        self.assertIsNone(scheduler.job_id)
        self.assertTrue(all(call.args[0].startswith('SELECT')
                            for call in controller.store.connection.execute.call_args_list))
        scheduler.job_id = 'job-test'
        scheduler.active_runtime = self.runtime
        scheduler._local_navigation_runtime_id = self.runtime.runtime_id
        scheduler.store.connection.execute.return_value.fetchone.return_value = {
            'kind': 'browser_local_navigation'}
        with patch('aos.local_navigation_admission.check_synthetic_navigation_runtime',
                   return_value={'browser_runtime_id': self.runtime.runtime_id}):
            scheduler.check_local_navigation_binding('job-test')
        scheduler.store.connection.execute.return_value.fetchone.return_value = {'kind': 'browser_form'}
        with self.assertRaises(AOSFault):
            scheduler.check_local_navigation_binding('job-test')
        scheduler.store.connection.execute.return_value.fetchone.return_value = {
            'kind': 'browser_local_navigation'}
        with patch('aos.local_navigation_admission.check_synthetic_navigation_runtime',
                   return_value={'browser_runtime_id': 'browser-' + 'f' * 32}):
            with self.assertRaises(AOSFault):
                scheduler.check_local_navigation_binding('job-test')
            with (patch.object(scheduler, 'check_lease'),
                  patch('aos.desktop_tasks.ComputerGateway.execute') as execute):
                gateway = LeasedGateway(scheduler, 'job-test', self.runtime)
                with self.assertRaises(AOSFault):
                    gateway.execute(Mock(), 'decision-test')
                execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
