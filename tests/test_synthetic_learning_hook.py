import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from aos.computer import WorkspaceRuntime
from aos.contracts import Settings
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.storage import TrajectoryStore


class FixtureDesktop(WorkspaceRuntime):
    pins = {'image_id': 'synthetic-image'}
    container_id = None

    def perform(self, tool, arguments):
        if tool == 'probe':
            return {'display': True, 'xfce': True, 'note': True, 'vnc': 'RFB synthetic'}
        raise AssertionError('Unexpected fixture tool')


class SyntheticLearningHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'trajectory.sqlite')
        self.runtime = FixtureDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.scheduler = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                                          synthetic_learning_stream_dir=self.root / 'learning')

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    def start(self, *, learning_metadata=False):
        state = self.controller.state()
        return self.scheduler.start(state['lease_id'], state['generation'],
                                    learning_metadata=learning_metadata)['job_id']

    async def approval(self):
        for _ in range(200):
            approval = self.scheduler.status()['approval']
            if approval:
                return approval
            await asyncio.sleep(.005)
        self.fail('No approval request produced')

    async def test_default_is_off_and_opt_in_requires_configured_private_outbox(self):
        self.scheduler.synthetic_learning_stream_dir = None
        state = self.controller.state()
        with self.assertRaisesRegex(Exception, 'not configured'):
            self.scheduler.start(state['lease_id'], state['generation'], learning_metadata=True)
        job_id = self.start()
        approval = await self.approval()
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.learning_status(job_id), {'enabled': False, 'state': 'disabled'})
        self.assertFalse((self.root / 'learning').exists())
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_events WHERE kind LIKE 'learning_stream_%'").fetchone()[0], 0)

    async def test_agent_workspace_cannot_host_private_learning_outbox(self):
        with self.assertRaisesRegex(ValueError, 'outside the agent workspace'):
            DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                             synthetic_learning_stream_dir=self.settings.workspace / 'learning')

    async def test_opt_in_polls_after_committed_decision_and_after_verification(self):
        job_id = self.start(learning_metadata=True)
        pending = await self.approval()
        progress = self.scheduler.learning_status(job_id)
        self.assertEqual(progress['state'], 'collecting')
        self.assertEqual(progress['phase'], 'pre_approval')
        self.assertEqual(progress['entries_by_role'], {'system1': 0, 'system2': 0})
        self.assertGreaterEqual(progress['duration_ms'], 0)
        self.assertFalse(self.store.connection.in_transaction)
        self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        final = self.scheduler.learning_status(job_id)
        self.assertEqual((final['state'], final['phase'], final['total_entries']), ('synced', 'settled', 0))
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_events WHERE kind='learning_stream_poll'").fetchone()[0], 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertTrue((self.root / 'learning' / 'learning-stream.sqlite').is_file())
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='cancelled' WHERE run_id=(SELECT run_id FROM desktop_tasks WHERE job_id=?)",
                                          (job_id,))
        self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'recovery_required')

    async def test_outbox_failure_is_audited_but_cannot_change_verified_task_result(self):
        self.scheduler.synthetic_learning_stream_dir = self.settings.database
        job_id = self.start(learning_metadata=True)
        pending = await self.approval()
        failure = self.scheduler.learning_status(job_id)
        self.assertEqual(failure['state'], 'failed')
        self.assertEqual(failure['reason'], 'unsafe_or_unavailable')
        self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'failed')
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM desktop_events WHERE kind='learning_stream_failure'").fetchone()[0], 1)

    async def test_mismatched_configured_trajectory_cannot_be_collected(self):
        self.scheduler.settings = Settings(workspace=self.settings.workspace, database=self.root / 'other.sqlite')
        job_id = self.start(learning_metadata=True)
        pending = await self.approval()
        self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'failed')
        self.assertFalse((self.root / 'learning').exists())
        self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')

    async def test_pause_resume_repolls_same_run_without_claiming_terminal_sync(self):
        job_id = self.start(learning_metadata=True)
        first = await self.approval()
        self.controller.control('pause')
        await self.scheduler.pause()
        self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'paused')
        resumed = self.controller.control('resume')
        self.scheduler.resume(resumed['lease_id'], resumed['generation'])
        second = await self.approval()
        self.assertEqual(first['action']['run_id'], second['action']['run_id'])
        self.scheduler.respond(second['approval_id'], second['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'synced')

    async def test_missing_settlement_receipt_requires_recovery_after_scheduler_recreation(self):
        original = self.scheduler._learning_poll

        def interrupted(job_id, run_id, phase):
            if phase != 'settled':
                original(job_id, run_id, phase)

        with patch.object(self.scheduler, '_learning_poll', side_effect=interrupted):
            job_id = self.start(learning_metadata=True)
            pending = await self.approval()
            self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
            await self.scheduler.task
        recreated = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                                     synthetic_learning_stream_dir=self.root / 'learning')
        self.assertEqual(recreated.learning_status(job_id)['state'], 'recovery_required')
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')

    async def test_failed_receipt_write_is_visible_now_and_requires_recovery_after_restart(self):
        original = self.store.insert

        def fail_poll_receipt(table, **values):
            if table == 'desktop_events' and values.get('kind') == 'learning_stream_poll':
                raise OSError('synthetic receipt write failure')
            original(table, **values)

        with patch.object(self.store, 'insert', side_effect=fail_poll_receipt):
            job_id = self.start(learning_metadata=True)
            pending = await self.approval()
            self.assertEqual(self.scheduler.learning_status(job_id)['state'], 'failed_unpersisted')
            self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
            await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')
        recreated = DesktopScheduler(self.controller, self.settings, FixtureDecisionEngine(),
                                     synthetic_learning_stream_dir=self.root / 'learning')
        self.assertEqual(recreated.learning_status(job_id)['state'], 'recovery_required')

    async def test_authenticated_exact_job_status_and_api_opt_in_validation(self):
        origin = 'http://127.0.0.1:8765'
        app = create_console(self.controller, 'synthetic-token', origin, self.root,
                             self.settings.database, self.scheduler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                     headers={'Origin': origin}) as client:
            self.assertEqual((await client.get('/api/tasks/learning/job-' + '0' * 32)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            state = self.controller.state()
            payload = {'kind': 'hello', 'lease_id': state['lease_id'],
                       'generation': state['generation'], 'learning_metadata': True}
            self.assertEqual((await client.post('/api/tasks', json={**payload, 'learning_metadata': 'true'})).status_code, 400)
            response = await client.post('/api/tasks', json=payload)
            self.assertEqual(response.status_code, 200)
            job_id = response.json()['job_id']
            self.assertEqual((await client.get('/api/tasks/learning/not-a-job')).status_code, 404)
            pending = await self.approval()
            self.scheduler.respond(pending['approval_id'], pending['action_sha256'], True)
            await self.scheduler.task
            report = (await client.get('/api/tasks/learning/' + job_id)).json()
            self.assertEqual((report['enabled'], report['state']), (True, 'synced'))


@unittest.skipUnless(os.environ.get('AOS_REAL_TASK_TESTS') == '1',
                     'Requires explicit pinned real Decider and owned desktop opt-in')
class RealSyntheticLearningHookTests(unittest.TestCase):
    def test_real_decider_hello_emits_s1_only_after_opt_in(self):
        from aos.contracts import REPO_ROOT
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='learning-real-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            arguments = ('--synthetic-learning-stream-dir', str(root / 'learning-stream'))
            with task_server(root, 'decider', arguments) as (origin, token, client, server):
                control = client.get('/api/state').json()['control']
                started = client.post('/api/tasks', json={'kind': 'hello', 'lease_id': control['lease_id'],
                                                          'generation': control['generation'],
                                                          'learning_metadata': True})
                started.raise_for_status()
                job_id = started.json()['job_id']
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    if status['approval'] is not None:
                        approval = status['approval']
                        client.post('/api/approvals/' + approval['approval_id'], json={
                            'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                        break
                    if not status['busy']:
                        self.fail('Real Decider ended before approval')
                    time.sleep(.1)
                else:
                    self.fail('Real Decider approval timed out')
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    if not status['busy']:
                        break
                    time.sleep(.1)
                else:
                    self.fail('Real Decider settlement timed out')
                job = status['jobs'][0]
                self.assertEqual((job['job_id'], job['status'], job['real_model']), (job_id, 'succeeded', 1))
                learning = job['learning_metadata']
                self.assertEqual(learning['state'], 'synced')
                self.assertGreaterEqual(learning['entries_by_role']['system1'], 1)
                self.assertEqual(learning['entries_by_role']['system2'], 0)
                trace = client.get('/api/runs/' + job['run_id']).json()
                self.assertEqual([call['role'] for call in trace['model_calls']], ['system1'])
                self.assertEqual([verification['result'] for verification in trace['verifications']], ['passed'])

    def test_real_bonsai_and_decider_vision_emits_separate_roles(self):
        from aos.contracts import REPO_ROOT
        from test_desktop_task_ui import task_server

        with tempfile.TemporaryDirectory(prefix='learning-vision-real-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            arguments = ('--browser-tasks', '--vision-engine', 'bonsai',
                         '--synthetic-learning-stream-dir', str(root / 'learning-stream'))
            with task_server(root, 'decider', arguments) as (origin, token, client, server):
                control = client.get('/api/state').json()['control']
                started = client.post('/api/tasks', json={
                    'kind': 'vision_canvas', 'lease_id': control['lease_id'],
                    'generation': control['generation'], 'learning_metadata': True})
                started.raise_for_status()
                job_id = started.json()['job_id']
                deadline = time.monotonic() + 180
                approved = False
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    approval = status['approval']
                    if approval is not None and not approved:
                        self.assertEqual(approval['action']['tool'], 'vision.click')
                        client.post('/api/approvals/' + approval['approval_id'], json={
                            'action_sha256': approval['action_sha256'], 'accept': True}).raise_for_status()
                        approved = True
                    if not status['busy']:
                        break
                    time.sleep(.1)
                else:
                    self.fail('Real Bonsai/Decider vision settlement timed out')
                self.assertTrue(approved, status)
                job = status['jobs'][0]
                self.assertEqual((job['job_id'], job['status'], job['real_model']),
                                 (job_id, 'succeeded', 1))
                learning = job['learning_metadata']
                self.assertEqual(learning['state'], 'synced')
                self.assertGreaterEqual(learning['entries_by_role']['system1'], 1)
                self.assertGreaterEqual(learning['entries_by_role']['system2'], 1)
                trace = client.get('/api/runs/' + job['run_id']).json()
                self.assertEqual([call['role'] for call in trace['model_calls']], ['system2', 'system1'])
                self.assertEqual([call['status'] for call in trace['model_calls']], ['ok', 'ok'])
                self.assertEqual([verification['result'] for verification in trace['verifications']], ['passed'])
                with closing(sqlite3.connect(root / 'learning-stream/learning-stream.sqlite')) as connection:
                    entries = [json.loads(row[0]) for row in connection.execute(
                        'SELECT entry_json FROM entries WHERE run_id=? ORDER BY entry_id', (job['run_id'],))]
                self.assertEqual(sorted((entry['role'], entry['kind']) for entry in entries),
                                 [('system1', 'call_observed'), ('system1', 'verification_linked'),
                                  ('system2', 'call_observed'), ('system2', 'downstream_verification_linked')])
                downstream = next(entry for entry in entries
                                  if entry['kind'] == 'downstream_verification_linked')
                self.assertEqual(downstream['source']['verification_id'], trace['verifications'][0]['verification_id'])
                self.assertFalse(downstream['training_ready'])
                self.assertFalse(downstream['collection_authorized'])
                from aos.vision_skill_candidate import (derive_vision_dual_role_candidate,
                                                         derive_vision_skill_candidate)

                candidate = derive_vision_skill_candidate(root / 'store.sqlite', job['run_id'])
                self.assertEqual(candidate['model_role'], 'system2')
                self.assertFalse(candidate['supervisor_outcome_verified'])
                self.assertTrue(candidate['downstream_operator_outcome_verified'])
                self.assertFalse(candidate['activation_authorized'])
                selected = subprocess.run([sys.executable, '-m', 'aos.vision_skill_candidate',
                                           '--database', str(root / 'store.sqlite'), '--run-id', job['run_id'],
                                           '--snapshot-sha256', candidate['snapshot_sha256']],
                                          capture_output=True, text=True, timeout=10)
                self.assertEqual(selected.returncode, 0, selected.stderr)
                self.assertEqual(json.loads(selected.stdout), candidate)
                self.assertNotIn(job['run_id'], selected.stdout)
                pair = derive_vision_dual_role_candidate(
                    root / 'store.sqlite', job['run_id'], snapshot_sha256=candidate['snapshot_sha256'])
                self.assertEqual(pair['snapshot_sha256'], candidate['snapshot_sha256'])
                self.assertEqual(pair['supervisor'], candidate)
                self.assertEqual(pair['operator']['decision_ref'], candidate['operator_decision_ref'])
                self.assertEqual(pair['operator']['verification_ref'], candidate['downstream_verification_ref'])
                self.assertTrue(pair['operator']['outcome_verified'])
                self.assertFalse(pair['operator']['training_ready'])
                paired = subprocess.run([sys.executable, '-m', 'aos.vision_skill_candidate',
                                         '--database', str(root / 'store.sqlite'), '--run-id', job['run_id'],
                                         '--snapshot-sha256', candidate['snapshot_sha256'], '--dual-role'],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(paired.returncode, 0, paired.stderr)
                self.assertEqual(json.loads(paired.stdout), pair)
                self.assertNotIn(job['run_id'], paired.stdout)
                with closing(sqlite3.connect(root / 'store.sqlite')) as connection:
                    for table, column, condition, replacement in (
                        ('model_calls', 'request_json', "role='system2'", '{}'),
                        ('observations', 'payload_json', "kind='vision.scene'", '{}'),
                        ('human_interventions', 'actor', "kind='approve'", 'task_scoped_auto_approval'),
                        ('verifications', 'actual_json', "method='independent_canvas_equals'", '{}'),
                        ('verifications', 'method', "result='passed'", 'untrusted_verifier'),
                        ('verifications', 'evidence_refs_json', "result='passed'", '[]'),
                        ('actions', 'result_json', "tool='vision.verify'", '{}'),
                        ('observations', 'payload_json', "kind='vision.outcome'", '{}'),
                        ('actions', 'arguments_json', "tool='vision.click'", '{}'),
                    ):
                        row = connection.execute(
                            f'SELECT rowid,{column} FROM {table} WHERE run_id=? AND {condition}',
                            (job['run_id'],)).fetchone()
                        self.assertIsNotNone(row)
                        with self.subTest(table=table):
                            connection.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                                               (replacement, row[0]))
                            connection.commit()
                            with self.assertRaises((ValueError, KeyError, TypeError, ValidationError)):
                                derive_vision_skill_candidate(root / 'store.sqlite', job['run_id'])
                            connection.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                                               (row[1], row[0]))
                            connection.commit()


if __name__ == '__main__':
    unittest.main()
