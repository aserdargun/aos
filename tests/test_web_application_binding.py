import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT, digest
from aos.desktop import DesktopRuntime
from aos.desktop_mcp import DesktopMCPBrowserRuntime
from aos.desktop_mcp_bundle import PLAYWRIGHT_VERSION
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import (WebReadOnlyRoutePlan, WebRuntimeAttestation,
                                         WebRuntimePin, WebTaskAdmissionDraft, WebTaskContract,
                                         attest_web_task_runtime, bind_web_task,
                                         plan_web_readonly_routes, verify_web_readonly_routes,
                                         verify_web_task_binding)


PROFILE = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
FIXTURE = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())


class WebApplicationBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profiles = WebApplicationProfiles(Path(temporary.name) / 'profiles')
        self.profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
        self.checksum = profile_report(self.profile).profile_sha256
        self.profiles.register(self.profile, confirm_sha256=self.checksum)
        self.task = WebTaskContract.model_validate(copy.deepcopy(FIXTURE['task']))
        self.runtime = WebRuntimePin.model_validate(copy.deepcopy(FIXTURE['runtime']))

    def test_synthetic_fixture_and_canonical_schemas(self):
        self.assertTrue(FIXTURE['synthetic'])
        draft = bind_web_task(self.profiles, self.task, self.runtime)
        self.assertEqual(draft.model_dump(), FIXTURE['draft'])
        for name, model, value in (('web_task_contract', WebTaskContract, self.task),
                                   ('web_runtime_pin', WebRuntimePin, self.runtime),
                                   ('web_task_admission_draft', WebTaskAdmissionDraft, draft)):
            schema = json.loads((REPO_ROOT / f'schemas/{name}.schema.json').read_text())
            self.assertEqual({key: val for key, val in schema.items() if key != '$schema'}, model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(value.model_dump())

    def test_binding_pins_exact_profile_task_and_runtime_without_authority(self):
        draft = bind_web_task(self.profiles, self.task, self.runtime)
        self.assertEqual(draft.profile_sha256, self.checksum)
        self.assertEqual(draft.task_sha256, digest(self.task.model_dump()))
        self.assertEqual(draft.runtime_sha256, digest(self.runtime.model_dump()))
        self.assertEqual(draft.runtime.kind, 'docker_chromium_mcp')
        self.assertFalse(draft.execution_authorized)
        self.assertFalse(draft.collection_authorized)
        self.assertFalse(draft.runtime_identity_verified)
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(len(draft.blockers), 4)
        self.assertEqual(verify_web_task_binding(self.profiles, draft), draft)

    def test_readonly_route_plan_binds_exact_same_origin_pages_without_authority(self):
        routes = [self.task.entry_url,
                  'https://crm.example.invalid/details/?view=compact&page=1']
        plan = plan_web_readonly_routes(self.profiles, self.task, routes)
        self.assertEqual(plan.routes, routes)
        self.assertEqual(plan.task_sha256, digest(self.task.model_dump()))
        self.assertFalse(plan.execution_authorized)
        self.assertFalse(plan.collection_authorized)
        self.assertEqual(verify_web_readonly_routes(self.profiles, self.task, plan), plan)
        fixture = json.loads((REPO_ROOT / 'examples/web_readonly_route_plan.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/web_readonly_route_plan.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        self.assertEqual(fixture['plan'], plan.model_dump())
        self.assertEqual({key: value for key, value in schema.items() if key != '$schema'},
                         WebReadOnlyRoutePlan.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(fixture['plan'])
        for changed in ([routes[1], routes[0]], [routes[0], routes[0]],
                        [routes[0], 'https://other.example.invalid/details/'],
                        [routes[0], 'https://crm.example.invalid/details/?q=1&q=2'],
                        [routes[0], 'https://crm.example.invalid/details/?q=%0A'],
                        [routes[0] + '?q=1', routes[1]],
                        [routes[0], 'http://crm.example.invalid/details/']):
            with self.subTest(routes=changed), self.assertRaises(ValueError):
                plan_web_readonly_routes(self.profiles, self.task, changed)
        limited = WebTaskContract.model_validate({**self.task.model_dump(), 'max_pages': 1})
        with self.assertRaises(ValueError):
            plan_web_readonly_routes(self.profiles, limited, routes)
        with self.assertRaises(ValueError):
            verify_web_readonly_routes(self.profiles, self.task,
                plan.model_copy(update={'task_sha256': '0' * 64}))
        with self.assertRaises(ValueError):
            WebReadOnlyRoutePlan.model_validate({**plan.model_dump(), 'execution_authorized': True})

    def test_missing_or_changed_profile_or_outside_task_fails_closed(self):
        for changes in ({'profile_sha256': '0' * 64}, {'task_key': 'not-registered'},
                        {'entry_url': 'https://crm.example.invalid/other/'},
                        {'allowed_origins': ['https://other.example.invalid']},
                        {'allowed_origins': ['https://other.example.invalid', 'https://crm.example.invalid']}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                task = WebTaskContract.model_validate({**self.task.model_dump(), **changes})
                bind_web_task(self.profiles, task, self.runtime)

    def test_new_profile_revision_does_not_implicitly_rebind(self):
        revised = WebApplicationProfile.model_validate({**self.profile.model_dump(), 'revision': 2,
                'previous_sha256': self.checksum, 'task_keys': ['find-record']})
        new_sha = profile_report(revised).profile_sha256
        self.profiles.register(revised, confirm_sha256=new_sha)
        first = bind_web_task(self.profiles, self.task, self.runtime)
        self.assertEqual(first.profile_sha256, self.checksum)
        with self.assertRaises(ValueError):
            bind_web_task(self.profiles, WebTaskContract.model_validate({**self.task.model_dump(),
                        'profile_sha256': new_sha}), self.runtime)

    def test_scope_budget_and_authority_tampering_rejected(self):
        for changes in ({'tools': ['browser.click']}, {'tools': ['browser.snapshot', 'browser.verify']},
                        {'tools': ['browser.snapshot', 'browser.verify', 'browser.click', 'browser.click']},
                        {'tools': ['browser.snapshot', 'browser.verify', 'browser.evaluate']},
                        {'max_pages': 0}, {'max_pages': 9}, {'max_actions': 17}, {'max_seconds': 901},
                        {'network_access': True}, {'network_access': 0}, {'collection_authorized': True},
                        {'approval': 'approve_all'}, {'uncertain_write': 'retry'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                WebTaskContract.model_validate({**self.task.model_dump(), **changes})
        for changes in ({'network_mode': 'bridge'}, {'transport': 'cdp'}, {'display': ':0'},
                        {'container_id': 'short'}, {'image_id': 'latest'},
                        {'worker_sha256': 'short'}, {'runtime_digest': 'short'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                WebRuntimePin.model_validate({**self.runtime.model_dump(), **changes})

    def test_draft_cannot_hide_blockers_or_rewrite_hashes(self):
        draft = bind_web_task(self.profiles, self.task, self.runtime).model_dump()
        for changes in ({'execution_authorized': True}, {'collection_authorized': True},
                        {'runtime_identity_verified': True}, {'status': 'active'}, {'blockers': []},
                        {'task_sha256': '0' * 64}, {'runtime_sha256': '0' * 64},
                        {'binding_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                WebTaskAdmissionDraft.model_validate({**draft, **changes})

    def test_binding_recheck_requires_original_immutable_profile(self):
        draft = bind_web_task(self.profiles, self.task, self.runtime)
        (self.profiles.root / (self.checksum + '.json')).write_text('{}')
        with self.assertRaises(ValueError):
            verify_web_task_binding(self.profiles, draft)

    def test_mutated_nested_task_scope_is_revalidated(self):
        self.task.tools.append('browser.evaluate')
        with self.assertRaises(ValueError):
            bind_web_task(self.profiles, self.task, self.runtime)

    def mock_live_runtime(self):
        pin = self.runtime
        desktop = Mock(manifest=Path('synthetic'), runtime_id=pin.parent_runtime_id,
                       container_id=pin.container_id)
        desktop.status.return_value = {
            'kind': 'docker_xfce', 'running': True, 'runtime_id': pin.parent_runtime_id,
            'container_id': pin.container_id, 'image_id': pin.image_id, 'network': False,
            'workspace_identity': {'synthetic': True}, 'lifecycle_ref': 'synthetic'}
        runtime = DesktopMCPBrowserRuntime(desktop, Path('synthetic'))
        runtime.owned_desktop = Mock()
        runtime.status = Mock(return_value={
            'kind': pin.kind, 'browser_transport': pin.transport,
            'runtime_id': pin.runtime_id, 'parent_runtime_id': pin.parent_runtime_id,
            'container_id': pin.container_id, 'image_id': pin.image_id,
            'runtime_digest': pin.runtime_digest, 'worker_sha256': pin.worker_sha256,
            'browser_display': 'desktop', 'running': True, 'real_execution': True,
            'desktop': True, 'network': False,
            'isolation': {'ready': True, 'headed': True, 'display': pin.display,
                          'transport': pin.transport, 'bundle_sha256': pin.mcp_bundle_sha256,
                          'server_version': PLAYWRIGHT_VERSION,
                          'chromium_sha256': '2' * 64, 'network_namespace': 'synthetic-isolated',
                          'home_visible': False, 'docker_socket_visible': False}})
        runtime.pins = {'chromium_sha256': '2' * 64,
                        'mcp': {'bundle_sha256': pin.mcp_bundle_sha256}}
        runtime.process = Mock()
        runtime.process.poll.return_value = None
        return runtime

    def attest(self, runtime):
        with patch('aos.web_application_binding.read_bundle',
                   return_value=({'bundle_sha256': self.runtime.mcp_bundle_sha256}, b'synthetic')):
            return attest_web_task_runtime(self.profiles,
                    bind_web_task(self.profiles, self.task, self.runtime), runtime)

    def test_read_only_attestation_matches_owned_live_evidence_without_grant(self):
        runtime = self.mock_live_runtime()
        report = self.attest(runtime)
        self.assertEqual(report.status, 'observed_match')
        self.assertTrue(report.runtime_identity_observed)
        self.assertFalse(report.execution_authorized)
        self.assertFalse(report.collection_authorized)
        self.assertFalse(report.network_access_authorized)
        self.assertEqual(runtime.owned_desktop.call_count, 2)
        schema = json.loads((REPO_ROOT / 'schemas/web_runtime_attestation.schema.json').read_text())
        self.assertEqual({key: val for key, val in schema.items() if key != '$schema'},
                         WebRuntimeAttestation.model_json_schema())
        jsonschema.Draft202012Validator(schema).validate(report.model_dump())
        example = WebRuntimeAttestation.model_validate(FIXTURE['attestation'])
        self.assertEqual(example.binding_sha256, report.binding_sha256)
        self.assertEqual(example.runtime_sha256, report.runtime_sha256)
        jsonschema.Draft202012Validator(schema).validate(example.model_dump())
        for value in ('not-a-date', '2026-09-23T00:00:00', '2026-09-23T03:00:00+03:00'):
            with self.assertRaises(ValueError):
                WebRuntimeAttestation.model_validate({**example.model_dump(), 'observed_at': value})

    def test_read_only_attestation_rejects_stopped_or_changed_identity(self):
        changes = [
            ('browser', {'kind': 'docker_chromium'}),
            ('browser', {'runtime_id': 'browser-' + 'f' * 32}),
            ('browser', {'parent_runtime_id': 'desktop-' + 'f' * 32}),
            ('browser', {'container_id': 'f' * 64}),
            ('browser', {'image_id': 'sha256:' + 'f' * 64}),
            ('browser', {'runtime_digest': 'f' * 64}),
            ('browser', {'worker_sha256': '0' * 64}),
            ('browser', {'network': True}),
            ('browser', {'running': False}),
            ('desktop', {'container_id': 'f' * 64}),
            ('desktop', {'network': True}),
            ('desktop', {'running': False}),
            ('evidence', {'display': ':0'}),
            ('evidence', {'bundle_sha256': 'f' * 64}),
            ('evidence', {'network_namespace': os.readlink('/proc/self/ns/net')}),
        ]
        for component, values in changes:
            with self.subTest(component=component, values=values):
                runtime = self.mock_live_runtime()
                if component == 'browser':
                    runtime.status.return_value.update(values)
                elif component == 'desktop':
                    runtime.desktop.status.return_value.update(values)
                else:
                    runtime.status.return_value['isolation'].update(values)
                with self.assertRaises(ValueError):
                    self.attest(runtime)
        runtime = self.mock_live_runtime()
        runtime.process.poll.return_value = 1
        with self.assertRaises(ValueError):
            self.attest(runtime)

    def test_read_only_attestation_requires_owned_instance_and_current_bundle(self):
        draft = bind_web_task(self.profiles, self.task, self.runtime)
        with self.assertRaises(ValueError):
            attest_web_task_runtime(self.profiles, draft, Mock())
        runtime = self.mock_live_runtime()
        runtime.owned_desktop.side_effect = ValueError('unowned')
        with self.assertRaises(ValueError):
            self.attest(runtime)
        runtime = self.mock_live_runtime()
        with patch('aos.web_application_binding.read_bundle',
                   return_value=({'bundle_sha256': 'f' * 64}, b'synthetic')):
            with self.assertRaises(ValueError):
                attest_web_task_runtime(self.profiles, draft, runtime)


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1' and
                     os.environ.get('AOS_DESKTOP_MCP_TESTS') == '1',
                     'Opt in to real Ubuntu Playwright MCP')
class WebApplicationBindingIntegrationTests(unittest.TestCase):
    def test_real_owned_mcp_runtime_attestation_is_read_only_and_expires_on_stop(self):
        with tempfile.TemporaryDirectory(prefix='web-binding-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            desktop = DesktopRuntime(root / 'workspace', REPO_ROOT / 'models/desktop-manifest.json')
            try:
                desktop.start()
                runtime = DesktopMCPBrowserRuntime(desktop, REPO_ROOT / 'models/desktop-mcp-v001/manifest.json')
                try:
                    runtime.start()
                    profile = WebApplicationProfile.model_validate(copy.deepcopy(PROFILE))
                    profiles = WebApplicationProfiles(root / 'profiles')
                    checksum = profile_report(profile).profile_sha256
                    profiles.register(profile, confirm_sha256=checksum)
                    task = WebTaskContract.model_validate(copy.deepcopy(FIXTURE['task']))
                    observed = runtime.status()
                    pin = WebRuntimePin(runtime_id=observed['runtime_id'],
                                        parent_runtime_id=observed['parent_runtime_id'],
                                        container_id=observed['container_id'], image_id=observed['image_id'],
                                        mcp_bundle_sha256=observed['isolation']['bundle_sha256'],
                                        worker_sha256=observed['worker_sha256'],
                                        runtime_digest=observed['runtime_digest'])
                    draft = bind_web_task(profiles, task, pin)
                    report = attest_web_task_runtime(profiles, draft, runtime)
                    self.assertEqual(report.status, 'observed_match')
                    self.assertFalse(report.execution_authorized)
                    self.assertFalse(report.collection_authorized)
                    self.assertFalse(report.network_access_authorized)
                    runtime.stop()
                    with self.assertRaises(ValueError):
                        attest_web_task_runtime(profiles, draft, runtime)
                finally:
                    runtime.stop()
            finally:
                desktop.stop()


if __name__ == '__main__':
    unittest.main()
