"""Synthetic original-store control effects, never physical release or infer resolution."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_evidence_client import ScientistEvidenceClient
from aos.scientist_evidence_journal import ScientistEvidenceJournal
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_inventory import scientist_control_inventory
from aos.scientist_protocol import scientist_request_frame, scientist_request_sha256
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn
from aos.storage import TrajectoryStore

import test_scientist_intents as intent_fixture
from test_scientist_admission_history import BOOT_ID, admission_capture_fixture
from test_scientist_decider_receipt import request_fixture
from test_scientist_evidence_client import SyntheticEvidenceServer
from test_scientist_evidence_transport import evidence_codec_fixture, evidence_response_fixture
from test_scientist_profile_output import OUTPUT_PIN


class ScientistEvidenceJournalTests(unittest.TestCase):
    def setUp(self):
        intent_fixture.ScientistIntentTests.setUp(self)
        self.path.parent.chmod(0o700)
        self.peer = replace(self.peer, pid=os.getpid(), uid=os.getuid(), boot_id=BOOT_ID)
        self.infer = request_fixture()
        self.history = self.history_for(self.store)
        self.infer_journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)
        self.infer_journal.persist_intent(scientist_request_frame(self.infer)[:-1],
            scientist_request_sha256(self.infer), time.monotonic() + 60, self.peer)
        self.original, self.original_sha = self.history.read(self.infer.request_id)
        self.codec = evidence_codec_fixture()
        self.control = {'schema': 'aos-scientist-control-evidence.v2', 'version': 2,
            'op': 'capability', 'control_id': 'd' * 32, 'profile_id': self.infer.profile_id,
            'deployment_digest': self.infer.deployment_digest,
            'target': {'request_id': self.infer.request_id, 'request_sha256': scientist_request_sha256(self.infer),
                       'original_peer_generation_sha256': digest(self.original.admission_binding.caller_generation.model_dump(mode='json'))},
            'expected_capability_sha256': None, 'evidence_schema_sha256': self.codec.evidence_schema_sha256,
            'transport_schema_sha256': self.codec.transport_schema_sha256}
        self.verify = Mock(return_value=None)
        self.evidence = self.journal_for(self.binding)

    @staticmethod
    def history_for(store):
        def capture(request, binding, peer):
            value = admission_capture_fixture(request, peer)
            value['admission_binding']['profile_pin']['output_contract'] = deepcopy(OUTPUT_PIN)
            return value
        return ScientistAdmissionHistory(store, capture=capture, verify_current=lambda *_args: None,
                                         clock=lambda: 100, record_version='2.0')

    def journal_for(self, binding, **options):
        return ScientistEvidenceJournal(self.store, binding, codec=self.codec, admission_history=self.history,
                                       **({'verify_control': self.verify} | options))

    def persist(self, journal=None, request=None, deadline=None):
        frame = self.codec.encode_request(request or self.control)
        (journal or self.evidence).persist_intent(frame, hashlib.sha256(frame).hexdigest(),
                                                deadline or time.monotonic() + 5, self.peer)

    def response(self, request=None):
        return evidence_response_fixture(request or self.control, self.original.admission_binding.model_dump(mode='json'))

    def counts(self):
        with closing(sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)) as reader:
            return tuple(reader.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                         for table in ('scientist_evidence_controls', 'scientist_evidence_responses'))

    def original_rows(self):
        return {table: [tuple(row) for row in self.store.connection.execute('SELECT * FROM ' + table)]
                for table in ('scientist_turn_intents', 'scientist_admission_history')}

    def takeover(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET owner='HUMAN',lease_id='human',generation=1")
        return self.binding.model_copy(update={'owner': 'HUMAN', 'lease_id': 'human', 'generation': 1})

    def test_atomic_control_and_separate_response_preserve_original_infer_fence(self):
        original = self.original_rows()
        observations = []
        self.verify.side_effect = lambda *_args: observations.append(self.counts())
        self.persist()
        self.assertEqual(observations, [(0, 0), (0, 0)])
        self.assertEqual(self.counts(), (1, 0))
        self.assertEqual(scientist_control_inventory(self.store, self.binding.session_id)['pending_count'], 1)
        self.assertTrue(self.evidence.inspect(self.control['control_id'])['pending'])
        self.evidence.record_response(self.response(), self.peer)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(scientist_control_inventory(self.store, self.binding.session_id)['pending_count'], 0)
        self.assertFalse(self.evidence.inspect(self.control['control_id'])['pending'])
        self.assertEqual(self.original_rows(), original)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
        self.assertEqual(observations[-2:], [(1, 0), (1, 0)])

    def test_authorize_is_readonly_and_accepts_only_exact_current_exchange_before_and_after_ack(self):
        original = self.original_rows()
        self.evidence.authorize(self.control, self.peer)
        self.assertEqual(self.counts(), (0, 0))
        self.persist()
        self.evidence.authorize(self.control, self.peer)
        self.assertEqual(self.counts(), (1, 0))
        for request, peer in (({**self.control, 'control_id': 'e' * 32}, self.peer),
                              ({**self.control, 'op': 'reconcile', 'expected_capability_sha256': 'f' * 64}, self.peer),
                              (self.control, replace(self.peer, pid=self.peer.pid + 1))):
            with self.assertRaises(ScientistAdmissionError):
                self.evidence.authorize(request, peer)
        self.evidence.record_response(self.response(), self.peer)
        self.evidence.authorize(self.control, self.peer)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.original_rows(), original)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=generation+1")
        with self.assertRaises(ScientistAdmissionError):
            self.evidence.authorize(self.control, self.peer)

    def test_authorize_default_deny_and_late_owner_change_cannot_grant_cleanup(self):
        denied = ScientistEvidenceJournal(self.store, self.binding, codec=self.codec, admission_history=self.history)
        with self.assertRaises(ScientistAdmissionError):
            denied.authorize(self.control, self.peer)

        def revoke(*_arguments):
            self.store.connection.execute("UPDATE desktop_sessions SET generation=generation+1")

        self.verify.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError):
            self.evidence.authorize(self.control, self.peer)
        self.assertEqual(self.counts(), (0, 0))

    def test_both_operations_block_new_same_target_until_exact_response(self):
        self.persist()
        next_control = {**self.control, 'control_id': 'e' * 32, 'op': 'reconcile',
                        'expected_capability_sha256': 'a' * 64}
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist(request=next_control)
        self.evidence.record_response(self.response(), self.peer)
        self.persist(request=next_control)
        self.assertEqual(self.counts(), (2, 1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist(request={**self.control, 'control_id': 'f' * 32})

    def test_restart_preserves_pending_control_and_read_is_callback_free(self):
        self.persist()
        self.store.close()
        self.store = TrajectoryStore(self.path)
        self.history = self.history_for(self.store)
        restarted = self.journal_for(self.binding)
        with self.assertRaises(ScientistAdmissionError):
            self.persist(restarted, {**self.control, 'control_id': 'e' * 32})
        current = self.store.connection.execute('SELECT generation FROM desktop_sessions').fetchone()[0]
        fresh_binding = self.binding.model_copy(update={'owner': 'HUMAN', 'lease_id': 'restart-human',
                                                        'generation': current + 1})
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET status='running',owner='HUMAN',lease_id='restart-human',generation=?",
                                          (current + 1,))
        restarted = self.journal_for(fresh_binding)
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist(restarted, {**self.control, 'control_id': 'e' * 32})
        self.verify.side_effect = AssertionError('Readonly inspection cannot authorize')
        self.history.verify_current = self.history.capture = self.history.clock = self.verify
        self.assertTrue(restarted.inspect(self.control['control_id'])['pending'])

    def test_duplicate_exact_or_changed_control_and_response_never_adopt(self):
        self.persist()
        for request in (self.control, {**self.control, 'op': 'reconcile', 'expected_capability_sha256': 'a' * 64}):
            with self.assertRaises(sqlite3.IntegrityError):
                self.persist(request=request)
        self.evidence.record_response(self.response(), self.peer)
        with self.assertRaises(sqlite3.IntegrityError):
            self.evidence.record_response(self.response(), self.peer)

    def test_sql_append_only_and_original_target_guards_survive_direct_writes(self):
        self.persist()
        self.evidence.record_response(self.response(), self.peer)
        for table in ('scientist_evidence_controls', 'scientist_evidence_responses'):
            for statement in (f'UPDATE {table} SET created_at=created_at', f'DELETE FROM {table}'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    with self.store.connection:
                        self.store.connection.execute(statement)
        row = dict(self.store.connection.execute('SELECT * FROM scientist_evidence_controls').fetchone())
        row.update(control_id='e' * 32, admission_record_sha256='f' * 64,
                   request_json=canonical({**self.control, 'control_id': 'e' * 32}))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connection:
                self.store.connection.execute('INSERT INTO scientist_evidence_controls VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))

    def test_foreign_request_hash_profile_caller_or_deployment_denied(self):
        for change in ('request_id', 'request_sha256', 'original_peer_generation_sha256', 'profile_id', 'deployment_digest'):
            request = deepcopy(self.control)
            if change in request['target']:
                request['target'][change] = 'f' * len(request['target'][change])
            else:
                request[change] = 'aos.bonsai.vision.v1' if change == 'profile_id' else 'f' * 64
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError):
                self.persist(request=request)
        self.assertEqual(self.counts(), (0, 0))
        self.verify.assert_not_called()

    def test_default_denied_current_authority_and_foreign_store_cannot_persist(self):
        denied = ScientistEvidenceJournal(self.store, self.binding, codec=self.codec, admission_history=self.history)
        with self.assertRaises(ScientistAdmissionError):
            self.persist(denied)
        foreign = Mock()
        with self.assertRaises(ScientistAdmissionError):
            ScientistEvidenceJournal(foreign, self.binding, codec=self.codec, admission_history=self.history)
        self.assertEqual(self.counts(), (0, 0))

    def test_legacy_original_without_admission_identity_cannot_be_adopted(self):
        binding = self.binding.model_copy(update={'session_id': 'legacy', 'runtime_id': 'legacy-runtime'})
        with self.store.connection:
            self.store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('legacy', 'legacy-runtime', 'synthetic-image', 'AGENT', 'lease', 0, 'running', 'synthetic', 'synthetic'))
        infer = self.infer.model_copy(update={'request_id': '9' * 32})
        legacy = ScientistIntentJournal(self.store, binding)
        legacy.persist_intent(scientist_request_frame(infer)[:-1], scientist_request_sha256(infer),
                              time.monotonic() + 60, self.peer)
        control = deepcopy(self.control)
        control['target'].update(request_id=infer.request_id, request_sha256=scientist_request_sha256(infer))
        with self.assertRaises(ScientistAdmissionError):
            self.persist(self.journal_for(binding), control)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_admission_history').fetchone()[0], 1)

    def test_human_takeover_requires_fresh_binding_and_explicit_cleanup_without_infer_adoption(self):
        original = self.original_rows()
        human_binding = self.takeover()
        with self.assertRaises(ScientistAdmissionError):
            self.persist()
        human = self.journal_for(human_binding)
        self.persist(human)
        human.record_response(self.response(), self.peer)
        self.assertEqual(self.verify.call_args.args[2].owner, 'HUMAN')
        self.assertEqual(self.original_rows(), original)
        with self.assertRaises(ScientistAdmissionError):
            self.infer_journal.verify_admission(self.infer)

    def test_takeover_does_not_reset_or_adopt_an_older_pending_control(self):
        self.persist()
        human = self.journal_for(self.takeover())
        with self.assertRaises(ScientistAdmissionError):
            human.record_response(self.response(), self.peer)
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist(human, {**self.control, 'control_id': 'e' * 32})
        self.assertTrue(human.inspect(self.control['control_id'])['pending'])

    def test_late_owner_change_or_callback_failure_rolls_back_each_atomic_append(self):
        def revoke(*_arguments):
            self.store.connection.execute("UPDATE desktop_sessions SET owner='HUMAN',generation=1,lease_id='revoked'")
        self.verify.side_effect = [None, ScientistAdmissionError('late control revoke')]
        with self.assertRaises(ScientistAdmissionError):
            self.persist()
        self.assertEqual(self.counts(), (0, 0))
        self.verify.side_effect = None
        self.persist()
        self.verify.side_effect = revoke
        with self.assertRaises(ScientistAdmissionError):
            self.evidence.record_response(self.response(), self.peer)
        self.assertEqual(self.counts(), (1, 0))

    def test_control_timeout_and_wrong_authenticated_peer_do_not_settle_exchange(self):
        for deadline in (time.monotonic() - 1, time.monotonic() + 11, float('nan'), True):
            with self.subTest(deadline=deadline), self.assertRaises(ScientistAdmissionError):
                self.persist(deadline=deadline)
        self.persist()
        with self.assertRaises(ScientistAdmissionError):
            self.evidence.record_response(self.response(), replace(self.peer, pid=self.peer.pid + 1))
        self.assertEqual(self.counts(), (1, 0))

    def test_expired_original_infer_deadline_does_not_mint_new_admission_for_cleanup(self):
        original = self.original_rows()
        with unittest.mock.patch('aos.scientist_intents.time.monotonic', return_value=time.monotonic() + 1000):
            with self.assertRaises(ScientistAdmissionError):
                self.infer_journal.verify_admission(self.infer)
            self.persist()
            self.evidence.record_response(self.response(), self.peer)
        self.assertEqual(self.original_rows(), original)

    def test_actual_private_socket_journals_control_and_response_on_original_store(self):
        observed = []
        server = SyntheticEvidenceServer(self.path.parent,
                                        inspect=lambda _frame: observed.append(self.counts()))
        self.addCleanup(server.close)
        authenticator = Mock()
        authenticator.authenticate.return_value = self.peer
        authenticator.still_current.return_value = True
        client = ScientistEvidenceClient(server.path, codec=self.codec, authenticator=authenticator,
            authorize=self.evidence.authorize, persist_intent=self.evidence.persist_intent,
            record_response=self.evidence.record_response)
        original = self.original_rows()
        with patch('test_scientist_evidence_client.evidence_response_fixture',
                   side_effect=lambda request, _binding: self.response(request)):
            response = client.exchange(self.control)
            reconcile = {**self.control, 'control_id': 'e' * 32, 'op': 'reconcile',
                         'expected_capability_sha256': response['capability_sha256']}
            evidence = client.exchange(reconcile)
        self.assertTrue(response['ok'])
        self.assertTrue(evidence['ok'])
        self.assertEqual(observed, [(1, 0), (2, 1)])
        self.assertEqual(self.counts(), (2, 2))
        self.assertEqual(self.evidence.inspect(self.control['control_id'])['response'], response)
        self.assertEqual(self.original_rows(), original)

    def test_actual_lost_response_stays_pending_and_cannot_be_retried_with_new_client(self):
        server = SyntheticEvidenceServer(self.path.parent, mode='disconnect')
        self.addCleanup(server.close)
        authenticator = Mock()
        authenticator.authenticate.return_value = self.peer
        authenticator.still_current.return_value = True

        def client():
            return ScientistEvidenceClient(server.path, codec=self.codec, authenticator=authenticator,
                authorize=self.evidence.authorize, persist_intent=self.evidence.persist_intent,
                record_response=self.evidence.record_response)

        with self.assertRaises(ScientistUncertainTurn):
            client().exchange(self.control)
        self.assertEqual(self.counts(), (1, 0))
        with self.assertRaises(ScientistAdmissionError):
            client().exchange({**self.control, 'control_id': 'e' * 32})
        self.assertEqual(len(server.requests), 1)

    def test_failed_response_callback_or_post_commit_revoke_is_uncertain_without_infer_resolution(self):
        server = SyntheticEvidenceServer(self.path.parent, mode='negative')
        self.addCleanup(server.close)
        authenticator = Mock()
        authenticator.authenticate.return_value = self.peer
        authenticator.still_current.return_value = True
        revoked = False

        def authorize(*_arguments):
            if revoked:
                raise ScientistAdmissionError('Post-commit authorization revoked')
            return self.evidence.authorize(*_arguments)

        def record(response, peer):
            nonlocal revoked
            self.evidence.record_response(response, peer)
            revoked = True

        original = self.original_rows()
        client = ScientistEvidenceClient(server.path, codec=self.codec, authenticator=authenticator,
            authorize=authorize, persist_intent=self.evidence.persist_intent, record_response=record)
        with self.assertRaises(ScientistUncertainTurn):
            client.exchange(self.control)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(client.uncertain_control_id, self.control['control_id'])
        self.assertEqual(self.original_rows(), original)
        next_control = {**self.control, 'control_id': 'e' * 32}
        client = ScientistEvidenceClient(server.path, codec=self.codec, authenticator=authenticator,
            authorize=self.evidence.authorize, persist_intent=self.evidence.persist_intent,
            record_response=Mock(side_effect=RuntimeError('Synthetic response disk failure')))
        with self.assertRaises(ScientistUncertainTurn):
            client.exchange(next_control)
        self.assertEqual(self.counts(), (2, 1))
        self.assertTrue(self.evidence.inspect(next_control['control_id'])['pending'])
