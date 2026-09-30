import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT, canonical, digest
from aos.dataset import validator
from aos.learning_event_stream import (MIGRATION, _open_store, poll_learning_stream,
                                       watch_learning_stream)
from aos.storage import TrajectoryStore


DECIDER = {'deployment_id': 'decider-synthetic', 'kind': 'decider_native_worker',
           'real_model': True, 'pins': {'revision': 'synthetic'}}
BONSAI = {'deployment_id': 'bonsai-synthetic', 'kind': 'bonsai_native_supervisor',
          'real_model': True, 'pins': {'revision': 'synthetic'}}


class LearningEventStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'source.sqlite'
        self.outbox = self.root / 'outbox'
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        with self.store.connection:
            self.store.insert('tasks', task_id='task-synthetic', original_goal='private-secret-never-export',
                              normalized_goal='synthetic', success_criteria_json='[]',
                              workspace_scope_json='[]', created_at='2026-01-01T00:00:00Z')
            self.store.insert('runs', run_id='run-synthetic', task_id='task-synthetic', status='running',
                              outcome='unknown', policy_version='hello-policy-v1', environment_json='{}',
                              deployment_snapshot_json=canonical({**DECIDER, 'supervisor': BONSAI}),
                              started_at='2026-01-01T00:00:00Z')
            self.store.insert('steps', step_id='step-synthetic', run_id='run-synthetic', ordinal=0,
                              state='DECIDE', started_at='2026-01-01T00:00:00Z')

    def add_system1(self):
        with self.store.connection:
            self.store.insert('model_calls', call_id='call-decider', run_id='run-synthetic',
                              step_id='step-synthetic', deployment_id=DECIDER['deployment_id'], role='system1',
                              request_json='{"password":"private-secret-never-export"}',
                              response_json='{"selected_option":"write_file"}', status='ok',
                              created_at='2026-01-01T00:00:01Z')
            self.store.insert('state_snapshots', snapshot_id='snapshot-synthetic', run_id='run-synthetic',
                              step_id='step-synthetic', state_version=1,
                              state_json='{"private":"private-secret-never-export"}',
                              content_sha256='0' * 64, created_at='2026-01-01T00:00:01Z')
            self.store.insert('decisions', decision_id='decision-synthetic', run_id='run-synthetic',
                              step_id='step-synthetic', snapshot_id='snapshot-synthetic', call_id='call-decider',
                              question='private-secret-never-export', options_json='[{"id":"write_file"},{"id":"ask_human"}]',
                              probabilities_json='{"write_file":1,"ask_human":0}', selected_option='write_file',
                              confidence=1, policy_result='allow', created_at='2026-01-01T00:00:01Z')

    def add_system2(self):
        with self.store.connection:
            self.store.insert('model_calls', call_id='call-bonsai', run_id='run-synthetic',
                              step_id='step-synthetic', deployment_id=BONSAI['deployment_id'], role='system2',
                              request_json='{"password":"private-secret-never-export"}',
                              response_json='{"diagnosis":"synthetic"}', status='ok',
                              created_at='2026-01-01T00:00:02Z')

    def add_verification(self):
        content = canonical(HELLO_CONTENT)
        readback = canonical({'content': HELLO_CONTENT})
        with self.store.connection:
            self.store.insert('actions', action_id='action-write', run_id='run-synthetic',
                              step_id='step-synthetic', decision_id='decision-synthetic', idempotency_key='write-key',
                              tool='filesystem.write', arguments_json='{"secret":"private-secret-never-export"}',
                              status='ok', actual_option='write_file', created_at='2026-01-01T00:00:03Z')
            self.store.insert('actions', action_id='action-read', run_id='run-synthetic',
                              step_id='step-synthetic', decision_id='decision-synthetic', idempotency_key='read-key',
                              tool='filesystem.read', arguments_json='{}', result_json=readback,
                              status='ok', actual_option='write_file', created_at='2026-01-01T00:00:04Z')
            self.store.insert('observations', observation_id='observation-read', run_id='run-synthetic',
                              step_id='step-synthetic', action_id='action-read', kind='filesystem.read',
                              payload_json=readback, created_at='2026-01-01T00:00:04Z')
            self.store.insert('verifications', verification_id='verification-synthetic', run_id='run-synthetic',
                              step_id='step-synthetic', action_id='action-write', criterion='exact',
                              method='independent_read_equals', result='passed', expected_json=content,
                              actual_json=content, evidence_refs_json='["observation-read"]',
                              verifier='aos-exact-bytes-v1', created_at='2026-01-01T00:00:05Z')

    def add_vision_downstream(self):
        scene = json.loads((REPO_ROOT / 'examples/vision_scene.json').read_text())['scene']
        capture = {'capture_id': scene['capture_id'], 'width': 640, 'height': 360,
                   'sha256': 'a' * 64, 'state_version': scene['state_version'],
                   'artifact_id': 'artifact-' + 'b' * 32}
        request = {'purpose': 'synthetic_canvas_vision',
                   'capture': {key: value for key, value in capture.items()
                               if key not in {'state_version', 'artifact_id'}},
                   'state_version': capture['state_version'], 'artifact_id': capture['artifact_id']}
        outcome = {'selected': 'SAVE', 'clicks': 1}
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET request_json=?,response_json=? '
                                          "WHERE call_id='call-bonsai'", (json.dumps(request), json.dumps(scene)))
            self.store.insert('artifacts', artifact_id=capture['artifact_id'], run_id='run-synthetic',
                              step_id='step-synthetic', relative_path='private-capture.png', media_type='image/png',
                              sha256=capture['sha256'], size_bytes=1, redaction_status='raw',
                              created_at='2026-01-01T00:00:02Z')
            self.store.insert('observations', observation_id='capture-observation-synthetic',
                              run_id='run-synthetic', step_id='step-synthetic', kind='vision.capture',
                              payload_json=json.dumps(capture), created_at='2026-01-01T00:00:02Z')
            self.store.insert('observations', observation_id='scene-observation-synthetic',
                              run_id='run-synthetic', step_id='step-synthetic', kind='vision.scene',
                              payload_json=json.dumps(scene), created_at='2026-01-01T00:00:02Z')
            self.store.connection.execute('UPDATE state_snapshots SET state_json=? '
                "WHERE snapshot_id='snapshot-synthetic'", (json.dumps({
                    'task_kind': 'vision_canvas', 'capture_id': scene['capture_id'],
                    'scene_sha256': digest(scene)}),))
            self.store.connection.execute("UPDATE actions SET tool='vision.click' WHERE action_id='action-write'")
            self.store.connection.execute('UPDATE actions SET tool=?,result_json=? '
                "WHERE action_id='action-read'", ('vision.verify', json.dumps(outcome)))
            self.store.connection.execute('UPDATE observations SET kind=?,payload_json=? '
                "WHERE observation_id='observation-read'", ('vision.outcome', json.dumps(outcome)))
            self.store.connection.execute('UPDATE verifications SET method=?,verifier=?,expected_json=?,actual_json=?,result=? '
                "WHERE verification_id='verification-synthetic'",
                ('independent_canvas_equals', 'aos-visual-canvas-v1', json.dumps(outcome),
                 json.dumps(outcome), 'failed'))

    def poll(self, roles=('system1', 'system2')):
        return poll_learning_stream(self.database, 'run-synthetic', roles, self.outbox)

    def entries(self):
        with closing(sqlite3.connect(self.outbox / 'learning-stream.sqlite')) as connection:
            return [json.loads(row[0]) for row in connection.execute('SELECT entry_json FROM entries ORDER BY entry_id')]

    def test_incremental_role_separated_restart_dedup_and_no_raw_content(self):
        empty = self.poll()
        self.assertEqual(empty['total_entries'], 0)
        self.add_system1()
        first = self.poll()
        self.assertEqual(first['new_entries'], 1)
        self.assertEqual(first['entries_by_role'], {'system1': 1, 'system2': 0})
        self.assertEqual(self.poll()['new_entries'], 0)
        self.add_system2()
        second = self.poll()
        self.assertEqual(second['entries_by_role'], {'system1': 1, 'system2': 1})
        self.add_verification()
        third = self.poll()
        self.assertEqual(third['new_entries'], 1)
        self.assertEqual(third['entries_by_role'], {'system1': 2, 'system2': 1})
        entries = self.entries()
        self.assertEqual(sorted((entry['role'], entry['kind']) for entry in entries),
                         [('system1', 'call_observed'), ('system1', 'verification_linked'),
                          ('system2', 'call_observed')])
        self.assertTrue(all(not entry['training_ready'] and not entry['collection_authorized'] for entry in entries))
        self.assertTrue(all(validator('learning_event_stream_entry').is_valid(entry) for entry in entries))
        self.assertNotIn('private-secret-never-export', json.dumps(entries))
        self.assertNotIn(HELLO_CONTENT, json.dumps(entries))
        self.assertEqual(self.poll()['new_entries'], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM verifications').fetchone()[0], 1)

    def test_no_bonsai_call_means_no_system2_record(self):
        self.add_system1()
        report = self.poll()
        self.assertEqual(report['entries_by_role']['system2'], 0)
        self.assertEqual([entry['role'] for entry in self.entries()], ['system1'])

    def test_bonsai_downstream_link_is_incremental_not_gold_and_fails_closed_on_scene_drift(self):
        self.add_system1()
        self.add_system2()
        self.add_verification()
        self.add_vision_downstream()
        first = self.poll()
        self.assertEqual(first['entries_by_role'], {'system1': 1, 'system2': 1})
        with self.store.connection:
            self.store.connection.execute("UPDATE verifications SET result='passed' "
                                          "WHERE verification_id='verification-synthetic'")
        second = self.poll()
        self.assertEqual(second['new_entries'], 2)
        self.assertEqual(second['entries_by_role'], {'system1': 2, 'system2': 2})
        downstream = next(entry for entry in self.entries()
                          if entry['kind'] == 'downstream_verification_linked')
        self.assertEqual(downstream['role'], 'system2')
        self.assertEqual(downstream['derivation_version'], 'learning-stream-v2')
        self.assertEqual(downstream['source']['scene_observation_id'], 'scene-observation-synthetic')
        self.assertEqual(downstream['source']['verification_id'], 'verification-synthetic')
        self.assertFalse(downstream['training_ready'])
        self.assertFalse(downstream['collection_authorized'])
        self.assertTrue(validator('learning_event_stream_entry').is_valid(downstream))
        self.assertNotIn('private-capture.png', json.dumps(downstream))
        self.assertEqual(self.poll()['new_entries'], 0)
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json='{}' "
                                          "WHERE observation_id='scene-observation-synthetic'")
        with self.assertRaisesRegex(ValueError, 'learning_stream_source_regressed_or_changed'):
            self.poll()

    def test_verification_is_appended_only_after_independent_pass(self):
        self.add_system1()
        self.poll()
        self.add_verification()
        with self.store.connection:
            self.store.connection.execute("UPDATE verifications SET result='failed' WHERE verification_id='verification-synthetic'")
        self.assertEqual(self.poll()['new_entries'], 0)
        with self.store.connection:
            self.store.connection.execute("UPDATE verifications SET result='passed' WHERE verification_id='verification-synthetic'")
        self.assertEqual(self.poll()['new_entries'], 1)
        self.assertEqual([entry['kind'] for entry in self.entries() if entry['kind'] == 'verification_linked'],
                         ['verification_linked'])

    def test_persisted_entries_cannot_be_updated_deleted_or_replaced(self):
        self.add_system1()
        self.poll()
        with closing(sqlite3.connect(self.outbox / 'learning-stream.sqlite')) as connection:
            entry_id = connection.execute('SELECT entry_id FROM entries').fetchone()[0]
            for command, parameters in (
                    ('UPDATE entries SET content_sha256=? WHERE entry_id=?', ('0' * 64, entry_id)),
                    ('DELETE FROM entries WHERE entry_id=?', (entry_id,)),
                    ('INSERT OR REPLACE INTO entries SELECT * FROM entries WHERE entry_id=?', (entry_id,))):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(command, parameters)
        self.assertEqual(self.poll()['new_entries'], 0)

    def test_source_mutation_fails_without_appending_or_replacing(self):
        self.add_system1()
        self.poll()
        first = self.entries()
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET response_json=? WHERE call_id=?',
                                          ('{"selected_option":"ask_human"}', 'call-decider'))
        with self.assertRaisesRegex(ValueError, 'source_regressed_or_changed'):
            self.poll()
        self.assertEqual(self.entries(), first)

    def test_changed_database_identity_and_non_synthetic_policy_fail_closed(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='unknown-policy' WHERE run_id='run-synthetic'")
        with self.assertRaisesRegex(ValueError, 'requires_synthetic_run'):
            self.poll()
        self.assertFalse(self.outbox.exists())
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='hello-policy-v1' WHERE run_id='run-synthetic'")
        self.poll()
        replacement = self.root / 'replacement.sqlite'
        with closing(sqlite3.connect(replacement)) as destination:
            self.store.connection.backup(destination)
        replacement.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'learning_stream_source_changed'):
            poll_learning_stream(replacement, 'run-synthetic', ('system1',), self.outbox)

    def test_run_policy_drift_after_empty_poll_fails_closed(self):
        self.poll()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='browser-form-policy-v1' WHERE run_id='run-synthetic'")
        with self.assertRaisesRegex(ValueError, 'learning_stream_run_identity_changed'):
            self.poll()
        self.assertEqual(self.entries(), [])

    def test_corrupt_or_symlinked_outbox_fails_closed(self):
        self.poll()
        path = self.outbox / 'learning-stream.sqlite'
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private_learning_file_required'):
            self.poll()
        path.chmod(0o600)
        path.unlink()
        path.symlink_to(self.database)
        with self.assertRaises(ValueError):
            self.poll()

    def test_cli_watches_synthetic_writer_during_run_and_hides_private_content(self):
        command = [sys.executable, '-m', 'aos.learning_event_stream', '--database', str(self.database),
                   '--run-id', 'run-synthetic', '--roles', 'both', '--outbox-dir', str(self.outbox),
                   '--watch-seconds', '1.1', '--interval-seconds', '0.25']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.35)
            self.add_system1()
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertEqual(process.returncode, 0, stderr)
        report = json.loads(stdout)
        self.assertTrue(validator('learning_event_stream_report').is_valid(report))
        self.assertEqual(report['mode'], 'opt_in_synthetic_metadata_watch')
        self.assertGreaterEqual(report['polls'], 2)
        self.assertEqual(report['entries_by_role'], {'system1': 1, 'system2': 0})
        self.assertEqual(report['new_entries'], 1)
        self.assertFalse(report['training_ready'])
        self.assertNotIn('private-secret-never-export', stdout)

    def test_fixture_schema_and_migration_are_canonical(self):
        fixture = json.loads((REPO_ROOT / 'examples/learning_event_stream.json').read_text())
        validator('learning_event_stream_entry').validate(fixture['entry'])
        validator('learning_event_stream_entry').validate(fixture['downstream_entry'])
        validator('learning_event_stream_report').validate(fixture['report'])
        downstream = fixture['downstream_entry']
        for invalid in (
                {**downstream, 'role': 'system1'},
                {**downstream, 'derivation_version': 'learning-stream-v1'},
                {**downstream, 'source': {key: value for key, value in downstream['source'].items()
                                          if key != 'scene_observation_id'}},
                {**downstream, 'training_ready': True}):
            with self.subTest(invalid=invalid):
                self.assertFalse(validator('learning_event_stream_entry').is_valid(invalid))
        self.assertFalse(validator('learning_event_stream_entry').is_valid(
            {**fixture['entry'], 'request_json': {'password': 'private'}}))
        self.poll()
        with closing(sqlite3.connect(self.outbox / 'learning-stream.sqlite')) as connection:
            self.assertEqual(connection.execute('SELECT version,name FROM learning_migrations').fetchall(),
                             [(1, 'incremental_metadata'), (2, 'downstream_metadata')])

    def test_existing_v1_outbox_is_upgraded_without_losing_entries(self):
        self.outbox.mkdir(mode=0o700)
        path = self.outbox / 'learning-stream.sqlite'
        path.touch(mode=0o600)
        fixture = json.loads((REPO_ROOT / 'examples/learning_event_stream.json').read_text())['entry']
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(MIGRATION.read_text())
            connection.execute('INSERT INTO entries VALUES(?,?,?,?,?,?)',
                               (fixture['entry_id'], fixture['source']['run_id'], fixture['role'],
                                fixture['kind'], canonical(fixture), digest(fixture)))
            connection.commit()
        connection, directory = _open_store(self.outbox)
        try:
            self.assertEqual(connection.execute('SELECT entry_json FROM entries').fetchone()[0],
                             canonical(fixture))
            self.assertEqual([tuple(row) for row in connection.execute(
                'SELECT version,name FROM learning_migrations ORDER BY version')],
                [(1, 'incremental_metadata'), (2, 'downstream_metadata')])
        finally:
            connection.close()
            os.close(directory)

    def test_corrupt_v1_outbox_is_not_migrated(self):
        self.outbox.mkdir(mode=0o700)
        path = self.outbox / 'learning-stream.sqlite'
        path.touch(mode=0o600)
        fixture = json.loads((REPO_ROOT / 'examples/learning_event_stream.json').read_text())['entry']
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(MIGRATION.read_text())
            connection.execute('INSERT INTO entries VALUES(?,?,?,?,?,?)',
                               (fixture['entry_id'], fixture['source']['run_id'], fixture['role'],
                                fixture['kind'], canonical(fixture), '0' * 64))
            connection.commit()
        with self.assertRaisesRegex(ValueError, 'learning_stream_schema_or_integrity_failure'):
            _open_store(self.outbox)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute('SELECT version FROM learning_migrations').fetchall(), [(1,)])
            self.assertEqual(connection.execute('SELECT count(*) FROM entries').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
