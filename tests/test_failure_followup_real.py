from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_FAILURE_FOLLOWUP_REAL_TESTS') == '1',
                     'Requires explicit pinned Decider and isolated Docker follow-up acceptance')
class FailureFollowupRealTests(unittest.TestCase):
    def test_cancel_before_effect_then_separately_approved_real_decider_followup(self):
        from test_desktop_task_ui import task_server

        root = Path(tempfile.mkdtemp(prefix='failure-followup-real-', dir=REPO_ROOT / 'data'))
        root.chmod(0o700)
        started_at = time.perf_counter()
        with task_server(root, 'decider') as (origin, token, client, server):
            def post(path, value):
                response = client.post(path, json=value)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            def control():
                current = client.get('/api/state').json()['control']
                return {key: current[key] for key in ('lease_id', 'generation')}

            def wait_for(job_id, approval=False):
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    job = next(item for item in status['jobs'] if item['job_id'] == job_id)
                    if approval and status['approval'] is not None:
                        self.assertEqual(status['approval']['job_id'], job_id)
                        return status, job
                    if job['status'] in {'succeeded', 'failed', 'cancelled'} and not status['reserved']:
                        self.assertFalse(approval, 'Real model task terminated before manual approval')
                        return status, job
                    time.sleep(.05)
                self.fail('Isolated real-model task did not reach its required boundary')

            def query(statement, parameters=()):
                with closing(sqlite3.connect(f'file:{root / "store.sqlite"}?mode=ro', uri=True)) as database:
                    return database.execute(statement, parameters).fetchall()

            source = post('/api/tasks', {'kind': 'hello', 'approve_all': False, **control()})
            source_status, source_job = wait_for(source['job_id'], approval=True)
            source_approval = source_status['approval']
            self.assertEqual(source_approval['action']['tool'], 'filesystem.write')
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertEqual(query('SELECT count(*) FROM actions WHERE run_id=?', (source_job['run_id'],)), [(0,)])
            post('/api/approvals/' + source_approval['approval_id'], {
                'action_sha256': source_approval['action_sha256'], 'accept': False})
            unused_status, source_job = wait_for(source['job_id'])
            self.assertEqual(source_job['status'], 'cancelled')
            self.assertEqual(source_job['real_model'], 1)
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertFalse((root / 'failure-improvements').exists())
            self.assertFalse((root / 'failure-followups').exists())

            source_selection = {'schema_version': '1.0', 'job_id': source['job_id']}
            candidate = post('/api/tasks/failure-improvement/preview', source_selection)
            roles = candidate['candidate']['model_roles']
            self.assertTrue(roles['system1']['real_model'])
            self.assertEqual(roles['system1']['calls'], {'ok': 1, 'error': 0, 'timeout': 0, 'cancelled': 0})
            self.assertIsNone(roles['system2']['deployment_id'])
            self.assertEqual(candidate['candidate']['approval_counts']['rejected'], 1)
            self.assertFalse((root / 'failure-improvements').exists())
            selected_candidate = {**source_selection, 'candidate_sha256': candidate['candidate_sha256']}
            save_request = {**source_selection, 'confirm_sha256': candidate['candidate_sha256'], **control()}
            denied = client.post('/api/tasks/failure-improvement/save', json={
                **save_request, 'consent': False})
            self.assertEqual(denied.status_code, 409)
            post('/api/tasks/failure-improvement/save', {**save_request, 'consent': True})
            choice = next(option for option in candidate['review_options']
                          if option['decision'] == 'accept' and option['correction_code'] == 'refresh_observation')
            review = post('/api/tasks/failure-improvement/review', {**selected_candidate, **choice, **control()})
            followup_selection = {'schema_version': '1.0', 'source_job_id': source['job_id'],
                                  'candidate_sha256': review['candidate_sha256'],
                                  'receipt_sha256': review['review']['receipt_sha256']}
            preview = post('/api/tasks/failure-followup/preview', followup_selection)
            self.assertEqual(preview['target']['system1_deployment_id'], roles['system1']['deployment_id'])
            self.assertFalse((root / 'failure-followups').exists())
            admission = {'schema_version': '1.0', 'preview': preview,
                         'confirm_sha256': preview['confirm_sha256'], 'consent': True, **control()}
            for changes in ({'consent': False}, {'confirm_sha256': '0' * 64}):
                denied = client.post('/api/tasks/failure-followup/start', json=admission | changes)
                self.assertEqual(denied.status_code, 409)
            self.assertEqual(query('SELECT count(*) FROM desktop_tasks'), [(1,)])
            followup = post('/api/tasks/failure-followup/start', admission)
            self.assertNotEqual(followup['job_id'], source['job_id'])
            self.assertTrue(followup['manual_approval_required'])
            followup_status, followup_job = wait_for(followup['job_id'], approval=True)
            followup_approval = followup_status['approval']
            self.assertEqual(followup_approval['action']['tool'], 'filesystem.write')
            self.assertNotEqual(followup_approval['approval_id'], source_approval['approval_id'])
            self.assertNotEqual(followup_approval['action_sha256'], source_approval['action_sha256'])
            self.assertIsNone(followup_status['auto_approval'])
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertEqual(query('SELECT count(*) FROM actions WHERE run_id=?', (followup_job['run_id'],)), [(0,)])
            incomplete = post('/api/tasks/failure-followup/inspect', {
                'schema_version': '1.0', 'job_id': followup['job_id']})
            self.assertEqual(incomplete['outcome']['status'], 'not_verified')
            old_approval = client.post('/api/approvals/' + source_approval['approval_id'], json={
                'action_sha256': source_approval['action_sha256'], 'accept': True})
            self.assertEqual(old_approval.status_code, 409)
            post('/api/approvals/' + followup_approval['approval_id'], {
                'action_sha256': followup_approval['action_sha256'], 'accept': True})
            unused_status, followup_job = wait_for(followup['job_id'])
            self.assertEqual(followup_job['status'], 'succeeded')
            self.assertEqual(followup_job['real_model'], 1)
            self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
            report = post('/api/tasks/failure-followup/inspect', {
                'schema_version': '1.0', 'job_id': followup['job_id']})
            self.assertEqual(report['intent_sha256'], followup['intent_sha256'])
            for field in ('historical_binding_verified', 'current_source_valid', 'current_review_valid'):
                self.assertIs(report[field], True)
            self.assertEqual(report['outcome'], {'status': 'verified', 'verification_count': 1,
                'scope': 'fixed_task_outcome', 'reason': 'independent_evidence_verified'})
            for field in ('guidance_applied', 'causality_verified', 'gold', 'training_ready'):
                self.assertIs(report[field], False)
            self.assertEqual(query('SELECT status FROM desktop_approvals WHERE job_id=?',
                                   (followup['job_id'],)), [('consumed',)])
            self.assertEqual(query("SELECT actor FROM human_interventions WHERE run_id=? AND kind='approve'",
                                   (followup_job['run_id'],)), [('local_authenticated_user',)])
            self.assertEqual(query('SELECT tool,status FROM actions WHERE run_id=? ORDER BY created_at',
                                   (followup_job['run_id'],)), [('filesystem.write', 'ok'), ('filesystem.read', 'ok')])
            for job in (source_job, followup_job):
                self.assertEqual(query('SELECT role,status,deployment_id FROM model_calls WHERE run_id=?',
                                       (job['run_id'],)), [('system1', 'ok', roles['system1']['deployment_id'])])
            self.assertEqual(query('SELECT count(*) FROM actions WHERE run_id=?', (source_job['run_id'],)), [(0,)])
            self.assertEqual(query('SELECT status FROM desktop_tasks WHERE job_id=?', (source['job_id'],)), [('cancelled',)])
            post('/api/tasks/failure-improvement/revoke', {**selected_candidate,
                'receipt_sha256': review['review']['receipt_sha256'], **control()})
            revoked = post('/api/tasks/failure-followup/inspect', {
                'schema_version': '1.0', 'job_id': followup['job_id']})
            self.assertTrue(revoked['historical_binding_verified'])
            self.assertTrue(revoked['current_source_valid'])
            self.assertFalse(revoked['current_review_valid'])
            self.assertEqual(revoked['outcome'], report['outcome'])
            self.assertEqual(client.post('/api/tasks/failure-followup/preview', json=followup_selection).status_code, 409)
            self.assertEqual(query('SELECT count(*) FROM desktop_tasks'), [(2,)])
            print(json.dumps({'evidence': str(root), 'real_model': 'pinned Decider',
                              'system1_deployment_id': roles['system1']['deployment_id'],
                              'source_job_id': source['job_id'], 'followup_job_id': followup['job_id'],
                              'approval_driver': 'authenticated test harness', 'source_effects': 0,
                              'followup_effects': 1, 'independent_readbacks': 1,
                              'guidance_applied': False, 'causality_verified': False,
                              'seconds': round(time.perf_counter() - started_at, 3)}), flush=True)


if __name__ == '__main__':
    unittest.main()
