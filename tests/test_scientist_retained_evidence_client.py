"""Owned AOS-synthetic v3 Unix sockets and SQLite; no Scientist or GPU execution."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import time
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_evidence_client import ScientistEvidenceClient
from aos.scientist_evidence_journal import ScientistEvidenceJournal
from aos.scientist_release_proof import ScientistReleaseProofVerifier
from aos.scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn

import test_scientist_budget_witness as budget_fixture
import test_scientist_evidence_client as socket_fixture
import test_scientist_evidence_transport as transport_fixture


RETAINED_PIN = 'cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070'


class ScientistRetainedEvidenceClientTests(unittest.TestCase):
    def setUp(self):
        self.fixture = budget_fixture.ScientistBudgetWitnessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.path.parent
        self.root.chmod(0o700)
        self.original_rows = self.fixture.rows()
        self.proof, self.witness, self.envelope, self.physical = self.fixture.retained_fixture()
        self.peer = replace(self.fixture.peer, pid=os.getpid(), uid=os.getuid())
        self.codec = self.retained_codec()
        self.authority = Mock(return_value=None)
        self.control = {
            'schema': 'aos-scientist-control-evidence.v3', 'version': 3, 'op': 'capability',
            'control_id': 'd' * 32, 'profile_id': self.fixture.request.profile_id,
            'deployment_digest': self.fixture.request.deployment_digest,
            'target': deepcopy(self.witness['target']), 'expected_capability_sha256': None,
            'evidence_schema_sha256': transport_fixture.EVIDENCE_PIN, 'transport_schema_sha256': RETAINED_PIN}
        self.authenticator = Mock()
        self.authenticator.authenticate.return_value = self.peer
        self.authenticator.still_current.return_value = True
        self.response_patch = patch.object(socket_fixture, 'evidence_response_fixture', side_effect=self.response)
        self.response_patch.start()
        self.addCleanup(self.response_patch.stop)
        self.server = socket_fixture.SyntheticEvidenceServer(self.root)
        self.addCleanup(self.server.close)

    def retained_codec(self, capability=None):
        return ScientistRetainedEvidenceCodec(
            (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes(),
            transport_schema_sha256=RETAINED_PIN, evidence_schema_sha256=transport_fixture.EVIDENCE_PIN,
            expected_capability=capability)

    def response(self, request, _synthetic_unused_binding):
        if request['op'] == 'capability':
            response = transport_fixture.evidence_response_fixture(
                request, self.fixture.original.admission_binding.model_dump(mode='json'))
            response['data']['transport_schema_sha256'] = RETAINED_PIN
        else:
            response = {'control_id': request['control_id'], 'op': request['op'], 'ok': True,
                'capability_sha256': request['expected_capability_sha256'], 'error': None,
                'data': {'evidence': deepcopy(self.envelope), 'original_budget_witness': deepcopy(self.witness)}}
        response.update(schema='aos-scientist-control-evidence.v3', version=3)
        return response

    def journal(self, codec=None):
        return ScientistEvidenceJournal(self.fixture.store, self.fixture.binding,
            codec=codec or self.codec, admission_history=self.fixture.history, verify_control=self.authority)

    def client(self, journal, *, record_response=None):
        return ScientistEvidenceClient(self.server.path, codec=journal.codec, timeout_seconds=2,
            authenticator=self.authenticator, authorize=journal.authorize,
            persist_intent=journal.persist_intent, record_response=record_response or journal.record_response)

    def capability_then_reconcile(self):
        journal = self.journal()
        capability_response = self.client(journal).exchange(self.control)
        self.assertEqual(capability_response['data']['admission'], 'denied')
        capability = capability_response['data']['capability']
        codec = self.retained_codec(capability)
        request = self.control | {'op': 'reconcile', 'control_id': 'e' * 32,
                                 'expected_capability_sha256': digest(capability)}
        return self.journal(codec), request

    def counts(self):
        return tuple(self.fixture.store.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('scientist_evidence_controls', 'scientist_evidence_responses'))

    def assert_infer_unchanged(self):
        self.assertEqual(self.fixture.rows(), self.original_rows)
        self.assertEqual(self.fixture.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')

    def test_actual_socket_capability_reconcile_immutable_ack_and_independent_proof_composition(self):
        journal, request = self.capability_then_reconcile()
        response = self.client(journal).exchange(request)
        self.assertEqual(self.counts(), (2, 2))
        inspected = journal.inspect(request['control_id'])
        self.assertFalse(inspected['pending'])
        self.assertEqual(inspected['response'], response)
        self.assertEqual(inspected['response_sha256'], hashlib.sha256(canonical(response).encode()).hexdigest())
        terminal = self.proof.verify_response(self.fixture.request, codec=journal.codec,
            response_bytes=canonical(response).encode(), control_request_bytes=journal.codec.encode_request(request))
        self.assertEqual(terminal.allocation_binding_sha256, self.witness['allocation_binding_sha256'])
        self.physical.assert_called_once()
        self.assertEqual(len(self.server.requests), 2)
        self.assert_infer_unchanged()

    def test_wrong_v2_version_or_pin_and_default_authority_send_no_bytes(self):
        journal = self.journal()
        for request in (self.control | {'schema': 'aos-scientist-control-evidence.v2', 'version': 2},
                        self.control | {'transport_schema_sha256': transport_fixture.TRANSPORT_PIN}):
            with self.subTest(request=request), self.assertRaises(ScientistAdmissionError):
                self.client(journal).exchange(request)
        with self.assertRaises(ScientistAdmissionError):
            ScientistEvidenceClient(self.server.path, codec=self.codec,
                authenticator=self.authenticator).exchange(self.control)
        denied = ScientistEvidenceJournal(self.fixture.store, self.fixture.binding, codec=self.codec,
            admission_history=self.fixture.history)
        with self.assertRaises(ScientistAdmissionError):
            self.client(denied).exchange(self.control)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.counts(), (0, 0))
        self.assert_infer_unchanged()

    def test_pending_v2_control_blocks_v3_in_fresh_client_without_adoption(self):
        codec = transport_fixture.evidence_codec_fixture()
        journal = self.journal(codec)
        legacy = self.control | {'schema': 'aos-scientist-control-evidence.v2', 'version': 2,
                                 'transport_schema_sha256': transport_fixture.TRANSPORT_PIN}
        frame = codec.encode_request(legacy)
        journal.persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 2, self.peer)
        before = tuple(self.fixture.store.connection.iterdump())
        fresh = self.client(self.journal())
        with self.assertRaises(ScientistAdmissionError):
            fresh.exchange(self.control | {'control_id': 'e' * 32})
        self.assertIsNone(fresh.uncertain_control_id)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(tuple(self.fixture.store.connection.iterdump()), before)
        self.assertEqual(self.counts(), (1, 0))
        self.assert_infer_unchanged()

    def test_response_committed_then_callback_failure_is_uncertain_without_any_replay(self):
        journal, request = self.capability_then_reconcile()

        def lost_ack(response, peer):
            journal.record_response(response, peer)
            raise ScientistAdmissionError('Synthetic post-write acknowledgment lost')

        client = self.client(journal, record_response=lost_ack)
        with self.assertRaises(ScientistUncertainTurn):
            client.exchange(request)
        self.assertEqual(client.uncertain_control_id, request['control_id'])
        self.assertFalse(journal.inspect(request['control_id'])['pending'])
        self.assertEqual(self.counts(), (2, 2))
        for retry in (request, request | {'control_id': 'f' * 32}):
            with self.assertRaises(ScientistAdmissionError):
                client.exchange(retry)
        self.assertEqual(len(self.server.requests), 2)
        self.physical.assert_not_called()
        self.assert_infer_unchanged()

    def test_ack_does_not_supply_default_current_resolver_or_physical_authority(self):
        journal, request = self.capability_then_reconcile()
        response = self.client(journal).exchange(request)
        retained = response['data']
        budget = self.fixture.verifier().verify(self.fixture.request,
            canonical(retained['original_budget_witness']).encode())
        options = dict(self.proof.options)
        for omitted in ('verify_resolver', 'verify_physical'):
            selected = options.copy()
            selected.pop(omitted)
            verifier = ScientistReleaseProofVerifier(self.fixture.history, expected_budget=budget, **selected)
            with self.subTest(omitted=omitted), self.assertRaises(ScientistAdmissionError):
                verifier.verify(self.fixture.request, canonical(retained['evidence']).encode())
        self.physical.assert_not_called()
        self.assertEqual(self.counts(), (2, 2))
        self.assert_infer_unchanged()

    def test_capability_ack_and_retryable_error_never_enter_budget_or_proof_verification(self):
        journal = self.journal()
        response = self.client(journal).exchange(self.control)
        with self.assertRaises(ScientistAdmissionError):
            self.proof.verify_response(self.fixture.request, codec=self.codec,
                response_bytes=canonical(response).encode(), control_request_bytes=self.codec.encode_request(self.control))
        capability = response['data']['capability']
        codec = self.retained_codec(capability)
        request = self.control | {'op': 'reconcile', 'control_id': 'e' * 32,
                                 'expected_capability_sha256': digest(capability)}
        error = {'schema': request['schema'], 'version': 3, 'control_id': request['control_id'],
                 'op': 'reconcile', 'ok': False, 'capability_sha256': None, 'data': None,
                 'error': {'code': 'busy', 'retryable': True}}
        with self.assertRaises(ScientistAdmissionError):
            self.proof.verify_response(self.fixture.request, codec=codec,
                response_bytes=canonical(error).encode(), control_request_bytes=codec.encode_request(request))
        self.fixture.source.assert_not_called()
        self.fixture.output.assert_not_called()
        self.fixture.resolver.assert_not_called()
        self.physical.assert_not_called()
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(len(self.server.requests), 1)
        self.assert_infer_unchanged()
