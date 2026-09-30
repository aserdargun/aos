import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aos.contracts import Action, canonical, digest
from aos.owned_adapter_comparison import compare_adapter_runs
from aos.owned_adapter_identity import PROOF_KIND


class OwnedAdapterComparisonTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE runs(run_id TEXT PRIMARY KEY,status TEXT,outcome TEXT,deployment_snapshot_json TEXT,
                started_at TEXT,ended_at TEXT);
            CREATE TABLE model_calls(call_id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,deployment_id TEXT,
                role TEXT,status TEXT,latency_ms REAL,request_json TEXT,response_json TEXT,created_at TEXT);
            CREATE TABLE actions(action_id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,tool TEXT,
                arguments_json TEXT,status TEXT,created_at TEXT,completed_at TEXT,decision_id TEXT,actual_option TEXT);
            CREATE TABLE decisions(decision_id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,call_id TEXT,
                snapshot_id TEXT,selected_option TEXT,created_at TEXT);
            CREATE TABLE state_snapshots(snapshot_id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,
                state_json TEXT,content_sha256 TEXT,created_at TEXT);
            CREATE TABLE action_envelopes(action_id TEXT PRIMARY KEY,envelope_json TEXT,payload_sha256 TEXT);
            CREATE TABLE verifications(verification_id TEXT PRIMARY KEY,run_id TEXT,action_id TEXT,
                result TEXT,expected_json TEXT,actual_json TEXT,method TEXT);
            CREATE TABLE desktop_tasks(job_id TEXT PRIMARY KEY,run_id TEXT,kind TEXT,status TEXT,
                created_at TEXT,updated_at TEXT);
            CREATE TABLE desktop_approvals(approval_id TEXT PRIMARY KEY,job_id TEXT,envelope_json TEXT,
                action_sha256 TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE observations(observation_id TEXT PRIMARY KEY,run_id TEXT,step_id TEXT,action_id TEXT,
                kind TEXT,payload_json TEXT,created_at TEXT);
        ''')
        self.base_run_id = 'run-base'
        self.adapter_run_id = 'run-adapter'
        base_pins = {
            'model_files': {'model.safetensors': 'a' * 64},
            'checkpoint_revision': 'b' * 40,
            'tokenizer_revision': 'c' * 40,
            'code_revision': 'd' * 40,
        }
        self.base_deployment_id = 'decider-' + digest(base_pins)
        binding = {
            'protocol': 'owned-adapter-runtime-v1',
            'authorization_sha256': '1' * 64,
            'adaptation_report_sha256': '2' * 64,
            'artifact_sha256': '3' * 64,
            'input_sha256': '4' * 64,
            'deployment_manifest_sha256': '5' * 64,
            'base_deployment_id': self.base_deployment_id,
            'runtime_worker_sha256': '6' * 64,
        }
        adapter_pins = {**base_pins, 'owned_adapter_runtime': binding}
        self.adapter_deployment_id = 'owned-adapter-' + digest(binding)
        self.adapter_identity = {
            'deployment_id': self.adapter_deployment_id,
            'kind': 'owned_episode_adapter_runtime',
            'real_model': True,
            'base_deployment_id': self.base_deployment_id,
            'adapter_binding_sha256': digest(binding),
            'pins': adapter_pins,
        }
        base_identity = {
            'deployment_id': self.base_deployment_id,
            'kind': 'decider_native_worker',
            'real_model': True,
            'pins': base_pins,
        }
        self._insert_run(self.base_run_id, self.base_deployment_id, base_identity, 'base', 10.0, False)
        self._insert_run(self.adapter_run_id, self.adapter_deployment_id,
                         self.adapter_identity, 'adapter', 12.5, True)

    def tearDown(self):
        self.connection.close()

    def _insert_run(self, run_id, deployment_id, identity, prefix, latency, adapter):
        def timestamp(seconds):
            return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
        self.connection.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                                 (run_id, 'succeeded', 'passed', canonical(identity), timestamp(0), timestamp(61)))
        job_id = 'job-' + prefix
        self.connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?,?,?)',
                                 (job_id, run_id, 'browser_remote_form', 'succeeded', timestamp(0), timestamp(62)))
        tools = ('browser.form.open', 'browser.form.state_before', 'browser.form.fill',
                 'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after')
        for index in range(6):
            step_id = f'{prefix}-step-{index}'
            call_id = f'{prefix}-call-{index}'
            action_id = f'{prefix}-action-{index}'
            self.connection.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?)', (
                call_id, run_id, step_id, deployment_id, 'system1', 'ok', latency + index,
                '{}', '{}', timestamp(index * 10 + 2)))
            action = Action(
                task_id='task-' + prefix,
                run_id=run_id,
                step_id=step_id,
                action_id=action_id,
                runtime_id='runtime-' + prefix,
                state_version=index,
                owner_lease_id='lease-' + prefix,
                tool=tools[index],
                arguments={'url': 'https://fixture.invalid/form', 'stage': 3 if index == 4 else index},
                expected_effect='observe finite synthetic form',
                deadline=1767225600.0 + index,
                idempotency_key=f'{prefix}-key-{index}',
                selected_option='observe',
            )
            action_dict = action.model_dump(mode='json')
            action_sha256 = digest(action_dict)
            self.connection.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)', (
                action_id, run_id, step_id, action.tool, canonical(action.arguments), 'ok',
                timestamp(index * 10 + 6), timestamp(index * 10 + 7), f'{prefix}-decision-{index}', action.selected_option))
            snapshot_id = f'{prefix}-snapshot-{index}'
            state = {'phase': 'DECIDE', 'run_id': run_id, 'step_id': step_id,
                     'deployment_id': deployment_id, 'runtime_id': action.runtime_id,
                     'owner_lease_id': action.owner_lease_id}
            self.connection.execute('INSERT INTO state_snapshots VALUES(?,?,?,?,?,?)', (
                snapshot_id, run_id, step_id, canonical(state), digest(state), timestamp(index * 10 + 1)))
            self.connection.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?)', (
                f'{prefix}-decision-{index}', run_id, step_id, call_id, snapshot_id,
                action.selected_option, timestamp(index * 10 + 3)))
            self.connection.execute('INSERT INTO action_envelopes VALUES(?,?,?)', (
                action_id, canonical(action_dict), action_sha256))
            if index in (4, 5):
                method = ('independent_https_form_transport_readback' if index == 4
                          else 'declared_https_form_state_readback')
                self.connection.execute('INSERT INTO verifications VALUES(?,?,?,?,?,?,?)', (
                    f'{prefix}-verification-{index}', run_id, action_id, 'passed', '"ok"', '"ok"', method))
            if index == 4:
                readback = action.model_copy(update={'action_id': f'{prefix}-readback',
                    'idempotency_key': f'{prefix}-readback-key', 'tool': 'browser.form.observe',
                    'arguments': action.arguments | {'stage': 4}})
                readback_dict = readback.model_dump(mode='json')
                self.connection.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)', (
                    readback.action_id, run_id, step_id, readback.tool, canonical(readback.arguments), 'ok',
                    timestamp(48), timestamp(49), f'{prefix}-decision-{index}', action.selected_option))
                self.connection.execute('INSERT INTO action_envelopes VALUES(?,?,?)', (
                    readback.action_id, canonical(readback_dict), digest(readback_dict)))
            created = timestamp(index * 10 + 4)
            updated = timestamp(index * 10 + 5)
            approval = {
                'schema_version': '1.0',
                'approval_id': f'{prefix}-approval-{index}',
                'job_id': job_id,
                'action': action_dict,
                'action_sha256': action_sha256,
                'expires_at': 1767225600.0 + index,
            }
            self.connection.execute('INSERT INTO desktop_approvals VALUES(?,?,?,?,?,?,?)', (
                approval['approval_id'], job_id, canonical(approval), action_sha256, 'consumed',
                created, updated))
            if adapter:
                proof = {
                    'call_id': call_id,
                    'run_id': run_id,
                    'step_id': step_id,
                    'deployment_id': deployment_id,
                }
                self.connection.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)', (
                    f'{prefix}-proof-{index}', run_id, step_id, None, PROOF_KIND,
                    canonical(proof), f'2026-01-01T00:00:{index + 1:02d}+00:00'))
        self.connection.commit()

    def _compare(self):
        with (patch('aos.owned_adapter_comparison.verify_adapter_registry', return_value=True),
              patch('aos.owned_adapter_comparison.verify_adapter_inference', return_value=True) as verify):
            report = compare_adapter_runs(
                self.connection, self.base_run_id, self.adapter_run_id,
                base_deployment_id=self.base_deployment_id,
                adapter_deployment_id=self.adapter_deployment_id)
        self.assertEqual(verify.call_count, 6)
        return report

    def test_compares_six_verified_calls_without_quality_claim(self):
        report = self._compare()
        self.assertEqual(report['status'], 'owned_adapter_pair_comparison_verified')
        self.assertEqual(report['scope'], 'two_run_timing_only')
        self.assertEqual(report['base']['call_latency_sum_ms'], 75.0)
        self.assertEqual(report['adapter']['call_latency_sum_ms'], 90.0)
        self.assertEqual(report['adapter_minus_base']['call_latency_sum_ms'], 15.0)
        self.assertEqual(report['base']['approval_window_sum_ms'], 6000.0)
        self.assertEqual(report['adapter']['approval_window_sum_ms'], 6000.0)
        self.assertEqual(report['base']['action_count'], 7)
        self.assertEqual(report['adapter']['action_count'], 7)
        self.assertFalse(report['quality_superiority_verified'])
        self.assertFalse(report['training_ready'])
        self.assertFalse(report['promotion_authorized'])

    def test_rejects_mixed_deployment_and_missing_adapter_proof(self):
        self.connection.execute('UPDATE model_calls SET deployment_id=? WHERE run_id=? AND call_id=?',
                                ('decider-' + 'e' * 64, self.adapter_run_id, 'adapter-call-0'))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()
        self.connection.execute('UPDATE model_calls SET deployment_id=? WHERE run_id=? AND call_id=?',
                                (self.adapter_deployment_id, self.adapter_run_id, 'adapter-call-0'))
        self.connection.execute('DELETE FROM observations WHERE observation_id=?', ('adapter-proof-0',))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()

    def test_rejects_adapter_proof_on_base_and_changed_approval_binding(self):
        self.connection.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)', (
            'unexpected-base-proof', self.base_run_id, 'base-step-0', None, PROOF_KIND,
            '{}', '2026-01-01T00:00:01+00:00'))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()
        self.connection.execute('DELETE FROM observations WHERE observation_id=?', ('unexpected-base-proof',))
        self.connection.execute('UPDATE desktop_approvals SET action_sha256=? WHERE approval_id=?',
                                ('f' * 64, 'adapter-approval-0'))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()

    def test_rejects_negative_time_and_non_successful_runs(self):
        self.connection.execute('UPDATE desktop_approvals SET updated_at=? WHERE approval_id=?',
                                ('2025-12-31T23:59:59+00:00', 'adapter-approval-0'))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()
        self.connection.execute('UPDATE desktop_approvals SET updated_at=? WHERE approval_id=?',
                                ('2026-01-01T00:00:05+00:00', 'adapter-approval-0'))
        self.connection.execute('UPDATE model_calls SET latency_ms=? WHERE call_id=?',
                                (-1.0, 'base-call-0'))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()
        self.connection.execute('UPDATE model_calls SET latency_ms=? WHERE call_id=?',
                                (10.0, 'base-call-0'))
        self.connection.execute('UPDATE runs SET outcome=? WHERE run_id=?', ('unknown', self.base_run_id))
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            self._compare()

    def test_rejects_duplicate_run_or_deployment_identity(self):
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            compare_adapter_runs(self.connection, self.base_run_id, self.base_run_id,
                                 base_deployment_id=self.base_deployment_id,
                                 adapter_deployment_id=self.adapter_deployment_id)
        with self.assertRaisesRegex(ValueError, 'owned_adapter_comparison_unavailable'):
            compare_adapter_runs(self.connection, self.base_run_id, self.adapter_run_id,
                                 base_deployment_id=self.base_deployment_id,
                                 adapter_deployment_id=self.base_deployment_id)

    def assert_mutations_rejected(self, mutations):
        for statement, arguments in mutations:
            self.connection.execute('SAVEPOINT synthetic_mutation')
            try:
                changed = self.connection.execute(statement, arguments)
                self.assertEqual(changed.rowcount, 1)
                with self.subTest(statement=statement, arguments=arguments), \
                        self.assertRaisesRegex(ValueError, '^owned_adapter_comparison_unavailable$'):
                    self._compare()
            finally:
                self.connection.execute('ROLLBACK TO synthetic_mutation')
                self.connection.execute('RELEASE synthetic_mutation')
        self._compare()

    def test_action_decision_call_and_snapshot_cannot_cross_join(self):
        self.assert_mutations_rejected([
            ('UPDATE actions SET decision_id=? WHERE action_id=?', ('base-decision-1', 'base-action-0')),
            ('UPDATE actions SET decision_id=? WHERE action_id=?', ('adapter-decision-0', 'base-action-0')),
            ('UPDATE decisions SET call_id=? WHERE decision_id=?', ('base-call-1', 'base-decision-0')),
            ('UPDATE decisions SET call_id=? WHERE decision_id=?', ('missing-call', 'base-decision-0')),
            ('UPDATE decisions SET snapshot_id=? WHERE decision_id=?', ('adapter-snapshot-0', 'base-decision-0')),
            ('UPDATE decisions SET selected_option=? WHERE decision_id=?', ('other', 'base-decision-0')),
            ('UPDATE model_calls SET step_id=? WHERE call_id=?', ('other-step', 'base-call-0')),
            ('UPDATE state_snapshots SET content_sha256=? WHERE snapshot_id=?', ('0' * 64, 'base-snapshot-0')),
            ('DELETE FROM decisions WHERE decision_id=?', ('base-decision-0',)),
        ])

    def test_approval_windows_are_bounded_ordered_and_before_their_actions(self):
        self.assert_mutations_rejected([
            ('UPDATE desktop_approvals SET created_at=? WHERE approval_id=?',
             ('2025-12-31T23:59:59+00:00', 'base-approval-0')),
            ('UPDATE desktop_approvals SET updated_at=? WHERE approval_id=?',
             ('2026-01-01T00:00:07+00:00', 'base-approval-0')),
            ('UPDATE desktop_approvals SET created_at=? WHERE approval_id=?',
             ('2026-01-01T00:00:04+00:00', 'base-approval-1')),
            ('UPDATE actions SET completed_at=? WHERE action_id=?',
             ('2026-01-01T00:00:05+00:00', 'base-action-0')),
            ('UPDATE actions SET completed_at=? WHERE action_id=?',
             ('2026-01-01T00:01:02+00:00', 'base-action-5')),
            ('UPDATE runs SET ended_at=? WHERE run_id=?',
             ('2026-01-01T00:00:56+00:00', self.base_run_id)),
            ('UPDATE desktop_tasks SET updated_at=? WHERE job_id=?',
             ('2026-01-01T00:01:00+00:00', 'job-base')),
            ('UPDATE desktop_tasks SET created_at=? WHERE job_id=?',
             ('2026-01-01T00:00:01+00:00', 'job-base')),
            ('UPDATE decisions SET created_at=? WHERE decision_id=?',
             ('2026-01-01T00:00:06+00:00', 'base-decision-0')),
        ])

    def test_approval_envelope_cannot_name_other_job_or_approval(self):
        approval = json.loads(self.connection.execute('SELECT envelope_json FROM desktop_approvals '
                                                     'WHERE approval_id=?', ('base-approval-0',)).fetchone()[0])
        self.assert_mutations_rejected([
            ('UPDATE desktop_approvals SET envelope_json=? WHERE approval_id=?',
             (canonical(approval | {'job_id': 'job-other'}), 'base-approval-0')),
            ('UPDATE desktop_approvals SET envelope_json=? WHERE approval_id=?',
             (canonical(approval | {'approval_id': 'other-approval'}), 'base-approval-0')),
        ])

    def test_base_snapshot_cannot_claim_fixture_or_different_pins(self):
        identity = json.loads(self.connection.execute('SELECT deployment_snapshot_json FROM runs WHERE run_id=?',
                                                      (self.base_run_id,)).fetchone()[0])
        self.assert_mutations_rejected([
            ('UPDATE runs SET deployment_snapshot_json=? WHERE run_id=?',
             (canonical(identity | {'kind': 'fixture'}), self.base_run_id)),
            ('UPDATE runs SET deployment_snapshot_json=? WHERE run_id=?',
             (canonical(identity | {'real_model': False}), self.base_run_id)),
            ('UPDATE runs SET deployment_snapshot_json=? WHERE run_id=?',
             (canonical(identity | {'pins': {'different': True}}), self.base_run_id)),
        ])

    def test_deterministic_readback_must_exist_and_join_receipt_before_state_after(self):
        self.assert_mutations_rejected([
            ('DELETE FROM actions WHERE action_id=?', ('base-readback',)),
            ('UPDATE actions SET tool=? WHERE action_id=?', ('browser.other', 'base-readback')),
            ('UPDATE actions SET decision_id=? WHERE action_id=?', ('base-decision-0', 'base-readback')),
            ('UPDATE actions SET actual_option=? WHERE action_id=?', ('other', 'base-readback')),
            ('UPDATE actions SET created_at=? WHERE action_id=?',
             ('2026-01-01T00:00:46+00:00', 'base-readback')),
            ('UPDATE actions SET completed_at=? WHERE action_id=?',
             ('2026-01-01T00:00:57+00:00', 'base-readback')),
            ('DELETE FROM action_envelopes WHERE action_id=?', ('base-readback',)),
        ])

    def test_real_two_verification_methods_must_bind_receipt_and_state_after(self):
        self.assert_mutations_rejected([
            ('DELETE FROM verifications WHERE verification_id=?', ('base-verification-4',)),
            ('UPDATE verifications SET action_id=? WHERE verification_id=?',
             ('base-readback', 'base-verification-4')),
            ('UPDATE verifications SET method=? WHERE verification_id=?',
             ('unrelated', 'base-verification-4')),
            ('UPDATE verifications SET result=? WHERE verification_id=?',
             ('failed', 'base-verification-5')),
            ('UPDATE verifications SET actual_json=? WHERE verification_id=?',
             ('"changed"', 'base-verification-5')),
        ])

    def test_rehashed_readback_cannot_change_receipt_scope(self):
        original = json.loads(self.connection.execute('SELECT envelope_json FROM action_envelopes '
                                                      'WHERE action_id=?', ('base-readback',)).fetchone()[0])
        for arguments in (original['arguments'] | {'stage': 3},
                          original['arguments'] | {'url': 'https://other.invalid/'},
                          original['arguments'] | {'extra': 'not_authorized'}):
            self.connection.execute('SAVEPOINT synthetic_readback')
            try:
                changed = original | {'arguments': arguments}
                self.connection.execute('UPDATE actions SET arguments_json=? WHERE action_id=?',
                                         (canonical(arguments), 'base-readback'))
                self.connection.execute('UPDATE action_envelopes SET envelope_json=?,payload_sha256=? WHERE action_id=?',
                                         (canonical(changed), digest(changed), 'base-readback'))
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    self._compare()
            finally:
                self.connection.execute('ROLLBACK TO synthetic_readback')
                self.connection.execute('RELEASE synthetic_readback')


if __name__ == '__main__':
    unittest.main()
