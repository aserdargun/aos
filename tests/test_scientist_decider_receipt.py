import asyncio
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from aos.contracts import HELLO_CONTENT, Settings
from aos.desktop_control import DesktopController
from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_decider_receipt import validate_decider_receipt
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest, scientist_request_frame
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError, ScientistTurnClient, ScientistUncertainTurn
from aos.storage import TrajectoryStore
from test_desktop_tasks import FixtureDesktop


def request_fixture():
    return ScientistTurnRequest(request_id='a' * 32, profile_id='aos.decider.turn.v1',
        deployment_digest='b' * 64, payload={'request': {
            'state': 'Synthetic CPU receipt validation; no model call.', 'question': 'Which allowed action?',
            'options': [{'id': 'write_file', 'label': 'Write'}, {'id': 'ask_human', 'label': 'Ask'}]}})


def receipt_fixture(request):
    options = request['payload']['request']['options']
    metrics = {'latency_ms': 2.5, 'load_ms': 0.0, 'inference_ms': 2.0,
               'reused': False, 'prepared_cpu': False, 'input_tokens': 32,
               'peak_vram_bytes': 1024, 'broker_activation_load_ms': 1.5}
    unit = 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service'
    return {'version': 1, 'request_id': request['request_id'], 'profile_id': request['profile_id'],
            'deployment_digest': request['deployment_digest'],
            'generation': {'unit': unit, 'invocation_id': 'd' * 32, 'main_pid': 1234,
                           'control_group': '/synthetic/' + unit},
            'response': {'deployment_digest': request['deployment_digest'],
                         'prediction': {'selected_option': 'write_file',
                                        'probabilities': {option['id']: float(option['id'] == 'write_file')
                                                          for option in options}},
                         'metrics': metrics}, 'usage': deepcopy(metrics)}


class ReceiptBroker:
    def __init__(self, root, transform=None):
        self.path = root / 'synthetic-receipt.sock'
        self.requests = []
        self.errors = []
        self.transform = transform
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(.05)
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with connection:
                    connection.settimeout(2)
                    frame = bytearray()
                    while b'\n' not in frame:
                        chunk = connection.recv(8192)
                        if not chunk:
                            break
                        frame.extend(chunk)
                        if len(frame) > 128 * 1024:
                            raise ValueError('Synthetic request exceeded its bound')
                    if not frame:
                        continue
                    if b'\n' not in frame:
                        raise ValueError('Synthetic request ended before its newline frame')
                    self.requests.append(bytes(frame))
                    receipt = receipt_fixture(json.loads(frame))
                    if self.transform is not None:
                        self.transform(receipt)
                    connection.sendall(json.dumps(receipt, allow_nan=False).encode() + b'\n')
            except Exception as error:
                self.errors.append(error)
                return

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(3)
        if self.thread.is_alive() or self.errors:
            raise AssertionError('Synthetic receipt broker failed to close cleanly: ' + repr(self.errors))


def authenticator_fixture():
    authenticator = Mock()
    authenticator.authenticate.return_value = BrokerPeer(os.getpid(), os.getuid(), 1,
                                                         'synthetic', 'd' * 32, '/synthetic')
    authenticator.still_current.return_value = True
    return authenticator


