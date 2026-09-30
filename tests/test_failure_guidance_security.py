import unittest
from unittest.mock import patch

from aos.contracts import AOSFault
import test_failure_guidance as guidance_fixtures


class FailureGuidanceSecurityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guidance_fixtures.FailureGuidanceTests.asyncSetUp
    asyncTearDown = guidance_fixtures.FailureGuidanceTests.asyncTearDown
    authority = guidance_fixtures.FailureGuidanceTests.authority
    approval = guidance_fixtures.FailureGuidanceTests.approval
    source = guidance_fixtures.FailureGuidanceTests.source
    request = guidance_fixtures.FailureGuidanceTests.request
    started = guidance_fixtures.FailureGuidanceTests.started

    async def test_failed_guidance_admission_rolls_back_job_and_followup_admission(self):
        selection = await self.source()
        preview = self.scheduler.preview_failure_guidance(**selection)
        with patch.object(self.scheduler.failure_guidance, 'admit', side_effect=ValueError('synthetic admission failure')):
            with self.assertRaises(ValueError):
                self.scheduler.start_failure_guidance(**self.request(preview))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_events WHERE kind IN ('failure_followup_admission','failure_guidance_admission')").fetchone()[0], 0)
        self.assertEqual(self.scheduler._failure_followup_jobs, {})
        self.assertEqual(self.scheduler._failure_guidance_jobs, {})
        self.assertEqual(self.engine.states, [])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_revocation_during_model_call_blocks_approval_and_effect(self):
        selection = await self.source()
        preview = self.scheduler.preview_failure_guidance(**selection)
        original = self.engine.decide

        async def revoke_during_call(state, options):
            self.scheduler.failure_improvements.revoke(
                selection['source_job_id'], selection['candidate_sha256'], selection['receipt_sha256'])
            return await original(state, options)

        with patch.object(self.engine, 'decide', revoke_during_call):
            self.scheduler.start_failure_guidance(**self.request(preview))
            await self.scheduler.task
        self.assertEqual(len(self.engine.states), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_approvals').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_cancelled_job_cannot_reuse_context_or_approval(self):
        unused_selection, unused_preview, started = await self.started()
        approval = await self.approval()
        await self.scheduler.cancel('stop')
        with self.assertRaises(AOSFault):
            self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        with self.assertRaises(AOSFault):
            self.scheduler.check_failure_guidance(started['job_id'], require_context=True)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())


if __name__ == '__main__':
    unittest.main()
