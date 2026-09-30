import asyncio
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import AOSFault, Action, ErrorCode, Failure, Phase, REPO_ROOT, Settings, State, canonical, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop_control import DesktopController
from aos.desktop_tasks import Approval, DesktopScheduler
from aos.failure_improvement import FailureImprovement, FailureImprovementService
from aos.registries import DeploymentRegistry
from aos.storage import TrajectoryStore
from tests.test_desktop_tasks import FixtureDesktop, WaitingEngine


SECRET = 'synthetic-private-failure-detail-not-for-projection'


class FaultEngine(FixtureDecisionEngine):
    async def decide(self, state, options):
        raise AOSFault(ErrorCode.MODEL_FAILURE, SECRET)


class FailureImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = TrajectoryStore(self.root / 'source.sqlite')
        self.addCleanup(self.store.close)
        self.service = FailureImprovementService(self.root / 'source.sqlite',
                                                 self.root / 'failure-improvements', 'session-fixture')
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id='session-fixture', runtime_id='runtime-fixture',
                              image_id='synthetic-image', owner='AGENT', lease_id='lease-fixture', generation=0,
                              status='running', created_at='2026-09-27T00:00:00Z', updated_at='2026-09-27T00:00:00Z')
        self.state = State(task_id='task-fixture', run_id='run-fixture', step_id='step-fixture',
                           runtime_id='runtime-fixture', deployment_id=FixtureDecisionEngine.identity['deployment_id'],
                           owner_lease_id='lease-fixture', original_goal=SECRET)
        self.store.create_run(self.state, FixtureDecisionEngine.identity,
                              {'runtime_id': self.state.runtime_id})
        failed = self.state.advance(Phase.FAILED, last_error=Failure(
            code='MODEL_FAILURE', detail=SECRET, retryable=False, evidence_refs=[]))
        self.store.save_state(self.state, failed)
        self.store.finish(failed, 'failed', 'unknown')
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id='job-fixture', session_id='session-fixture',
                              run_id=self.state.run_id, kind='hello', lease_id='lease-fixture', generation=0,
                              status='failed', real_model=0, created_at='2026-09-27T00:00:00Z',
                              updated_at='2026-09-27T00:00:01Z', runtime_id=self.state.runtime_id)

    def preview(self):
        return self.service.preview('job-fixture')

    def save(self):
        preview = self.preview()
        return self.service.save('job-fixture', preview['candidate_sha256'], True)

    def review(self):
        saved = self.save()
        option = saved['review_options'][0]
        return self.service.review('job-fixture', saved['candidate_sha256'], **option)

    def test_preview_private_counts_without_writes_and_stable_parent_control(self):
        before = self.store.connection.total_changes
        preview = self.preview()
        self.assertFalse(preview['saved'])
        self.assertFalse(self.service.directory.exists())
        self.assertEqual(before, self.store.connection.total_changes)
        self.assertNotIn(SECRET, canonical(preview))
        self.assertEqual(preview['candidate']['failure_code'], 'MODEL_FAILURE')
        self.assertFalse(preview['candidate']['model_roles']['system1']['real_model'])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=1,lease_id='new-lease',owner='HUMAN'")
        self.assertEqual(preview, self.preview())

    def test_explicit_save_review_revoke_and_private_immutable_files(self):
        reviewed = self.review()
        self.assertTrue(reviewed['saved'])
        self.assertEqual(reviewed['review']['decision'], 'accept')
        self.assertFalse(reviewed['review']['revoked'])
        revoked = self.service.revoke('job-fixture', reviewed['candidate_sha256'], reviewed['review']['receipt_sha256'])
        self.assertTrue(revoked['review']['revoked'])
        self.assertTrue(revoked['source_current'])
        self.assertEqual(self.service.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(list(self.service.directory.iterdir())), 3)
        for path in self.service.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertNotIn(SECRET, path.read_text())
        self.assertEqual(self.service.save('job-fixture', reviewed['candidate_sha256'], True), revoked)
        self.assertEqual(self.service.revoke('job-fixture', reviewed['candidate_sha256'],
                                           reviewed['review']['receipt_sha256']), revoked)
        self.assertEqual(self.service.review('job-fixture', reviewed['candidate_sha256'],
                                            **reviewed['review_options'][0]), revoked)
        with self.assertRaises(ValueError):
            self.service.review('job-fixture', reviewed['candidate_sha256'], **reviewed['review_options'][1])

    def test_consent_and_confirmation_are_exact_before_persistence(self):
        preview = self.preview()
        for consent in (False, 1, 'true', None):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                self.service.save('job-fixture', preview['candidate_sha256'], consent)
        with self.assertRaises(ValueError):
            self.service.save('job-fixture', 'f' * 64, True)
        self.assertFalse(self.service.directory.exists())

    def test_review_reject_and_forbidden_arbitrary_corrections(self):
        saved = self.save()
        for decision, code in (('accept', None), ('reject', 'repair_environment'), ('accept', SECRET)):
            with self.subTest(decision=decision, code=code), self.assertRaises(ValueError):
                self.service.review('job-fixture', saved['candidate_sha256'], decision, code, 'a' * 64)
        option = saved['review_options'][-1]
        result = self.service.review('job-fixture', saved['candidate_sha256'], **option)
        self.assertEqual(result['review']['decision'], 'reject')
        self.assertIsNone(result['review']['correction_code'])

    def test_source_drift_blocks_save_review_but_not_exact_revocation(self):
        reviewed = self.review()
        with self.store.connection:
            self.store.connection.execute("UPDATE runtime_states SET state_json='{}'")
        with self.assertRaises(ValueError):
            self.preview()
        result = self.service.revoke('job-fixture', reviewed['candidate_sha256'], reviewed['review']['receipt_sha256'])
        self.assertFalse(result['source_current'])
        self.assertTrue(result['review']['revoked'])

    def test_saved_job_and_session_are_required_even_for_revocation(self):
        reviewed = self.review()
        for service, job in ((self.service, 'other-job'), (FailureImprovementService(
                self.root / 'source.sqlite', self.service.directory, 'other-session'), 'job-fixture')):
            with self.subTest(job=job), self.assertRaises(ValueError):
                service.revoke(job, reviewed['candidate_sha256'], reviewed['review']['receipt_sha256'])

    def test_changed_terminal_metadata_invalidates_exact_confirmation(self):
        saved = self.save()
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET generation=1")
        with self.assertRaises(ValueError):
            self.service.review('job-fixture', saved['candidate_sha256'], **saved['review_options'][0])

    def test_unknown_success_waiting_human_and_missing_run_fail_closed(self):
        for status in ('succeeded', 'waiting_human', 'running'):
            with self.subTest(status=status):
                with self.store.connection:
                    self.store.connection.execute('UPDATE desktop_tasks SET status=?', (status,))
                with self.assertRaises(ValueError):
                    self.preview()
        with self.assertRaises(ValueError):
            self.service.preview('missing-job')

    def test_snapshot_and_unknown_failure_codes_fail_closed(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE runtime_states SET state_version=99")
        with self.assertRaises(ValueError):
            self.preview()

    def test_bounds_precede_fingerprint(self):
        with self.store.connection:
            for ordinal in range(501):
                self.store.insert('observations', observation_id=f'observation-{ordinal}', run_id=self.state.run_id,
                                  step_id=self.state.step_id, kind='synthetic', payload_json='{}',
                                  created_at='2026-09-27T00:00:00Z')
        with patch('aos.failure_improvement.source_fingerprint') as fingerprint:
            with self.assertRaises(ValueError):
                self.preview()
            fingerprint.assert_not_called()

    def test_approval_source_binding_and_pending_approval_rejected(self):
        action = Action(task_id=self.state.task_id, run_id=self.state.run_id, step_id=self.state.step_id,
                        action_id='action-fixture', runtime_id=self.state.runtime_id, state_version=0,
                        owner_lease_id=self.state.owner_lease_id, tool='filesystem.write', arguments={},
                        expected_effect='synthetic', deadline=1.0, idempotency_key='synthetic', selected_option='write')
        approval = Approval(approval_id='approval-fixture', job_id='job-fixture', action=action,
                            action_sha256=digest(action.model_dump(mode='json')), expires_at=1.0)
        with self.store.connection:
            self.store.insert('desktop_approvals', approval_id=approval.approval_id, job_id=approval.job_id,
                              envelope_json=approval.model_dump_json(), action_sha256=approval.action_sha256,
                              expires_at=approval.expires_at, status='rejected', created_at='2026-09-27T00:00:00Z',
                              updated_at='2026-09-27T00:00:00Z')
        saved = self.save()
        self.assertEqual(saved['candidate']['approval_counts']['rejected'], 1)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_approvals SET status='revoked'")
        with self.assertRaises(ValueError):
            self.service.review('job-fixture', saved['candidate_sha256'], **saved['review_options'][0])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_approvals SET status='pending'")
        with self.assertRaises(ValueError):
            self.preview()
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_approvals SET status='rejected',action_sha256=?", ('a' * 64,))
        with self.assertRaises(ValueError):
            self.preview()

    def test_uncertain_action_not_a_reviewable_failure(self):
        with self.store.connection:
            self.store.insert('actions', action_id='action-uncertain', run_id=self.state.run_id,
                              step_id=self.state.step_id, tool='filesystem.write', arguments_json='{}',
                              status='uncertain', idempotency_key='uncertain-fixture', created_at='2026-09-27T00:00:00Z')
        with self.assertRaises(ValueError):
            self.preview()

    def test_declared_real_role_requires_exact_registry_and_call_joins(self):
        pins = {'checkpoint_revision': 'a' * 40, 'tokenizer_revision': 'b' * 40,
                'model_files': {'model.safetensors': 'c' * 64}}
        identity = {'deployment_id': 'decider-' + digest(pins), 'kind': 'decider_native_worker',
                    'real_model': True, 'pins': pins}
        DeploymentRegistry(self.store).record_experiment(identity)
        state = State(task_id='task-declared', run_id='run-declared', step_id='step-declared',
                      runtime_id=self.state.runtime_id, deployment_id=identity['deployment_id'],
                      owner_lease_id=self.state.owner_lease_id)
        self.store.create_run(state, identity, {'runtime_id': state.runtime_id})
        failed = state.advance(Phase.FAILED, last_error=Failure(code='MODEL_FAILURE', detail=SECRET,
                                                              retryable=False, evidence_refs=[]))
        self.store.save_state(state, failed)
        self.store.finish(failed, 'failed', 'unknown')
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id='job-declared', session_id='session-fixture', run_id=state.run_id,
                              kind='hello', lease_id=state.owner_lease_id, generation=0, status='failed', real_model=1,
                              created_at='2026-09-27T00:00:00Z', updated_at='2026-09-27T00:00:00Z', runtime_id=state.runtime_id)
            self.store.insert('model_calls', call_id='call-declared', run_id=state.run_id, step_id=state.step_id,
                              deployment_id=state.deployment_id, role='system1', request_json=canonical({'synthetic': SECRET}),
                              response_json=None, status='error', created_at='2026-09-27T00:00:00Z')
        projected = self.service.preview('job-declared')
        self.assertEqual(projected['candidate']['model_roles']['system1']['calls']['error'], 1)
        self.assertTrue(projected['candidate']['model_roles']['system1']['real_model'])
        self.assertNotIn(SECRET, canonical(projected))
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET role='system2' WHERE call_id='call-declared'")
        with self.assertRaises(ValueError):
            self.service.preview('job-declared')
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET role='system1' WHERE call_id='call-declared'")
            self.store.connection.execute("UPDATE deployments SET config_sha256=? WHERE deployment_id=?",
                                          ('d' * 64, state.deployment_id))
        with self.assertRaises(ValueError):
            self.service.preview('job-declared')

    def test_private_file_symlink_hardlink_and_public_modes_rejected(self):
        saved = self.save()
        path = self.service.directory / (saved['candidate_sha256'] + '.json')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.preview()
        path.chmod(0o600)
        linked = self.root / 'linked'
        os.link(path, linked)
        with self.assertRaises(ValueError):
            self.preview()
        linked.unlink()
        path.rename(linked)
        path.symlink_to(linked)
        with self.assertRaises(OSError):
            self.preview()

    def test_canonical_schema_and_example(self):
        schema = json.loads((REPO_ROOT / 'schemas/failure_improvement.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  **FailureImprovement.model_json_schema()})
        jsonschema.Draft202012Validator(schema).validate(self.preview())
        example = json.loads((REPO_ROOT / 'examples/failure_improvement.json').read_text())
        self.assertTrue(example['candidate']['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(example)

    def test_numeric_boolean_tampering_is_not_normalized(self):
        saved = self.save()
        path = self.service.directory / (saved['candidate_sha256'] + '.json')
        original = path.read_text()
        for field, number in (('metadata_only', 1), ('training_ready', 0), ('gold', 0)):
            changed = json.loads(original)
            changed['candidate'][field] = number
            path.write_text(canonical(changed))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.preview()
        path.write_text(original)

    def test_external_lock_fails_without_waiting(self):
        saved = self.save()
        descriptor = os.open(self.service.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.service.review('job-fixture', saved['candidate_sha256'], **saved['review_options'][0])
        finally:
            os.close(descriptor)

    def test_missing_saved_candidate_is_not_repaired_from_source(self):
        reviewed = self.review()
        path = self.service.directory / (reviewed['candidate_sha256'] + '.json')
        path.unlink()
        with self.assertRaises(ValueError):
            self.service.save('job-fixture', reviewed['candidate_sha256'], True)
        self.assertFalse(path.exists())


class ActualSchedulerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'store.sqlite')
        self.runtime = FixtureDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FaultEngine())
        self.service = FailureImprovementService(self.settings.database, self.root / 'failures', self.controller.session_id)

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    async def test_actual_fixture_failure_to_review_no_reexecution(self):
        control = self.controller.state()
        self.scheduler.start(control['lease_id'], control['generation'])
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        job = self.scheduler.status()['jobs'][0]
        self.assertEqual(job['status'], 'failed')
        before = self.store.connection.total_changes
        preview = self.service.preview(job['job_id'])
        saved = self.service.save(job['job_id'], preview['candidate_sha256'], True)
        reviewed = self.service.review(job['job_id'], saved['candidate_sha256'], **saved['review_options'][0])
        self.service.revoke(job['job_id'], saved['candidate_sha256'], reviewed['review']['receipt_sha256'])
        self.assertEqual(before, self.store.connection.total_changes)
        self.assertFalse(self.scheduler.busy)
        self.assertNotIn(SECRET, canonical(preview))
        self.assertEqual(preview['candidate']['failure_code'], 'MODEL_FAILURE')

    async def test_actual_cancelled_waiting_engine_is_metadata_only(self):
        engine = WaitingEngine()
        self.scheduler.engine = engine
        control = self.controller.state()
        self.scheduler.start(control['lease_id'], control['generation'])
        await asyncio.wait_for(engine.started.wait(), 2)
        await self.scheduler.cancel()
        job = self.scheduler.status()['jobs'][0]
        self.assertEqual(job['status'], 'cancelled')
        result = self.service.preview(job['job_id'])
        self.assertEqual(result['candidate']['outcome'], 'cancelled')
        self.assertIsNone(result['candidate']['failure_code'])

    async def test_actual_rejected_approval_remains_human_guidance_not_model_blame(self):
        self.scheduler.engine = FixtureDecisionEngine()
        control = self.controller.state()
        self.scheduler.start(control['lease_id'], control['generation'])
        approval = None
        for attempt in range(200):
            approval = self.scheduler.status()['approval']
            if approval is not None:
                break
            await asyncio.sleep(.001)
        self.assertIsNotNone(approval)
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], False)
        await asyncio.gather(self.scheduler.task, return_exceptions=True)
        job = self.scheduler.status()['jobs'][0]
        self.assertEqual(job['status'], 'cancelled')
        result = self.service.preview(job['job_id'])
        self.assertEqual(result['candidate']['approval_counts']['rejected'], 1)
        self.assertFalse(result['candidate']['failure_attribution_verified'])
        self.assertFalse(result['candidate']['training_ready'])
