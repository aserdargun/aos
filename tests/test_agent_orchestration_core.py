import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from aos.agent_contracts import (
    AgentAdmissionError, AgentAuthority, AgentBudget, AgentCleanupProof, AgentHandle,
    AgentJobRequest, AgentJobStatus, AgentObservation, AgentPrepared, AgentRegistration, AgentResultProof,
)
from aos.agent_orchestration import AgentOrchestrator
from aos.agent_orchestration_store import AgentOrchestrationStore


class SyntheticScriptedAdapter:
    def __init__(self):
        self.calls = []
        self.wait_approval = False
        self.approved = False
        self.terminal = False
        self.result_verified = True
        self.cleanup_verified = True
        self.cleanup_scope = 'local_process'
        self.fail_dispatch = False
        self.cancel_needs_approval = False
        self.cancel_approved = False
        self.cancelled = False
        self.on_dispatch = None
        self.on_cleanup = None
        self.dispatch_gate = None

    async def prepare(self, request):
        self.calls.append('prepare')
        handle = AgentHandle(job_id=request.job_id, request_sha256=request.request_sha256(),
            authority=request.authority, handle_id=request.job_id, payload={'synthetic': True})
        return AgentPrepared(handle=handle, ready=not self.wait_approval)

    async def dispatch(self, prepared):
        self.calls.append('dispatch')
        if self.on_dispatch:
            self.on_dispatch()
        if self.dispatch_gate:
            await self.dispatch_gate.wait()
        if self.fail_dispatch:
            raise OSError('Synthetic lost acknowledgement')
        return prepared.handle

    async def observe(self, handle):
        self.calls.append('observe')
        if self.cancelled:
            state = 'cancelled'
        elif self.terminal:
            state = 'succeeded'
        elif 'cancel_action' in handle.payload:
            state = 'ready' if self.cancel_approved else 'waiting_approval'
        elif self.wait_approval:
            state = 'ready' if self.approved else 'waiting_approval'
        else:
            state = 'running'
        return AgentObservation(handle=handle, state=state)

    async def request_cancel(self, handle):
        self.calls.append('cancel')
        if self.cancel_needs_approval and 'cancel_action' not in handle.payload:
            updated = handle.model_copy(update={'payload': {**handle.payload, 'cancel_action': 'synthetic-stop'}})
            return AgentObservation(handle=updated, state='waiting_approval')
        self.cancelled = True
        return AgentObservation(handle=handle, state='cancelled')

    async def verify_result(self, handle):
        self.calls.append('verify_result')
        return AgentResultProof(handle=handle, verified=self.result_verified,
            outcome='cancelled' if self.cancelled else 'succeeded',
            evidence_sha256='a' * 64 if self.result_verified else None)

    async def verify_cleanup(self, handle):
        self.calls.append('verify_cleanup')
        if self.on_cleanup:
            self.on_cleanup()
        return AgentCleanupProof(handle=handle, verified=self.cleanup_verified, scope=self.cleanup_scope,
                                 evidence_sha256='b' * 64 if self.cleanup_verified else None)


class AgentOrchestrationCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'delegated.sqlite3'
        self.store = AgentOrchestrationStore(self.path)
        self.authority = AgentAuthority(principal='synthetic-principal', runtime_id='synthetic-runtime',
                                       lease_id='synthetic-lease', owner='AGENT', generation=1)
        self.adapter = SyntheticScriptedAdapter()
        self.runner = AgentOrchestrator(self.store, adapters={'synthetic': self.adapter},
                                       current_authority=lambda request: self.authority)
        self.registration = AgentRegistration(agent_id='synthetic-agent', version='1', adapter_kind='synthetic',
            capabilities=['synthetic.work'], max_active_instances=2,
            per_job_budget=AgentBudget(wall_seconds=10, model_tokens=100, experiments=1),
            aggregate_budget=AgentBudget(wall_seconds=20, model_tokens=200, experiments=2),
            cleanup_scope='local_process')
        self.runner.register(self.registration)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def request(self, job_id='job-one', **updates):
        base = AgentJobRequest(job_id=job_id, agent_id='synthetic-agent', agent_version='1',
            operation='synthetic.work', authority=self.authority,
            budget=AgentBudget(wall_seconds=10, model_tokens=100, experiments=1), payload={'synthetic': True})
        return AgentJobRequest.model_validate({**base.model_dump(), **updates})

    async def start(self, job_id='job-one'):
        self.runner.submit(self.request(job_id))
        self.assertEqual((await self.runner.advance(job_id)).state, 'prepared')
        self.assertEqual((await self.runner.advance(job_id)).state, 'running')

    def test_private_separate_store_single_writer_and_foreign_database_rejected(self):
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with self.assertRaises(BlockingIOError):
            AgentOrchestrationStore(self.path)
        foreign = Path(self.temporary.name) / 'foreign.sqlite3'
        descriptor = os.open(foreign, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
            connection.commit()
        with self.assertRaises(sqlite3.OperationalError):
            AgentOrchestrationStore(foreign)

    def test_symlink_and_nonprivate_file_rejected(self):
        symbolic = Path(self.temporary.name) / 'symbolic.sqlite3'
        symbolic.symlink_to(self.path)
        with self.assertRaises(OSError):
            AgentOrchestrationStore(symbolic)
        loose = Path(self.temporary.name) / 'loose.sqlite3'
        loose.touch(mode=0o644)
        loose.chmod(0o644)
        with self.assertRaises(AgentAdmissionError):
            AgentOrchestrationStore(loose)

    def test_registration_request_and_sql_audit_immutable(self):
        self.assertEqual(self.runner.register(self.registration), self.runner.register(self.registration))
        with self.assertRaises(AgentAdmissionError):
            self.runner.register(self.registration.model_copy(update={'max_active_instances': 3}))
        self.runner.submit(self.request())
        self.runner.submit(self.request())
        with self.assertRaises(AgentAdmissionError):
            self.runner.submit(self.request(payload={'changed': True}))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE agent_jobs SET request_json='{}',revision=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('DELETE FROM agent_job_events')

    def test_default_authority_and_unknown_adapter_deny(self):
        denied = AgentOrchestrator(self.store, adapters={'synthetic': self.adapter})
        with self.assertRaises(AgentAdmissionError):
            denied.submit(self.request())
        with self.assertRaises(AgentAdmissionError):
            denied.register(self.registration.model_copy(update={'adapter_kind': 'missing'}))
        self.assertEqual(self.adapter.calls, [])

    def test_insert_or_replace_cannot_bypass_immutable_identity_or_events(self):
        self.runner.submit(self.request())
        for table in ['agent_registrations', 'agent_jobs', 'agent_job_events']:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}')
        self.assertEqual(self.runner.status('job-one').state, 'queued')

    def test_strict_capability_budget_dependencies_and_roles(self):
        for changes in [{'operation': 'unauthorized'}, {'dependencies': ['missing']}]:
            with self.assertRaises(AgentAdmissionError):
                self.runner.submit(self.request(**changes))
        with self.assertRaises(AgentAdmissionError):
            self.runner.submit(self.request(budget={'wall_seconds': 11, 'model_tokens': 100, 'experiments': 1}))
        for changes in [{'budget': {'wall_seconds': True, 'model_tokens': 0, 'experiments': 0}},
                        {'delegated_by': 'external-agent'}, {'dependencies': ['job-one']}]:
            with self.assertRaises(ValidationError):
                self.request(**changes)
        with self.assertRaises((ValidationError, ValueError)):
            self.request(payload={'nonfinite': float('nan')}).request_sha256()

    async def test_verified_result_and_cleanup_release_capacity(self):
        await self.start()
        self.adapter.terminal = True
        status = await self.runner.advance('job-one')
        self.assertEqual(status.state, 'succeeded')
        self.assertFalse(status.reservation_held)
        self.assertTrue(status.result_proof.verified)
        self.assertTrue(status.cleanup_proof.verified)
        self.assertEqual(status.result_proof.handle, status.handle)

    async def test_dependency_can_queue_but_cannot_start_until_verified_cleanup(self):
        self.runner.submit(self.request('parent'))
        self.runner.submit(self.request('child', dependencies=['parent']))
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('child')
        self.assertEqual(self.adapter.calls, [])
        await self.runner.advance('parent')
        await self.runner.advance('parent')
        self.adapter.terminal = True
        self.adapter.cleanup_verified = False
        self.assertEqual((await self.runner.advance('parent')).state, 'verifying')
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('child')
        self.adapter.cleanup_verified = True
        self.assertEqual((await self.runner.reconcile('parent')).state, 'succeeded')
        self.assertEqual((await self.runner.advance('child')).state, 'prepared')

    async def test_capacity_and_aggregate_reservations_are_atomic(self):
        for job_id in ['one', 'two', 'three']:
            self.runner.submit(self.request(job_id))
        await asyncio.gather(self.runner.advance('one'), self.runner.advance('two'))
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('three')
        self.assertEqual(self.runner.status('three').state, 'queued')
        self.assertEqual(self.adapter.calls.count('prepare'), 2)

    async def test_aggregate_budget_limits_even_when_instance_slots_remain(self):
        registration = self.registration.model_copy(update={'version': '2', 'max_active_instances': 3})
        self.runner.register(registration)
        for job_id in ['one', 'two', 'three']:
            self.runner.submit(self.request(job_id, agent_version='2'))
        await self.runner.advance('one')
        await self.runner.advance('two')
        with self.assertRaisesRegex(AgentAdmissionError, 'Aggregate'):
            await self.runner.advance('three')

    async def test_waiting_approval_reuses_durable_preparation_after_reopen(self):
        self.adapter.wait_approval = True
        self.runner.submit(self.request())
        self.assertEqual((await self.runner.advance('job-one')).state, 'waiting_approval')
        await self.runner.advance('job-one')
        self.store.close()
        self.store = AgentOrchestrationStore(self.path)
        self.runner = AgentOrchestrator(self.store, adapters={'synthetic': self.adapter},
                                       current_authority=lambda request: self.authority)
        self.adapter.approved = True
        self.assertEqual((await self.runner.advance('job-one')).state, 'prepared')
        await self.runner.advance('job-one')
        self.assertEqual(self.adapter.calls.count('prepare'), 1)
        self.assertEqual(self.adapter.calls.count('dispatch'), 1)

    async def test_dispatch_intent_committed_before_external_call(self):
        def inspect_committed():
            with closing(sqlite3.connect(self.path)) as reader:
                self.assertEqual(reader.execute('SELECT state,reservation_held FROM agent_jobs').fetchone(),
                                 ('dispatch_intent', 1))
        self.adapter.on_dispatch = inspect_committed
        await self.start()

    async def test_dispatch_handle_evolution_clears_preparation_observation(self):
        self.adapter.wait_approval = True
        self.adapter.approved = True
        dispatch = self.adapter.dispatch
        async def evolved(prepared):
            handle = await dispatch(prepared)
            return handle.model_copy(update={'payload': {**handle.payload, 'remote_run_id': 'synthetic-remote'}})
        self.adapter.dispatch = evolved
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        self.assertEqual((await self.runner.advance('job-one')).state, 'prepared')
        self.assertIsNotNone(self.runner.status('job-one').observation)
        status = await self.runner.advance('job-one')
        self.assertEqual(status.state, 'running')
        self.assertEqual(status.handle.payload['remote_run_id'], 'synthetic-remote')
        self.assertIsNone(status.observation)
        self.assertIsNone(status.result_proof)
        self.assertIsNone(status.cleanup_proof)

    async def test_lost_dispatch_ack_never_replays_and_holds_capacity(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        self.adapter.fail_dispatch = True
        with self.assertRaises(OSError):
            await self.runner.advance('job-one')
        self.assertEqual(self.runner.status('job-one').state, 'uncertain')
        self.assertTrue(self.runner.status('job-one').reservation_held)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        await self.runner.reconcile('job-one')
        self.assertEqual(self.runner.status('job-one').state, 'uncertain')
        self.assertEqual(self.adapter.calls.count('dispatch'), 1)

    async def test_prepare_failure_survives_reopen_without_duplicate_proposal(self):
        async def lost_prepare(request):
            self.adapter.calls.append('prepare')
            with closing(sqlite3.connect(self.path)) as reader:
                self.assertEqual(reader.execute('SELECT state,reservation_held FROM agent_jobs').fetchone(),
                                 ('prepare_intent', 1))
            raise OSError('Synthetic proposal acknowledgement lost')
        self.adapter.prepare = lost_prepare
        self.runner.submit(self.request())
        with self.assertRaises(OSError):
            await self.runner.advance('job-one')
        self.store.close()
        self.store = AgentOrchestrationStore(self.path)
        self.runner = AgentOrchestrator(self.store, adapters={'synthetic': self.adapter},
                                       current_authority=lambda request: self.authority)
        self.assertTrue(self.runner.status('job-one').reservation_held)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        with self.assertRaises(AgentAdmissionError):
            await self.runner.reconcile('job-one')
        self.assertEqual(self.adapter.calls, ['prepare'])

    async def test_dispatch_result_write_failure_keeps_uncertainty_and_capacity(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        change = self.store.change
        def fail_acknowledgement(job_id, **updates):
            if updates.get('state') == 'running':
                raise sqlite3.OperationalError('Synthetic durable acknowledgement failure')
            return change(job_id, **updates)
        self.store.change = fail_acknowledgement
        with self.assertRaises(sqlite3.OperationalError):
            await self.runner.advance('job-one')
        self.assertEqual(self.runner.status('job-one').state, 'uncertain')
        self.assertTrue(self.runner.status('job-one').reservation_held)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.assertEqual(self.adapter.calls.count('dispatch'), 1)

    async def test_reopen_all_interrupted_effects_uncertain_never_replayed(self):
        for job_id in ['preparing', 'dispatching', 'cancelling']:
            self.runner.submit(self.request(job_id))
        self.store.reserve('preparing')
        await self.runner.advance('dispatching')
        with self.store.transaction():
            self.store.change('dispatching', state='dispatch_intent')
        self.registration = self.registration.model_copy(update={'agent_id': 'second-agent'})
        self.runner.register(self.registration)
        self.runner.submit(self.request('other-cancel', agent_id='second-agent'))
        await self.runner.advance('other-cancel')
        with self.store.transaction():
            self.store.change('other-cancel', state='cancel_intent')
        calls = list(self.adapter.calls)
        self.store.close()
        self.store = AgentOrchestrationStore(self.path)
        self.runner = AgentOrchestrator(self.store, adapters={'synthetic': self.adapter},
                                       current_authority=lambda request: self.authority)
        for job_id in ['preparing', 'dispatching', 'other-cancel']:
            self.assertEqual(self.runner.status(job_id).state, 'uncertain')
            self.assertTrue(self.runner.status(job_id).reservation_held)
            with self.assertRaises(AgentAdmissionError):
                await self.runner.advance(job_id)
        self.assertEqual(self.adapter.calls, calls)

    async def test_principal_owner_generation_or_lease_change_denies_effect(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        original = self.authority
        for changes in [{'principal': 'other'}, {'owner': 'HUMAN'}, {'generation': 2}, {'lease_id': 'new'}]:
            self.authority = original.model_copy(update=changes)
            with self.assertRaises(AgentAdmissionError):
                await self.runner.advance('job-one')
        self.assertEqual(self.adapter.calls, ['prepare'])

    async def test_post_effect_fence_change_is_uncertain_not_success(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        self.adapter.on_dispatch = lambda: setattr(self, 'authority', self.authority.model_copy(update={'generation': 2}))
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.assertEqual(self.runner.status('job-one').state, 'uncertain')
        self.assertTrue(self.runner.status('job-one').reservation_held)

    async def test_async_cancelled_wait_retains_intent_and_no_second_dispatch(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        self.adapter.dispatch_gate = asyncio.Event()
        task = asyncio.create_task(self.runner.advance('job-one'))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runner.status('job-one').state, 'uncertain')
        self.assertTrue(self.runner.status('job-one').reservation_held)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.assertEqual(self.adapter.calls.count('dispatch'), 1)

    async def test_cancel_proposal_waits_and_persists_before_fresh_approved_effect(self):
        await self.start()
        self.adapter.cancel_needs_approval = True
        status = await self.runner.request_cancel('job-one')
        self.assertEqual(status.state, 'cancel_requested')
        self.assertEqual(status.handle.payload['cancel_action'], 'synthetic-stop')
        self.assertTrue(status.reservation_held)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.request_cancel('job-one')
        self.adapter.cancel_approved = True
        await self.runner.advance('job-one')
        self.assertFalse(self.adapter.cancelled)
        status = await self.runner.request_cancel('job-one')
        self.assertEqual(status.state, 'cancelled')
        self.assertFalse(status.reservation_held)
        self.assertEqual(self.adapter.calls.count('cancel'), 2)

    async def test_queued_cancel_has_no_adapter_or_reservation(self):
        self.runner.submit(self.request())
        self.assertEqual((await self.runner.request_cancel('job-one')).state, 'cancelled')
        self.assertEqual(self.adapter.calls, [])
        self.assertFalse(self.runner.status('job-one').reservation_held)

    async def test_unverified_cleanup_or_result_holds_capacity_and_no_success(self):
        await self.start()
        self.adapter.terminal = True
        self.adapter.cleanup_verified = False
        self.assertEqual((await self.runner.advance('job-one')).state, 'verifying')
        self.adapter.cleanup_verified = True
        self.adapter.result_verified = False
        self.assertEqual((await self.runner.reconcile('job-one')).state, 'verifying')
        self.assertTrue(self.runner.status('job-one').reservation_held)

    async def test_cleanup_scope_and_final_authority_are_not_interchangeable(self):
        await self.start()
        self.adapter.terminal = True
        self.adapter.cleanup_scope = 'scientist_gpu'
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.assertTrue(self.runner.status('job-one').reservation_held)
        self.adapter.cleanup_scope = 'local_process'
        self.adapter.on_cleanup = lambda: setattr(self, 'authority', self.authority.model_copy(update={'generation': 2}))
        with self.assertRaises(AgentAdmissionError):
            await self.runner.reconcile('job-one')
        self.assertTrue(self.runner.status('job-one').reservation_held)

    async def test_handle_or_proof_from_another_job_rejected(self):
        await self.start()
        original = self.adapter.observe
        async def foreign(handle):
            observation = await original(handle)
            return observation.model_copy(update={'handle': handle.model_copy(update={'job_id': 'foreign'})})
        self.adapter.observe = foreign
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.assertTrue(self.runner.status('job-one').reservation_held)

    async def test_sql_cannot_release_capacity_without_cleanup_proof(self):
        await self.start()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction():
                self.store.change('job-one', state='failed', reservation_held=False)
        self.assertTrue(self.runner.status('job-one').reservation_held)

    async def test_corrupted_stored_proof_cannot_authorize_dependency(self):
        await self.start()
        self.adapter.terminal = True
        await self.runner.advance('job-one')
        status = self.runner.status('job-one')
        altered = status.result_proof.model_dump(mode='json')
        altered['handle']['request_sha256'] = 'c' * 64
        for trigger in ['agent_job_identity', 'agent_job_transition']:
            self.store.connection.execute(f'DROP TRIGGER {trigger}')
        self.store.connection.execute('UPDATE agent_jobs SET result_proof_json=? WHERE job_id=?',
                                      (json.dumps(altered), 'job-one'))
        with self.assertRaises(AgentAdmissionError):
            self.runner.status('job-one')
        with self.assertRaises(AgentAdmissionError):
            self.runner.submit(self.request('child', dependencies=['job-one']))

    async def test_same_job_parallel_advance_is_rejected(self):
        self.runner.submit(self.request())
        await self.runner.advance('job-one')
        self.adapter.dispatch_gate = asyncio.Event()
        task = asyncio.create_task(self.runner.advance('job-one'))
        await asyncio.sleep(0)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('job-one')
        self.adapter.dispatch_gate.set()
        await task

    def test_canonical_schemas_and_synthetic_examples_match_models(self):
        root = Path(__file__).resolve().parents[1]
        models = {'agent_registration': AgentRegistration, 'agent_job_request': AgentJobRequest,
                  'agent_handle': AgentHandle, 'agent_prepared': AgentPrepared,
                  'agent_observation': AgentObservation, 'agent_result_proof': AgentResultProof,
                  'agent_cleanup_proof': AgentCleanupProof, 'agent_job_status': AgentJobStatus}
        for name, model in models.items():
            self.assertEqual(json.loads((root / f'schemas/{name}.schema.json').read_text()), model.model_json_schema())
            example = json.loads((root / f'examples/{name}.json').read_text())
            model.model_validate(example, strict=True)


if __name__ == '__main__':
    unittest.main()
