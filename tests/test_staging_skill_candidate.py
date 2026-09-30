import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from aos.contracts import REPO_ROOT
from aos.dataset import validator
from aos.reusable_decider import ReusableDeciderEngine
from aos.staging_skill_candidate import StagingSkillCandidate, derive_staging_skill_candidate


class StagingSkillCandidateContractTests(unittest.TestCase):
    def test_schema_and_synthetic_fixture_are_content_free_and_non_authorizing(self):
        schema = json.loads((REPO_ROOT / 'schemas/staging_skill_candidate.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                                  **StagingSkillCandidate.model_json_schema()})
        fixture = json.loads((REPO_ROOT / 'examples/staging_skill_candidate.json').read_text())
        self.assertTrue(fixture['synthetic'])
        validator('staging_skill_candidate').validate(fixture['candidate'])
        for changed in ({'activation_authorized': True}, {'training_ready': True},
                        {'model_role': 'system2'}, {'raw_page_text': 'private'}):
            self.assertFalse(validator('staging_skill_candidate').is_valid({**fixture['candidate'], **changed}))
        missing = dict(fixture['candidate'])
        del missing['activation_authorized']
        self.assertFalse(validator('staging_skill_candidate').is_valid(missing))

    def test_invalid_source_is_not_opened_or_created(self):
        missing = REPO_ROOT / 'data' / 'missing-staging-skill-candidate-test.sqlite'
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            derive_staging_skill_candidate(missing, '../wrong')
        with self.assertRaises(ValueError):
            derive_staging_skill_candidate(missing, 'run-valid', snapshot_sha256='wrong')
        self.assertFalse(missing.exists())
        completed = subprocess.run([sys.executable, '-m', 'aos.staging_skill_candidate', '--database', str(missing),
                                    '--run-id', 'run-valid'], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, '')
        self.assertNotIn(str(missing), completed.stderr)
        self.assertFalse(missing.exists())


REAL_ENABLED = (os.environ.get('AOS_DESKTOP_TESTS') == '1'
                and os.environ.get('AOS_DESKTOP_MCP_TESTS') == '1'
                and os.environ.get('AOS_REAL_BROWSER_TASK_TESTS') == '1')


@unittest.skipUnless(REAL_ENABLED, 'Requires owned Ubuntu/Chromium/MCP and real pinned Decider opt-in')
class StagingSkillCandidateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_visible_scheduler import VisibleSchedulerTests

        await VisibleSchedulerTests.asyncSetUp(self)
        self.scheduler.desktop_staging_mcp_manifest = REPO_ROOT / 'models/desktop-mcp-v001/manifest.json'
        self.scheduler.engine = ReusableDeciderEngine(
            REPO_ROOT / 'models/decider-manifest.json',
            Path(os.environ.get('AOS_MODEL_PYTHON', str(Path.home() / '.venv/bin/python'))))

    async def asyncTearDown(self):
        from test_visible_scheduler import VisibleSchedulerTests

        await VisibleSchedulerTests.asyncTearDown(self)

    async def approval(self, tool):
        from test_visible_scheduler import VisibleSchedulerTests

        return await VisibleSchedulerTests.approval(self, tool)

    def accept(self, pending):
        from test_visible_scheduler import VisibleSchedulerTests

        return VisibleSchedulerTests.accept(self, pending)

    async def completed_run(self):
        state = self.controller.state()
        self.scheduler.start(state['lease_id'], state['generation'], 'browser_staging_workflow')
        for tool in ('browser.staging.open', 'browser.staging.follow',
                     'browser.staging.fill', 'browser.staging.submit'):
            self.accept(await self.approval(tool))
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        return self.scheduler.status()['jobs'][0]['run_id']

    async def test_real_source_yields_only_content_free_unreviewed_s1_candidate(self):
        run_id = await self.completed_run()
        report = derive_staging_skill_candidate(self.settings.database, run_id)
        validator('staging_skill_candidate').validate(report)
        self.assertEqual(report['model_role'], 'system1')
        self.assertEqual([step['stage'] for step in report['steps']],
                         ['open_app', 'follow_draft', 'fill_message', 'submit_draft'])
        self.assertEqual((report['approved_action_count'], report['independent_verification_count'],
                          report['observed_submission_count']), (4, 4, 1))
        self.assertFalse(report['activation_authorized'])
        self.assertFalse(report['training_ready'])
        self.assertEqual(report, derive_staging_skill_candidate(
            self.settings.database, run_id, snapshot_sha256=report['snapshot_sha256']))
        with self.assertRaisesRegex(ValueError, 'staging_snapshot_changed'):
            derive_staging_skill_candidate(self.settings.database, run_id, snapshot_sha256='0' * 64)
        encoded = json.dumps(report)
        for private in (run_id, 'Hello from the local agent.', 'Synthetic draft',
                        self.settings.database.as_posix(), 'snapshot_id', 'element_id'):
            self.assertNotIn(private, encoded)
        completed = subprocess.run([sys.executable, '-m', 'aos.staging_skill_candidate',
                                    '--database', str(self.settings.database), '--run-id', run_id,
                                    '--snapshot-sha256', report['snapshot_sha256']],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), report)

    async def test_model_approval_and_readback_tampering_fail_closed(self):
        run_id = await self.completed_run()
        baseline = derive_staging_skill_candidate(self.settings.database, run_id)
        cases = (
            ('model_calls', 'request_json', 'call_id', 'not-a-matching-input', '{"state":"changed"}'),
            ('desktop_approvals', 'status', 'approval_id', 'approval-used', 'revoked'),
            ('human_interventions', 'actor', 'intervention_id', 'human-actor', 'task_scoped_auto_approval'),
            ('verifications', 'actual_json', 'verification_id', 'verification-result', '{}'),
            ('observations', 'payload_json', 'observation_id', 'independent-readback', '{}'),
        )
        connection = self.store.connection
        for table, column, identity_column, label, replacement in cases:
            selector = ("WHERE action_id IS NOT NULL" if table == 'observations' else '')
            row = connection.execute(f'SELECT {identity_column},{column} FROM {table} {selector} LIMIT 1').fetchone()
            with self.subTest(label=label):
                with connection:
                    connection.execute(f'UPDATE {table} SET {column}=? WHERE {identity_column}=?',
                                       (replacement, row[identity_column]))
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    derive_staging_skill_candidate(self.settings.database, run_id)
                if table == 'model_calls':
                    rejected = subprocess.run([sys.executable, '-m', 'aos.staging_skill_candidate',
                                               '--database', str(self.settings.database), '--run-id', run_id],
                                              capture_output=True, text=True, timeout=10)
                    self.assertEqual(rejected.returncode, 1)
                    self.assertEqual(rejected.stdout, '')
                    self.assertNotIn(str(self.settings.database), rejected.stderr)
                with connection:
                    connection.execute(f'UPDATE {table} SET {column}=? WHERE {identity_column}=?',
                                       (row[column], row[identity_column]))
        refreshed = derive_staging_skill_candidate(self.settings.database, run_id)
        self.assertEqual({key: value for key, value in refreshed.items() if key != 'snapshot_sha256'},
                         {key: value for key, value in baseline.items() if key != 'snapshot_sha256'})


if __name__ == '__main__':
    unittest.main()
