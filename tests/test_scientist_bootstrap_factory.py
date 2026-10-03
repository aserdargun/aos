"""Synthetic reviewed pins, owned CPU UDS and real controller SQLite only."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aos.desktop_control import DesktopController
from aos.scientist_admission_history import ScientistAdmissionBindingV2
from aos.scientist_bootstrap import CONTROL_DESCRIPTOR_SHA256
from aos.scientist_bootstrap_factory import BOOTSTRAP_INTENT_KIND, ScientistBootstrapAdmissionFactory
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_protocol import scientist_request_frame, scientist_request_sha256
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn

from test_desktop_tasks import FixtureDesktop
from test_scientist_evidence_client import SyntheticEvidenceServer
import test_scientist_bootstrap as bootstrap_cases


class ScientistBootstrapFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = bootstrap_cases.ScientistBootstrapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = FixtureDesktop(self.fixture.root / 'workspace')
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.controller = DesktopController(self.fixture.store, self.runtime)
        state = self.controller.state()
        self.fixture.binding = self.fixture.binding.model_copy(update={key: state[key] for key in
            ('session_id', 'runtime_id', 'owner', 'lease_id', 'generation')})
        self.thread = threading.get_ident()
        self.source = Mock(side_effect=self.verify_source)
        self.reviewed = {self.fixture.request.profile_id: deepcopy(self.fixture.stable)}
        self.server = SyntheticEvidenceServer(self.fixture.root, inspect=self.inspect_sent_intent)
        self.addCleanup(self.server.close)
        self.factory = self.make_factory()
        self.history = self.factory(self.controller)
        self.capture = self.history.capture

    def verify_source(self, selected):
        self.assertEqual(threading.get_ident(), self.thread)
        self.assertEqual(set(selected), {self.fixture.request.profile_id})
        self.assertIsInstance(selected[self.fixture.request.profile_id], ScientistAdmissionBindingV2)
        self.assertEqual(selected[self.fixture.request.profile_id].model_dump(mode='json'), self.fixture.stable)

    def make_factory(self, **changes):
        return ScientistBootstrapAdmissionFactory(self.reviewed, self.server.path, **({
            'control_descriptor_sha256': CONTROL_DESCRIPTOR_SHA256, 'verify_source': self.source,
            'authenticator': self.fixture.authenticator, 'clock': lambda: self.fixture.now} | changes))

    def inspect_sent_intent(self, frame):
        control = json.loads(frame)
        with closing(sqlite3.connect(self.fixture.fixture.path)) as reader:
            row = reader.execute('SELECT kind,payload_json FROM desktop_events WHERE event_id=?',
                                 (control['control_id'],)).fetchone()
        self.assertEqual(row[0], BOOTSTRAP_INTENT_KIND)
        payload = json.loads(row[1])
        self.assertEqual(payload['control_frame'], frame[:-1].decode())
        self.assertEqual(payload['control_sha256'], hashlib.sha256(frame[:-1]).hexdigest())
        self.assertEqual(payload['authority'], 'audit-only')

    async def prepare(self):
        await self.capture.prepare_async(self.fixture.request, self.fixture.binding,
            self.factory.expected_peer(self.fixture.request, self.fixture.binding))

    def audit_rows(self):
        return list(self.fixture.store.connection.execute('SELECT * FROM desktop_events WHERE kind=?', (BOOTSTRAP_INTENT_KIND,)))

    def no_original_intent(self):
        self.assertFalse(self.fixture.store.connection.in_transaction)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0], 0)

    async def test_actual_factory_prefetch_audit_then_same_store_original_history(self):
        self.assertIs(self.factory(self.controller), self.history)
        self.assertIs(self.history.store, self.controller.store)
        self.assertEqual(self.history.record_version, '2.0')
        await self.prepare()
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.audit_rows()), 1)
        payload = json.loads(self.audit_rows()[0]['payload_json'])
        self.assertEqual(payload['intent_binding'], self.fixture.binding.model_dump(mode='json'))
        self.assertEqual(json.loads(payload['control_frame'])['target'], None)
        self.assertNotIn('payload', payload)
        self.assertNotIn(self.fixture.request.payload['text'], payload['control_frame'])
        journal = ScientistIntentJournal(self.controller.store, self.fixture.binding, admission_history=self.history)
        journal.persist_intent(self.fixture.fixture.frame, self.fixture.fixture.digest, time.monotonic() + 5, self.fixture.peer)
        record, _checksum = self.history.read(self.fixture.request.request_id)
        self.assertEqual(record.admission_binding.model_dump(mode='json'), self.fixture.stable)
        journal.verify_admission(self.fixture.request)
        self.assertEqual(len(self.server.requests), 1)

    async def test_reviewed_map_copied_and_source_receives_no_mutable_factory_pins(self):
        self.reviewed[self.fixture.request.profile_id]['policy_sha256'] = 'f' * 64
        def mutate(selected):
            self.verify_source(selected)
            selected[self.fixture.request.profile_id] = selected[self.fixture.request.profile_id].model_copy(
                update={'policy_sha256': 'e' * 64})
        self.source.side_effect = mutate
        await self.prepare()
        self.assertEqual(len(self.server.requests), 1)

    async def test_runtime_confirmation_before_controller_and_default_denial(self):
        fresh = self.make_factory()
        fresh.confirm_runtime({self.fixture.request.profile_id: self.fixture.request.deployment_digest})
        from aos.scientist_bootstrap_factory import _deny_source
        denied = self.make_factory(verify_source=_deny_source)
        with self.assertRaises(ScientistAdmissionError): denied(self.controller)
        with self.assertRaises(ScientistAdmissionError):
            fresh.confirm_runtime({self.fixture.request.profile_id: 'f' * 64})
        self.assertEqual(self.server.requests, [])

    async def test_foreign_controller_same_store_cannot_rebind_factory(self):
        foreign = DesktopController(self.fixture.store, self.runtime)
        with self.assertRaises(ScientistAdmissionError): self.factory(foreign)
        self.assertIs(self.factory(self.controller), self.history)
        self.assertEqual(self.server.requests, [])

    async def test_output_contract_is_independent_copy_of_reviewed_pin(self):
        original = self.factory.output_contract.model_dump(mode='json')
        returned = self.factory.output_contract
        self.assertIsNot(returned, self.factory.output_contract)
        with self.assertRaises(ValueError):
            returned.bundle_sha256 = 'f' * 64
        changed = returned.model_copy(update={'bundle_sha256': 'f' * 64})
        self.assertNotEqual(changed.model_dump(mode='json'), original)
        self.assertEqual(self.factory.output_contract.model_dump(mode='json'), original)
        self.assertEqual(self.server.requests, [])

    async def test_mixed_reviewed_output_contracts_deny_single_startup_pin(self):
        reviewed = deepcopy(self.reviewed)
        profile = 'aos.bonsai.vision.v1'
        second = deepcopy(self.fixture.stable)
        second['profile_id'] = profile
        second['profile_pin']['output_contract']['bundle_sha256'] = 'f' * 64
        reviewed[profile] = second
        factory = ScientistBootstrapAdmissionFactory(reviewed, self.server.path,
            control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256, verify_source=self.source,
            authenticator=self.fixture.authenticator)
        with self.assertRaises(ScientistAdmissionError):
            _contract = factory.output_contract
        self.assertEqual(self.server.requests, [])

    async def test_bad_pins_or_missing_output_contract_rejected_before_socket(self):
        for change in ('descriptor', 'caller', 'output', 'profile'):
            reviewed = deepcopy(self.reviewed)
            value = reviewed[self.fixture.request.profile_id]
            if change == 'descriptor': value['infer_schema']['sha256'] = 'f' * 64
            elif change == 'caller': value['caller_generation']['pid'] += 1
            elif change == 'output': del value['profile_pin']['output_contract']
            else: value['profile_id'] = 'aos.bonsai.vision.v1'
            with self.subTest(change=change), self.assertRaises((ScientistAdmissionError, ValueError)):
                ScientistBootstrapAdmissionFactory(reviewed, self.server.path,
                    control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256, verify_source=self.source)
        self.assertEqual(self.server.requests, [])

    async def test_self_consistent_foreign_ack_cannot_replace_reviewed_binding(self):
        def mutate(response):
            capability = response['data']['capability']
            capability['policy_sha256'] = capability['admission_binding']['policy_sha256'] = 'f' * 64
            capability['admission_binding_sha256'] = digest(capability['admission_binding'])
            response['capability_sha256'] = digest(capability)
        self.fixture.response_transform = mutate
        with self.assertRaises((ScientistAdmissionError, ScientistUncertainTurn)): await self.prepare()
        self.assertIsNone(self.capture._prepared)
        self.assertEqual(len(self.audit_rows()), 1)
        self.no_original_intent()

    async def test_current_desktop_revoke_denies_before_socket(self):
        self.controller.control('pause')
        with self.assertRaises(ScientistAdmissionError): await self.prepare()
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.audit_rows(), [])
        self.no_original_intent()

    async def test_source_revoke_before_audit_commit_rolls_back_and_sends_no_frame(self):
        def revoke(selected):
            self.verify_source(selected)
            if self.fixture.store.connection.in_transaction and self.audit_rows():
                raise ScientistAdmissionError('Synthetic source revoked during audit transaction')
        self.source.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError): await self.prepare()
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.audit_rows(), [])
        self.no_original_intent()

    async def test_duplicate_event_id_rolls_back_without_packet(self):
        control_id = 'e' * 32
        with self.fixture.store.connection:
            self.fixture.store.insert('desktop_events', event_id=control_id, session_id=self.controller.session_id,
                                      kind='synthetic_existing_event', payload_json='{}', created_at='synthetic')
        with patch('aos.scientist_bootstrap.uuid4', return_value=Mock(hex=control_id)):
            with self.assertRaises(sqlite3.IntegrityError): await self.prepare()
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.audit_rows(), [])
        self.no_original_intent()

    async def test_failed_postcommit_exact_readback_sends_no_packet(self):
        with patch.object(self.factory, '_read_event', return_value=None):
            with self.assertRaises(ScientistAdmissionError): await self.prepare()
        self.assertEqual(len(self.audit_rows()), 1)
        self.assertEqual(self.server.requests, [])
        self.assertTrue(self.capture._blocked)
        self.no_original_intent()

    async def test_unresolved_original_journal_blocks_new_bootstrap_authority(self):
        other = self.fixture.request.model_copy(update={'request_id': 'c' * 32})
        journal = ScientistIntentJournal(self.fixture.store, self.fixture.binding)
        journal.persist_intent(scientist_request_frame(other)[:-1], scientist_request_sha256(other),
                               time.monotonic() + 5, self.fixture.peer)
        with self.assertRaises(ScientistAdmissionError): await self.prepare()
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.audit_rows(), [])

    async def test_forged_control_frame_cannot_use_last_current_context(self):
        self.factory._bootstrap_current(self.fixture.request, self.fixture.binding, self.fixture.peer)
        frame = self.fixture.codec.encode_request(self.fixture.control())
        with self.assertRaises(ScientistAdmissionError):
            self.factory._persist_intent(frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 1, self.fixture.peer)
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.server.requests, [])

    async def test_nondurable_sqlite_configuration_denies_audit_and_packet(self):
        self.fixture.store.connection.execute('PRAGMA synchronous=OFF')
        with self.assertRaises(ScientistAdmissionError): await self.prepare()
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.server.requests, [])
        self.no_original_intent()

    async def test_boolean_source_authority_and_invalid_configuration_deny(self):
        self.source.side_effect = None
        self.source.return_value = True
        with self.assertRaises(ScientistAdmissionError): await self.prepare()
        for options in ({'timeout_seconds': True}, {'timeout_seconds': 11}, {'clock': 1}, {'authenticator': object()}):
            with self.subTest(options=options), self.assertRaises(ScientistAdmissionError):
                self.make_factory(**options)
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.server.requests, [])


if __name__ == '__main__':
    unittest.main()
