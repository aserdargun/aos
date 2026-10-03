import hashlib
from contextlib import closing
import json
import os
import select
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock

import jsonschema

from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest, scientist_request_frame
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError, ScientistTurnClient
from aos.storage import TrajectoryStore
from test_scientist_transport import SyntheticBroker


class ScientistIntentTests(unittest.TestCase):
    def test_binding_schema_matches_canonical_typed_model(self):
        schema = json.loads((Path(__file__).resolve().parents[1] /
                             'schemas/scientist_intent_binding.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        schema.pop('$schema')
        self.assertEqual(schema, ScientistIntentBinding.model_json_schema())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'synthetic.sqlite3'
        self.store = TrajectoryStore(self.path)
        self.addCleanup(lambda: self.store.close())
        with self.store.connection:
            self.store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                ('session', 'runtime', 'synthetic-image', 'AGENT', 'lease', 0, 'running', 'synthetic', 'synthetic'))
        self.binding = ScientistIntentBinding(session_id='session', runtime_id='runtime',
                    owner='AGENT', lease_id='lease', generation=0, authorization_context_sha256='a' * 64)
        self.journal = ScientistIntentJournal(self.store, self.binding)
        self.request = ScientistTurnRequest(request_id='a' * 32, profile_id='aos.decider.turn.v1',
                                            deployment_digest='b' * 64, payload={'text': 'İş synthetic'})
        self.frame = scientist_request_frame(self.request)[:-1]
        self.digest = hashlib.sha256(self.frame).hexdigest()
        self.peer = BrokerPeer(1234, 1000, 42, 'synthetic-boot', 'c' * 32, '/synthetic')
        unit = 'swapp-aos-gpu-turn-' + 'd' * 32 + '.service'
        self.receipt = ScientistTurnReceipt(version=1, request_id=self.request.request_id,
                    profile_id=self.request.profile_id, deployment_digest=self.request.deployment_digest,
                    generation={'unit': unit, 'invocation_id': 'e' * 32, 'main_pid': 5678,
                                'control_group': '/synthetic/' + unit}, response={}, usage={})

    def persist(self):
        self.journal.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)

    def test_committed_exact_intent_is_visible_to_independent_readonly_connection(self):
        self.persist()
        with closing(sqlite3.connect(self.path)) as reader:
            row = reader.execute('SELECT request_json,request_sha256,state FROM scientist_turn_intents').fetchone()
        self.assertEqual(row, (self.frame.decode(), self.digest, 'pending'))
        self.journal.verify_admission(self.request)
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist()
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_admission(self.request.model_copy(update={'request_id': 'f' * 32}))

    def test_restart_preserves_pending_intent_and_cannot_adopt_new_generation(self):
        self.persist()
        self.store.close()
        self.store = TrajectoryStore(self.path)
        row = self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()
        self.assertEqual(row['state'], 'pending')
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET status='running',generation=1,lease_id='new'")
        successor = ScientistIntentJournal(self.store, self.binding.model_copy(update={'generation': 1, 'lease_id': 'new'}))
        with self.assertRaises(ScientistAdmissionError):
            successor.verify_admission(self.request)
        with self.assertRaises(ScientistAdmissionError):
            successor.record_receipt(self.receipt, self.peer)

    def test_owned_cpu_writer_crash_after_commit_preserves_unresolved_intent(self):
        path = Path(self.temporary.name) / 'crash.sqlite3'
        source = '''import hashlib, sys, time
from pathlib import Path
from aos.storage import TrajectoryStore
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame
from aos.scientist_transport import BrokerPeer
store = TrajectoryStore(Path(sys.argv[1]))
with store.connection:
    store.connection.execute("INSERT INTO desktop_sessions VALUES('session','runtime','synthetic','AGENT','lease',0,'running','synthetic','synthetic')")
binding = ScientistIntentBinding(session_id='session',runtime_id='runtime',lease_id='lease',generation=0,owner='AGENT',authorization_context_sha256='a'*64)
request = ScientistTurnRequest(request_id='a'*32,profile_id='aos.decider.turn.v1',deployment_digest='b'*64,payload={})
frame = scientist_request_frame(request)[:-1]
ScientistIntentJournal(store,binding).persist_intent(frame,hashlib.sha256(frame).hexdigest(),time.monotonic()+60,BrokerPeer(1234,1000,42,'synthetic','c'*32,'/synthetic'))
print('committed',flush=True)
time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-c', source, str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline(), b'committed\n')
            process.kill()
            process.communicate(timeout=5)
            self.assertLess(process.returncode, 0)
            restarted = TrajectoryStore(path)
            try:
                self.assertEqual(restarted.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
            finally:
                restarted.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_wrong_owner_generation_runtime_or_lease_blocks_before_insert(self):
        for field, value in [('owner', 'HUMAN'), ('generation', 1), ('runtime_id', 'foreign'), ('lease_id', 'old')]:
            journal = ScientistIntentJournal(self.store, self.binding.model_copy(update={field: value}))
            with self.assertRaises(ScientistAdmissionError):
                journal.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_turn_intents').fetchone()[0], 0)

    def test_expired_noncanonical_hash_mismatch_and_nested_transaction_are_denied(self):
        for frame, digest, deadline in [(self.frame, self.digest, 1),
                (self.frame + b' ', self.digest, time.monotonic() + 60),
                (self.frame, '0' * 64, time.monotonic() + 60)]:
            with self.assertRaises(ScientistAdmissionError):
                self.journal.persist_intent(frame, digest, deadline, self.peer)
        self.store.connection.execute('BEGIN')
        try:
            with self.assertRaises(ScientistAdmissionError):
                self.persist()
        finally:
            self.store.connection.rollback()

    def test_receipt_recording_is_not_cleanup_or_permission_to_start_again(self):
        self.persist()
        self.journal.record_receipt(self.receipt, self.peer)
        row = self.store.connection.execute('SELECT * FROM scientist_turn_intents').fetchone()
        self.assertEqual(row['state'], 'receipt_recorded')
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_admission(self.request.model_copy(update={'request_id': 'f' * 32}))
        with self.assertRaises(ScientistAdmissionError):
            self.journal.record_receipt(self.receipt, self.peer)

    def test_old_peer_or_foreign_deployment_receipt_leaves_intent_pending(self):
        self.persist()
        for peer in [replace(self.peer, start_ticks=99), replace(self.peer, invocation_id='f' * 32)]:
            with self.assertRaises(ScientistAdmissionError):
                self.journal.record_receipt(self.receipt, peer)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.record_receipt(self.receipt.model_copy(update={'deployment_digest': 'f' * 64}), self.peer)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')

    def test_revoke_during_dispatch_prevents_recording_receipt(self):
        self.persist()
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET owner='PAUSED'")
        with self.assertRaises(ScientistAdmissionError):
            self.journal.record_receipt(self.receipt, self.peer)

    def test_sql_cannot_rewrite_or_delete_pending_identity(self):
        self.persist()
        for sql in ["DELETE FROM scientist_turn_intents", "UPDATE scientist_turn_intents SET deadline=deadline+1",
                    "UPDATE scientist_turn_intents SET request_sha256='" + 'f' * 64 + "'"]:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection:
                    self.store.connection.execute(sql)

    def test_cpu_transport_commits_intent_and_receipt_but_does_not_clear_drain_gate(self):
        broker = SyntheticBroker(Path(self.temporary.name))
        self.addCleanup(broker.close)
        authenticator = Mock()
        authenticator.authenticate.return_value = replace(self.peer, pid=os.getpid(), uid=os.getuid())
        authenticator.still_current.return_value = True
        client = ScientistTurnClient(broker.path, authenticator=authenticator,
                                     verify_admission=self.journal.verify_admission,
                                     persist_intent=self.journal.persist_intent,
                                     record_receipt=self.journal.record_receipt)
        receipt = client.infer(self.request)
        self.assertTrue(receipt.response['synthetic_cpu_fixture'])
        with closing(sqlite3.connect(self.path)) as reader:
            row = reader.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
        self.assertEqual(row[0], 'receipt_recorded')
        self.assertEqual(json.loads(row[1])['request_id'], self.request.request_id)
        with self.assertRaises(ScientistAdmissionError):
            client.infer(self.request.model_copy(update={'request_id': 'f' * 32}))
        self.assertEqual(len(broker.requests), 1)
