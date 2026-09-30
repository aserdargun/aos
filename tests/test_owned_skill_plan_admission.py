import json
import sqlite3
import unittest

from aos.contracts import canonical, digest
from aos.owned_skill_plan_admission import planning_admission, verify_planning_admission
from test_owned_skill_plan_bound import synthetic_native_contract_bundle


class OwnedSkillPlanAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.bundle = synthetic_native_contract_bundle()
        self.execution = {key: 'a' * 64 for key in (
            'candidate_execution_sha256', 'preview_sha256', 'reuse_admission_sha256',
            'candidate_sha256', 'invocation_sha256')}
        self.execution.update(run_id='synthetic-run', planning_bundle_sha256=digest(self.bundle))
        self.payload = planning_admission(self.execution, self.bundle)
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE observations(run_id, action_id, step_id, kind, payload_json, created_at);
            CREATE TABLE state_snapshots(run_id, state_version, step_id, state_json, created_at);
            CREATE TABLE actions(run_id, created_at);
        ''')
        self.connection.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (
            'synthetic-run', None, 'synthetic-step', 'skill.owned_planning_admission',
            canonical(self.payload), '2026-09-27T00:00:01Z'))
        self.connection.executemany('INSERT INTO state_snapshots VALUES(?,?,?,?,?)', [
            ('synthetic-run', 0, 'synthetic-step', canonical({'phase': 'CREATED'}), '2026-09-27T00:00:00Z'),
            ('synthetic-run', 1, 'synthetic-step', canonical({'phase': 'OBSERVE'}), '2026-09-27T00:00:02Z')])
        self.connection.execute('INSERT INTO actions VALUES(?,?)', ('synthetic-run', '2026-09-27T00:00:03Z'))

    def verify(self):
        return verify_planning_admission(self.connection, self.execution, self.bundle)

    def test_synthetic_preaction_record_has_only_content_free_bound_provenance(self):
        self.assertTrue(self.verify())
        self.assertNotIn('goal', self.payload)
        self.assertNotIn('model_response', self.payload)
        self.assertEqual(self.payload['user_confirmed_plan_sha256'], digest(self.bundle))
        for key in ('execution_authorized', 'activation_authorized', 'training_ready', 'independent_held_out'):
            self.assertFalse(self.payload[key])
        with self.assertRaises(ValueError):
            planning_admission(self.execution | {'planning_bundle_sha256': '0' * 64}, self.bundle)

    def test_missing_duplicate_or_action_bound_observation_is_rejected(self):
        for statement in (
            'DELETE FROM observations',
            'INSERT INTO observations SELECT * FROM observations',
            "UPDATE observations SET action_id='synthetic-action'",
            "UPDATE observations SET step_id='other-step'",
            "UPDATE observations SET run_id='other-run'",
            'DELETE FROM actions',
            'DELETE FROM state_snapshots WHERE state_version=0',
            'DELETE FROM state_snapshots WHERE state_version=1',
        ):
            with self.subTest(statement=statement):
                self.connection.execute('SAVEPOINT mutation')
                self.connection.execute(statement)
                self.assertFalse(self.verify())
                self.connection.execute('ROLLBACK TO mutation')
                self.connection.execute('RELEASE mutation')

    def test_authority_and_confirmation_mutations_are_rejected(self):
        for key in ('planning_bundle_sha256', 'preview_sha256', 'candidate_execution_sha256',
                    'lease_id', 'runtime_id', 'generation', 'user_confirmed_plan_sha256'):
            with self.subTest(key=key):
                changed = self.payload | {key: 99 if key == 'generation' else 'changed'}
                self.connection.execute('UPDATE observations SET payload_json=?', (canonical(changed),))
                self.assertFalse(self.verify())
        self.connection.execute('UPDATE observations SET payload_json=?', (json.dumps(self.payload, indent=2),))
        self.assertFalse(self.verify())

    def test_postaction_or_preinitial_admission_cannot_verify(self):
        for timestamp in ('2026-09-26T23:59:59Z', '2026-09-27T00:00:04Z'):
            self.connection.execute('UPDATE observations SET created_at=?', (timestamp,))
            self.assertFalse(self.verify())
        self.connection.execute("UPDATE observations SET created_at='2026-09-27T00:00:01Z'")
        self.connection.execute("UPDATE state_snapshots SET state_json=? WHERE state_version=0",
                                (canonical({'phase': 'EXECUTE'}),))
        self.assertFalse(self.verify())
