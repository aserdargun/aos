import copy
import json
import sqlite3
import unittest

from aos.contracts import REPO_ROOT, Option, Phase, Prediction, State, canonical, digest
from aos.dataset import validator
from aos.decision import decision_request
from aos.owned_episode_candidates import (
    derive_system1_candidates, derive_system2_candidate,
)


class OwnedEpisodeCandidateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE runs(run_id TEXT, task_id TEXT, deployment_snapshot_json TEXT);
            CREATE TABLE deployments(deployment_id TEXT, status TEXT, config_json TEXT,
                                    config_sha256 TEXT);
            CREATE TABLE model_calls(call_id TEXT, run_id TEXT, step_id TEXT,
                deployment_id TEXT, role TEXT, status TEXT, request_json TEXT,
                response_json TEXT, created_at TEXT);
            CREATE TABLE decisions(decision_id TEXT, run_id TEXT, step_id TEXT,
                snapshot_id TEXT, call_id TEXT, question TEXT, options_json TEXT,
                probabilities_json TEXT, selected_option TEXT, confidence REAL,
                policy_result TEXT);
            CREATE TABLE state_snapshots(snapshot_id TEXT, run_id TEXT, step_id TEXT,
                state_version INTEGER, state_json TEXT, content_sha256 TEXT);
        ''')
        self.run_id = 'run-synthetic-test'
        self.episode_id = 'episode-' + '1' * 32
        self.task_id = 'task-synthetic-test'
        self.step_id = 'step-synthetic-test'
        self.snapshot_id = 'snapshot-synthetic-test'
        self.call_id = 'call-synthetic-test'
        self.decision_id = 'decision-synthetic-test'
        self.pins = {'revision': 'synthetic-test-only'}
        self.identity = {
            'deployment_id': 'decider-' + digest(self.pins),
            'kind': 'decider_native_worker', 'real_model': True,
            'pins': self.pins,
        }
        self.connection.execute(
            'INSERT INTO runs VALUES(?,?,?)',
            (self.run_id, self.task_id, canonical(self.identity)))
        self.connection.execute(
            'INSERT INTO deployments VALUES(?,?,?,?)',
            (self.identity['deployment_id'], 'EXPERIMENTAL', canonical(self.pins),
             digest(self.pins)))
        self.state = State(
            task_id=self.task_id, run_id=self.run_id, step_id=self.step_id,
            runtime_id='runtime-synthetic-test', deployment_id=self.identity['deployment_id'],
            owner_lease_id='lease-synthetic-test', phase=Phase.DECIDE,
            normalized_goal='synthetic goal', observation='synthetic observation')
        self.options = [Option(id='write_file', label='Create authorized file'),
                        Option(id='ask_human', label='Ask the user')]
        self.request = decision_request(self.state, self.options)
        self.prediction = Prediction(
            selected_option='write_file',
            probabilities={'write_file': 0.9, 'ask_human': 0.1})
        self.connection.execute(
            'INSERT INTO state_snapshots VALUES(?,?,?,?,?,?)',
            (self.snapshot_id, self.run_id, self.step_id, self.state.state_version,
             self.state.model_dump_json(), digest(self.state.model_dump(mode='json'))))
        self.connection.execute(
            'INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?)',
            (self.call_id, self.run_id, self.step_id, self.identity['deployment_id'],
             'system1', 'ok', canonical(self.request), self.prediction.model_dump_json(),
             '2026-01-01T00:00:00Z'))
        self.connection.execute(
            'INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.decision_id, self.run_id, self.step_id, self.snapshot_id, self.call_id,
             self.request['question'], canonical(self.request['options']),
             canonical(self.prediction.probabilities), self.prediction.selected_option,
             0.9, 'allow'))

    def tearDown(self):
        self.connection.close()

    def system1(self):
        return derive_system1_candidates(
            self.connection, self.run_id, episode_id=self.episode_id,
            consent_sha256='a' * 64, planning_bundle_sha256='b' * 64,
            source_group_sha256='c' * 64)

    def test_system1_candidate_binds_exact_decision_time_request_and_prediction(self):
        candidate, = self.system1()
        validator('owned_episode_candidate').validate(candidate)
        self.assertEqual(candidate['role'], 'system1')
        self.assertEqual(candidate['input']['request'], self.request)
        self.assertEqual(candidate['input']['request_sha256'], digest(self.request))
        self.assertEqual(candidate['prediction'], self.prediction.model_dump(mode='json'))
        self.assertEqual(candidate['source']['planning_bundle_sha256'], 'b' * 64)
        self.assertEqual(candidate['candidate_id'], digest({
            key: value for key, value in candidate.items() if key != 'candidate_id'}))
        self.assertFalse(candidate['training_ready'])
        self.assertFalse(candidate['reviewed'])
        self.assertNotIn('outcome', candidate)
        self.assertNotIn('target', candidate)

    def test_system1_candidate_identity_is_stable_across_repeated_derivation(self):
        first = self.system1()
        second = self.system1()
        self.assertEqual(first, second)
        self.assertNotEqual(first[0]['candidate_id'], digest({'other': 'episode'}))

    def test_system1_rejects_changed_request_output_snapshot_or_deployment(self):
        mutations = (
            ("UPDATE model_calls SET request_json='{}' WHERE call_id=?",
             ('request_json', canonical(self.request))),
            ("UPDATE model_calls SET response_json='{}' WHERE call_id=?",
             ('response_json', self.prediction.model_dump_json())),
            ("UPDATE model_calls SET deployment_id='foreign' WHERE call_id=?",
             ('deployment_id', self.identity['deployment_id'])),
        )
        for sql, (column, original) in mutations:
            with self.subTest(sql=sql):
                self.connection.execute(sql, (self.call_id,))
                with self.assertRaises(ValueError):
                    self.system1()
                self.connection.execute(
                    f'UPDATE model_calls SET {column}=? WHERE call_id=?',
                    (original, self.call_id))

    def test_system1_rejects_snapshot_hash_and_join_mutation(self):
        self.connection.execute(
            'UPDATE state_snapshots SET content_sha256=? WHERE snapshot_id=?',
            ('0' * 64, self.snapshot_id))
        with self.assertRaises(ValueError):
            self.system1()

    def test_system2_rejects_fixture_or_modified_native_planning_bundle(self):
        bundle = json.loads(
            (REPO_ROOT / 'examples/owned_skill_planning_bundle.json').read_text())['bundle']
        arguments = {'episode_id': self.episode_id,
                     'consent_sha256': 'a' * 64,
                     'source_group_sha256': 'c' * 64}
        self.assertIs(bundle['real_model'], False)
        with self.assertRaises(ValueError):
            derive_system2_candidate(bundle, **arguments)

        changed = copy.deepcopy(bundle)
        changed['model_request'] = {'tampered': True}
        with self.assertRaises(ValueError):
            derive_system2_candidate(changed, **arguments)

    def test_schema_fixture_is_synthetic_and_false_claims_are_literal(self):
        fixture = json.loads((REPO_ROOT / 'examples/owned_episode_candidate.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('owned_episode_candidate').validate(fixture['candidate'])
        candidate = fixture['candidate']
        for changed in (
            {**candidate, 'training_ready': 0},
            {**candidate, 'execution_authorized': True},
            {**candidate, 'role': 'system1'},
            {**candidate, 'extra': 'not allowed'},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(validator('owned_episode_candidate').is_valid(changed))


if __name__ == '__main__':
    unittest.main()
