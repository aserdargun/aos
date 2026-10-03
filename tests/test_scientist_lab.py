import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import digest
from aos.scientist_intents import ScientistIntentBinding
from aos.scientist_lab import (
    ScientistLabAction, ScientistLabBudget, ScientistLabClient, ScientistLabPolicy,
    ScientistLabStart, ScientistLabTask, ScientistLabUncertain, ScientistLabHandle,
)
from aos.scientist_transport import ScientistAdmissionError


class SyntheticLabServer:
    def __init__(self):
        self.requests = []
        self.mode = 'success'
        self.state = 'queued'
        self.run_id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.entered = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                outer.requests.append(('POST', self.path, body))
                if outer.mode in {'hold_start', 'hold_stop'}:
                    outer.entered.set()
                    outer.release.wait(2)
                if self.path == '/v1/runs':
                    if outer.mode == 'lost_ack':
                        self.connection.close()
                        return
                    self.send_json({'run_id': outer.run_id, 'state': 'queued', 'reused': False}, 202)
                else:
                    if outer.mode != 'ignored_stop':
                        outer.state = 'stop_requested'
                    self.send_json(outer.status())

            def do_GET(self):
                outer.requests.append(('GET', self.path, b''))
                if self.path.endswith('/report'):
                    report = outer.report()
                    if outer.mode == 'bad_report_hash':
                        report['report_sha256'] = 'f' * 64
                    if outer.mode == 'status_drift':
                        outer.state = 'failed'
                    self.send_json(report)
                else:
                    value = outer.status()
                    if outer.mode == 'foreign_run':
                        value['run_id'] = 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'
                    if outer.mode == 'foreign_origin':
                        value['origin'] = 'local'
                    self.send_json(value)

            def send_json(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def report(self):
        body = {'run_id': self.run_id, 'status': self.state, 'synthetic_cpu_fixture': True}
        digest = hashlib.sha256(json.dumps(body, ensure_ascii=False, allow_nan=False,
                               sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'run_id': self.run_id, 'report_sha256': digest, 'report': body, 'verified_at': 'synthetic'}

    def status(self):
        return {'run_id': self.run_id, 'origin': 'aos', 'purpose': 'research', 'state': self.state,
                'created_at': 'synthetic', 'updated_at': 'synthetic',
                'stop_requested': self.state in {'stop_requested', 'stopped'},
                'report_sha256': self.report()['report_sha256'] if self.state in {'completed', 'stopped', 'failed'} else None}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError('Synthetic Lab HTTP server failed cleanup')


class ScientistLabTests(unittest.TestCase):
    def test_canonical_schemas_match_typed_external_task_and_action_models(self):
        root = Path(__file__).resolve().parents[1] / 'schemas'
        for model, name in [(ScientistLabBudget, 'scientist_lab_budget'),
                            (ScientistLabStart, 'scientist_lab_start'),
                            (ScientistLabHandle, 'scientist_lab_handle'),
                            (ScientistLabTask, 'scientist_lab_task'),
                            (ScientistLabAction, 'scientist_lab_action')]:
            schema = json.loads((root / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            schema.pop('$schema')
            self.assertEqual(schema, model.model_json_schema())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.token = Path(self.temporary.name) / 'synthetic.token'
        self.token.write_text('synthetic-test-token\n')
        self.token.chmod(0o600)
        self.server = SyntheticLabServer()
        self.addCleanup(self.server.close)
        self.authority = Mock(return_value=None)
        self.effect = Mock(return_value=None)
        self.client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                    allowed_suites=frozenset({'synthetic.allowed.v1'}), verify_authority=self.authority,
                    authorize_and_persist=self.effect)
        self.task = ScientistLabTask(authority_url=self.server.url, principal_id='synthetic-aos', state_version=3,
            binding=ScientistIntentBinding(session_id='session', runtime_id='runtime', owner='AGENT',
                    lease_id='lease', generation=2, authorization_context_sha256='a' * 64),
            request=ScientistLabStart(idempotency_key='synthetic-task-key-0001', track='anomaly',
                    suite='synthetic.allowed.v1', budget=ScientistLabBudget(experiments=1, wall_seconds=30, model_tokens=100),
                    program_version='director.v1', external_task_id='task-' + 'a' * 32,
                    external_run_id='run-' + 'b' * 32, external_action_id='action-' + 'c' * 32))

    def action(self, tool, task=None):
        task = task or self.task
        return ScientistLabAction(task_id=task.request.external_task_id, run_id=task.request.external_run_id,
            step_id='step-synthetic', action_id=task.request.external_action_id if tool == 'lab.start' else 'action-' + 'd' * 32,
            runtime_id='runtime', state_version=3, owner_lease_id='lease', tool=tool,
            arguments={'request_sha256': digest(task.request.model_dump(mode='json'))} if tool == 'lab.start' else {'lab_run_id': task.lab_run_id},
            expected_effect='Synthetic bounded Lab control operation', deadline=time.time() + 30,
            idempotency_key=task.request.idempotency_key, selected_option=tool)

    def bound(self):
        return self.task.model_copy(update={'lab_run_id': self.server.run_id})

    def test_default_authority_and_effect_admission_are_denied_before_post(self):
        client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                                    allowed_suites=self.client.allowed_suites)
        with self.assertRaises(ScientistAdmissionError):
            client.execute(self.task, self.action('lab.start'))
        client.verify_authority = self.authority
        with self.assertRaises(ScientistAdmissionError):
            client.execute(self.task, self.action('lab.start'))
        self.assertEqual(self.server.requests, [])

    def test_cpu_start_readback_status_stop_and_independent_terminal_report(self):
        handle = self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(handle.run_id, self.server.run_id)
        self.assertEqual([entry[:2] for entry in self.server.requests[:2]],
                         [('POST', '/v1/runs'), ('GET', '/v1/runs/' + self.server.run_id)])
        task = self.bound()
        status = self.client.execute(task, self.action('lab.status', task))
        self.assertEqual(status.state, 'queued')
        stop = self.client.execute(task, self.action('lab.stop', task))
        self.assertEqual(stop.state, 'stop_requested')
        with self.assertRaises(ValueError):
            self.client.execute(task, self.action('lab.report', task))
        self.server.state = 'stopped'
        report = self.client.execute(task, self.action('lab.report', task))
        self.assertTrue(report.report['synthetic_cpu_fixture'])
        self.assertEqual(self.effect.call_count, 2)
        self.assertEqual(json.loads(self.effect.call_args_list[0].args[2]), self.task.request.model_dump(mode='json'))

    def test_scope_principal_suite_state_lease_and_selected_option_mismatch_deny(self):
        action = self.action('lab.start')
        for field, value in [('runtime_id', 'foreign'), ('state_version', 99), ('owner_lease_id', 'old'),
                             ('selected_option', 'other'), ('task_id', 'other'), ('deadline', 1.0)]:
            with self.assertRaises(ScientistAdmissionError):
                self.client.execute(self.task, action.model_copy(update={field: value}))
        for task in [self.task.model_copy(update={'principal_id': 'foreign'}),
                     self.task.model_copy(update={'authority_url': 'http://127.0.0.1:1'})]:
            with self.assertRaises(ScientistAdmissionError):
                self.client.execute(task, action)
        self.assertEqual(self.server.requests, [])

    def test_effect_requires_fresh_human_and_durable_intent_provider(self):
        self.effect.side_effect = ScientistAdmissionError('human approval absent or stale')
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(self.server.requests, [])

    def test_revoke_after_intent_prevents_effect(self):
        self.authority.side_effect = [None, ScientistAdmissionError('revoked')]
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, self.action('lab.start'))
        self.effect.assert_called_once()
        self.assertEqual(self.server.requests, [])

    def test_lost_ack_blocks_new_action_and_key_without_blind_retry(self):
        self.server.mode = 'lost_ack'
        with self.assertRaises(ScientistLabUncertain):
            self.client.execute(self.task, self.action('lab.start'))
        changed = self.task.model_copy(update={'request': self.task.request.model_copy(
            update={'external_action_id': 'action-' + 'f' * 32, 'idempotency_key': 'synthetic-new-key-0002'})})
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(changed, self.action('lab.start', changed))
        self.assertEqual(len(self.server.requests), 1)

    def test_foreign_start_readback_is_uncertain_not_success(self):
        for mode in ['foreign_run', 'foreign_origin']:
            with self.subTest(mode=mode):
                self.server.mode = mode
                client = ScientistLabClient(self.server.url, self.token, principal_id='synthetic-aos',
                    allowed_suites=self.client.allowed_suites, verify_authority=self.authority, authorize_and_persist=self.effect)
                with self.assertRaises(ScientistLabUncertain):
                    client.execute(self.task, self.action('lab.start'))

    def test_report_requires_stable_status_and_matching_hash(self):
        task = self.bound()
        for mode in ['bad_report_hash', 'status_drift']:
            self.server.state = 'stopped'
            self.server.mode = mode
            with self.assertRaises(ValueError):
                self.client.execute(task, self.action('lab.report', task))

    def test_baseline_terminal_status_denies_before_fetching_report(self):
        self.server.state = 'stopped'
        original_status = self.server.status
        with patch.object(self.server, 'status', side_effect=lambda: original_status() | {'purpose': 'baseline'}):
            with self.assertRaisesRegex(ValueError, 'research'):
                self.client.execute(self.bound(), self.action('lab.report', self.bound()))
        self.assertEqual([entry[:2] for entry in self.server.requests],
                         [('GET', '/v1/runs/' + self.server.run_id)])
        self.effect.assert_not_called()
        self.assertIsNone(self.client.uncertain_action_id)

    def test_public_symlink_and_header_injection_token_are_rejected(self):
        self.token.chmod(0o644)
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, self.action('lab.start'))
        self.token.chmod(0o600)
        self.token.write_text('synthetic\nInjected: value')
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(self.server.requests, [])
        alias = self.token.with_name('alias.token')
        alias.symlink_to(self.token)
        client = ScientistLabClient(self.server.url, alias, principal_id='synthetic-aos',
                                    allowed_suites=self.client.allowed_suites, verify_authority=self.authority,
                                    authorize_and_persist=self.effect)
        with self.assertRaises(OSError):
            client.execute(self.task, self.action('lab.start'))

    def test_nonloopback_dns_and_ambiguous_authorities_are_rejected(self):
        for url in ['http://example.com:8766', 'http://localhost:8766', 'http://127.0.0.1:8766/x',
                    'http://user@127.0.0.1:8766', 'http://127.0.0.1:8766?x=1', 'https://127.0.0.1:8766']:
            with self.assertRaises(ValueError):
                ScientistLabClient(url, self.token, principal_id='synthetic-aos', allowed_suites=self.client.allowed_suites)

    def test_callback_mutation_cannot_change_the_frozen_request(self):
        def approve(task, action, body):
            action.arguments['request_sha256'] = 'f' * 64

        self.effect.side_effect = approve
        self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(json.loads(self.server.requests[0][2]), self.task.request.model_dump(mode='json'))

    def test_post_success_replay_does_not_post_again(self):
        action = self.action('lab.start')
        self.client.execute(self.task, action)
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, action)
        self.assertEqual(sum(entry[0] == 'POST' for entry in self.server.requests), 1)

    def test_scope_allowlist_and_boolean_budget_are_not_model_permissions(self):
        bad_request = self.task.request.model_copy(update={'suite': 'foreign.suite'})
        task = self.task.model_copy(update={'request': bad_request})
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(task, self.action('lab.start', task))
        with self.assertRaises(ValueError):
            ScientistLabBudget(experiments=True, wall_seconds=30, model_tokens=100)
        self.authority.return_value = True
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(self.server.requests, [])

    def test_private_token_is_not_read_until_scope_authority_passes(self):
        self.authority.side_effect = ScientistAdmissionError('wrong principal')
        with patch('aos.scientist_lab.os.open') as reader:
            with self.assertRaises(ScientistAdmissionError):
                self.client.execute(self.task, self.action('lab.start'))
            reader.assert_not_called()

    def test_post_dispatch_authority_change_does_not_return_success(self):
        self.authority.side_effect = [None, None, ScientistAdmissionError('generation changed')]
        with self.assertRaises(ScientistLabUncertain):
            self.client.execute(self.task, self.action('lab.start'))
        self.assertEqual(self.client.uncertain_action_id, self.task.request.external_action_id)

    def test_stop_ack_without_a_stop_request_is_uncertain(self):
        self.server.mode = 'ignored_stop'
        task = self.bound()
        with self.assertRaises(ScientistLabUncertain):
            self.client.execute(task, self.action('lab.stop', task))

    def test_human_control_metadata_allows_bound_inspection_and_stop_not_start(self):
        task = self.bound().model_copy(update={'binding': self.task.binding.model_copy(
                                      update={'owner': 'HUMAN'})})
        self.assertEqual(self.client.execute(task, self.action('lab.status', task)).state, 'queued')
        self.assertEqual(self.client.execute(task, self.action('lab.stop', task)).state, 'stop_requested')
        start = self.task.model_copy(update={'binding': task.binding})
        with self.assertRaises(ScientistAdmissionError):
            self.client.execute(start, self.action('lab.start', start))
