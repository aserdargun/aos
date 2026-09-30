from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema
from pydantic import ValidationError

from aos.contracts import Failure, Phase, REPO_ROOT, State, canonical, identifier
from aos.storage import TrajectoryStore
from aos.task_progress import TaskProgress, elapsed_time, task_progress


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = TrajectoryStore(Path(self.temporary.name) / 'synthetic.sqlite')
        self.addCleanup(self.store.close)
        self.current = datetime(2026, 9, 21, 12, 0, 10, tzinfo=timezone.utc)
        self.created = '2026-09-21T12:00:00+00:00'
        self.updated = '2026-09-21T12:00:04+00:00'
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id='session-original', runtime_id='desktop-original',
                              image_id='synthetic-image', owner='AGENT', lease_id='lease-original', generation=0,
                              status='running', created_at=self.created, updated_at=self.created)
        self.state = State(task_id='task-original', run_id='run-original', step_id='step-original',
                           runtime_id='desktop-original', deployment_id='fixture', owner_lease_id='lease-original')
        self.store.create_run(self.state, {'synthetic': True}, {'runtime_id': self.state.runtime_id})
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id='job-original', session_id='session-original',
                              run_id=self.state.run_id, kind='hello', lease_id='lease-original', generation=0,
                              status='running', real_model=0, created_at=self.created, updated_at=self.updated,
                              runtime_id=self.state.runtime_id)

    def progress(self, session_id='session-original'):
        return task_progress(self.store, 'job-original', session_id, current=self.current)

    def call(self, *, status='ok', latency=100.0, role='system1', state=None):
        state = state or self.state
        with self.store.connection:
            self.store.insert('model_calls', call_id=identifier('call'), run_id=state.run_id, step_id=state.step_id,
                              deployment_id='fixture', role=role, request_json='{"synthetic_private_prompt":"not public"}',
                              response_json='{"synthetic_private_response":"not public"}', status=status,
                              latency_ms=latency, created_at=self.created)

    def test_canonical_schema_and_explicitly_synthetic_fixture(self):
        schema = json.loads((REPO_ROOT / 'schemas/task_progress.schema.json').read_text())
        self.assertEqual(schema, {'$schema': 'https://json-schema.org/draft/2020-12/schema', **TaskProgress.model_json_schema()})
        example = json.loads((REPO_ROOT / 'examples/task_progress.json').read_text())
        self.assertTrue(example['synthetic'])
        jsonschema.Draft202012Validator(schema).validate(example['progress'])
        TaskProgress.model_validate_json(json.dumps(example['progress']))
        for changes in ({'elapsed_ms': -1.0}, {'elapsed_ms': float('inf')}, {'state_version': True},
                        {'failure_code': 'PRIVATE_CONTENT'},
                        {'model_calls': [{'role': 'system1', 'status': 'ok', 'latency_ms': 1.0}] * 11},
                        {'model_call_totals': {'system1': {'ok': 2, 'total': 1},
                                               'system2': {'ok': 0, 'total': 0}}},
                        {'model_call_latency': {'system1': {'samples': 0, 'p50_ms': 1, 'p95_ms': 1},
                                                'system2': {'samples': 0}}},
                        {'model_call_latency': {'system1': {'samples': 1, 'p50_ms': 2, 'p95_ms': 1},
                                                'system2': {'samples': 0}}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                TaskProgress.model_validate(changes)

    def test_current_bound_phase_and_elapsed_do_not_mutate_database(self):
        changed = self.store.connection.total_changes
        self.assertEqual(self.progress(), {'phase': 'CREATED', 'state_version': 0,
                                          'elapsed_ms': 10000.0, 'failure_code': None, 'model_calls': [],
                                          'model_call_totals': {'system1': {'ok': 0, 'total': 0},
                                                                'system2': {'ok': 0, 'total': 0}},
                                          'model_call_latency': {
                                              'system1': {'samples': 0, 'p50_ms': None, 'p95_ms': None},
                                              'system2': {'samples': 0, 'p50_ms': None, 'p95_ms': None}}})
        self.assertEqual(self.store.connection.total_changes, changed)
        self.assertFalse(self.store.connection.in_transaction)
        later = self.state.advance(Phase.OBSERVE)
        self.store.save_state(self.state, later)
        self.assertEqual(self.progress()['phase'], 'OBSERVE')
        self.assertEqual(self.progress()['state_version'], 1)

    def test_paused_and_terminal_elapsed_freezes_and_resume_includes_wait(self):
        for status in ('paused', 'succeeded', 'failed', 'cancelled', 'waiting_human'):
            with self.subTest(status=status), self.store.connection:
                self.store.connection.execute('UPDATE desktop_tasks SET status=? WHERE job_id=?', (status, 'job-original'))
                self.assertEqual(self.progress()['elapsed_ms'], 4000.0)
        for status in ('queued', 'running', 'waiting_approval'):
            with self.subTest(status=status), self.store.connection:
                self.store.connection.execute('UPDATE desktop_tasks SET status=? WHERE job_id=?', (status, 'job-original'))
                self.assertEqual(self.progress()['elapsed_ms'], 10000.0)

    def test_missing_run_or_job_has_no_fabricated_phase(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET run_id=NULL,runtime_id=NULL,status='queued'")
        self.assertEqual(self.progress(), {'phase': None, 'state_version': None,
                                          'elapsed_ms': 10000.0, 'failure_code': None, 'model_calls': [],
                                          'model_call_totals': None, 'model_call_latency': None})
        self.assertEqual(task_progress(self.store, 'missing', 'session-original', current=self.current),
                         {'phase': None, 'state_version': None, 'elapsed_ms': None,
                          'failure_code': None, 'model_calls': [], 'model_call_totals': None, 'model_call_latency': None})

    def test_terminal_failure_exposes_only_bound_code_not_private_detail(self):
        failed = self.state.advance(Phase.FAILED, last_error=Failure(
            code='MODEL_FAILURE', detail='synthetic private prompt must stay private',
            retryable=False, evidence_refs=[]))
        self.store.save_state(self.state, failed)
        self.store.finish(failed, 'failed', 'unknown')
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='failed' WHERE job_id='job-original'")
        result = self.progress()
        self.assertEqual(result['failure_code'], 'MODEL_FAILURE')
        self.assertNotIn('synthetic private prompt', json.dumps(result))
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='succeeded' WHERE run_id='run-original'")
        self.assertIsNone(self.progress()['failure_code'])
        with self.store.connection:
            self.store.connection.execute("UPDATE runs SET status='failed' WHERE run_id='run-original'")
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='succeeded' WHERE job_id='job-original'")
        self.assertIsNone(self.progress()['failure_code'])
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_tasks SET status='failed' WHERE job_id='job-original'")
            self.store.connection.execute("UPDATE runtime_states SET state_json='{}' WHERE run_id='run-original'")
        self.assertIsNone(self.progress()['failure_code'])

    def test_foreign_session_and_cross_run_state_are_not_disclosed(self):
        self.call()
        self.assertIsNone(self.progress('foreign-session')['phase'])
        foreign = self.state.model_copy(update={'run_id': 'run-foreign'})
        with self.store.connection:
            self.store.connection.execute('UPDATE runtime_states SET state_json=? WHERE run_id=?',
                                          (foreign.model_dump_json(), self.state.run_id))
        self.assertIsNone(self.progress()['phase'])
        self.assertEqual(self.progress()['model_calls'], [])

    def test_hello_cannot_borrow_another_desktop_session_runtime(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET runtime_id='foreign-desktop'")
        self.assertIsNone(self.progress()['phase'])
        self.assertIsNone(self.progress()['state_version'])

    def test_runtime_kind_task_version_and_parent_bindings_fail_closed(self):
        for changes in ({'runtime_id': 'foreign'}, {'task_kind': 'browser_form'}, {'task_id': 'foreign'}, {'state_version': 100}):
            with self.subTest(changes=changes), self.store.connection:
                self.store.connection.execute('UPDATE runtime_states SET state_json=? WHERE run_id=?',
                                              (self.state.model_copy(update=changes).model_dump_json(), self.state.run_id))
                self.assertIsNone(self.progress()['phase'])
        with self.store.connection:
            self.store.connection.execute('UPDATE runtime_states SET state_json=? WHERE run_id=?',
                                          (self.state.model_dump_json(), self.state.run_id))
            self.store.connection.execute('UPDATE runs SET environment_json=?',
                                          (canonical({'runtime_id': self.state.runtime_id, 'parent_runtime_id': 'foreign'}),))
        self.assertIsNone(self.progress()['phase'])

    def test_missing_malformed_or_stale_persisted_state_is_unavailable(self):
        for state_json in ('{}', '[]', '{"phase":"invented"}'):
            with self.subTest(state_json=state_json), self.store.connection:
                self.store.connection.execute('UPDATE runtime_states SET state_json=?', (state_json,))
                self.assertIsNone(self.progress()['phase'])
        with self.store.connection:
            self.store.connection.execute('DELETE FROM runtime_states')
        self.assertIsNone(self.progress()['phase'])

    def test_snapshot_and_step_mismatch_cannot_publish_stale_phase(self):
        with self.store.connection:
            self.store.connection.execute("UPDATE state_snapshots SET content_sha256=?", ('0' * 64,))
        self.assertIsNone(self.progress()['phase'])
        self.store.snapshot(self.state)
        with self.store.connection:
            self.store.connection.execute("UPDATE steps SET state='FAILED'")
        self.assertIsNone(self.progress()['phase'])

    def test_finalized_model_calls_are_bounded_newest_first_without_payloads(self):
        for index in range(12):
            self.call(status=('ok', 'error', 'timeout', 'cancelled')[index % 4], latency=float(index),
                      role='system1' if index % 2 else 'system2')
        self.call(status='error', latency=None)
        result = self.progress()
        self.assertEqual([call['latency_ms'] for call in result['model_calls']], list(map(float, range(11, 1, -1))))
        self.assertEqual(result['model_call_totals'], {
            'system1': {'ok': 0, 'total': 6}, 'system2': {'ok': 3, 'total': 6}})
        self.assertEqual(result['model_call_latency'], {
            'system1': {'samples': 0, 'p50_ms': None, 'p95_ms': None},
            'system2': {'samples': 3, 'p50_ms': 4.0, 'p95_ms': 8.0}})
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('request_json', json.dumps(result))
        self.assertNotIn('response_json', json.dumps(result))
        self.assertNotIn('state_json', json.dumps(result))

    def test_foreign_run_inflight_and_malformed_model_metrics_are_excluded(self):
        foreign = self.state.model_copy(update={'task_id': 'foreign-task', 'run_id': 'foreign-run', 'step_id': 'foreign-step'})
        self.store.create_run(foreign, {'synthetic': True}, {'runtime_id': foreign.runtime_id})
        self.call(state=foreign, latency=9999.0)
        self.call(status='error', latency=None)
        self.call(latency=float('inf'))
        self.call(latency='not-a-number')
        self.call(latency=1.0)
        self.assertEqual(self.progress()['model_calls'], [{'role': 'system1', 'status': 'ok', 'latency_ms': 1.0}])
        self.assertEqual(self.progress()['model_call_totals'], {
            'system1': {'ok': 1, 'total': 1}, 'system2': {'ok': 0, 'total': 0}})
        self.assertEqual(self.progress()['model_call_latency']['system1'], {
            'samples': 1, 'p50_ms': 1.0, 'p95_ms': 1.0})

    def test_run_bound_percentiles_are_not_limited_to_recent_ten_calls(self):
        for latency in range(1, 22):
            self.call(latency=float(latency))
        summary = self.progress()['model_call_latency']['system1']
        self.assertEqual(summary, {'samples': 21, 'p50_ms': 11.0, 'p95_ms': 20.0})
        self.assertEqual(len(self.progress()['model_calls']), 10)

    def test_latency_summary_fails_closed_above_bounded_sample_count(self):
        for latency in range(257):
            self.call(latency=float(latency))
        self.assertIsNone(self.progress()['model_call_latency'])
        self.assertEqual(self.progress()['model_call_totals']['system1']['ok'], 257)

    def test_malformed_naive_and_backwards_timestamps_are_unavailable(self):
        for created, updated, status in (('bad', self.updated, 'running'),
                                         ('2026-09-21T12:00:00', self.updated, 'running'),
                                         (self.created, 'bad', 'paused'),
                                         (self.created, '2026-09-21T11:00:00+00:00', 'failed'),
                                         ('2026-09-22T12:00:00+00:00', self.updated, 'running')):
            with self.subTest(created=created, updated=updated, status=status):
                self.assertIsNone(elapsed_time(created, updated, status, self.current))
