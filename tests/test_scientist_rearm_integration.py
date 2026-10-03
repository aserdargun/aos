"""Actual AOS-synthetic UDS and original SQLite; no Scientist or GPU execution."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_async import ScientistAsyncTurnClient
from aos.scientist_desktop import ScientistDesktopBinding
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnRequest
from aos.scientist_retained_host import ScientistRetainedHost
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn

import test_scientist_evidence_transport as transport_cases
import test_scientist_intents as intent_cases
import test_scientist_retained_evidence_client as retained_cases
import test_scientist_transport as socket_cases


class ScientistRearmIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.socket = retained_cases.ScientistRetainedEvidenceClientTests()
        self.addCleanup(self.socket.doCleanups)
        setup = intent_cases.ScientistIntentTests.setUp
        persist = ScientistIntentJournal.persist_intent

        def live_synthetic_peer(fixture):
            setup(fixture)
            fixture.path.parent.chmod(0o700)
            fixture.peer = replace(fixture.peer, pid=os.getpid(), uid=os.getuid())

        def original_lost_ack(journal, frame, checksum, deadline, peer):
            self.broker = socket_cases.SyntheticBroker(self.socket.fixture.path.parent, mode='disconnect')
            self.addCleanup(self.broker.close)
            authenticator = Mock()
            authenticator.authenticate.return_value = peer
            authenticator.still_current.return_value = True
            self.client = ScientistAsyncTurnClient(self.broker.path, timeout_seconds=2,
                authenticator=authenticator, verify_admission=journal.verify_admission,
                persist_intent=lambda *arguments: persist(journal, *arguments),
                record_receipt=journal.record_receipt)
            request = ScientistTurnRequest.model_validate_json(frame, strict=True)
            with self.assertRaises(ScientistUncertainTurn):
                asyncio.run(self.client.infer(request))
            self.assertEqual(self.client.uncertain_request_id, request.request_id)
            self.assertEqual(self.broker.requests, [frame + b'\n'])

        with patch.object(intent_cases.ScientistIntentTests, 'setUp', live_synthetic_peer), \
                patch.object(ScientistIntentJournal, 'persist_intent', original_lost_ack):
            self.socket.setUp()
        self.fixture = self.socket.fixture
        self.original_rows = self.fixture.rows()
        self.resolution_authority = Mock(return_value=None)
        self.host = ScientistRetainedHost(self.fixture.store, self.fixture.history, self.fixture.binding,
            socket_path=self.socket.server.path,
            reviewed_schema_bytes=(REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            transport_schema_sha256=retained_cases.RETAINED_PIN,
            evidence_schema_sha256=transport_cases.EVIDENCE_PIN,
            authenticator=self.socket.authenticator, verifier=self.socket.proof,
            verify_control=self.socket.authority, verify_resolution=self.resolution_authority,
            timeout_seconds=2)
        controller = SimpleNamespace(store=self.fixture.store,
            state=lambda: dict(self.fixture.store.connection.execute(
                "SELECT * FROM desktop_sessions WHERE session_id='session'").fetchone()))
        self.binding = ScientistDesktopBinding(controller, admission_history=self.fixture.history)
        self.binding.engine = SimpleNamespace(client=self.client)
        self.binding.scheduler = SimpleNamespace(closed=False, _scientist_engines=Mock(return_value=None))
        self.capability_id, self.reconcile_id = 'd' * 32, 'e' * 32

    def retained_ack(self):
        self.host.discover(self.fixture.request.request_id, self.capability_id)
        self.host.reconcile(self.fixture.request.request_id, self.capability_id, self.reconcile_id)
        return self.host.inspect(self.reconcile_id,
            capability_control_id=self.capability_id)['response_sha256']

    def rearm(self, response_sha256):
        self.binding.rearm_retained_client(self.host, self.fixture.request.request_id,
            reconcile_control_id=self.reconcile_id, capability_control_id=self.capability_id,
            response_sha256=response_sha256)

    def fresh_request(self):
        payload = deepcopy(self.fixture.request.payload)
        payload['request']['options'][0]['id'] = 'fresh_synthetic_choice'
        return self.fixture.request.model_copy(update={'request_id': 'f' * 32, 'payload': payload})

    def test_lost_original_ack_full_retained_resolution_explicit_rearm_then_fresh_turn(self):
        fresh = self.fresh_request()
        with self.assertRaises(ScientistAdmissionError):
            asyncio.run(self.client.infer(fresh))
        response_sha256 = self.retained_ack()
        self.socket.physical.assert_not_called()
        self.assertEqual(self.fixture.rows(), self.original_rows)
        resolution = self.host.resolve(self.reconcile_id, self.capability_id,
            response_sha256=response_sha256)
        self.assertEqual(resolution['request_id'], self.fixture.request.request_id)
        self.assertEqual(self.client.uncertain_request_id, self.fixture.request.request_id)
        with self.assertRaises(ScientistAdmissionError):
            asyncio.run(self.client.infer(fresh))
        physical_count = self.socket.physical.call_count
        self.rearm(response_sha256)
        self.assertGreater(self.socket.physical.call_count, physical_count)
        self.fixture.source.assert_called()
        self.fixture.output.assert_called()
        self.fixture.resolver.assert_called()
        self.assertIsNone(self.client.uncertain_request_id)
        self.assertEqual(self.fixture.rows(), self.original_rows)
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.broker.mode = 'success'
        receipt = asyncio.run(self.client.infer(fresh))
        self.assertTrue(receipt.response['synthetic_cpu_fixture'])
        admission, checksum = self.fixture.history.read(fresh.request_id)
        self.assertEqual(admission.schema_version, '2.0')
        self.assertNotEqual(checksum, self.fixture.original_sha)
        self.assertEqual(len(self.broker.requests), 2)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assertEqual(self.fixture.history.read(self.fixture.request.request_id),
            (self.fixture.original, self.fixture.original_sha))
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT state,receipt_json FROM scientist_turn_intents WHERE request_id=?',
            (self.fixture.request.request_id,)).fetchone()[:], ('pending', None))
        self.assertEqual(tuple(self.fixture.store.connection.execute(
            'SELECT * FROM scientist_turn_intents WHERE request_id=?',
            (self.fixture.request.request_id,)).fetchone()), self.original_rows['scientist_turn_intents'][0])
        for operation in (lambda: asyncio.run(self.client.infer(self.fixture.request)),
                          lambda: self.fixture.journal.verify_admission(self.fixture.request)):
            with self.assertRaises(ScientistAdmissionError):
                operation()
        self.assertEqual(len(self.broker.requests), 2)

    def test_retained_ack_failed_physical_proof_preserves_original_uncertainty(self):
        response_sha256 = self.retained_ack()
        self.socket.physical.side_effect = ScientistAdmissionError('Synthetic independent drain denied')
        with self.assertRaises(ScientistAdmissionError):
            self.rearm(response_sha256)
        self.assertEqual(self.client.uncertain_request_id, self.fixture.request.request_id)
        self.assertEqual(self.fixture.rows(), self.original_rows)
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)
        with self.assertRaises(ScientistAdmissionError):
            asyncio.run(self.client.infer(self.fresh_request()))
        self.assertEqual(len(self.broker.requests), 1)
        self.assertEqual(len(self.socket.server.requests), 2)
