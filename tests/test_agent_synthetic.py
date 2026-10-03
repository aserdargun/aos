import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema

from aos.agent_contracts import AgentAdmissionError, AgentAuthority, AgentBudget, AgentJobRequest
from aos.agent_synthetic import SyntheticAgentAdapter, SyntheticAgentPayload
from aos.contracts import REPO_ROOT


class SyntheticAgentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = SyntheticAgentAdapter(self.root)
        self.authority = AgentAuthority(principal='synthetic', runtime_id='synthetic-runtime',
                                        lease_id='synthetic-lease', owner='AGENT', generation=1)

    async def asyncTearDown(self):
        await self.adapter.close()
        self.temporary.cleanup()

    def request(self, job_id='synthetic-one', *, delay_ms=0, wall_seconds=5):
        return AgentJobRequest(job_id=job_id, agent_id='synthetic', agent_version='1.0',
            operation='synthetic.write.v1', authority=self.authority,
            budget=AgentBudget(wall_seconds=wall_seconds, model_tokens=0, experiments=0),
            payload={'text': 'Explicitly synthetic CPU result', 'delay_ms': delay_ms})

    async def test_canonical_payload_schema_and_explicitly_synthetic_example(self):
        schema = json.loads((REPO_ROOT / 'schemas/agent_synthetic_payload.schema.json').read_text())
        example = json.loads((REPO_ROOT / 'examples/agent_synthetic_payload.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(example)
        schema.pop('$schema')
        self.assertEqual(schema, SyntheticAgentPayload.model_json_schema())
        self.assertIn('synthetic', SyntheticAgentPayload.model_validate(example).text)

    async def wait_terminal(self, handle):
        async with asyncio.timeout(5):
            while (observation := await self.adapter.observe(handle)).state == 'running':
                await asyncio.sleep(0.02)
        return observation

    async def test_two_isolated_processes_cancel_one_without_affecting_other(self):
        first = await self.adapter.dispatch(await self.adapter.prepare(self.request(delay_ms=300)))
        second = await self.adapter.dispatch(await self.adapter.prepare(self.request('synthetic-two', delay_ms=150)))
        self.assertNotEqual(first.handle_id, second.handle_id)
        self.assertNotEqual(self.adapter._owned[first.handle_id].process.pid,
                            self.adapter._owned[second.handle_id].process.pid)
        self.assertFalse((await self.adapter.verify_cleanup(first)).verified)
        self.assertEqual((await self.adapter.request_cancel(first)).state, 'cancelled')
        self.assertEqual((await self.wait_terminal(second)).state, 'succeeded')
        for handle, outcome in ((first, 'cancelled'), (second, 'succeeded')):
            proof = await self.adapter.verify_result(handle)
            self.assertTrue(proof.verified)
            self.assertEqual(proof.outcome, outcome)
            self.assertTrue((await self.adapter.verify_cleanup(handle)).verified)

    async def test_tampered_result_is_not_independently_verified(self):
        handle = await self.adapter.dispatch(await self.adapter.prepare(self.request()))
        await self.wait_terminal(handle)
        result = self.root / handle.handle_id / 'result.json'
        result.write_text('{}')
        self.assertFalse((await self.adapter.verify_result(handle)).verified)
        self.assertTrue((await self.adapter.verify_cleanup(handle)).verified)

    async def test_duplicate_dispatch_and_reopened_adapter_never_replay(self):
        prepared = await self.adapter.prepare(self.request(delay_ms=100))
        handle = await self.adapter.dispatch(prepared)
        with self.assertRaises(FileExistsError):
            await self.adapter.dispatch(prepared)
        reopened = SyntheticAgentAdapter(self.root)
        try:
            self.assertEqual((await reopened.observe(handle)).state, 'uncertain')
            self.assertEqual((await reopened.request_cancel(handle)).state, 'uncertain')
            self.assertFalse((await reopened.verify_cleanup(handle)).verified)
            with self.assertRaises(FileExistsError):
                await reopened.dispatch(prepared)
        finally:
            await reopened.close()
        self.assertEqual((await self.wait_terminal(handle)).state, 'succeeded')

    async def test_wall_budget_stops_fixed_worker_without_result(self):
        handle = await self.adapter.dispatch(await self.adapter.prepare(self.request(delay_ms=2000, wall_seconds=1)))
        self.assertEqual((await self.wait_terminal(handle)).state, 'failed')
        self.assertTrue((await self.adapter.verify_cleanup(handle)).verified)
        self.assertFalse((self.root / handle.handle_id / 'result.json').exists())

    async def test_altered_authority_and_symlinked_artifacts_fail_closed(self):
        prepared = await self.adapter.prepare(self.request())
        changed = prepared.handle.model_copy(update={'authority': self.authority.model_copy(update={'generation': 2})})
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.request_cancel(changed)
        workspace = self.root / prepared.handle.handle_id
        (workspace / 'request.json').rename(workspace / 'original.json')
        (workspace / 'request.json').symlink_to(workspace / 'original.json')
        with self.assertRaises(OSError):
            await self.adapter.dispatch(prepared)
        self.assertEqual(self.adapter._owned, {})

    async def test_fixed_operation_rejects_model_resources_and_unbounded_payload(self):
        original = self.request()
        for changed in (
            original.model_copy(update={'operation': 'shell'}),
            original.model_copy(update={'payload': original.payload | {'command': 'false'}}),
            original.model_copy(update={'budget': original.budget.model_copy(update={'model_tokens': 1})}),
            original.model_copy(update={'payload': {'text': 'A' * 4097, 'delay_ms': 0}}),
        ):
            with self.assertRaises(ValueError):
                await self.adapter.prepare(changed)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_replaced_workspace_cannot_control_owned_process(self):
        prepared = await self.adapter.prepare(self.request(delay_ms=200))
        handle = await self.adapter.dispatch(prepared)
        workspace = self.root / handle.handle_id
        request_bytes = (workspace / 'request.json').read_bytes()
        workspace.rename(self.root / 'old-owned-workspace')
        workspace.mkdir(mode=0o700)
        replacement = workspace / 'request.json'
        replacement.write_bytes(request_bytes)
        replacement.chmod(0o600)
        with self.assertRaises(AgentAdmissionError):
            await self.adapter.request_cancel(handle)
        self.assertFalse(self.adapter._owned[handle.handle_id].cancelled)


if __name__ == '__main__':
    unittest.main()
