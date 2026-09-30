import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aos.browser_operator import BrowserOperator
from aos.contracts import AOSFault, Option, Phase, Prediction, Settings, State, canonical, digest
from aos.learning_events import project_learning_events
from aos.owned_adapter_identity import (
    PROOF_KIND, adapter_registry_values, validate_adapter_identity,
    validate_runtime_binding, verify_adapter_inference, verify_adapter_run,
)
from aos.registries import DeploymentRegistry
from aos.storage import TrajectoryStore


def synthetic_identity():
    base = {'model_files': {'model.safetensors': '1' * 64},
            'checkpoint_revision': '2' * 40, 'tokenizer_revision': '2' * 40,
            'code_revision': '3' * 40}
    binding = {'protocol': 'owned-adapter-runtime-v1',
               'authorization_sha256': '4' * 64, 'adaptation_report_sha256': '5' * 64,
               'artifact_sha256': '6' * 64, 'input_sha256': '7' * 64,
               'deployment_manifest_sha256': '8' * 64,
               'base_deployment_id': 'decider-' + digest(base),
               'runtime_worker_sha256': '9' * 64}
    return {'deployment_id': 'owned-adapter-' + digest(binding),
            'kind': 'owned_episode_adapter_runtime', 'real_model': True,
            'base_deployment_id': binding['base_deployment_id'],
            'adapter_binding_sha256': digest(binding),
            'pins': base | {'owned_adapter_runtime': binding}}


class OwnedAdapterIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = Settings(workspace=root / 'workspace', database=root / 'trajectory.sqlite')
        self.store = TrajectoryStore(self.settings.database)
        self.addCleanup(self.store.close)
        self.identity = synthetic_identity()
        self.binding = self.identity['pins']['owned_adapter_runtime']
        self.metrics = {'adapter_loaded': True, 'adapter_hook_calls': 2,
                        'adapter_sha256': self.binding['artifact_sha256'],
                        'base_parameters_unchanged': True}

        async def decide(_state, _options):
            return Prediction(selected_option='save', probabilities={'save': 0.99, 'ask': 0.01})

        self.engine = SimpleNamespace(identity=self.identity, decide=decide, last_metrics=self.metrics)
        self.operator = BrowserOperator(self.settings, self.store,
                                        SimpleNamespace(runtime_id='runtime-synthetic'), self.engine)
        self.options = [Option(id='save', label='Save synthetic content'),
                        Option(id='ask', label='Ask the user')]

    def start(self):
        DeploymentRegistry(self.store).record_experiment(self.identity)
        state = State(task_id='task-synthetic', run_id='run-synthetic', step_id='step-synthetic',
                      runtime_id='runtime-synthetic', deployment_id=self.identity['deployment_id'],
                      owner_lease_id='lease-synthetic', task_kind='browser_form',
                      normalized_goal='Synthetic private content must not appear in proof')
        self.store.create_run(state, self.identity, {'synthetic': True})
        state = self.operator.advance(state, Phase.OBSERVE)
        return self.operator.advance(state, Phase.DECIDE, observation='Synthetic decision-time state')

    def call(self):
        state = self.start()
        asyncio.run(self.operator.choose(state, self.options))
        return self.store.connection.execute('SELECT * FROM model_calls').fetchone()

    def complete_six(self):
        state = self.start()
        for ordinal in range(6):
            state, _decision, _prediction, _allowed = asyncio.run(self.operator.choose(state, self.options))
            state = self.operator.advance(state, Phase.EXECUTE)
            state = self.operator.advance(state, Phase.VERIFY)
            if ordinal < 5:
                state = self.operator.advance(state, Phase.OBSERVE)
                state = self.operator.advance(state, Phase.DECIDE)
        state = self.operator.advance(state, Phase.SUCCEEDED)
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded',outcome='passed' WHERE run_id=?",
                                          (state.run_id,))
        return {'runtime_binding': self.binding, 'adapter_deployment_id': self.identity['deployment_id']}

    def test_historical_identity_is_closed_and_does_not_require_current_worker(self):
        self.assertEqual(validate_runtime_binding(self.binding), self.binding)
        self.assertEqual(validate_adapter_identity(self.identity), self.binding)
        variants = [self.identity | {'extra': False}, self.identity | {'real_model': 1},
                    self.identity | {'deployment_id': self.binding['base_deployment_id']},
                    self.identity | {'adapter_binding_sha256': '0' * 64},
                    self.identity | {'base_deployment_id': 'decider-' + '0' * 64}]
        changed = copy.deepcopy(self.identity)
        changed['pins']['model_files']['model.safetensors'] = '0' * 64
        variants.append(changed)
        for identity in variants:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                validate_adapter_identity(identity)
        for binding in (self.binding | {'extra': True}, self.binding | {'protocol': 'base'},
                        self.binding | {'artifact_sha256': 'bad'}):
            with self.assertRaises(ValueError):
                validate_runtime_binding(binding)

    def test_registry_records_real_adapter_fk_without_promotion_and_rejects_drift(self):
        registry = DeploymentRegistry(self.store)
        registry.record_experiment(self.identity)
        registry.record_experiment(self.identity)
        expected = adapter_registry_values(self.identity)
        deployment = self.store.connection.execute('SELECT * FROM deployments').fetchone()
        self.assertEqual(deployment['adapter_id'], expected['adapter_id'])
        self.assertEqual(deployment['status'], 'EXPERIMENTAL')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM active_deployments').fetchone()[0], 0)
        self.store.connection.execute("UPDATE adapters SET compatibility='passed'")
        self.store.connection.commit()
        with self.assertRaises(AOSFault):
            registry.record_experiment(self.identity)

    def test_successful_call_proof_is_content_free_and_projection_requires_it(self):
        call = self.call()
        self.assertTrue(verify_adapter_inference(self.store.connection, call, self.identity))
        proof = self.store.connection.execute('SELECT * FROM observations WHERE kind=?', (PROOF_KIND,)).fetchone()
        self.assertNotIn('private content', proof['payload_json'])
        self.assertNotIn('Synthetic decision-time state', proof['payload_json'])
        events = project_learning_events(self.store.connection, call['run_id'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['model_kind'], self.identity['kind'])
        self.store.connection.execute('DELETE FROM observations WHERE observation_id=?', (proof['observation_id'],))
        self.assertFalse(verify_adapter_inference(self.store.connection, call, self.identity))
        self.assertEqual(project_learning_events(self.store.connection, call['run_id']), [])

    def test_missing_metrics_leave_call_error_and_create_no_decision_or_proof(self):
        state = self.start()
        self.engine.last_metrics = {}
        with self.assertRaises(ValueError):
            asyncio.run(self.operator.choose(state, self.options))
        self.assertEqual(self.store.connection.execute('SELECT status FROM model_calls').fetchone()[0], 'error')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM observations WHERE kind=?',
                                                      (PROOF_KIND,)).fetchone()[0], 0)

    def test_base_fallback_and_mid_call_identity_change_never_create_decision(self):
        state = self.start()
        original = copy.deepcopy(self.identity)
        self.engine.identity = {'deployment_id': self.binding['base_deployment_id'],
                                'kind': 'decider_native_worker', 'real_model': True,
                                'pins': {key: value for key, value in self.identity['pins'].items()
                                         if key != 'owned_adapter_runtime'}}
        with self.assertRaises(ValueError):
            asyncio.run(self.operator.choose(state, self.options))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.engine.identity = original

        async def changed_identity(_state, _options):
            self.engine.identity = self.engine.identity | {'deployment_id': self.binding['base_deployment_id']}
            return Prediction(selected_option='save', probabilities={'save': 0.99, 'ask': 0.01})

        self.engine.decide = changed_identity
        with self.assertRaises(AOSFault):
            asyncio.run(self.operator.choose(state, self.options))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT status FROM model_calls').fetchone()[0], 'error')

    def test_registry_config_and_call_response_tamper_invalidate_proof(self):
        call = self.call()
        connection = self.store.connection
        connection.execute("UPDATE deployments SET config_sha256=?", ('0' * 64,))
        self.assertFalse(verify_adapter_inference(connection, call, self.identity))
        connection.execute("UPDATE deployments SET config_sha256=?", (digest(self.identity['pins']),))
        self.assertTrue(verify_adapter_inference(connection, call, self.identity))
        connection.execute("UPDATE model_calls SET response_json=? WHERE call_id=?",
                           (canonical({'selected_option': 'ask', 'probabilities': {'save': 0.01, 'ask': 0.99}}),
                            call['call_id']))
        changed = connection.execute('SELECT * FROM model_calls WHERE call_id=?', (call['call_id'],)).fetchone()
        self.assertFalse(verify_adapter_inference(connection, changed, self.identity))

    def test_historical_verification_is_read_only(self):
        admission = self.complete_six()
        readonly = TrajectoryStore(self.settings.database, readonly=True)
        try:
            before = readonly.connection.total_changes
            self.assertTrue(verify_adapter_run(readonly.connection, 'run-synthetic', admission))
            self.assertEqual(readonly.connection.total_changes, before)
        finally:
            readonly.close()

    def test_proof_call_response_registry_and_timestamp_mutations_fail_closed(self):
        call = self.call()
        proof = self.store.connection.execute('SELECT * FROM observations WHERE kind=?', (PROOF_KIND,)).fetchone()
        original = json.loads(proof['payload_json'])
        for key, value in (('adapter_hook_calls', 0), ('adapter_hook_calls', True),
                           ('binding_sha256', '0' * 64), ('response_sha256', '0' * 64),
                           ('run_id', 'run-other'), ('base_parameters_unchanged', False)):
            self.store.connection.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                (canonical(original | {key: value}), proof['observation_id']))
            self.assertFalse(verify_adapter_inference(self.store.connection, call, self.identity))
        self.store.connection.execute('UPDATE observations SET payload_json=? WHERE observation_id=?',
                                     (proof['payload_json'], proof['observation_id']))
        self.assertTrue(verify_adapter_inference(self.store.connection, call, self.identity))
        self.store.connection.execute("UPDATE observations SET created_at='2000-01-01T00:00:00Z' WHERE kind=?", (PROOF_KIND,))
        self.assertFalse(verify_adapter_inference(self.store.connection, call, self.identity))
        self.store.connection.execute('UPDATE observations SET created_at=? WHERE observation_id=?',
                                     (proof['created_at'], proof['observation_id']))
        self.store.insert('observations', **{**dict(proof), 'observation_id': 'observation-duplicate'})
        self.assertFalse(verify_adapter_inference(self.store.connection, call, self.identity))

    def test_six_call_run_rejects_mixed_base_and_missing_or_extra_proofs(self):
        admission = self.complete_six()
        connection = self.store.connection
        self.assertTrue(verify_adapter_run(connection, 'run-synthetic', admission))
        call = connection.execute('SELECT * FROM model_calls LIMIT 1').fetchone()
        connection.execute('UPDATE model_calls SET deployment_id=? WHERE call_id=?',
                           (self.binding['base_deployment_id'], call['call_id']))
        self.assertFalse(verify_adapter_run(connection, 'run-synthetic', admission))
        connection.execute('UPDATE model_calls SET deployment_id=? WHERE call_id=?',
                           (self.identity['deployment_id'], call['call_id']))
        self.assertTrue(verify_adapter_run(connection, 'run-synthetic', admission))
        proof = connection.execute('SELECT * FROM observations WHERE kind=? LIMIT 1', (PROOF_KIND,)).fetchone()
        self.store.insert('observations', **{**dict(proof), 'observation_id': 'proof-extra'})
        self.assertFalse(verify_adapter_run(connection, 'run-synthetic', admission))
        connection.execute("DELETE FROM observations WHERE observation_id='proof-extra'")
        connection.execute('DELETE FROM observations WHERE observation_id=?', (proof['observation_id'],))
        self.assertFalse(verify_adapter_run(connection, 'run-synthetic', admission))


if __name__ == '__main__':
    unittest.main()
