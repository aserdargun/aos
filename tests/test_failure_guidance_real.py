from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from aos.contracts import HELLO_CONTENT, REPO_ROOT, Prediction, State, canonical, digest


@unittest.skipUnless(os.environ.get('AOS_DESKTOP_TESTS') == '1'
                     and os.environ.get('AOS_FAILURE_GUIDANCE_REAL_TESTS') == '1',
                     'Requires explicit idle pinned Decider and isolated Docker guidance acceptance')
class FailureGuidanceRealTests(unittest.TestCase):
    def test_real_request_contains_separately_consented_guidance_without_causal_claim(self):
        from test_desktop_task_ui import task_server

        root = Path(tempfile.mkdtemp(prefix='failure-guidance-real-', dir=REPO_ROOT / 'data'))
        root.chmod(0o700)
        started_at = time.perf_counter()
        deployment_id = 'decider-' + digest(json.loads((REPO_ROOT / 'models/decider-manifest.json').read_text()))
        with task_server(root, 'decider') as (origin, token, client, server):
            def post(path, value):
                response = client.post(path, json=value)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            def control():
                current = client.get('/api/state').json()['control']
                return {key: current[key] for key in ('lease_id', 'generation')}

            def query(statement, parameters=()):
                with closing(sqlite3.connect(f'file:{root / "store.sqlite"}?mode=ro', uri=True)) as database:
                    database.row_factory = sqlite3.Row
                    return [dict(row) for row in database.execute(statement, parameters)]

            def wait(job_id, approval=False):
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    status = client.get('/api/tasks').json()
                    job = next(item for item in status['jobs'] if item['job_id'] == job_id)
                    if approval and status['approval'] is not None:
                        self.assertEqual(status['approval']['job_id'], job_id)
                        return status['approval'], job
                    if job['status'] in {'failed', 'cancelled', 'succeeded'} and not status['reserved']:
                        self.assertFalse(approval, 'Guided real-model task terminated before approval')
                        return None, job
                    time.sleep(.05)
                self.fail('Isolated guided task did not reach the required boundary')

            source = post('/api/tasks', {'kind': 'hello', 'approve_all': False, **control()})
            rejected, unused_job = wait(source['job_id'], approval=True)
            self.assertEqual(rejected['action']['tool'], 'filesystem.write')
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertEqual(query('SELECT * FROM actions WHERE run_id=?', (unused_job['run_id'],)), [])
            post('/api/approvals/' + rejected['approval_id'], {
                'action_sha256': rejected['action_sha256'], 'accept': False})
            unused_approval, source_job = wait(source['job_id'])
            self.assertEqual(source_job['status'], 'cancelled')
            self.assertEqual(source_job['real_model'], 1)
            self.assertFalse((root / 'workspace/hello.txt').exists())
            selection = {'schema_version': '1.0', 'job_id': source['job_id']}
            candidate = post('/api/tasks/failure-improvement/preview', selection)
            post('/api/tasks/failure-improvement/save', {**selection,
                'confirm_sha256': candidate['candidate_sha256'], 'consent': True, **control()})
            option = next(value for value in candidate['review_options']
                          if value['decision'] == 'accept' and value['correction_code'] == 'refresh_observation')
            review = post('/api/tasks/failure-improvement/review', {**selection,
                'candidate_sha256': candidate['candidate_sha256'], **option, **control()})
            preview = post('/api/tasks/failure-guidance/preview', {'schema_version': '1.0',
                'source_job_id': source['job_id'], 'candidate_sha256': candidate['candidate_sha256'],
                'receipt_sha256': review['review']['receipt_sha256']})
            self.assertEqual(preview['followup']['target']['system1_deployment_id'], deployment_id)
            self.assertFalse((root / 'failure-guidance').exists())
            admission = {'schema_version': '1.0', 'preview': preview,
                'confirm_sha256': preview['confirm_sha256'], 'consent': True, **control()}
            for changes in ({'consent': False}, {'confirm_sha256': '0' * 64},
                            {'preview': preview | {'context_sha256': '0' * 64}},
                            {'preview': preview | {'guidance_code': 'repair_environment'}}):
                denied = client.post('/api/tasks/failure-guidance/start', json=admission | changes)
                self.assertEqual(denied.status_code, 409, denied.text)
            self.assertEqual(len(query('SELECT * FROM desktop_tasks')), 1)
            self.assertFalse((root / 'failure-guidance').exists())
            started = post('/api/tasks/failure-guidance/start', admission)
            self.assertTrue(started['manual_approval_required'])
            self.assertNotEqual(started['job_id'], source['job_id'])
            approval, unused_job = wait(started['job_id'], approval=True)
            self.assertNotEqual(approval['approval_id'], rejected['approval_id'])
            self.assertNotEqual(approval['action_sha256'], rejected['action_sha256'])
            self.assertEqual(approval['action']['tool'], 'filesystem.write')
            self.assertIsNone(client.get('/api/tasks').json()['auto_approval'])
            self.assertFalse((root / 'workspace/hello.txt').exists())
            self.assertEqual(query('SELECT * FROM actions WHERE run_id=?', (unused_job['run_id'],)), [])
            pending = post('/api/tasks/failure-guidance/inspect', {'schema_version': '1.0', 'job_id': started['job_id']})
            self.assertTrue(pending['model_request_verified'], pending)
            self.assertEqual(pending['followup']['outcome']['status'], 'not_verified')
            denied = client.post('/api/approvals/' + rejected['approval_id'], json={
                'action_sha256': rejected['action_sha256'], 'accept': True})
            self.assertEqual(denied.status_code, 409)
            post('/api/approvals/' + approval['approval_id'], {
                'action_sha256': approval['action_sha256'], 'accept': True})
            unused_approval, guided_job = wait(started['job_id'])
            self.assertEqual(guided_job['status'], 'succeeded')
            self.assertEqual(guided_job['real_model'], 1)
            self.assertEqual((root / 'workspace/hello.txt').read_text(), HELLO_CONTENT)
            report = post('/api/tasks/failure-guidance/inspect', {'schema_version': '1.0', 'job_id': started['job_id']})
            for field in ('context_binding_verified', 'model_request_verified', 'guidance_applied'):
                self.assertTrue(report[field], report)
            for field in ('causality_verified', 'gold', 'training_ready'):
                self.assertFalse(report[field])
            self.assertFalse(report['followup']['guidance_applied'])
            self.assertEqual(report['guidance_intent_sha256'], started['guidance_intent_sha256'])
            self.assertEqual(report['followup']['intent_sha256'], started['intent_sha256'])
            for field in ('historical_binding_verified', 'current_source_valid', 'current_review_valid'):
                self.assertTrue(report['followup'][field], report)
            self.assertEqual(report['followup']['outcome']['status'], 'verified')
            self.assertEqual(query('SELECT status FROM desktop_approvals WHERE job_id=?', (started['job_id'],)),
                             [{'status': 'consumed'}])
            self.assertEqual(query("SELECT actor FROM human_interventions WHERE run_id=? AND kind='approve'",
                                   (guided_job['run_id'],)), [{'actor': 'local_authenticated_user'}])
            with closing(sqlite3.connect(f'file:{root / "store.sqlite"}?mode=ro', uri=True)) as database:
                database.row_factory = sqlite3.Row
                database.execute('PRAGMA query_only=ON')
                database.execute('BEGIN')
                for job in (source_job, guided_job):
                    self.assertEqual([tuple(row) for row in database.execute(
                        'SELECT role,status,deployment_id FROM model_calls WHERE run_id=?', (job['run_id'],))],
                        [('system1', 'ok', deployment_id)])
                self.assertEqual(database.execute('SELECT count(*) FROM actions WHERE run_id=?',
                                                 (source_job['run_id'],)).fetchone()[0], 0)
                self.assertEqual([tuple(row) for row in database.execute(
                    'SELECT tool,status FROM actions WHERE run_id=? ORDER BY created_at', (guided_job['run_id'],))],
                                 [('filesystem.write', 'ok'), ('filesystem.read', 'ok')])
                from aos.decision import decision_request
                from aos.failure_guidance import ADMISSION, MARKER, OBSERVATIONS, context, context_options, observation_context

                self.assertEqual(database.execute('SELECT count(*) FROM observations WHERE run_id=? AND kind=?',
                                                 (source_job['run_id'], MARKER)).fetchone()[0], 0)
                source_snapshot = database.execute('SELECT state_json FROM state_snapshots JOIN decisions '
                    'USING(snapshot_id) WHERE decisions.run_id=?', (source_job['run_id'],)).fetchone()
                source_state = State.model_validate_json(source_snapshot['state_json'])
                self.assertEqual(source_state.observation, OBSERVATIONS[0])
                self.assertEqual(database.execute('SELECT request_json FROM model_calls WHERE run_id=?',
                    (source_job['run_id'],)).fetchone()[0],
                    canonical(decision_request(source_state, context_options(OBSERVATIONS[0]))))
                events = database.execute('SELECT * FROM desktop_events WHERE kind=? AND '
                    "json_extract(payload_json,'$.job_id')=?", (ADMISSION, started['job_id'])).fetchall()
                self.assertEqual(len(events), 1)
                binding = {'schema_version': '1.0', 'guidance_intent_sha256': started['guidance_intent_sha256'],
                           'intent_sha256': started['intent_sha256'], 'job_id': started['job_id'],
                           'source_job_id': source['job_id']}
                self.assertEqual(events[0]['payload_json'], canonical(binding))
                self.assertEqual(events[0]['session_id'], preview['followup']['target']['session_id'])
                run = database.execute('SELECT started_at FROM runs WHERE run_id=?',
                                       (guided_job['run_id'],)).fetchone()
                self.assertLessEqual(events[0]['created_at'], run['started_at'])
                observations = database.execute('SELECT * FROM observations WHERE run_id=? AND kind=?',
                                               (guided_job['run_id'], MARKER)).fetchall()
                self.assertEqual(len(observations), 1)
                observation = observations[0]
                marker = json.loads(observation['payload_json'])
                snapshot = database.execute('SELECT * FROM state_snapshots WHERE snapshot_id=?',
                                            (marker['snapshot_id'],)).fetchone()
                state = State.model_validate_json(snapshot['state_json'])
                self.assertEqual(state.run_id, guided_job['run_id'])
                self.assertEqual(state.phase.value, 'DECIDE')
                self.assertEqual(state.deployment_id, deployment_id)
                self.assertEqual(state.observation, observation_context(OBSERVATIONS[0], preview['guidance_code']))
                options = context_options(OBSERVATIONS[0])
                request = decision_request(state, options)
                self.assertEqual(preview['context_sha256'], digest(context(preview['guidance_code'])))
                self.assertEqual(snapshot['content_sha256'], digest(state.model_dump(mode='json')))
                self.assertEqual((snapshot['run_id'], snapshot['step_id'], snapshot['state_version']),
                                 (state.run_id, state.step_id, state.state_version))
                self.assertEqual(observation['step_id'], state.step_id)
                self.assertIsNone(observation['action_id'])
                self.assertEqual(observation['payload_json'], canonical({**binding,
                    **{key: preview['followup'][key] for key in ('source_sha256', 'candidate_sha256', 'receipt_sha256')},
                    'guidance_code': preview['guidance_code'], 'context_version': preview['context_version'],
                    'context_sha256': preview['context_sha256'], 'run_id': state.run_id, 'step_id': state.step_id,
                    'state_version': state.state_version, 'snapshot_id': snapshot['snapshot_id'],
                    'state_sha256': snapshot['content_sha256'], 'options_sha256': digest(request['options']),
                    'request_sha256': digest(request), 'call_id': marker['call_id']}))
                call = database.execute('SELECT * FROM model_calls WHERE call_id=?', (marker['call_id'],)).fetchone()
                self.assertEqual((call['run_id'], call['step_id'], call['role'], call['deployment_id'], call['status']),
                                 (state.run_id, state.step_id, 'system1', deployment_id, 'ok'))
                self.assertEqual(call['request_json'], canonical(request))
                decisions = database.execute('SELECT * FROM decisions WHERE run_id=? AND snapshot_id=?',
                                             (state.run_id, snapshot['snapshot_id'])).fetchall()
                self.assertEqual(len(decisions), 1)
                decision = decisions[0]
                prediction = Prediction.model_validate_json(call['response_json'])
                prediction.validate_options(options)
                self.assertEqual((decision['step_id'], decision['call_id'], decision['selected_option']),
                                 (state.step_id, call['call_id'], prediction.selected_option))
                self.assertEqual(decision['probabilities_json'], canonical(prediction.probabilities))
                self.assertEqual(decision['options_json'], canonical(request['options']))
                self.assertEqual(decision['question'], request['question'])
                self.assertLessEqual(snapshot['created_at'], observation['created_at'])
                self.assertLessEqual(call['created_at'], observation['created_at'])
                self.assertLessEqual(observation['created_at'], decision['created_at'])
            post('/api/tasks/failure-improvement/revoke', {**selection,
                'candidate_sha256': candidate['candidate_sha256'], 'receipt_sha256': review['review']['receipt_sha256'],
                **control()})
            revoked = post('/api/tasks/failure-guidance/inspect', {'schema_version': '1.0', 'job_id': started['job_id']})
            for field in ('context_binding_verified', 'model_request_verified', 'guidance_applied'):
                self.assertTrue(revoked[field], revoked)
            self.assertTrue(revoked['followup']['historical_binding_verified'])
            self.assertTrue(revoked['followup']['current_source_valid'])
            self.assertFalse(revoked['followup']['current_review_valid'])
            self.assertEqual(revoked['followup']['outcome'], report['followup']['outcome'])
            denied = client.post('/api/tasks/failure-guidance/preview', json={'schema_version': '1.0',
                'source_job_id': source['job_id'], 'candidate_sha256': candidate['candidate_sha256'],
                'receipt_sha256': review['review']['receipt_sha256']})
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(len(query('SELECT * FROM desktop_tasks')), 2)
            intent = root / 'failure-guidance' / ('intent-' + started['guidance_intent_sha256'] + '.json')
            intent_bytes = intent.read_bytes()
            intent_mode = intent.stat().st_mode & 0o777
            try:
                intent.chmod(0o600)
                for malformed in (b'{', b'{}'):
                    intent.write_bytes(malformed)
                    denied = client.post('/api/tasks/failure-guidance/inspect', json={
                        'schema_version': '1.0', 'job_id': started['job_id']})
                    self.assertEqual(denied.status_code, 409, denied.text)
            finally:
                intent.write_bytes(intent_bytes)
                intent.chmod(intent_mode)
            self.assertEqual(post('/api/tasks/failure-guidance/inspect', {
                'schema_version': '1.0', 'job_id': started['job_id']}), revoked)
            print(json.dumps({'evidence': str(root), 'source_job_id': source['job_id'],
                'guided_job_id': started['job_id'], 'real_model': 'pinned Decider',
                'system1_deployment_id': deployment_id,
                'approval_driver': 'authenticated test harness', 'guidance_applied': True,
                'review_revoked_after_completion': True, 'malformed_intent_rejected': True,
                'causality_verified': False, 'gold': False, 'training_ready': False,
                'seconds': round(time.perf_counter() - started_at, 3)}), flush=True)


if __name__ == '__main__':
    unittest.main()
