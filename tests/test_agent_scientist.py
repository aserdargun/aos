import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import jsonschema

from aos.agent_contracts import (
    AgentAdmissionError, AgentAuthority, AgentBudget, AgentCleanupProof,
    AgentJobRequest, AgentPrepared, AgentRegistration,
)
from aos.agent_orchestration import AgentOrchestrator
from aos.agent_orchestration_store import AgentOrchestrationStore
from aos.agent_scientist import ScientistAgentAdapter, ScientistAgentHandlePayload, ScientistAgentPayload
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.scientist_lab import ScientistLabClient, ScientistLabUncertain
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore
from test_scientist_lab import SyntheticLabServer


class ScientistAgentAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_schemas_and_explicitly_synthetic_examples(self):
        root = Path(__file__).resolve().parents[1]
        for model, name in ((ScientistAgentPayload, 'agent_scientist_payload'),
                            (ScientistAgentHandlePayload, 'agent_scientist_handle_payload')):
            schema = json.loads((root / 'schemas' / (name + '.schema.json')).read_text())
            example = json.loads((root / 'examples' / (name + '.json')).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(example, schema)
            model.model_validate(example, strict=True)
            schema.pop('$schema')
            self.assertEqual(schema, model.model_json_schema())
        invalid = dict(example, stop_action_id='action-' + 'a' * 32)
        with self.assertRaises(ValueError):
            ScientistAgentHandlePayload.model_validate(invalid, strict=True)

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TrajectoryStore(self.root / 'synthetic-lab.sqlite3')
        runtime = SimpleNamespace(runtime_id='runtime-synthetic', pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.server = SyntheticLabServer()
        token = self.root / 'synthetic.token'
        token.write_text('synthetic-test-token\n')
        token.chmod(0o600)
        self.remote = ScientistLabClient(self.server.url, token, principal_id='synthetic-aos',
            allowed_suites=frozenset({'synthetic.allowed.v1'}))
        self.service = ScientistLabService(self.controller, self.remote,
            authorization_context_sha256='a' * 64, program_version='director.v1',
            verify_capability=lambda *arguments: None)
        self.adapter = ScientistAgentAdapter(self.service)
        app = create_console(self.controller, 'synthetic-console-token', 'http://testserver',
            self.root, scientist_lab=self.service, web_profiles_root=self.root / 'profiles')
        self.console = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver')
        self.headers = {'Origin': 'http://testserver'}
        await self.console.post('/api/login', headers=self.headers, json={'token': 'synthetic-console-token'})
        self.orchestration_store = AgentOrchestrationStore(self.root / 'synthetic-orchestration.sqlite3')
        self.runner = AgentOrchestrator(self.orchestration_store, adapters={'scientist': self.adapter},
                                       current_authority=lambda request: self.authority())
        budget = AgentBudget(wall_seconds=30, model_tokens=100, experiments=1)
        self.runner.register(AgentRegistration(agent_id='scientist', version='v1', adapter_kind='scientist',
            capabilities=['scientist.lab.start.v1'], max_active_instances=1, per_job_budget=budget,
            aggregate_budget=budget, cleanup_scope='scientist_gpu'))

    async def asyncTearDown(self):
        await self.console.aclose()
        self.server.close()
        self.orchestration_store.close()
        self.store.close()
        self.temporary.cleanup()

    def authority(self):
        current = self.controller.state()
        return AgentAuthority(principal='synthetic-aos', **{key: current[key]
            for key in ('runtime_id', 'lease_id', 'owner', 'generation')})

    def request(self, job_id='scientist-one'):
        return AgentJobRequest(job_id=job_id, agent_id='scientist', agent_version='v1',
            operation='scientist.lab.start.v1', authority=self.authority(),
            budget=AgentBudget(wall_seconds=30, model_tokens=100, experiments=1),
            payload={'suite': 'synthetic.allowed.v1', 'track': 'anomaly', 'program_version': 'director.v1'})

    async def approve(self, handle, *, stop=False):
        action_id = handle.payload['stop_action_id' if stop else 'action_id']
        envelope_sha256 = handle.payload['stop_envelope_sha256' if stop else 'envelope_sha256']
        response = await self.console.post('/api/scientist/approve', headers=self.headers,
            json={'action_id': action_id, 'envelope_sha256': envelope_sha256, 'accept': True})
        self.assertEqual(response.status_code, 200, response.text)

    async def started(self):
        prepared = await self.adapter.prepare(self.request())
        await self.approve(prepared.handle)
        observation = await self.adapter.observe(prepared.handle)
        self.assertEqual(observation.state, 'ready')
        return await self.adapter.dispatch(AgentPrepared(handle=observation.handle, ready=True))

    def posts(self, suffix):
        return [entry for entry in self.server.requests if entry[0] == 'POST' and entry[1].endswith(suffix)]

    async def test_no_approval_denies_even_forged_ready_and_exact_envelope_is_required(self):
        prepared = await self.adapter.prepare(self.request())
        self.assertFalse(prepared.ready)
        self.assertEqual((await self.adapter.observe(prepared.handle)).state, 'waiting_approval')
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.dispatch(AgentPrepared(handle=prepared.handle, ready=True))
        altered = prepared.handle.model_copy(update={'payload': prepared.handle.payload | {'envelope_sha256': 'f' * 64}})
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.observe(altered)
        self.assertEqual(self.server.requests, [])

    async def test_approved_start_once_report_verified_but_capacity_and_GPU_release_held(self):
        self.runner.submit(self.request())
        status = await self.runner.advance('scientist-one')
        self.assertEqual(status.state, 'waiting_approval')
        await self.approve(status.handle)
        self.assertEqual((await self.runner.advance('scientist-one')).state, 'prepared')
        running = await self.runner.advance('scientist-one')
        self.assertEqual(running.state, 'running')
        self.assertEqual(running.handle.payload['remote_run_id'], self.server.run_id)
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.dispatch(AgentPrepared(handle=running.handle, ready=True))
        self.assertEqual(len(self.posts('/v1/runs')), 1)
        self.server.state = 'completed'
        verified = await self.runner.advance('scientist-one')
        self.assertEqual(verified.state, 'verifying')
        self.assertTrue(verified.result_proof.verified)
        self.assertEqual(verified.result_proof.handle, verified.handle)
        self.assertFalse(verified.cleanup_proof.verified)
        self.assertEqual(verified.cleanup_proof.scope, 'scientist_gpu')
        self.assertTrue(verified.reservation_held)
        self.assertNotIn('report', verified.handle.payload)
        self.assertEqual(self.service.readbacks.inventory(self.controller.session_id,
            running.handle.handle_id)['items'], [])
        self.runner.submit(self.request('scientist-two'))
        with self.assertRaises(AgentAdmissionError):
            await self.runner.advance('scientist-two')
        self.assertEqual(len(self.posts('/v1/runs')), 1)

    async def test_job_STOP_requires_separate_console_approval_and_explicit_second_cancel(self):
        self.runner.submit(self.request())
        initial = await self.runner.advance('scientist-one')
        await self.approve(initial.handle)
        await self.runner.advance('scientist-one')
        await self.runner.advance('scientist-one')
        waiting = await self.runner.request_cancel('scientist-one')
        self.assertEqual(waiting.observation.state, 'waiting_approval')
        self.assertEqual(self.posts('/stop'), [])
        self.assertFalse((await self.adapter.verify_cleanup(waiting.handle)).verified)
        with self.assertRaises(AgentAdmissionError):
            await self.runner.request_cancel('scientist-one')
        await self.approve(waiting.handle, stop=True)
        ready = await self.runner.advance('scientist-one')
        self.assertEqual(ready.observation.state, 'ready')
        self.assertEqual(self.posts('/stop'), [])
        stopped = await self.runner.request_cancel('scientist-one')
        self.assertEqual(stopped.observation.state, 'running')
        self.assertTrue(stopped.reservation_held)
        self.assertEqual(len(self.posts('/stop')), 1)
        await self.adapter.request_cancel(stopped.handle)
        self.assertEqual(len(self.posts('/stop')), 1)
        self.assertFalse(self.service._closing)
        self.assertFalse(self.remote._closed)
        self.server.state = 'stopped'
        terminal = await self.runner.advance('scientist-one')
        self.assertEqual(terminal.result_proof.outcome, 'cancelled')
        self.assertTrue(terminal.result_proof.verified)
        self.assertFalse(terminal.cleanup_proof.verified)
        self.assertTrue(terminal.reservation_held)

    async def test_takeover_and_new_generation_are_not_adopted_for_reads_or_STOP(self):
        handle = await self.started()
        before = list(self.server.requests)
        self.controller.control('take-control')
        for operation in (self.adapter.observe, self.adapter.request_cancel,
                          self.adapter.verify_result, self.adapter.verify_cleanup):
            with self.assertRaises(AgentAdmissionError):
                await operation(handle)
        self.assertEqual(self.server.requests, before)
        self.assertGreater(self.service._load_task(handle.handle_id).binding.generation,
                           handle.payload['original_binding']['generation'])

    async def test_takeover_after_start_request_begins_is_uncertain_and_never_replayed(self):
        prepared = await self.adapter.prepare(self.request())
        await self.approve(prepared.handle)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.adapter.dispatch(AgentPrepared(handle=prepared.handle, ready=True)))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 2))
        self.controller.control('take-control')
        self.server.release.set()
        with self.assertRaises(ScientistLabUncertain):
            await pending
        self.assertEqual(len(self.posts('/v1/runs')), 1)
        row = self.store.connection.execute('SELECT state FROM scientist_lab_actions WHERE action_id=?',
            (prepared.handle.payload['action_id'],)).fetchone()
        self.assertEqual(row['state'], 'intent')

    async def test_lost_ack_is_service_wide_uncertain_and_never_replayed(self):
        prepared = await self.adapter.prepare(self.request())
        await self.approve(prepared.handle)
        self.server.mode = 'lost_ack'
        with self.assertRaises(ScientistLabUncertain):
            await self.adapter.dispatch(AgentPrepared(handle=prepared.handle, ready=True))
        self.assertEqual((await self.adapter.observe(prepared.handle)).state, 'uncertain')
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.dispatch(AgentPrepared(handle=prepared.handle, ready=True))
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.prepare(self.request('scientist-two'))
        self.assertEqual(len(self.posts('/v1/runs')), 1)

    async def test_controls_are_serialized_for_the_same_injected_client(self):
        handle = await self.started()
        other = ScientistAgentAdapter(self.service)
        self.assertIs(other._controls, self.adapter._controls)
        original = self.remote.execute_async
        active = 0
        maximum = 0

        async def counted(task, action):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02)
                return await original(task, action)
            finally:
                active -= 1

        with patch.object(self.remote, 'execute_async', counted):
            observations = await asyncio.gather(self.adapter.observe(handle), other.observe(handle))
        self.assertEqual([item.state for item in observations], ['running', 'running'])
        self.assertEqual(maximum, 1)

    async def test_bad_report_does_not_prove_result_or_retain_content(self):
        handle = await self.started()
        self.server.state = 'completed'
        self.server.mode = 'bad_report_hash'
        self.assertFalse((await self.adapter.verify_result(handle)).verified)
        self.assertFalse((await self.adapter.verify_cleanup(handle)).verified)
        self.assertEqual(self.service.readbacks.inventory(self.controller.session_id, handle.handle_id)['items'], [])

    async def test_rejected_STOP_observes_actual_job_without_inventing_failure_or_new_STOP(self):
        handle = await self.started()
        proposal = await self.adapter.request_cancel(handle)
        response = await self.console.post('/api/scientist/approve', headers=self.headers,
            json={'action_id': proposal.handle.payload['stop_action_id'],
                  'envelope_sha256': proposal.handle.payload['stop_envelope_sha256'], 'accept': False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.adapter.observe(proposal.handle)).state, 'running')
        self.assertEqual((await self.adapter.request_cancel(proposal.handle)).state, 'running')
        self.assertEqual(self.posts('/stop'), [])
        self.server.state = 'completed'
        self.assertEqual((await self.adapter.observe(proposal.handle)).state, 'succeeded')
        self.assertTrue((await self.adapter.verify_result(proposal.handle)).verified)
        self.assertFalse((await self.adapter.verify_cleanup(proposal.handle)).verified)

    async def test_trusted_cleanup_prover_must_bind_exact_handle_and_GPU_scope(self):
        handle = await self.started()

        async def trusted(current):
            return AgentCleanupProof(handle=current, verified=True, scope='scientist_gpu', evidence_sha256='c' * 64)

        adapter = ScientistAgentAdapter(self.service, cleanup_prover=trusted)
        proof = await adapter.verify_cleanup(handle)
        self.assertTrue(proof.verified)
        self.assertEqual(proof.handle, handle)

        async def altered(current):
            return AgentCleanupProof(handle=current.model_copy(update={'handle_id': 'foreign'}),
                verified=True, scope='scientist_gpu', evidence_sha256='c' * 64)

        adapter = ScientistAgentAdapter(self.service, cleanup_prover=altered)
        with self.assertRaises(AgentAdmissionError):
            await adapter.verify_cleanup(handle)

    async def test_strict_payload_budget_and_principal_are_denied_before_proposal(self):
        request = self.request()
        for changed in (
            request.model_copy(update={'payload': request.payload | {'approval': True}}),
            request.model_copy(update={'operation': 'lab.stop'}),
            request.model_copy(update={'budget': request.budget.model_copy(update={'experiments': 0})}),
            request.model_copy(update={'budget': request.budget.model_copy(update={'experiments': 36})}),
            request.model_copy(update={'budget': request.budget.model_copy(update={'wall_seconds': 14401})}),
            request.model_copy(update={'budget': request.budget.model_copy(update={'model_tokens': 350001})}),
            request.model_copy(update={'authority': request.authority.model_copy(update={'principal': 'foreign'})}),
        ):
            with self.assertRaises(ValueError):
                await self.adapter.prepare(changed)
        self.assertEqual(self.service.inventory()['jobs'], [])
        self.assertEqual(self.server.requests, [])


if __name__ == '__main__':
    unittest.main()
