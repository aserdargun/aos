from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aos.contracts import REPO_ROOT
from aos.scientist_inventory import _admission_identity, scientist_inference_inventory
from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from aos.scientist_transport import BrokerPeer
from aos.storage import TrajectoryStore

import test_scientist_admission_history as history_fixture


class ScientistAdmissionInventoryTests(unittest.TestCase):
    def test_persisted_history_metadata_is_readonly_redacted_and_never_refreshes_authority(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-scientist-history-inventory-') as temporary:
            path = Path(temporary) / 'synthetic.sqlite3'
            with closing(TrajectoryStore(path)) as store:
                with store.connection:
                    store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                        ('session', 'runtime', 'synthetic', 'AGENT', 'private-lease', 0,
                         'running', 'synthetic', 'synthetic'))
                binding = ScientistIntentBinding(session_id='session', runtime_id='runtime', owner='AGENT',
                    lease_id='private-lease', generation=0, authorization_context_sha256='d' * 64)
                request = ScientistTurnRequest(request_id='a' * 32, profile_id='aos.decider.turn.v1',
                    deployment_digest='b' * 64, payload={'private_prompt': 'synthetic-private-prompt'})
                peer = BrokerPeer(1234, 1000, 42, '11111111-1111-1111-1111-111111111111',
                    'c' * 32, '/synthetic-private-cgroup')
                history = ScientistAdmissionHistory(store,
                    capture=lambda original, _binding, original_peer:
                        history_fixture.admission_capture_fixture(original, original_peer),
                    verify_current=lambda *arguments: None, clock=lambda: 100)
                journal = ScientistIntentJournal(store, binding, admission_history=history)
                journal.persist_intent(scientist_request_frame(request)[:-1], scientist_request_sha256(request),
                    time.monotonic() + 60, peer)
                record, checksum = history.read(request.request_id)
                callbacks = [Mock(side_effect=AssertionError('Read must not refresh admission')) for _index in range(3)]

                def readonly_history(original_store):
                    return ScientistAdmissionHistory(original_store, capture=callbacks[0],
                        verify_current=callbacks[1], clock=callbacks[2])

                before = hashlib.sha256(path.read_bytes()).hexdigest()
                with closing(TrajectoryStore(path, readonly=True)) as reader:
                    controller = SimpleNamespace(store=reader, session_id='session')
                    with patch('aos.scientist_admission_history.ScientistAdmissionHistory', side_effect=readonly_history):
                        inventory = scientist_inference_inventory(controller)
                    self.assertEqual(inventory['intents'][0]['admission_identity'], {
                        'available': True, 'present': True, 'record_sha256': checksum,
                        'binding_sha256': record.admission_binding_sha256})
                    self.assertTrue(inventory['admission_blocked'])
                    self.assertFalse(inventory['joint_runtime_admitted'])
                    self.assertFalse(inventory['gpu_release_verified'])
                    for private in ('private-lease', 'synthetic-private-prompt', peer.boot_id, peer.control_group,
                                    'caller_generation', 'policy_sha256', 'source_fingerprints', 'capability_sha256'):
                        self.assertNotIn(private, str(inventory))
                    self.assertEqual(reader.connection.total_changes, 0)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
                for callback in callbacks:
                    callback.assert_not_called()
                with store.connection:
                    store.connection.execute('DROP TRIGGER scientist_admission_history_no_update')
                    store.connection.execute('UPDATE scientist_admission_history SET record_sha256=?', ('f' * 64,))
                changes = store.connection.total_changes
                inventory = scientist_inference_inventory(SimpleNamespace(store=store, session_id='session'))
                self.assertEqual(inventory['intents'][0]['admission_identity'], {
                    'available': False, 'present': False, 'record_sha256': None, 'binding_sha256': None})
                self.assertTrue(inventory['admission_blocked'])
                self.assertEqual(store.connection.total_changes, changes)

    def test_original_database_versions_remain_absent_readonly_and_unadopted(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-scientist-inventory-') as temporary:
            for version in range(18, 23):
                path = Path(temporary) / f'synthetic-{version}.sqlite3'
                with closing(sqlite3.connect(path)) as connection:
                    for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql')):
                        if int(migration.name.split('_', 1)[0]) <= version:
                            connection.executescript(migration.read_text())
                    connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                        ('session', 'runtime', 'synthetic', 'AGENT', 'private-lease', 0,
                         'running', 'synthetic', 'synthetic'))
                    connection.execute('INSERT INTO scientist_turn_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('a' * 32, 'session', json.dumps({'session_id': 'session', 'runtime_id': 'runtime',
                         'owner': 'AGENT', 'lease_id': 'private-lease', 'generation': 0,
                         'authorization_context_sha256': 'd' * 64}),
                         json.dumps({'request_id': 'a' * 32, 'profile_id': 'aos.decider.turn.v1',
                          'deployment_digest': 'b' * 64, 'private': 'secret'}),
                         'c' * 64, '{"private_peer":"secret"}', 1.0, 'pending', None, 'synthetic'))
                    connection.commit()
                    schema = connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    controller = SimpleNamespace(store=SimpleNamespace(connection=connection), session_id='session')
                    identity = _admission_identity(controller.store, 'a' * 32)
                    with self.subTest(version=version):
                        self.assertEqual(identity, {
                            'available': True, 'present': False, 'record_sha256': None, 'binding_sha256': None})
                        if version >= 19:
                            inventory = scientist_inference_inventory(controller)
                            self.assertEqual(inventory['intents'][0]['admission_identity'], identity)
                            self.assertTrue(inventory['admission_blocked'])
                            self.assertFalse(inventory['joint_runtime_admitted'])
                            self.assertFalse(inventory['gpu_release_verified'])
                            self.assertNotIn('secret', str(inventory))
                        self.assertEqual(connection.total_changes, 0)
                        self.assertEqual([tuple(row) for row in connection.execute(
                            'SELECT type,name,sql FROM sqlite_master ORDER BY name')], schema)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_malformed_history_table_is_unavailable_not_safe_absence(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.row_factory = sqlite3.Row
            for migration in sorted((REPO_ROOT / 'database/migrations').glob('*.sql')):
                if int(migration.name.split('_', 1)[0]) <= 22:
                    connection.executescript(migration.read_text())
            connection.execute('CREATE TABLE scientist_admission_history (malformed TEXT)')
            before = connection.total_changes
            self.assertEqual(_admission_identity(SimpleNamespace(connection=connection), 'a' * 32), {
                'available': False, 'present': False, 'record_sha256': None, 'binding_sha256': None})
            self.assertEqual(connection.total_changes, before)
            connection.execute('DROP TABLE scientist_admission_history')
            connection.execute('CREATE TABLE scientist_admission_history (request_id TEXT)')
            connection.execute('INSERT INTO scientist_admission_history VALUES(?)', ('a' * 32,))
            before = connection.total_changes
            self.assertEqual(_admission_identity(SimpleNamespace(connection=connection), 'a' * 32), {
                'available': False, 'present': False, 'record_sha256': None, 'binding_sha256': None})
            self.assertEqual(connection.total_changes, before)
