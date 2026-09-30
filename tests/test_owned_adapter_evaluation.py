from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import asyncio

from aos.contracts import canonical, digest
from aos.owned_adapter_evaluation import OwnedAdapterEvaluation, PairedBaseEngine
from aos.owned_adapter_pair_contract import pair_observation, pair_record
from aos.owned_adapter_runtime import OwnedAdapterRuntime
from aos.owned_episode_learning import OwnedEpisodeStore
from tests.test_owned_adapter_pair_contract import synthetic_runtime_preview


class OwnedAdapterEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.preview = synthetic_runtime_preview()
        admission = self.preview['admission']
        self.episode_id = admission['episode_id']
        self.authorization_sha256 = admission['authorization_sha256']
        self.control = {'owner': 'AGENT', 'status': 'running',
                        'lease_id': 'lease-synthetic', 'generation': 2}
        self.scheduler = SimpleNamespace(
            controller=SimpleNamespace(session_id='session-synthetic',
                runtime=SimpleNamespace(runtime_id='runtime-synthetic'), state=lambda: self.control),
            engine=SimpleNamespace(identity={'deployment_id': admission['runtime_binding']['base_deployment_id']},
                manifest=self.root / 'synthetic-manifest.json', python=Path('/synthetic/python'), timeout=45),
            closed=False, restart_quiesced=False, paused=False, busy=False, reserved=False,
            sequences=SimpleNamespace(reserved=False), planning_reserved=False, adaptation_reserved=False,
            _owned_selected_candidate_execution_session_starts=0,
            remote_form_owned_candidate_session=SimpleNamespace(directory=self.root / 'owned-source'),
            job_id=None, task=Mock(spec=['cancel']),
            store=SimpleNamespace(insert=Mock()),
        )
        self.source_report = {'authorization': {'candidate_execution_sha256': 'b' * 64,
            'input_sha256': 'c' * 64, 'deployment_manifest_sha256': 'd' * 64,
            'deployment_id': self.scheduler.engine.identity['deployment_id']}, 'artifact_sha256': 'e' * 64}
        self.assertEqual(digest(self.source_report), admission['adaptation_report_sha256'])
        store = OwnedEpisodeStore(self.root / 'owned-episodes')
        with store.directory(self.episode_id, create=True):
            pass
        self.learning = SimpleNamespace(scheduler=self.scheduler, store=store,
            preparation=SimpleNamespace(reserved=False),
            adaptation=SimpleNamespace(inspect=Mock(return_value=deepcopy(self.source_report))))
        self.runtime = OwnedAdapterRuntime(self.learning)
        self.learning.adapter_runtime = self.runtime
        self.runtime.preview = Mock(side_effect=self.runtime_preview)
        self.runtime.start = Mock(side_effect=lambda *arguments: self.start_result('adapter'))
        self.scheduler.start_owned_form_candidate_execution = Mock(side_effect=lambda **arguments: self.start_result('base'))
        self.scheduler._assert_owned_candidate_execution_start_available = Mock(side_effect=self.available)
        self.service = OwnedAdapterEvaluation(self.learning)
        self.record = pair_record(self.preview, session_id='session-synthetic', runtime_id='runtime-synthetic',
                                  lease_id='lease-synthetic', generation=2)
        self.pair_sha256 = digest(self.record)
        self.arguments = {'episode_id': self.episode_id, 'authorization_sha256': self.authorization_sha256,
            'case_key': self.record['arguments']['case_key'],
            'development_value': self.record['arguments']['development_value'],
            'lease_id': 'lease-synthetic', 'generation': 2}
        self.loader = patch('aos.owned_adapter_evaluation.load_candidate_execution_bundle',
            return_value=({'manifest': {'parameter_variant_sha256': '9' * 64}}, '8' * 64))
        self.load_bundle = self.loader.start()
        self.addCleanup(self.loader.stop)
        worker_pin = patch('aos.owned_adapter_engine.runtime_worker_pin',
                           return_value=admission['runtime_binding']['runtime_worker_sha256'])
        self.worker_pin = worker_pin.start()
        self.addCleanup(worker_pin.stop)

    def runtime_preview(self, episode_id, authorization_sha256, case_key, development_value, lease_id, generation):
        self.runtime.control(lease_id, generation)
        self.assertEqual((episode_id, authorization_sha256, case_key, development_value),
                         tuple(self.arguments[key] for key in ('episode_id', 'authorization_sha256',
                                                               'case_key', 'development_value')))
        return deepcopy(self.preview)

    def available(self, preview_sha256, selected_lane):
        self.assertTrue(selected_lane)
        self.assertIn(preview_sha256, [self.record[arm + '_preview']['preview_sha256'] for arm in ('base', 'adapter')])
        if self.scheduler._owned_selected_candidate_execution_session_starts >= 4:
            raise ValueError('synthetic quota exhausted')

    def start_result(self, arm):
        self.scheduler._owned_selected_candidate_execution_session_starts += 1
        return {'job_id': 'job-' + arm, 'candidate_execution_sha256': ('1' if arm == 'base' else '2') * 64}

    def commit(self):
        return self.service.commit(**self.arguments, confirm_sha256=self.pair_sha256,
                                   experimental_evaluation_authorized=True)

    def start(self, arm='base', **changes):
        arguments = {'episode_id': self.episode_id, 'pair_sha256': self.pair_sha256, 'arm': arm,
            'lease_id': 'lease-synthetic', 'generation': 2, 'confirm_sha256': self.pair_sha256,
            'experimental_runtime_authorized': True}
        return self.service.start(**(arguments | changes))

    def activate(self):
        self.commit()
        result = self.start()
        self.scheduler.job_id = result['job_id']
        self.scheduler.busy = self.scheduler.reserved = True
        self.scheduler._active_owned_candidate_execution = {
            'job_id': result['job_id'], 'candidate_execution_sha256': result['candidate_execution_sha256']}
        return result

    def private_files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_preview_is_read_only_and_commit_requires_exact_separate_consent(self):
        before = self.private_files()
        result = self.service.preview(**self.arguments)
        self.assertEqual(result['record'], self.record)
        self.assertFalse(result['committed'])
        self.assertEqual(self.private_files(), before)
        for consent in (False, 1, None):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                self.service.commit(**self.arguments, confirm_sha256=self.pair_sha256,
                                    experimental_evaluation_authorized=consent)
        with self.assertRaises(ValueError):
            self.service.commit(**self.arguments, confirm_sha256='0' * 64,
                                experimental_evaluation_authorized=True)
        self.assertEqual(self.private_files(), before)
        self.assertTrue(self.commit()['committed'])
        self.assertEqual(self.service.load(self.episode_id, self.pair_sha256), self.record)
        self.scheduler.start_owned_form_candidate_execution.assert_not_called()
        self.runtime.start.assert_not_called()

    def test_preview_rejects_training_input_and_insufficient_remaining_quota(self):
        self.load_bundle.return_value = ({'manifest': {
            'parameter_variant_sha256': self.record['base_preview']['parameter_variant_sha256']}}, '8' * 64)
        with self.assertRaises(ValueError):
            self.service.preview(**self.arguments)
        self.scheduler._owned_selected_candidate_execution_session_starts = 3
        with self.assertRaises(ValueError):
            self.service.preview(**self.arguments)
        self.assertEqual(self.private_files(), {})

    def test_each_arm_requires_exact_confirmation_and_boolean_consent(self):
        self.commit()
        for changes in ({'experimental_runtime_authorized': False}, {'experimental_runtime_authorized': 1},
                        {'confirm_sha256': '0' * 64}, {'arm': 'other'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.start(**changes)
        self.scheduler.start_owned_form_candidate_execution.assert_not_called()
        self.runtime.start.assert_not_called()

    def test_base_start_is_one_shot_and_does_not_automatically_start_adapter(self):
        self.commit()
        result = self.start()
        self.assertTrue(result['accepted'])
        self.scheduler.start_owned_form_candidate_execution.assert_called_once_with(
            **self.record['arguments'], preview_sha256=self.record['base_preview']['preview_sha256'],
            confirm_sha256=self.record['base_preview']['preview_sha256'], lease_id='lease-synthetic', generation=2)
        self.runtime.start.assert_not_called()
        self.assertIn('job-base', self.service.jobs)
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.scheduler.start_owned_form_candidate_execution.call_count, 1)

    def test_failed_start_burns_attempt_without_fabricating_receipt(self):
        self.commit()
        self.scheduler.start_owned_form_candidate_execution.side_effect = ValueError('synthetic pre-run failure')
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.service._read(self.episode_id, self.pair_sha256, '-base-attempt')['arm'], 'base')
        with self.assertRaises(FileNotFoundError):
            self.service._read(self.episode_id, self.pair_sha256, '-base-started')
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.scheduler.start_owned_form_candidate_execution.call_count, 1)

    def test_adapter_requires_verified_base_and_own_explicit_start(self):
        self.commit()
        with patch.object(self.service, 'report', return_value={'arms': {'base': {'status': 'failed'}}}):
            with self.assertRaises(ValueError):
                self.start('adapter')
        self.runtime.start.assert_not_called()
        with patch.object(self.service, 'report', return_value={'arms': {'base': {'status': 'verified'}}}):
            self.start('adapter')
        self.runtime.start.assert_called_once_with(self.episode_id, self.authorization_sha256,
            self.record['arguments']['case_key'], self.record['arguments']['development_value'],
            'lease-synthetic', 2, self.record['adapter_preview']['preview_sha256'], True)

    def test_current_session_runtime_and_control_cannot_rebind_committed_pair(self):
        self.commit()
        for container, key, changed in ((self.control, 'lease_id', 'lease-other'),
                (self.control, 'generation', 3), (self.control, 'owner', 'HUMAN')):
            previous = container[key]
            container[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.start()
            container[key] = previous
        for container, key in ((self.scheduler.controller, 'session_id'),
                                (self.scheduler.controller.runtime, 'runtime_id')):
            previous = getattr(container, key)
            setattr(container, key, 'other')
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.start()
            setattr(container, key, previous)
        self.scheduler.start_owned_form_candidate_execution.assert_not_called()

    def test_changed_source_or_preview_is_rejected_before_persisting_attempt(self):
        self.commit()
        original = self.private_files()
        changed = deepcopy(self.source_report)
        changed['artifact_sha256'] = '0' * 64
        self.learning.adaptation.inspect.return_value = changed
        with self.assertRaises(ValueError):
            self.start()
        self.learning.adaptation.inspect.return_value = deepcopy(self.source_report)
        self.preview['arguments']['development_value'] = 'other'
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.private_files(), original)
        self.scheduler.start_owned_form_candidate_execution.assert_not_called()

    def test_receipt_publication_failure_cancels_scheduled_job(self):
        self.commit()
        original = self.service._put
        def publish(episode_id, checksum, value, suffix=''):
            if suffix == '-base-started':
                raise OSError('synthetic publication failure')
            return original(episode_id, checksum, value, suffix)
        with patch.object(self.service, '_put', side_effect=publish), self.assertRaises(OSError):
            self.start()
        self.scheduler.task.cancel.assert_called_once_with()

    def test_per_action_source_scope_is_exact_and_restored_even_when_revoked(self):
        result = self.activate()
        source = self.source_report['authorization']['candidate_execution_sha256']
        def inspect(*arguments):
            self.assertTrue(self.service.permits_audit(source))
            self.assertFalse(self.service.permits_audit(result['candidate_execution_sha256']))
            return deepcopy(self.source_report)
        self.learning.adaptation.inspect.side_effect = inspect
        self.service.validate_job(result['job_id'])
        self.assertFalse(self.service.permits_audit(source))
        self.learning.adaptation.inspect.side_effect = ValueError('synthetic revoked receipt')
        with self.assertRaises(ValueError):
            self.service.validate_job(result['job_id'])
        self.assertFalse(self.service.permits_audit(source))

    def test_per_action_validation_rejects_missing_attempt_and_changed_job(self):
        result = self.activate()
        self.scheduler.job_id = 'job-other'
        with self.assertRaises(ValueError):
            self.service.validate_job(result['job_id'])
        self.scheduler.job_id = result['job_id']
        attempt = self.root / 'owned-episodes' / self.episode_id / ('pair-' + self.pair_sha256 + '-base-attempt.json')
        attempt.unlink()
        with self.assertRaises((FileNotFoundError, ValueError)):
            self.service.validate_job(result['job_id'])

    def test_running_pair_rejects_foreign_active_execution_and_self_source(self):
        result = self.activate()
        for changed in (None, {'job_id': 'job-other', 'candidate_execution_sha256': result['candidate_execution_sha256']},
                        {'job_id': result['job_id'], 'candidate_execution_sha256': '0' * 64}):
            self.scheduler._active_owned_candidate_execution = changed
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.service.validate_job(result['job_id'])
        self.scheduler._active_owned_candidate_execution = {
            'job_id': result['job_id'], 'candidate_execution_sha256': result['candidate_execution_sha256']}
        self.service.jobs[result['job_id']]['source_execution'] = result['candidate_execution_sha256']
        with self.assertRaises(ValueError):
            self.service.validate_job(result['job_id'])

    def test_base_engine_is_fresh_without_prewarm_and_exact_identity(self):
        result = self.activate()
        fresh = SimpleNamespace(identity=deepcopy(self.scheduler.engine.identity))
        with patch('aos.owned_adapter_evaluation.PairedBaseEngine', return_value=fresh) as constructor:
            self.assertIs(self.service.base_engine(result['job_id']), fresh)
            self.assertIs(constructor.call_args.args[0], self.scheduler.engine)
            constructor.call_args.args[1]()
        self.assertIsNot(fresh, self.scheduler.engine)
        with patch('aos.owned_adapter_evaluation.PairedBaseEngine',
                   return_value=SimpleNamespace(identity={'deployment_id': 'decider-' + '0' * 64})):
            with self.assertRaises(ValueError):
                self.service.base_engine(result['job_id'])

    def test_base_engine_cold_constructor_and_decision_source_checks(self):
        validate = Mock()
        with patch('aos.owned_adapter_evaluation.ReusableDeciderEngine.__init__', return_value=None) as initialize:
            engine = PairedBaseEngine(self.scheduler.engine, validate)
        initialize.assert_called_once_with(self.scheduler.engine.manifest, self.scheduler.engine.python,
            timeout=45, cpu_prewarm=False, idle_seconds=75, gpu_idle_seconds=0)
        with patch('aos.owned_adapter_evaluation.ReusableDeciderEngine.decide', new_callable=AsyncMock,
                   return_value='synthetic prediction') as decide:
            self.assertEqual(asyncio.run(engine.decide('synthetic state', ['synthetic option'])),
                             'synthetic prediction')
            self.assertEqual(validate.call_count, 2)
            decide.assert_awaited_once()
            validate.side_effect = ValueError('synthetic revoked source')
            with self.assertRaises(ValueError):
                asyncio.run(engine.decide('synthetic state', ['synthetic option']))
            self.assertEqual(decide.await_count, 1)

    def test_changed_worker_pin_prevents_start_before_attempt(self):
        self.commit()
        before = self.private_files()
        self.worker_pin.return_value = '0' * 64
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.private_files(), before)

    def historical_fixture(self):
        self.commit()
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript('''
            CREATE TABLE desktop_tasks(job_id TEXT, session_id TEXT, runtime_id TEXT,
                lease_id TEXT, generation INTEGER, status TEXT, run_id TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE state_snapshots(run_id TEXT, state_version INTEGER, state_json TEXT,
                step_id TEXT, created_at TEXT);
            CREATE TABLE observations(run_id TEXT, step_id TEXT, action_id TEXT, kind TEXT,
                payload_json TEXT, created_at TEXT);
            CREATE TABLE model_calls(run_id TEXT, created_at TEXT);
            CREATE TABLE actions(run_id TEXT, created_at TEXT);
        ''')
        bundles = {}
        metrics = {}
        for arm, offset in (('base', 0), ('adapter', 10)):
            timestamp = lambda second: f'2026-09-27T00:00:{offset + second:02d}.000Z'
            checksum = ('1' if arm == 'base' else '2') * 64
            receipt = {'pair_sha256': self.pair_sha256, 'arm': arm,
                'job_id': 'job-' + arm, 'candidate_execution_sha256': checksum}
            self.service._put(self.episode_id, self.pair_sha256,
                {'pair_sha256': self.pair_sha256, 'arm': arm, 'created_at': timestamp(1)}, '-' + arm + '-attempt')
            self.service._put(self.episode_id, self.pair_sha256, receipt, '-' + arm + '-started')
            run_id, step_id = 'run-' + arm, 'step-' + arm
            browser_id = 'browser-' + arm
            connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                ('job-' + arm, 'session-synthetic', browser_id, 'lease-synthetic', 2,
                 'succeeded', run_id, timestamp(2), timestamp(7)))
            state = {'phase': 'CREATED', 'step_id': step_id, 'runtime_id': browser_id,
                     'owner_lease_id': 'lease-synthetic'}
            connection.execute('INSERT INTO state_snapshots VALUES(?,?,?,?,?)',
                               (run_id, 0, canonical(state), step_id, timestamp(3)))
            observation = pair_observation(self.record, self.pair_sha256, arm, job_id='job-' + arm,
                run_id=run_id, step_id=step_id, candidate_execution_sha256=checksum)
            connection.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                (run_id, step_id, None, 'model.owned_adapter_evaluation', canonical(observation), timestamp(4)))
            connection.execute('INSERT INTO model_calls VALUES(?,?)', (run_id, timestamp(5)))
            connection.execute('INSERT INTO actions VALUES(?,?)', (run_id, timestamp(6)))
            bundles[checksum] = {'manifest': deepcopy(self.record[arm + '_preview']),
                'completion': {'job_id': 'job-' + arm, 'run_id': run_id},
                'reuse-admission': {'runtime_id': 'runtime-synthetic', 'desktop_session_id': 'session-synthetic',
                                    'lease_id': 'lease-synthetic', 'generation': 2}}
            metrics[arm] = {'run_id': run_id, 'deployment_id': self.record[arm + '_deployment_id'],
                'call_count': 6, 'action_count': 7, 'consumed_approval_count': 6,
                'adapter_proof_count': 0 if arm == 'base' else 6,
                'call_latency_sum_ms': 60.0, 'approval_window_sum_ms': 600.0}
        frozen = (connection, {'synthetic_snapshot': True})
        self.load_bundle.side_effect = lambda root, checksum: (deepcopy(bundles[checksum]), '8' * 64)
        self.scheduler.settings = SimpleNamespace(database=self.root / 'synthetic.sqlite')
        def audited(checksum, *, _audited_snapshot):
            self.assertIs(_audited_snapshot, frozen)
            return {'available': True, 'schema_version': bundles[checksum]['manifest']['schema_version'],
                    'report_sha256': '9' * 64}
        self.scheduler.audit_owned_form_candidate_execution = Mock(side_effect=audited)
        comparison = {'schema_version': '1.0', 'synthetic': True,
            'status': 'owned_adapter_pair_comparison_verified', 'scope': 'two_run_timing_only',
            **metrics, 'adapter_minus_base': {'call_latency_sum_ms': 0.0, 'approval_window_sum_ms': 0.0},
            'timing_interpretation': {'call_latency_sum_ms': 'decision_call_wall_clock_not_inference_only',
                                     'approval_window_sum_ms': 'approval_window_not_human_wait_only'},
            'quality_superiority_verified': False, 'training_ready': False, 'promotion_authorized': False}
        return frozen, bundles, comparison

    def test_historical_report_uses_one_frozen_snapshot_and_never_current_source_or_worker(self):
        frozen, _bundles, comparison = self.historical_fixture()
        self.learning.adaptation.inspect.reset_mock()
        with patch('aos.owned_adapter_evaluation.audit_snapshot', return_value=nullcontext(frozen)) as snapshot, \
                patch('aos.owned_adapter_comparison.compare_adapter_runs', return_value=comparison) as compare, \
                patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('subprocess.Popen', side_effect=AssertionError('worker forbidden')):
            result = self.service.report(self.episode_id, self.pair_sha256)
        self.assertTrue(result['verified'])
        self.assertFalse(result['promotion_authorized'])
        self.assertFalse(result['runtime_reuse_authorized'])
        snapshot.assert_called_once_with(self.scheduler.settings.database)
        self.assertEqual(self.scheduler.audit_owned_form_candidate_execution.call_count, 2)
        compare.assert_called_once_with(frozen[0], 'run-base', 'run-adapter',
            base_deployment_id=self.record['base_deployment_id'],
            adapter_deployment_id=self.record['adapter_deployment_id'])
        self.learning.adaptation.inspect.assert_not_called()

    def test_historical_arm_rejects_wrong_parent_browser_authority_and_late_marker(self):
        frozen, bundles, _comparison = self.historical_fixture()
        connection = frozen[0]
        self.assertEqual(self.service._arm_report(self.record, self.pair_sha256, 'base', frozen)['status'], 'verified')
        for column, value in (('session_id', 'session-other'), ('lease_id', 'lease-other'),
                              ('generation', 3), ('runtime_id', 'browser-other')):
            previous = connection.execute('SELECT ' + column + ' FROM desktop_tasks WHERE job_id=?',
                                          ('job-base',)).fetchone()[0]
            connection.execute('UPDATE desktop_tasks SET ' + column + '=? WHERE job_id=?', (value, 'job-base'))
            with self.subTest(column=column), self.assertRaises(ValueError):
                self.service._arm_report(self.record, self.pair_sha256, 'base', frozen)
            connection.execute('UPDATE desktop_tasks SET ' + column + '=? WHERE job_id=?', (previous, 'job-base'))
        bundles['1' * 64]['reuse-admission']['runtime_id'] = 'parent-other'
        with self.assertRaises(ValueError):
            self.service._arm_report(self.record, self.pair_sha256, 'base', frozen)
        bundles['1' * 64]['reuse-admission']['runtime_id'] = 'runtime-synthetic'
        connection.execute('UPDATE observations SET created_at=? WHERE run_id=?',
                           ('2026-09-27T00:00:06.000Z', 'run-base'))
        with self.assertRaises(ValueError):
            self.service._arm_report(self.record, self.pair_sha256, 'base', frozen)

    def test_historical_report_rejects_reversed_arm_order_before_comparison(self):
        frozen, _bundles, comparison = self.historical_fixture()
        frozen[0].execute('UPDATE desktop_tasks SET updated_at=? WHERE job_id=?',
                         ('2026-09-27T00:00:12.000Z', 'job-base'))
        with patch('aos.owned_adapter_evaluation.audit_snapshot', return_value=nullcontext(frozen)), \
                patch('aos.owned_adapter_comparison.compare_adapter_runs', return_value=comparison) as compare:
            with self.assertRaises(ValueError):
                self.service.report(self.episode_id, self.pair_sha256)
        compare.assert_not_called()

    def test_failed_arm_remains_visible_and_never_becomes_successful_comparison(self):
        frozen, _bundles, _comparison = self.historical_fixture()
        frozen[0].execute('UPDATE desktop_tasks SET status=? WHERE job_id=?', ('failed', 'job-adapter'))
        with patch('aos.owned_adapter_evaluation.audit_snapshot', return_value=nullcontext(frozen)), \
                patch('aos.owned_adapter_comparison.compare_adapter_runs') as compare:
            result = self.service.report(self.episode_id, self.pair_sha256)
        self.assertFalse(result['verified'])
        self.assertEqual(result['arms']['adapter']['status'], 'failed')
        self.assertIsNone(result['comparison'])
        compare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
