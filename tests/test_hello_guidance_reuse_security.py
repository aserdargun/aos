import asyncio
import unittest
from unittest.mock import patch

from aos.failure_guidance import FailureGuidanceContext
import test_hello_guidance_reuse as reuse_fixtures


class HelloGuidanceReuseSecurityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = reuse_fixtures.HelloGuidanceReuseTests.asyncSetUp
    asyncTearDown = reuse_fixtures.HelloGuidanceReuseTests.asyncTearDown
    authority = reuse_fixtures.HelloGuidanceReuseTests.authority
    approval = reuse_fixtures.HelloGuidanceReuseTests.approval
    source = reuse_fixtures.HelloGuidanceReuseTests.source
    request = reuse_fixtures.HelloGuidanceReuseTests.request
    published = reuse_fixtures.HelloGuidanceReuseTests.published
    revoke = reuse_fixtures.HelloGuidanceReuseTests.revoke

    async def test_queued_revocation_before_engine_coroutine_blocks_model_call(self):
        selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        preview = service.reuse_preview(saved['entry_sha256'])
        original = FailureGuidanceContext.record
        original_wait = asyncio.wait_for

        def revoke_at_next_loop_turn(context, state, options, call_id):
            original(context, state, options, call_id)
            asyncio.get_running_loop().call_soon(self.revoke, selection)

        async def yield_before_model(awaitable, timeout):
            await asyncio.sleep(0)
            return await original_wait(awaitable, timeout)

        with patch('aos.operator.asyncio.wait_for', yield_before_model):
            with patch.object(FailureGuidanceContext, 'record', revoke_at_next_loop_turn):
                self.scheduler.start_hello_guidance_reuse(**self.request(preview))
                await original_wait(self.scheduler.task, 5)
        self.assertEqual(self.engine.states, [])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_approvals').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertFalse(service.inspect(saved['entry_sha256'])['current_valid'])

    async def test_workspace_replacement_during_approval_blocks_both_workspaces(self):
        unused_selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        started = self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
        approval = await self.approval()
        original = self.runtime.root
        backup = original.with_name(original.name + '-pinned')
        original.rename(backup)
        original.mkdir()
        try:
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
            await self.scheduler.task
            self.assertFalse((original / 'hello.txt').exists())
            self.assertFalse((backup / 'hello.txt').exists())
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
            report = service.report(started['job_id'])
            self.assertTrue(report['entry_binding_verified'])
            self.assertFalse(report['current_entry_valid'])
            self.assertEqual(report['guidance']['followup']['outcome']['status'], 'not_verified')
        finally:
            original.rmdir()
            backup.rename(original)

    async def test_revocation_during_engine_call_blocks_new_approval(self):
        selection, unused_publication, saved = await self.published()
        service = self.scheduler.hello_guidance_reuse
        original = self.engine.decide

        async def revoke_during_call(state, options):
            self.revoke(selection)
            return await original(state, options)

        with patch.object(self.engine, 'decide', revoke_during_call):
            self.scheduler.start_hello_guidance_reuse(**self.request(service.reuse_preview(saved['entry_sha256'])))
            await self.scheduler.task
        self.assertEqual(len(self.engine.states), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_approvals').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())


if __name__ == '__main__':
    unittest.main()
