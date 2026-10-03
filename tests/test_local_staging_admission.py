import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import AOSFault, REPO_ROOT, Settings, canonical, digest, now
from aos.decision import FixtureDecisionEngine
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.desktop_tasks import DesktopScheduler, LeasedGateway
from aos.local_navigation_admission import (
    SyntheticStagingPin, check_synthetic_staging_preflight,
    check_synthetic_staging_profile, check_synthetic_staging_runtime)
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.storage import TrajectoryStore


FIXTURE = json.loads((REPO_ROOT / 'examples/synthetic_staging_pin.json').read_text())
LATEST_MIGRATION = max(int(path.name.split('_', 1)[0])
                       for path in (REPO_ROOT / 'database/migrations').glob('*.sql'))


class SyntheticStagingAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profile = WebApplicationProfile.model_validate(FIXTURE['profile'])
        self.profile_sha256 = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha256)
        self.bundle = {'bundle_sha256': 'd' * 64}
        bundle_reader = patch('aos.desktop_mcp_bundle.read_bundle',
                              return_value=(self.bundle, b'synthetic'))
        bundle_reader.start()
        self.addCleanup(bundle_reader.stop)
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
                                                fixture_request_guard=True, staging_workflow=True,
                                                fixture_port=32123)
        self.runtime.owned_desktop = Mock()
        self.runtime.pins = {**self.desktop_pins, 'mcp': self.bundle}
        self.runtime.process = Mock()
        self.runtime.process.poll.return_value = None
        self.runtime.status = Mock(return_value={
            'kind': 'docker_chromium_mcp', 'browser_transport': 'playwright_mcp',
            'staging_workflow': True, 'runtime_id': self.runtime.runtime_id,
            'parent_runtime_id': self.desktop.runtime_id,
            'container_id': self.desktop.container_id, 'image_id': self.desktop_pins['image_id'],
            'worker_sha256': hashlib.sha256(self.runtime.worker_source().encode()).hexdigest(),
            'fixture_sha256': hashlib.sha256(
                (REPO_ROOT / 'examples' / self.runtime.fixture_file).read_bytes()).hexdigest(),
            'runtime_digest': digest(self.runtime.pins), 'network': False, 'running': True,
            'real_execution': True, 'desktop': True, 'browser_display': 'desktop',
            'fixture_request_guard': True,
            'isolation': {'ready': True, 'headed': True, 'display': ':99',
                          'transport': 'playwright_mcp', 'staging_workflow': True,
                          'bundle_sha256': self.bundle['bundle_sha256'],
                          'server_version': '1.64.0-alpha-1789764292000',
                          'network_namespace': 'synthetic-isolated',
                          'chromium_sha256': self.desktop_pins['chromium_sha256'],
                          'home_visible': False, 'docker_socket_visible': False,
                          'fixture_proxy_mode': True,
                          'page_request_gate': True,
                          'fixture_request_guard': {'kind': 'landlock_tcp_connect_v1',
                                                    'fixture_port': 32123, 'verified': True}}})
        self.runtime.evidence = self.runtime.status.return_value['isolation']
        self.pin = SyntheticStagingPin.model_validate({**FIXTURE['pin'],
            'profile_sha256': self.profile_sha256,
            'worker_sha256': self.runtime.status()['worker_sha256'],
            'fixture_sha256': self.runtime.status()['fixture_sha256'],
            'runtime_digest': digest(self.runtime.pins)})

    def check_runtime(self):
        with patch('aos.local_navigation_admission.read_bundle', return_value=(self.bundle, b'synthetic')):
            return check_synthetic_staging_runtime(self.profiles, self.pin, self.runtime)

    def test_canonical_fixture_and_false_authority(self):
        self.assertTrue(FIXTURE['synthetic'])
        self.assertEqual(self.profile_sha256, FIXTURE['pin']['profile_sha256'])
        schema = json.loads((REPO_ROOT / 'schemas/synthetic_staging_pin.schema.json').read_text())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         SyntheticStagingPin.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(FIXTURE['pin'])
        for changes in ({'task_key': 'synthetic-local-navigation'},
                        {'network_mode': 'bridge'}, {'collection_authorized': True},
                        {'collection_authorized': 0}, {'fixture_port': True},
                        {'fixture_port': 80}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SyntheticStagingPin.model_validate({**self.pin.model_dump(), **changes})

    def test_profile_exact_app_route_and_task_required(self):
        self.assertEqual(check_synthetic_staging_profile(self.profiles, self.pin), self.profile)
        for changes in ({'task_keys': ['other-task']},
                        {'entry_url': 'http://127.0.0.1:32123/start'},
                        {'entry_url': 'http://127.0.0.1:32124/app',
                         'allowed_origins': ['http://127.0.0.1:32124']},
                        {'entry_url': 'https://example.invalid/app',
                         'allowed_origins': ['https://example.invalid'], 'environment': 'staging'}):
            with self.subTest(changes=changes):
                profile = WebApplicationProfile.model_validate({**self.profile.model_dump(), **changes})
                checksum = profile_report(profile).profile_sha256
                self.profiles.register(profile, confirm_sha256=checksum)
                pin = SyntheticStagingPin.model_validate({**self.pin.model_dump(),
                                                          'profile_sha256': checksum})
                with self.assertRaises(ValueError):
                    check_synthetic_staging_profile(self.profiles, pin)

    def test_runtime_and_guard_drift_fail_closed(self):
        report = self.check_runtime()
        self.assertEqual(report['status'], 'synthetic_runtime_match_only')
        self.assertTrue(report['profile_to_origin_bound'])
        self.assertFalse(report['account_role_verified'])
        self.assertFalse(report['execution_authorized'])
        self.assertFalse(report['collection_authorized'])
        for field, value in (('staging_workflow', False), ('container_id', 'f' * 64),
                             ('network', True), ('running', False), ('worker_sha256', 'f' * 64)):
            with self.subTest(field=field):
                original = self.runtime.status.return_value[field]
                self.runtime.status.return_value[field] = value
                with self.assertRaises(ValueError):
                    self.check_runtime()
                self.runtime.status.return_value[field] = original
        isolation = self.runtime.evidence
        for field, value in (('fixture_proxy_mode', False), ('page_request_gate', False),
                             ('staging_workflow', False),
                             ('fixture_request_guard', {'kind': 'landlock_tcp_connect_v1',
                                                       'fixture_port': 32124, 'verified': True})):
            with self.subTest(field=field):
                original = isolation[field]
                isolation[field] = value
                with self.assertRaises(ValueError):
                    self.check_runtime()
                isolation[field] = original
        self.runtime.process.poll.return_value = 1
        with self.assertRaises(ValueError):
            self.check_runtime()

    def test_preflight_and_scheduler_recheck_before_action(self):
        candidate = Mock(fixture_file=self.runtime.fixture_file)
        candidate.worker_source.return_value = self.runtime.worker_source()
        with (patch('aos.local_navigation_admission.DesktopMCPBrowserRuntime',
                    return_value=candidate) as constructor,
              patch('aos.local_navigation_admission.read_bundle',
                    return_value=(self.bundle, b'synthetic'))):
            check_synthetic_staging_preflight(self.profiles, self.pin, self.desktop, self.root / 'mcp.json')
            constructor.assert_called_once_with(self.desktop, self.root / 'mcp.json',
                                                fixture_request_guard=True, staging_workflow=True,
                                                fixture_port=32123)
            wrong = SyntheticStagingPin.model_validate({**self.pin.model_dump(),
                                                        'fixture_sha256': '0' * 64})
            with self.assertRaises(ValueError):
                check_synthetic_staging_preflight(self.profiles, wrong, self.desktop,
                                                  self.root / 'mcp.json')
        controller = SimpleNamespace(runtime=self.desktop, session_id='session',
                                     state=lambda: {'owner': 'AGENT', 'status': 'running',
                                                    'lease_id': 'lease', 'generation': 0,
                                                    'runtime_id': self.desktop.runtime_id}, store=Mock())
        settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                             browser_manifest=Path('synthetic'), desktop_browser=True,
                             desktop_staging_mcp_manifest=self.root / 'mcp.json',
                             local_staging_pin=self.pin)
        with self.assertRaises(ValueError):
            DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                             browser_manifest=Path('synthetic'), desktop_browser=True,
                             desktop_staging_mcp_manifest=self.root / 'mcp.json',
                             staging_fixture_port=32124,
                             local_staging_profiles=self.profiles, local_staging_pin=self.pin)
        scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine(),
                                     browser_manifest=Path('synthetic'), desktop_browser=True,
                                     desktop_staging_mcp_manifest=self.root / 'mcp.json',
                                     local_staging_profiles=self.profiles, local_staging_pin=self.pin)
        self.assertEqual(scheduler.staging_fixture_port, 32123)
        scheduler.store.connection.execute.return_value.fetchone.return_value = None
        with patch('aos.local_navigation_admission.check_synthetic_staging_preflight',
                   side_effect=ValueError('wrong runtime')):
            with self.assertRaises(AOSFault):
                scheduler.start('lease', 0, 'browser_staging_workflow')
        self.assertIsNone(scheduler.job_id)
        scheduler.job_id = 'job-test'
        scheduler.active_runtime = self.runtime
        scheduler._local_staging_runtime_id = self.runtime.runtime_id
        scheduler.store.connection.execute.return_value.fetchone.return_value = {'kind': 'browser_staging_workflow'}
        with patch('aos.local_navigation_admission.check_synthetic_staging_runtime',
                   return_value={'browser_runtime_id': self.runtime.runtime_id}):
            scheduler.check_local_staging_binding('job-test')
        scheduler.store.connection.execute.return_value.fetchone.return_value = {'kind': 'browser_form'}
        with self.assertRaises(AOSFault):
            scheduler.check_local_staging_binding('job-test')
        scheduler.store.connection.execute.return_value.fetchone.return_value = {'kind': 'browser_staging_workflow'}
        with patch('aos.local_navigation_admission.check_synthetic_staging_runtime',
                   return_value={'browser_runtime_id': 'browser-' + 'f' * 32}):
            with self.assertRaises(AOSFault):
                scheduler.check_local_staging_binding('job-test')
            with (patch.object(scheduler, 'check_lease'),
                  patch('aos.desktop_tasks.ComputerGateway.execute') as execute):
                gateway = LeasedGateway(scheduler, 'job-test', self.runtime)
                with self.assertRaises(AOSFault):
                    gateway.execute(Mock(), 'decision-test')
                execute.assert_not_called()

    def test_profile_run_binding_migration_is_append_only_and_kind_bound(self):
        store = TrajectoryStore(self.root / 'bindings.sqlite')
        self.addCleanup(store.close)
        self.assertEqual(store.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], LATEST_MIGRATION)
        with store.connection:
            store.insert('tasks', task_id='task', original_goal='synthetic', normalized_goal='synthetic',
                         success_criteria_json='{}', workspace_scope_json='[]', created_at=now())
            store.insert('runs', run_id='run', task_id='task', status='running',
                         policy_version='browser-staging-workflow-policy-v1', environment_json='{}',
                         deployment_snapshot_json='{}', started_at=now())
            store.insert('desktop_sessions', session_id='session', runtime_id='desktop', image_id='image',
                         owner='AGENT', lease_id='lease', generation=0, status='running',
                         created_at=now(), updated_at=now())
            store.insert('desktop_tasks', job_id='job', session_id='session', run_id='run',
                         kind='browser_staging_workflow', lease_id='lease', generation=0,
                         status='running', real_model=0, created_at=now(), updated_at=now(),
                         runtime_id='browser-runtime')
        record = self.pin.model_dump(mode='json')
        binding = {'job_id': 'job', 'run_id': 'run', 'profile_sha256': self.profile_sha256,
                   'task_key': self.pin.task_key, 'pin_json': canonical(record),
                   'pin_sha256': digest(record), 'browser_runtime_id': 'browser-runtime',
                   'created_at': now()}
        for change in ({'task_key': 'synthetic-local-navigation'},
                       {'browser_runtime_id': 'other-runtime'},
                       {'profile_sha256': '0' * 64},
                       {'pin_json': canonical({**record, 'collection_authorized': True})},
                       {'pin_json': canonical({**record, 'collection_authorized': 0})},
                       {'pin_json': canonical({key: value for key, value in record.items()
                                               if key != 'profile_sha256'})},
                       {'pin_json': canonical({key: value for key, value in record.items()
                                               if key != 'network_mode'})}):
            with self.subTest(change=change), self.assertRaises(sqlite3.IntegrityError):
                store.insert('desktop_web_profile_bindings', **{**binding, **change})
        with store.connection:
            store.insert('desktop_web_profile_bindings', **binding)
        for statement in ("UPDATE desktop_web_profile_bindings SET profile_sha256='" + '0' * 64 + "'",
                          'DELETE FROM desktop_web_profile_bindings',
                          "UPDATE desktop_tasks SET kind='browser_form' WHERE job_id='job'",
                          "UPDATE desktop_tasks SET runtime_id='other' WHERE job_id='job'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                store.connection.execute(statement)
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute(
                'INSERT OR REPLACE INTO desktop_web_profile_bindings '
                '(job_id,run_id,profile_sha256,task_key,pin_json,pin_sha256,browser_runtime_id,created_at) '
                'VALUES (?,?,?,?,?,?,?,?)', tuple(binding.values()))
        self.assertEqual(store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
