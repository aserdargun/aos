import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
import jsonschema

from aos import native_handover as handover
from aos.contracts import canonical, digest
from aos.lifecycle import ProcessIdentity
from aos.local_app import LocalAppState


class NativeHandoverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.session = 'app-' + 'a' * 32
        self.desktop = 'desktop-session-' + 'b' * 32
        self.supervisor = ProcessIdentity(boot_id='11111111-1111-1111-1111-111111111111',
                                          pid=123, start_ticks=42, pid_namespace=99, uid=1000)
        self.backend = self.supervisor.model_copy(update={'pid': 124})
        self.state = LocalAppState(session=self.session, mode='real', phase='running',
                                   supervisor=self.supervisor, backend=self.backend,
                                   token_name='desktop-console-' + 'c' * 16 + '.token', started_at='2026-10-03T00:00:00Z')
        self.current = (canonical(self.state.model_dump(mode='json')) + '\n').encode()
        self.current_path = self.root / 'current.json'
        self.current_path.write_bytes(self.current)
        self.current_path.chmod(0o600)
        self.control = {'session_id': self.desktop, 'runtime_id': 'synthetic-runtime', 'lease_id': 'synthetic-lease',
                        'generation': 0, 'owner': 'AGENT', 'status': 'running'}
        self.binding = {
            'snapshot_sha256': '1' * 64, 'session_ref': digest({'session_id': self.desktop}),
            'binding_sha256': '2' * 64, 'session_status': 'running', 'session_owner': 'AGENT',
            'generation': 0, 'job_count': 0, 'unresolved_inputs': 0, 'inspected_at': '2026-10-03T00:00:00Z',
            'lifecycle': {'journal_sha256': '3' * 64, 'birth_ref': '4' * 64,
                          'runtime_ref': digest({'runtime_id': 'synthetic-runtime'}), 'workspace_sha256': '5' * 64,
                          'image_id': 'sha256:' + '6' * 64, 'source_sha256': '7' * 64,
                          'recorded_stage': 'started', 'owner_observation': 'same_process', 'workspace_busy': True,
                          'container_observation': 'not_queried', 'observation_sha256': None,
                          'inspected_at': '2026-10-03T00:00:00Z'}}
        self.tasks = {'available': True, 'busy': False, 'reserved': False, 'jobs': [], 'approval': None,
                      'auto_approval': None, 'restart_quiesced': False,
                      'owned_skill_planning': {'available': False}, 'owned_web_goal_planning': {'available': False}}
        self.scientist = {'configured': False, 'jobs': [], 'inference': {
            'configured': False, 'unresolved_count': 0, 'unresolved_lab_effect_count': 0,
            'other_session_count': 0, 'truncated': False, 'local_cleanup_pending': False,
            'evidence_controls': {'available': True, 'supported': True, 'pending_count': 0}}}
        self.payloads = {'/api/state': {'control': self.control}, '/api/tasks': self.tasks,
                         '/api/session/binding': self.binding, '/api/scientist/jobs': self.scientist}
        self.requests = []
        self.response_override = None
        self.counts = {}
        self.client_class = httpx.Client

    def handler(self, request):
        self.requests.append((request.method, request.url.path))
        self.counts[request.url.path] = self.counts.get(request.url.path, 0) + 1
        if self.response_override:
            response = self.response_override(request)
            if response is not None:
                return response
        if request.url.path == '/api/login':
            return httpx.Response(200, json={'authenticated': True})
        return httpx.Response(200, json=self.payloads[request.url.path])

    def client(self, **arguments):
        self.assertFalse(arguments['trust_env'])
        self.assertFalse(arguments['follow_redirects'])
        return self.client_class(**arguments, transport=httpx.MockTransport(self.handler))

    def prepare(self, *, observer=None):
        records = [handover.ObservedNativeProcess(identity=self.supervisor, cgroups=['0::/shared-synthetic.scope']),
                   handover.ObservedNativeProcess(identity=self.backend, discovery_parent_pid=self.supervisor.pid,
                                                  cgroups=['0::/shared-synthetic.scope'])]
        with ExitStack() as stack:
            stack.enter_context(patch.object(handover.local_app, 'BASE', self.root))
            stack.enter_context(patch.object(handover.local_app, 'token_value', return_value='synthetic-token-never-persist'))
            stack.enter_context(patch.object(handover, 'observe_process', return_value='same_process'))
            stack.enter_context(patch.object(handover, 'observe_descendants', side_effect=observer or (lambda *_args: (records, []))))
            stack.enter_context(patch.object(handover.httpx, 'Client', side_effect=self.client))
            return handover.prepare_native_handover(self.session, self.root / 'preview.json')

    def test_clean_unconfigured_snapshot_is_not_execution_authority(self):
        preview = self.prepare()
        self.assertTrue(preview.local_idle_observed)
        self.assertTrue(preview.snapshot_continuity_verified)
        self.assertFalse(preview.scientist.lab_configured)
        self.assertFalse(preview.execution_authorized)
        self.assertFalse(preview.native_exclusion_verified)
        self.assertTrue(preview.user_transition_authorization_required)
        self.assertIn('scientist_canonical_reservation_missing', preview.blockers)
        self.assertEqual(preview.current_state_sha256, hashlib.sha256(self.current).hexdigest())
        output = self.root / 'preview.json'
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('synthetic-token-never-persist', output.read_text())
        self.assertEqual(self.current_path.read_bytes(), self.current)
        self.assertEqual({path.name for path in self.root.iterdir()}, {'current.json', 'preview.json'})
        self.assertEqual([request for request in self.requests if request[0] != 'GET'], [('POST', '/api/login')])
        self.assertTrue(all(path in handover.ENDPOINTS for method, path in self.requests if method == 'GET'))
        self.assertEqual(preview.procedure[3].command_argv, ['./scripts/aos-v1', 'stop', '--expected-session', self.session])
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_binding_audit_timestamps_may_change_but_hashes_may_not(self):
        def change(request):
            if request.url.path == '/api/session/binding' and self.counts[request.url.path] == 2:
                value = json.loads(canonical(self.binding))
                value['inspected_at'] = '2026-10-03T00:00:01Z'
                value['lifecycle']['inspected_at'] = '2026-10-03T00:00:01Z'
                return httpx.Response(200, json=value)
        self.response_override = change
        self.assertTrue(self.prepare().snapshot_continuity_verified)
        (self.root / 'preview.json').unlink()
        self.counts.clear()
        def change_hash(request):
            if request.url.path == '/api/session/binding' and self.counts[request.url.path] == 2:
                return httpx.Response(200, json=self.binding | {'snapshot_sha256': '8' * 64})
        self.response_override = change_hash
        preview = self.prepare()
        self.assertFalse(preview.local_idle_observed)
        self.assertFalse(preview.snapshot_continuity_verified)
        self.assertIn('endpoint_continuity_unverified:/api/session/binding', preview.blockers)

    def test_busy_reserved_approval_and_unresolved_block(self):
        self.tasks.update(busy=True, reserved=True, approval={}, auto_approval={})
        self.binding['unresolved_inputs'] = 1
        self.scientist['inference']['unresolved_count'] = 1
        preview = self.prepare()
        self.assertFalse(preview.local_idle_observed)
        self.assertTrue({'scheduler_not_idle', 'unresolved_inputs', 'scientist_unresolved_or_truncated'} <= set(preview.blockers))

    def test_configured_lab_requires_control_inventory_even_without_jobs(self):
        self.scientist['configured'] = True
        preview = self.prepare()
        self.assertFalse(preview.local_idle_observed)
        self.assertIn('lab_control_inventory_unavailable', preview.blockers)

    def test_all_thread_children_find_detached_worker_without_cgroup_authority(self):
        proc_root = self.root / 'synthetic-proc'
        worker = self.backend.model_copy(update={'pid': 125})
        for identity, threads in [(self.supervisor, {123: '124'}),
                                  (self.backend, {124: '', 126: '125'}), (worker, {125: ''})]:
            directory = proc_root / str(identity.pid)
            directory.mkdir(parents=True)
            (directory / 'cgroup').write_text('0::/shared-synthetic.scope\n')
            for thread_id, children in threads.items():
                thread = directory / 'task' / str(thread_id)
                thread.mkdir(parents=True)
                (thread / 'children').write_text(children)
        with patch.object(handover, 'Path', side_effect=lambda value: proc_root if value == '/proc' else Path(value)), \
                patch.object(handover, 'observe_process', return_value='same_process'), \
                patch.object(handover, 'process_identity', side_effect=lambda pid: {124: self.backend, 125: worker}[pid]), \
                patch.object(handover.os, 'getuid', return_value=1000):
            records, blockers = handover.observe_descendants(self.supervisor, self.backend)
        self.assertEqual(blockers, [])
        self.assertEqual({record.identity.pid for record in records}, {123, 124, 125})
        self.assertEqual({record.identity.pid: record.discovery_parent_pid for record in records},
                         {123: None, 124: 123, 125: 124})
        self.assertTrue(all(not record.ownership_verified and not record.signal_authorized for record in records))

    def test_unknown_redirect_duplicate_and_oversized_responses_block(self):
        responses = [httpx.Response(404), httpx.Response(302, headers={'Location': 'http://example.invalid'}),
                     httpx.Response(200, content=b'{"available":true,"available":false}'),
                     httpx.Response(200, content=b' ' * (handover.RESPONSE_LIMIT + 1))]
        for response in responses:
            with self.subTest(status=response.status_code, length=len(response.content)):
                self.response_override = lambda request: response if request.url.path == '/api/tasks' else None
                preview = self.prepare()
                self.assertFalse(preview.local_idle_observed)
                self.assertIsNone(preview.tasks)
                self.assertIn('endpoint_unavailable:/api/tasks', preview.blockers)
                (self.root / 'preview.json').unlink()

    def test_wrong_state_or_session_fails_before_login(self):
        changes = [{'version': '2'}, {'session': 'app-' + 'f' * 32}, {'mode': 'fixture'}, {'phase': 'stopped'}]
        for change in changes:
            with self.subTest(change=change):
                self.current_path.write_text(canonical(self.state.model_dump(mode='json') | change))
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(self.requests, [])
                self.assertFalse((self.root / 'preview.json').exists())

    def test_state_change_during_readback_refuses_publication(self):
        def change(request):
            if request.url.path == '/api/tasks':
                self.current_path.write_bytes(self.current + b' ')
        self.response_override = change
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.prepare()
        self.assertFalse((self.root / 'preview.json').exists())

    def test_changed_ancestry_or_unknown_process_does_not_prove_continuity(self):
        self.assertFalse(self.prepare(observer=lambda *_args: ([], ['descendant_inventory_unavailable'])).local_idle_observed)
        (self.root / 'preview.json').unlink()
        observed = iter([([handover.ObservedNativeProcess(identity=self.backend, cgroups=['0::/old'])], []),
                         ([handover.ObservedNativeProcess(identity=self.backend, cgroups=['0::/new'])], [])])
        preview = self.prepare(observer=lambda *_args: next(observed))
        self.assertFalse(preview.snapshot_continuity_verified)
        self.assertIn('descendant_snapshot_changed', preview.blockers)

    def test_http_mutations_rejected_without_request(self):
        with self.client_class(transport=httpx.MockTransport(self.handler)) as client:
            for method, path in [('POST', '/api/shared/drain'), ('POST', '/api/logout'), ('GET', '/api/unknown')]:
                with self.assertRaises(ValueError):
                    handover._response(client, method, path)
        self.assertEqual(self.requests, [])

    def test_process_identity_failure_denies_before_http(self):
        with patch.object(handover.local_app, 'BASE', self.root), patch.object(handover, 'observe_process', return_value='different_process'):
            with self.assertRaises(ValueError):
                handover.prepare_native_handover(self.session, self.root / 'preview.json')
        self.assertEqual(self.requests, [])

    def test_canonical_schema_and_no_authority_promotion(self):
        preview = self.prepare()
        schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/native_handover_preview.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        value = preview.model_dump(mode='json')
        validator.validate(value)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(value | {'execution_authorized': True})


if __name__ == '__main__':
    unittest.main()
