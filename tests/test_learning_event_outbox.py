import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import REPO_ROOT, digest
from aos.dataset import validator
from aos.dataset_audit import audit_snapshot
from aos.learning_event_outbox import derive_learning_checkpoint, load_learning_checkpoint
from aos.storage import TrajectoryStore


DECIDER = {'deployment_id': 'decider-test', 'kind': 'decider_native_worker',
           'real_model': True, 'pins': {'revision': 'synthetic'}}
BONSAI = {'deployment_id': 'bonsai-test', 'kind': 'bonsai_native_supervisor',
          'real_model': True, 'pins': {'revision': 'synthetic'}}


class LearningEventOutboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'trajectory.sqlite'
        self.outbox = self.root / 'learning-outbox'
        self.store = TrajectoryStore(self.database)
        self.addCleanup(self.store.close)
        with self.store.connection:
            self.store.insert('tasks', task_id='task-test', original_goal='private-token-do-not-export',
                              normalized_goal='synthetic', success_criteria_json='[]',
                              workspace_scope_json='[]', created_at='2026-01-01T00:00:00Z')
            self.store.insert('runs', run_id='run-test', task_id='task-test', status='running',
                              outcome='unknown', policy_version='hello-policy-v1',
                              environment_json='{"secret":"private-token-do-not-export"}',
                              deployment_snapshot_json=json.dumps({**DECIDER, 'supervisor': BONSAI}),
                              started_at='2026-01-01T00:00:00Z')
            self.store.insert('steps', step_id='step-test', run_id='run-test', ordinal=0,
                              state='DECIDE', started_at='2026-01-01T00:00:00Z')
            self.store.insert('model_calls', call_id='call-decider', run_id='run-test',
                              step_id='step-test', deployment_id=DECIDER['deployment_id'],
                              role='system1', request_json='{"password":"private-token-do-not-export"}',
                              response_json='{"secret":"private-token-do-not-export"}',
                              status='ok', created_at='2026-01-01T00:00:00Z')
            self.store.insert('state_snapshots', snapshot_id='state-test', run_id='run-test',
                              step_id='step-test', state_version=1, state_json='{}',
                              content_sha256='0' * 64, created_at='2026-01-01T00:00:00Z')
            self.store.insert('decisions', decision_id='decision-test', run_id='run-test',
                              step_id='step-test', snapshot_id='state-test', call_id='call-decider',
                              question='private-token-do-not-export',
                              options_json='[{"id":"a"},{"id":"b"}]', probabilities_json='{"a":1,"b":0}',
                              selected_option='a', confidence=1, policy_result='allow',
                              created_at='2026-01-01T00:00:00Z')
            self.store.insert('model_calls', call_id='call-bonsai', run_id='run-test',
                              step_id='step-test', deployment_id=BONSAI['deployment_id'],
                              role='system2', request_json='{"password":"private-token-do-not-export"}',
                              response_json='{"secret":"private-token-do-not-export"}',
                              status='ok', created_at='2026-01-01T00:00:00Z')

    def derive(self, role='system1'):
        return derive_learning_checkpoint(self.database, 'run-test', role, self.outbox)

    def filename(self, role='system1'):
        return self.outbox / (digest({'derivation_version': 'learning-evidence-v2',
                                      'role': role, 'run_id': 'run-test'}) + '.json')

    def test_fixture_schema_and_authority_are_explicitly_synthetic(self):
        fixture = json.loads((REPO_ROOT / 'examples/learning_event_outbox.json').read_text())
        self.assertTrue(fixture['synthetic'])
        record = fixture['checkpoint']
        validator('learning_event_outbox').validate(record)
        self.assertEqual(record['checkpoint_sha256'],
                         digest({name: value for name, value in record.items()
                                 if name != 'checkpoint_sha256'}))
        self.assertEqual(record['events_sha256'], digest(record['events']))
        self.assertFalse(validator('learning_event_outbox').is_valid(
            {**record, 'collection_authorized': True}))
        self.assertFalse(validator('learning_event_outbox').is_valid(
            {**record, 'raw_request': 'private'}))

    def test_restart_dedup_role_separation_and_no_payload_export(self):
        system1 = self.derive()
        self.assertEqual(system1['event_count'], 1)
        self.assertEqual(system1['events'][0]['role'], 'system1')
        self.assertFalse(system1['collection_authorized'])
        self.assertFalse(system1['training_ready'])
        self.assertEqual(load_learning_checkpoint(self.database, 'run-test', 'system1', self.outbox), system1)
        self.assertEqual(self.derive(), system1)
        system2 = self.derive('system2')
        self.assertEqual(system2['event_count'], 1)
        self.assertEqual(system2['events'][0]['role'], 'system2')
        self.assertNotEqual(system1['key_sha256'], system2['key_sha256'])
        self.assertEqual(len(list(self.outbox.glob('*.json'))), 2)
        self.assertEqual(self.outbox.stat().st_mode & 0o777, 0o700)
        for filename in (self.filename(), self.filename('system2')):
            self.assertEqual(filename.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('private-token-do-not-export', filename.read_text())
            self.assertNotIn('password', filename.read_text())

    def test_changed_source_conflicts_without_replacing_checkpoint(self):
        first = self.derive()
        original = self.filename().read_bytes()
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded' WHERE run_id='run-test'")
        with self.assertRaisesRegex(ValueError, 'learning_checkpoint_source_changed'):
            load_learning_checkpoint(self.database, 'run-test', 'system1', self.outbox)
        with self.assertRaisesRegex(ValueError, 'learning_checkpoint_key_conflict'):
            self.derive()
        self.assertEqual(self.filename().read_bytes(), original)
        with audit_snapshot(self.database) as (_, identity):
            self.assertNotEqual(first['source_snapshot_sha256'], identity['sha256'])

    def test_absent_bonsai_call_never_creates_fake_event(self):
        with self.store.connection:
            self.store.connection.execute("DELETE FROM model_calls WHERE call_id='call-bonsai'")
        checkpoint = self.derive('system2')
        self.assertEqual(checkpoint['event_count'], 0)
        self.assertEqual(checkpoint['events'], [])
        with audit_snapshot(self.database) as (_, identity):
            self.assertEqual(checkpoint['source_snapshot_sha256'], identity['sha256'])

    def test_corruption_symlink_and_unsafe_permissions_fail_closed(self):
        self.derive()
        filename = self.filename()
        content = filename.read_bytes()
        filename.write_bytes(content[:-1])
        with self.assertRaises(ValueError):
            self.derive()
        filename.write_bytes(content)
        filename.chmod(0o644)
        with self.assertRaises(ValueError):
            load_learning_checkpoint(self.database, 'run-test', 'system1', self.outbox)
        filename.chmod(0o600)
        filename.unlink()
        filename.symlink_to(self.database)
        with self.assertRaises((ValueError, OSError)):
            self.derive()

    def test_non_synthetic_source_and_invalid_selection_never_create_outbox(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET policy_version='other-policy' WHERE run_id='run-test'")
        with self.assertRaisesRegex(ValueError, 'requires_synthetic_policy'):
            self.derive()
        self.assertFalse(self.outbox.exists())
        with self.assertRaises(ValueError):
            derive_learning_checkpoint(self.database, '../bad', 'system1', self.outbox)
        with self.assertRaises(ValueError):
            derive_learning_checkpoint(self.database, 'run-test', 'system3', self.outbox)

    def test_link_failure_cleans_temporary_file_and_retries(self):
        with patch('aos.learning_event_outbox.os.link', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                self.derive()
        self.assertEqual(list(self.outbox.iterdir()), [])
        self.assertEqual(self.derive()['event_count'], 1)

    def test_restart_repairs_only_matching_published_temporary_link(self):
        first = self.derive()
        filename = self.filename()
        temporary = self.outbox / ('.checkpoint-' + 'a' * 32)
        os.link(filename, temporary)
        self.assertEqual(filename.stat().st_nlink, 2)
        with self.assertRaises(ValueError):
            load_learning_checkpoint(self.database, 'run-test', 'system1', self.outbox)
        self.assertEqual(self.derive(), first)
        self.assertFalse(temporary.exists())
        self.assertEqual(filename.stat().st_nlink, 1)
        self.assertEqual(load_learning_checkpoint(self.database, 'run-test', 'system1', self.outbox), first)

    def test_forged_or_extra_links_do_not_trigger_recovery(self):
        first = self.derive()
        filename = self.filename()
        outside = self.root / 'forged-link.json'
        os.link(filename, outside)
        with self.assertRaisesRegex(ValueError, 'learning_checkpoint_recovery_ambiguous'):
            self.derive()
        self.assertEqual(filename.stat().st_nlink, 2)
        temporary = self.outbox / ('.checkpoint-' + 'b' * 32)
        os.link(filename, temporary)
        with self.assertRaisesRegex(ValueError, 'invalid_private_learning_checkpoint'):
            self.derive()
        self.assertEqual(filename.stat().st_nlink, 3)
        self.assertEqual(json.loads(filename.read_text()), first)

    def test_source_change_does_not_repair_published_temporary_link(self):
        self.derive()
        filename = self.filename()
        temporary = self.outbox / ('.checkpoint-' + 'c' * 32)
        os.link(filename, temporary)
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded' WHERE run_id='run-test'")
        with self.assertRaisesRegex(ValueError, 'learning_checkpoint_source_changed'):
            self.derive()
        self.assertTrue(temporary.exists())
        self.assertEqual(filename.stat().st_nlink, 2)

    def test_explicit_cli_inspect_and_fixed_error(self):
        command = [sys.executable, '-m', 'aos.learning_event_outbox',
                   '--database', str(self.database), '--run-id', 'run-test',
                   '--role', 'system1', '--outbox-dir', str(self.outbox)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['event_count'], 1)
        inspect = subprocess.run([*command, '--inspect'], capture_output=True, text=True, timeout=10)
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        self.assertEqual(result.stdout, inspect.stdout)
        self.filename().write_text('broken')
        failed = subprocess.run([*command, '--inspect'], capture_output=True, text=True, timeout=10)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, '')
        self.assertNotIn(str(self.database), failed.stderr)
        self.assertNotIn('private-token-do-not-export', failed.stderr)


if __name__ == '__main__':
    unittest.main()