class DeciderReceiptValidationTests(unittest.TestCase):
    def setUp(self):
        self.request = request_fixture()
        self.value = receipt_fixture(self.request.model_dump())

    def validate(self, value=None, request=None):
        return validate_decider_receipt(request or self.request,
            ScientistTurnReceipt.model_validate(self.value if value is None else value, strict=True))

    def test_exact_fresh_worker_shape_is_accepted_without_mutation(self):
        original = deepcopy(self.value)
        self.assertIsNone(self.validate())
        self.assertEqual(self.value, original)
        self.value['response']['prediction']['selected_option'] = 'ask_human'
        self.assertIsNone(self.validate())

    def test_every_output_object_rejects_unknown_or_missing_fields(self):
        for path in [('response',), ('response', 'prediction'), ('response', 'prediction', 'probabilities'),
                     ('response', 'metrics'), ('usage',)]:
            for operation in ('extra', 'missing'):
                with self.subTest(path=path, operation=operation):
                    value = deepcopy(self.value)
                    target = value
                    for field in path:
                        target = target[field]
                    if operation == 'extra':
                        target['unknown'] = 0
                    else:
                        target.pop(next(iter(target)))
                    with self.assertRaises(ValueError):
                        self.validate(value)

    def test_prediction_is_bound_to_exact_original_ids_not_labels_or_confidence(self):
        for prediction in [
                {'selected_option': 'Write', 'probabilities': {'Write': 1.0, 'Ask': 0.0}},
                {'selected_option': 'foreign', 'probabilities': {'write_file': 1.0, 'ask_human': 0.0}},
                {'selected_option': 'write_file', 'probabilities': {'write_file': .7, 'ask_human': .7}},
                {'selected_option': 'write_file', 'probabilities': {'write_file': True, 'ask_human': False}},
                {'selected_option': 'write_file', 'probabilities': {'write_file': float('nan'), 'ask_human': 0.0}},
                {'selected_option': 'write_file', 'probabilities': {'write_file': 1.1, 'ask_human': -.1}}]:
            with self.subTest(prediction=prediction):
                value = deepcopy(self.value)
                value['response']['prediction'] = prediction
                with self.assertRaises(ValueError):
                    self.validate(value)

    def test_metrics_reject_type_budget_and_freshness_forgery(self):
        for field, value in [('input_tokens', True), ('input_tokens', 32.0), ('input_tokens', 0),
                             ('input_tokens', 1537), ('peak_vram_bytes', -1), ('peak_vram_bytes', 2**53),
                             ('latency_ms', float('inf')), ('load_ms', True), ('inference_ms', -1),
                             ('latency_ms', 720001), ('broker_activation_load_ms', 600001),
                             ('reused', True), ('reused', 0), ('prepared_cpu', True)]:
            with self.subTest(field=field, value=value):
                receipt = deepcopy(self.value)
                receipt['response']['metrics'][field] = value
                receipt['usage'][field] = value
                with self.assertRaises(ValueError):
                    self.validate(receipt)

    def test_usage_requires_exact_values_and_numeric_representation(self):
        for field, value in [('input_tokens', 33), ('latency_ms', 3.0), ('load_ms', 0)]:
            with self.subTest(field=field):
                receipt = deepcopy(self.value)
                receipt['usage'][field] = value
                with self.assertRaises(ValueError):
                    self.validate(receipt)

    def test_foreign_profile_request_and_deployment_are_denied(self):
        for field, value in [('profile_id', 'aos.bonsai.recovery.v1'), ('request_id', 'e' * 32),
                             ('deployment_digest', 'e' * 64)]:
            receipt = deepcopy(self.value)
            receipt[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(receipt)
        self.value['response']['deployment_digest'] = 'e' * 64
        with self.assertRaises(ValueError):
            self.validate()

    def test_original_options_are_finite_unique_and_exactly_typed(self):
        options = self.request.payload['request']['options']
        for invalid in [options[:1], options * 6, [options[0], options[0]],
                        [options[0], {'id': 'ask_human', 'label': 'Write'}],
                        [options[0], {'id': '', 'label': 'Ask'}],
                        [options[0], {'id': 'ask_human', 'label': 'Ask', 'scope': 'extra'}]]:
            request = self.request.model_copy(deep=True)
            request.payload['request']['options'] = invalid
            with self.subTest(options=invalid), self.assertRaises(ValueError):
                self.validate(request=request)


class DeciderReceiptTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.request = request_fixture()
        self.store = TrajectoryStore(self.root / 'synthetic.sqlite3')
        self.addCleanup(self.store.close)
        with self.store.connection:
            self.store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('session', 'runtime', 'synthetic', 'AGENT', 'lease', 0, 'running', 'synthetic', 'synthetic'))
        binding = ScientistIntentBinding(session_id='session', runtime_id='runtime', owner='AGENT',
            lease_id='lease', generation=0, authorization_context_sha256='a' * 64)
        self.journal = ScientistIntentJournal(self.store, binding)

    def client(self, *, transform=None, validator=validate_decider_receipt):
        broker = ReceiptBroker(self.root, transform)
        self.addCleanup(broker.close)
        client = ScientistTurnClient(broker.path, timeout_seconds=3, authenticator=authenticator_fixture(),
            verify_admission=self.journal.verify_admission, persist_intent=self.journal.persist_intent,
            record_receipt=self.journal.record_receipt, validate_receipt=validator)
        return client, broker

    def test_validated_receipt_commits_after_guard_and_preserves_wire_bytes(self):
        def validate(request, receipt):
            row = self.store.connection.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
            self.assertEqual(tuple(row), ('pending', None))
            validate_decider_receipt(request, receipt)
        client, broker = self.client(validator=validate)
        receipt = client.infer(self.request)
        self.assertEqual(broker.requests, [scientist_request_frame(self.request)])
        with closing(sqlite3.connect(self.root / 'synthetic.sqlite3')) as reader:
            state, recorded = reader.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
        self.assertEqual(state, 'receipt_recorded')
        self.assertEqual(json.loads(recorded), receipt.model_dump())
        self.assertIsNone(client.uncertain_request_id)

    def test_empty_authentication_probes_do_not_stop_receipt_broker(self):
        client, broker = self.client()
        for attempt in range(2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                probe.connect(str(broker.path))
        receipt = client.infer(self.request)
        self.assertEqual(receipt.request_id, self.request.request_id)
        self.assertEqual(broker.requests, [scientist_request_frame(self.request)])
        self.assertTrue(broker.thread.is_alive())

    def test_partial_request_eof_is_not_treated_as_empty_authentication_probe(self):
        broker = ReceiptBroker(self.root)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(str(broker.path))
                connection.sendall(b'{"version":1')
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(connection.recv(1), b'')
            self.assertEqual(broker.requests, [])
        finally:
            with self.assertRaisesRegex(AssertionError, 'ended before its newline frame'):
                broker.close()

    def test_rejected_output_keeps_durable_intent_pending_and_blocks_replay(self):
        client, broker = self.client(transform=lambda value: value['usage'].update(input_tokens=33))
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertEqual(client.uncertain_request_id, self.request.request_id)
        with closing(sqlite3.connect(self.root / 'synthetic.sqlite3')) as reader:
            row = reader.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
        self.assertEqual(row, ('pending', None))
        for request_id in (self.request.request_id, 'f' * 32):
            with self.assertRaises(ScientistAdmissionError):
                client.infer(self.request.model_copy(update={'request_id': request_id}))
        self.assertEqual(len(broker.requests), 1)

    def test_mutating_validator_cannot_replace_persisted_request_or_receipt(self):
        def validate(request, receipt):
            validate_decider_receipt(request, receipt)
            request.payload.clear()
            receipt.response.clear()
            receipt.usage.clear()
        client, broker = self.client(validator=validate)
        receipt = client.infer(self.request)
        self.assertEqual(receipt.model_dump(), receipt_fixture(self.request.model_dump()))
        self.assertEqual(broker.requests, [scientist_request_frame(self.request)])

    def test_callback_non_none_result_is_not_validation_attestation(self):
        client, broker = self.client(validator=lambda request, receipt: False)
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')
        self.assertEqual(len(broker.requests), 1)

    def test_takeover_during_validation_prevents_receipt_commit(self):
        def validate(request, receipt):
            validate_decider_receipt(request, receipt)
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_sessions SET owner='HUMAN'")
        client, broker = self.client(validator=validate)
        with self.assertRaises(ScientistUncertainTurn):
            client.infer(self.request)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')
        self.assertEqual(len(broker.requests), 1)

    def test_legacy_default_does_not_silently_enable_new_guard(self):
        client, broker = self.client(transform=lambda value: value['response'].update(legacy_extra=True), validator=None)
        self.assertTrue(client.infer(self.request).response['legacy_extra'])
        self.assertEqual(len(broker.requests), 1)

    def test_validator_does_not_grant_admission_and_noncallable_is_rejected(self):
        with self.assertRaises(ScientistAdmissionError):
            ScientistTurnClient(self.root / 'absent.sock', validate_receipt=validate_decider_receipt).infer(self.request)
        for invalid in (True, False, {}, 'validator'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ScientistTurnClient(self.root / 'absent.sock', validate_receipt=invalid)


class DeciderReceiptDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_guard_runs_on_host_before_journal_and_real_fixture_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            settings = Settings(workspace=root / 'workspace', database=root / 'synthetic.sqlite3')
            runtime = FixtureDesktop(settings.workspace)
            runtime.start()
            store = TrajectoryStore(settings.database)
            controller = DesktopController(store, runtime)
            broker = ReceiptBroker(root)
            scheduler = None
            validation_threads = []
            host_thread = threading.get_ident()
            def validate(request, receipt):
                validation_threads.append(threading.get_ident())
                self.assertEqual(store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                                 'pending')
                validate_decider_receipt(request, receipt)
            try:
                with self.assertRaises(ScientistAdmissionError):
                    create_scientist_desktop_scheduler(controller, settings, {'synthetic': True}, broker.path,
                                                      validate_receipt=validate)
                scheduler = create_scientist_desktop_scheduler(controller, settings,
                    {'synthetic_cpu_fixture': True, 'model_files': {'model.safetensors': 'a' * 64},
                     'checkpoint_revision': 'synthetic-cpu-fixture', 'tokenizer_revision': 'synthetic-cpu-fixture'},
                    broker.path, confirm_runtime=lambda profiles: None,
                    authenticator=authenticator_fixture(), timeout_seconds=2, validate_receipt=validate)
                desktop = controller.state()
                scheduler.start(desktop['lease_id'], desktop['generation'])
                for attempt in range(300):
                    approval = scheduler.status()['approval']
                    if approval or scheduler.task.done():
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(approval)
                self.assertFalse((runtime.root / 'hello.txt').exists())
                self.assertEqual(validation_threads, [host_thread])
                scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
                await asyncio.wait_for(asyncio.shield(scheduler.task), 3)
                self.assertEqual(scheduler.status()['jobs'][0]['status'], 'succeeded')
                self.assertEqual((runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
                self.assertEqual(store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
                self.assertEqual(len(broker.requests), 1)
            finally:
                if scheduler is not None:
                    await scheduler.close()
                broker.close()
                runtime.stop()
                store.close()

    async def test_async_validation_failure_remains_uncertain_without_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = ReceiptBroker(Path(directory), lambda value: value['usage'].update(input_tokens=33))
            writer = Mock(return_value=None)
            client = ScientistAsyncTurnClient(broker.path, timeout_seconds=2,
                authenticator=authenticator_fixture(), verify_admission=lambda request: None,
                persist_intent=lambda *arguments: None, record_receipt=writer,
                validate_receipt=validate_decider_receipt)
            try:
                with self.assertRaises(ScientistUncertainTurn):
                    await asyncio.wait_for(client.infer(request_fixture()), 3)
                writer.assert_not_called()
                self.assertEqual(client.uncertain_request_id, request_fixture().request_id)
                with self.assertRaises(ScientistAdmissionError):
                    await client.infer(request_fixture())
                self.assertEqual(len(broker.requests), 1)
            finally:
                broker.close()
