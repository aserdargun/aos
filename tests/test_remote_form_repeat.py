import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jsonschema
import httpx

from aos.contracts import REPO_ROOT, canonical, digest
from aos.dataset_audit import audit_snapshot
from aos.desktop_console import create_console
from aos.remote_form_repeat import (RemoteFormRepeatReport, RemoteFormStateRepeatReport,
                                    inspect_remote_form_repeats)
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebTaskAdmissionDraft
from aos.web_https_form_transport import WebHTTPSFormPlan
from aos.web_https_form_state_probe import WebHTTPSFormStatePlan


class RemoteFormRepeatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / 'trajectory.sqlite'
        self.profile = WebApplicationProfile.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile'])
        self.profile_sha = profile_report(self.profile).profile_sha256
        self.profiles = WebApplicationProfiles(self.root / 'profiles')
        self.profiles.register(self.profile, confirm_sha256=self.profile_sha)
        self.draft = WebTaskAdmissionDraft.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_binding.json').read_text())['draft'])
        self.plan = WebHTTPSFormPlan.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_https_form_transport.json').read_text())['plan'])
        self.plan_sha = digest(self.plan.model_dump())
        self.state_plan = WebHTTPSFormStatePlan.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_https_form_state.json').read_text())['plan'])
        self.state_sha = digest(self.state_plan.model_dump())
        self.store = TrajectoryStore(self.database)

    def arguments(self):
        return {'profiles': self.profiles.root, 'selected_profile_sha256': self.profile_sha,
                'selected_plan_sha256': self.plan_sha}

    def insert_failure(self, index, *, deployment='base', started='2026-09-25T00:00:00+00:00',
                       ended='2026-09-25T00:00:01+00:00'):
        session_id = f'session-{index}'
        job_id = f'job-{index}'
        run_id = f'run-{index}'
        task_id = f'task-{index}'
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id=session_id, runtime_id='desktop-test',
                              image_id='sha256:' + 'a' * 64, owner='AGENT', lease_id='lease-test',
                              generation=0, status='stopped', created_at=started, updated_at=ended)
            self.store.insert('tasks', task_id=task_id, original_goal='synthetic form',
                              normalized_goal='synthetic form', success_criteria_json='[]',
                              workspace_scope_json='[]', created_at=started)
            self.store.insert('runs', run_id=run_id, task_id=task_id, status='failed',
                              outcome='failed', policy_version='browser-remote-form-policy-v1',
                              environment_json='{}', deployment_snapshot_json=canonical({'id': deployment}),
                              started_at=started, ended_at=ended)
            self.store.insert('desktop_tasks', job_id=job_id, session_id=session_id, run_id=run_id,
                              kind='browser_remote_form', lease_id='lease-test', generation=0,
                              status='failed', real_model=1, created_at=started, updated_at=ended,
                              runtime_id=self.draft.runtime.runtime_id)
            self.store.insert('desktop_remote_form_bindings', job_id=job_id, run_id=run_id,
                              profile_sha256=self.profile_sha, binding_sha256=self.draft.binding_sha256,
                              runtime_sha256=self.draft.runtime_sha256, plan_sha256=self.plan_sha,
                              draft_json=canonical(self.draft.model_dump(mode='json')),
                              plan_json=canonical(self.plan.model_dump(mode='json')),
                              browser_runtime_id=self.draft.runtime.runtime_id, created_at=started)

    def insert_state_failure(self, index):
        self.insert_failure(index)
        with self.store.connection:
            self.store.insert('desktop_remote_form_state_bindings',
                              job_id=f'job-{index}', run_id=f'run-{index}',
                              form_plan_sha256=self.plan_sha,
                              state_plan_sha256=self.state_sha,
                              state_plan_json=canonical(self.state_plan.model_dump(mode='json')),
                              browser_runtime_id=self.draft.runtime.runtime_id,
                              created_at='2026-09-25T00:00:00+00:00')

    def test_synthetic_contract_and_all_failed_attempts(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_repeat.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_form_repeat.schema.json').read_text())
        self.assertTrue(fixture['synthetic'])
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        RemoteFormRepeatReport.model_validate(fixture['report'])
        with self.assertRaises(ValueError):
            RemoteFormRepeatReport.model_validate({**fixture['report'],
                                                   'elapsed_excluding_approval_p95_ms': 1300})
        with self.assertRaises(ValueError):
            RemoteFormRepeatReport.model_validate({**fixture['report'],
                                                   'post_approval_stage_ms': {}})
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(
                {**fixture['report'], 'post_approval_stage_ms': {}})
        with self.assertRaises(ValueError):
            RemoteFormRepeatReport.model_validate({**fixture['report'],
                'post_approval_stage_ms': {
                    **fixture['report']['post_approval_stage_ms'],
                    'browser.form.submit': {'samples': 2, 'p50_ms': 200, 'p95_ms': 200}}})
        RemoteFormRepeatReport.model_validate({**fixture['report'], 'bound_attempts': 32,
                                               'transport_verified': 31,
                                               'failed_or_cancelled': 1,
                                               'post_approval_stage_ms': {
                                                   tool: {**timing, 'samples': 31}
                                                   for tool, timing in fixture['report'][
                                                       'post_approval_stage_ms'].items()},
                                               'approval_window_sum_ms': 100000})
        for field in ('site_outcome_verified', 'real_site_acceptance'):
            with self.subTest(field=field), self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate({**fixture['report'], field: True})
        self.insert_failure(1, ended='2026-09-25T00:00:01+00:00')
        self.insert_failure(2, ended='2026-09-25T00:00:02+00:00')
        with self.store.connection:
            self.store.insert('steps', step_id='step-2', run_id='run-2', ordinal=0,
                              state='FAILED', started_at='2026-09-25T00:00:00+00:00')
            self.store.insert('model_calls', call_id='call-2', run_id='run-2',
                              step_id='step-2', deployment_id='recorded-base', role='system1',
                              request_json='{}', status='timeout', latency_ms=None,
                              created_at='2026-09-25T00:00:01+00:00')
        self.store.close()
        report = inspect_remote_form_repeats(self.database, **self.arguments())
        jsonschema.Draft202012Validator(schema).validate(report)
        self.assertEqual((report['bound_attempts'], report['transport_verified'],
                          report['failed_or_cancelled']), (2, 0, 2))
        self.assertEqual((report['elapsed_p50_ms'], report['elapsed_p95_ms']), (1000, 2000))
        self.assertEqual(report['approval_window_count'], 0)
        self.assertEqual(report['approval_window_sum_ms'], 0)
        self.assertEqual((report['elapsed_excluding_approval_p50_ms'],
                          report['elapsed_excluding_approval_p95_ms']), (1000, 2000))
        self.assertEqual(report['first_action_samples'], 0)
        self.assertIsNone(report['first_action_p50_ms'])
        self.assertEqual(report['model_call_count'], 1)
        self.assertEqual(report['model_latency_samples'], 0)
        self.assertEqual(report['post_approval_stage_ms'], {})
        self.assertFalse(report['site_outcome_verified'])

    def test_approval_windows_are_separate_and_fail_closed(self):
        self.insert_failure(1)
        self.insert_failure(2, ended='2026-09-25T00:00:02+00:00')
        windows = (
            ('one', 'job-1', 'consumed', '2026-09-25T00:00:00.200+00:00',
             '2026-09-25T00:00:00.500+00:00'),
            ('two', 'job-2', 'rejected', '2026-09-25T00:00:00.100+00:00',
             '2026-09-25T00:00:00.500+00:00'),
            ('three', 'job-2', 'expired', '2026-09-25T00:00:00.600+00:00',
             '2026-09-25T00:00:00.800+00:00'))
        with self.store.connection:
            for approval_id, job_id, status, created_at, updated_at in windows:
                self.store.insert('desktop_approvals', approval_id=approval_id, job_id=job_id,
                                  envelope_json='{}', action_sha256=str(len(approval_id)) * 64,
                                  expires_at=9999999999, status=status,
                                  created_at=created_at, updated_at=updated_at)
        self.store.close()
        report = inspect_remote_form_repeats(self.database, **self.arguments())
        self.assertEqual(report['approval_window_count'], 3)
        self.assertAlmostEqual(report['approval_window_sum_ms'], 900)
        self.assertEqual((report['elapsed_excluding_approval_p50_ms'],
                          report['elapsed_excluding_approval_p95_ms']), (700, 1400))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE desktop_approvals SET created_at=? WHERE approval_id='three'",
                               ('2026-09-25T00:00:00.400+00:00',))
        with self.assertRaisesRegex(ValueError, 'invalid_duration'):
            inspect_remote_form_repeats(self.database, **self.arguments())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE desktop_approvals SET created_at=? WHERE approval_id='three'",
                               ('2026-09-25T00:00:00.600+00:00',))
            connection.execute("UPDATE desktop_approvals SET status='pending' WHERE approval_id='three'")
        with self.assertRaisesRegex(ValueError, 'approval_not_terminal'):
            inspect_remote_form_repeats(self.database, **self.arguments())

    def test_state_scope_counts_only_exact_state_attempts(self):
        fixture = json.loads((REPO_ROOT / 'examples/remote_form_state_repeat.json').read_text())
        schema = json.loads((REPO_ROOT / 'schemas/remote_form_state_repeat.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(fixture['report'])
        RemoteFormStateRepeatReport.model_validate(fixture['report'])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(
                {key: value for key, value in fixture['report'].items()
                 if key != 'state_plan_sha256'})
        self.insert_failure(1)
        self.insert_state_failure(2)
        self.insert_state_failure(3)
        self.store.close()
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(self.database, **self.arguments())
        report = inspect_remote_form_repeats(
            self.database, **self.arguments(),
            selected_state_plan_sha256=self.state_sha)
        jsonschema.Draft202012Validator(schema).validate(report)
        self.assertEqual((report['bound_attempts'], report['transport_verified'],
                          report['failed_or_cancelled']), (2, 0, 2))
        self.assertEqual(report['state_plan_sha256'], self.state_sha)
        self.assertFalse(report['site_outcome_verified'])
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(self.database, **self.arguments(),
                                        selected_state_plan_sha256='0' * 64)
        command = subprocess.run(
            [sys.executable, '-m', 'aos.remote_form_repeat',
             '--database', str(self.database), '--profiles', str(self.profiles.root),
             '--selected-profile-sha256', self.profile_sha,
             '--selected-plan-sha256', self.plan_sha,
             '--selected-state-plan-sha256', self.state_sha],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)['bound_attempts'], 2)
        changed = TrajectoryStore(self.database)
        try:
            with changed.connection:
                changed.insert('desktop_remote_form_cookie_bindings',
                               job_id='job-2', run_id='run-2',
                               form_plan_sha256=self.plan_sha,
                               cookie_sha256='c' * 64,
                               browser_runtime_id=self.draft.runtime.runtime_id,
                               created_at='2026-09-25T00:00:00+00:00')
        finally:
            changed.close()
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(self.database, **self.arguments(),
                                        selected_state_plan_sha256=self.state_sha)

    def test_state_success_requires_matching_source_snapshot(self):
        self.insert_state_failure(1)
        self.insert_state_failure(2)
        self.store.close()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE desktop_tasks SET status='succeeded' WHERE job_id='job-1'")
            connection.execute("UPDATE runs SET status='succeeded',outcome='passed' WHERE run_id='run-1'")
        with audit_snapshot(self.database) as (_snapshot, identity):
            snapshot_sha = identity['sha256']
        with (patch('aos.remote_form_repeat.inspect_remote_form_learning_source',
                    return_value={'snapshot_sha256': snapshot_sha}) as source_audit,
              patch('aos.remote_form_repeat._post_approval_stage_durations',
                    return_value={tool: 100.0 for tool in (
                        'browser.form.open', 'browser.form.state_before',
                        'browser.form.fill', 'browser.form.submit',
                        'browser.form.receipt', 'browser.form.state_after')})):
            report = inspect_remote_form_repeats(
                self.database, **self.arguments(),
                selected_state_plan_sha256=self.state_sha)
        self.assertEqual((report['bound_attempts'], report['transport_verified']), (2, 1))
        self.assertEqual(len(report['post_approval_stage_ms']), 6)
        self.assertEqual(source_audit.call_args.kwargs['selected_state_plan_sha256'],
                         self.state_sha)
        with patch('aos.remote_form_repeat.inspect_remote_form_learning_source',
                   return_value={'snapshot_sha256': '0' * 64}):
            with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
                inspect_remote_form_repeats(
                    self.database, **self.arguments(),
                    selected_state_plan_sha256=self.state_sha)

    def test_fail_closed_selection_deployment_time_and_cli(self):
        self.insert_failure(1)
        self.store.close()
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(self.database, **self.arguments())
        cli = subprocess.run([sys.executable, '-m', 'aos.remote_form_repeat',
                              '--database', str(self.database), '--profiles', str(self.profiles.root),
                              '--selected-profile-sha256', self.profile_sha,
                              '--selected-plan-sha256', self.plan_sha],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(cli.returncode, 1)
        self.assertEqual(cli.stdout, '')
        self.assertNotIn(self.profile.entry_url, cli.stderr)
        alias = self.root / 'alias.sqlite'
        alias.symlink_to(self.database)
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(alias, **self.arguments())
        store = TrajectoryStore(self.database)
        self.store = store
        self.insert_failure(2, deployment='candidate')
        store.close()
        with self.assertRaisesRegex(ValueError, 'deployment_changed'):
            inspect_remote_form_repeats(self.database, **self.arguments())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE runs SET deployment_snapshot_json=? WHERE run_id=?',
                               (canonical({'id': 'base'}), 'run-2'))
            connection.execute('UPDATE desktop_tasks SET updated_at=? WHERE job_id=?',
                               ('2026-09-24T00:00:00+00:00', 'job-2'))
        with self.assertRaisesRegex(ValueError, 'invalid_duration'):
            inspect_remote_form_repeats(self.database, **self.arguments())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE desktop_tasks SET updated_at=?,status=? WHERE job_id=?',
                               ('2026-09-25T00:00:01+00:00', 'succeeded', 'job-2'))
            connection.execute('UPDATE runs SET status=?,outcome=? WHERE run_id=?',
                               ('succeeded', 'passed', 'run-2'))
        with self.assertRaises(ValueError):
            inspect_remote_form_repeats(self.database, **self.arguments())

    def test_success_count_requires_same_snapshot_source_audit(self):
        self.insert_failure(1)
        self.insert_failure(2)
        self.store.close()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('UPDATE desktop_tasks SET status=? WHERE job_id=?',
                               ('succeeded', 'job-1'))
            connection.execute('UPDATE runs SET status=?,outcome=? WHERE run_id=?',
                               ('succeeded', 'passed', 'run-1'))
        with audit_snapshot(self.database) as (_snapshot, identity):
            snapshot_sha = identity['sha256']
        with (patch('aos.remote_form_repeat.inspect_remote_form_learning_source',
                    return_value={'snapshot_sha256': snapshot_sha}) as source_audit,
              patch('aos.remote_form_repeat._post_approval_stage_durations',
                    return_value={tool: 100.0 for tool in (
                        'browser.form.open', 'browser.form.fill',
                        'browser.form.submit', 'browser.form.receipt')})):
            report = inspect_remote_form_repeats(self.database, **self.arguments())
        self.assertEqual((report['bound_attempts'], report['transport_verified'],
                          report['failed_or_cancelled']), (2, 1, 1))
        source_audit.assert_called_once()
        self.assertEqual(report['post_approval_stage_ms']['browser.form.submit']['samples'], 1)
        self.assertEqual(source_audit.call_args.args[1], 'run-1')
        with patch('aos.remote_form_repeat.inspect_remote_form_learning_source',
                   return_value={'snapshot_sha256': '0' * 64}):
            with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
                inspect_remote_form_repeats(self.database, **self.arguments())

    def test_authenticated_api_uses_only_pinned_source(self):
        self.insert_failure(1)
        self.insert_failure(2)
        controller = SimpleNamespace(store=self.store)
        scheduler = SimpleNamespace(store=self.store, remote_form_plan=self.plan,
                                    remote_entry_profiles=self.profiles,
                                    remote_entry_profile_sha256=self.profile_sha,
                                    remote_form_state_plan=None, remote_form_cookie=None)
        origin = 'http://127.0.0.1:8765'
        app = create_console(controller, 'synthetic-token', origin, self.root,
                             self.database, scheduler)

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                self.assertEqual((await client.get('/api/remote-form/repeats')).status_code, 401)
                self.assertEqual((await client.post('/api/login',
                                                    json={'token': 'synthetic-token'})).status_code, 200)
                self.assertEqual((await client.get('/api/remote-form/repeats?run_id=run-1')).status_code, 400)
                response = await client.get('/api/remote-form/repeats')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['bound_attempts'], 2)
                self.assertNotIn(self.profile.entry_url, response.text)
                self.insert_state_failure(3)
                self.insert_state_failure(4)
                scheduler.remote_form_state_plan = self.state_plan
                state_response = await client.get('/api/remote-form/repeats')
                self.assertEqual(state_response.status_code, 200, state_response.text)
                self.assertEqual(state_response.json()['mode'],
                                 'read_only_post_admission_form_state_repeats')
                self.assertEqual(state_response.json()['bound_attempts'], 2)
                self.assertNotIn(self.state_plan.state_url, state_response.text)
                scheduler.remote_form_state_plan = object()
                self.assertEqual((await client.get('/api/remote-form/repeats')).status_code, 409)
                scheduler.remote_form_state_plan = None
                scheduler.remote_entry_profile_sha256 = '0' * 64
                self.assertEqual((await client.get('/api/remote-form/repeats')).status_code, 409)
                scheduler.remote_entry_profile_sha256 = self.profile_sha
                scheduler.remote_form_cookie = object()
                self.assertEqual((await client.get('/api/remote-form/repeats')).status_code, 409)
                scheduler.remote_form_cookie = None
                scheduler.remote_form_plan = None
                self.assertEqual((await client.get('/api/remote-form/repeats')).status_code, 404)

        asyncio.run(exercise())
        self.store.close()


if __name__ == '__main__':
    unittest.main()
