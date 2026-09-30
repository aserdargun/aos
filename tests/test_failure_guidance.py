import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import AOSFault, HELLO_CONTENT, Prediction, REPO_ROOT, canonical, digest, now
from aos.decision import FixtureDecisionEngine, decision_request
from aos.failure_guidance import (GUIDANCE, MARKER, FailureGuidanceContext, FailureGuidancePreview,
                                  FailureGuidanceReport, FailureGuidanceStart)
import test_failure_followup_console as followup_fixtures


class CapturingFixtureEngine(FixtureDecisionEngine):
    def __init__(self):
        self.states = []

    async def decide(self, state, options):
        self.states.append((state, options))
        return await super().decide(state, options)


class FailureGuidanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = followup_fixtures.FailureFollowupConsoleTests.asyncSetUp
    asyncTearDown = followup_fixtures.FailureFollowupConsoleTests.asyncTearDown
    authority = followup_fixtures.FailureFollowupConsoleTests.authority
    approval = followup_fixtures.FailureFollowupConsoleTests.approval
    client = followup_fixtures.FailureFollowupConsoleTests.client

    async def source(self, code='refresh_observation'):
        self.scheduler.engine = followup_fixtures.FailedFixtureEngine()
        started = self.scheduler.start(**self.authority())
        await self.scheduler.task
        self.engine = CapturingFixtureEngine()
        self.scheduler.engine = self.engine
        service = self.scheduler.failure_improvements
        candidate = service.preview(started['job_id'])
        service.save(started['job_id'], candidate['candidate_sha256'], True)
        option = next(value for value in candidate['review_options']
                      if value['decision'] == 'accept' and value['correction_code'] == code)
        review = service.review(started['job_id'], candidate['candidate_sha256'], **option)
        return {'source_job_id': started['job_id'], 'candidate_sha256': candidate['candidate_sha256'],
                'receipt_sha256': review['review']['receipt_sha256']}

    def request(self, preview):
        return {'preview': preview, 'confirm_sha256': preview['confirm_sha256'],
                'consent': True, **self.authority()}

    async def started(self, code='refresh_observation'):
        selection = await self.source(code)
        preview = self.scheduler.preview_failure_guidance(**selection)
        started = self.scheduler.start_failure_guidance(**self.request(preview))
        return selection, preview, started

    async def complete(self, started):
        approval = await self.approval()
        self.assertEqual(approval['job_id'], started['job_id'])
        self.assertIsNone(self.scheduler.status()['auto_approval'])
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(self.scheduler.status()['jobs'][0]['status'], 'succeeded')

    async def test_finite_context_snapshot_request_binding_and_fixture_boundary(self):
        selection, preview, started = await self.started()
        await self.complete(started)
        state, options = self.engine.states[0]
        self.assertIn(GUIDANCE['refresh_observation'], state.observation)
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        marker = self.store.connection.execute('SELECT payload_json FROM observations WHERE run_id=? AND kind=?',
                                               (state.run_id, MARKER)).fetchone()
        marker = json.loads(marker[0])
        self.assertEqual(marker['request_sha256'], digest(decision_request(state, options)))
        self.assertEqual(marker['state_sha256'], digest(state.model_dump(mode='json')))
        self.assertEqual(marker['candidate_sha256'], selection['candidate_sha256'])
        report = self.scheduler.failure_guidance.inspect(started['job_id'])
        self.assertTrue(report['context_binding_verified'])
        self.assertFalse(report['model_request_verified'])
        self.assertFalse(report['guidance_applied'])
        self.assertEqual(report['followup']['outcome']['status'], 'verified')
        for field in ('guidance_applied', 'causality_verified', 'gold', 'training_ready'):
            self.assertFalse(report['followup'][field])
        for field in ('causality_verified', 'gold', 'training_ready'):
            self.assertFalse(report[field])
        self.assertEqual(self.scheduler.status()['jobs'][0]['failure_guidance']['guidance_intent_sha256'],
                         started['guidance_intent_sha256'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions WHERE run_id=(SELECT run_id FROM desktop_tasks WHERE job_id=?)',
                                                       (selection['source_job_id'],)).fetchone()[0], 0)
        with self.assertRaises((ValueError, OSError, AOSFault)):
            self.scheduler.start_failure_guidance(**self.request(preview))

    async def test_inspect_before_retry_uses_finite_context_without_extra_effects(self):
        unused_selection, unused_preview, started = await self.started('inspect_before_retry')
        await self.complete(started)
        self.assertIn(GUIDANCE['inspect_before_retry'], self.engine.states[0][0].observation)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM actions WHERE tool='filesystem.write'").fetchone()[0], 1)

    async def test_unsupported_guidance_is_not_execution_authority(self):
        for code in ('request_clarification', 'repair_environment'):
            with self.subTest(code=code):
                selection = await self.source(code)
                with self.assertRaises(ValueError):
                    self.scheduler.preview_failure_guidance(**selection)
        self.assertFalse((self.root / 'failure-guidance').exists())
        self.assertEqual(len(self.scheduler.status()['jobs']), 2)

    async def test_exact_api_auth_consent_control_and_fields(self):
        selection = await self.source()
        async with self.client() as client:
            request = {'schema_version': '1.0', **selection}
            self.assertEqual((await client.post('/api/tasks/failure-guidance/preview', json=request)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post('/api/tasks/failure-guidance/preview', json=request)
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()
            self.assertFalse((self.root / 'failure-guidance').exists())
            for changes in ({'consent': False}, {'consent': 1}, {'generation': True},
                            {'lease_id': 'stale'}, {'confirm_sha256': '0' * 64}):
                response = await client.post('/api/tasks/failure-guidance/start', json={
                    'schema_version': '1.0', **self.request(preview), **changes})
                self.assertEqual(response.status_code, 409, response.text)
            for payload in (request | {'extra': True}, request | {'schema_version': '2.0'}):
                self.assertEqual((await client.post('/api/tasks/failure-guidance/preview', json=payload)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/failure-guidance/preview?extra=1', json=request)).status_code, 400)
            self.assertEqual((await client.post('/api/tasks/failure-guidance/preview', json=request,
                headers={'Origin': 'http://wrong.example'})).status_code, 403)
            self.assertEqual((await client.post('/api/tasks/failure-guidance/inspect',
                content='{"schema_version":"1.0","job_id":"one","job_id":"two"}')).status_code, 400)
            self.assertEqual(len(self.scheduler.status()['jobs']), 1)
            response = await client.post('/api/tasks/failure-guidance/start', json={
                'schema_version': '1.0', **self.request(preview)})
            self.assertEqual(response.status_code, 200, response.text)
            started = response.json()
            await self.complete(started)
            response = await client.post('/api/tasks/failure-guidance/inspect', json={
                'schema_version': '1.0', 'job_id': started['job_id']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['context_binding_verified'])

    async def test_tampered_or_stale_context_preview_cannot_start(self):
        selection = await self.source()
        preview = self.scheduler.preview_failure_guidance(**selection)
        for changes in ({'guidance_code': 'repair_environment'}, {'context_sha256': '0' * 64},
                        {'context_version': 'unsupported'}, {'guidance_applied': True}):
            with self.assertRaises(ValueError):
                self.scheduler.start_failure_guidance(**self.request(preview | changes))
        changed = copy.deepcopy(preview)
        changed['followup']['target']['task_kind'] = 'browser_form'
        with self.assertRaises(ValueError):
            self.scheduler.start_failure_guidance(**self.request(changed))
        self.scheduler.settings = self.settings.model_copy(update={'model_timeout_seconds': 121.0})
        with self.assertRaises(ValueError):
            self.scheduler.start_failure_guidance(**self.request(preview))
        self.assertEqual(len(self.scheduler.status()['jobs']), 1)

    async def test_revocation_after_observation_before_decision_blocks_call(self):
        selection = await self.source()
        preview = self.scheduler.preview_failure_guidance(**selection)
        original = FailureGuidanceContext.apply

        def revoke_before_apply(context, state, observation):
            self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                       selection['receipt_sha256'])
            return original(context, state, observation)

        with patch.object(FailureGuidanceContext, 'apply', revoke_before_apply):
            self.scheduler.start_failure_guidance(**self.request(preview))
            await self.scheduler.task
        self.assertEqual(self.engine.states, [])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_revocation_during_approval_blocks_actual_effect(self):
        selection, unused_preview, unused_started = await self.started()
        approval = await self.approval()
        self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                   selection['receipt_sha256'])
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertEqual(len(self.engine.states), 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 0)
        self.assertFalse((self.runtime.root / 'hello.txt').exists())

    async def test_revocation_after_context_record_before_call_blocks_engine(self):
        selection = await self.source()
        preview = self.scheduler.preview_failure_guidance(**selection)
        original = FailureGuidanceContext.record

        def revoke_after_record(context, state, options, call_id):
            original(context, state, options, call_id)
            self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                       selection['receipt_sha256'])

        with patch.object(FailureGuidanceContext, 'record', revoke_after_record):
            started = self.scheduler.start_failure_guidance(**self.request(preview))
            await self.scheduler.task
        self.assertEqual(self.engine.states, [])
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        report = self.scheduler.failure_guidance.inspect(started['job_id'])
        self.assertFalse(report['guidance_applied'])
        self.assertFalse(report['model_request_verified'])

    async def test_changed_context_marker_blocks_effect_and_application_report(self):
        unused_selection, unused_preview, started = await self.started()
        approval = await self.approval()
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json='{}' WHERE kind=?", (MARKER,))
        self.scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await self.scheduler.task
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        report = self.scheduler.failure_guidance.inspect(started['job_id'])
        for field in ('context_binding_verified', 'model_request_verified', 'guidance_applied'):
            self.assertFalse(report[field])

    async def test_historical_outcome_is_separate_from_tampered_context_and_revocation(self):
        selection, unused_preview, started = await self.started()
        await self.complete(started)
        with self.store.connection:
            self.store.connection.execute("UPDATE observations SET payload_json='{}' WHERE kind=?", (MARKER,))
        self.scheduler.failure_improvements.revoke(selection['source_job_id'], selection['candidate_sha256'],
                                                   selection['receipt_sha256'])
        report = self.scheduler.failure_guidance.inspect(started['job_id'])
        self.assertFalse(report['context_binding_verified'])
        self.assertFalse(report['guidance_applied'])
        self.assertFalse(report['followup']['current_review_valid'])
        self.assertTrue(report['followup']['historical_binding_verified'])
        self.assertEqual(report['followup']['outcome']['status'], 'verified')

    async def test_nested_report_uses_the_same_frozen_database_connection(self):
        unused_selection, unused_preview, started = await self.started()
        await self.complete(started)
        with patch('aos.failure_followup.audit_snapshot', side_effect=AssertionError('second snapshot forbidden')):
            report = self.scheduler.failure_guidance.inspect(started['job_id'])
        self.assertTrue(report['context_binding_verified'])
        self.assertEqual(report['followup']['outcome']['status'], 'verified')

    async def test_private_intent_tampering_cannot_be_reported_as_application(self):
        unused_selection, unused_preview, started = await self.started()
        await self.complete(started)
        intent = self.scheduler.failure_guidance.directory / ('intent-' + started['guidance_intent_sha256'] + '.json')
        os.chmod(intent, 0o600)
        intent.write_text('{}')
        with self.assertRaises(ValueError):
            self.scheduler.failure_guidance.inspect(started['job_id'])

    async def test_context_without_followup_cannot_admit_a_job(self):
        for direct in (False, True):
            with self.assertRaises(AOSFault):
                method = self.scheduler._start if direct else self.scheduler.start
                method(**self.authority(), kind='hello', failure_guidance='0' * 64)
        self.assertEqual(self.scheduler.status()['jobs'], [])

    async def test_synthetic_call_binding_requires_success_and_matching_prediction(self):
        unused_selection, unused_preview, started = await self.started()
        await self.complete(started)
        state, options = self.engine.states[0]
        service = self.scheduler.failure_guidance
        intent = service._intent(started['guidance_intent_sha256'])
        payload = service._binding(started['guidance_intent_sha256'], started['intent_sha256'], started['job_id'])
        job = dict(self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                                                 (started['job_id'],)).fetchone())
        job['real_model'] = 1
        snapshot = self.store.connection.execute('SELECT snapshot_id FROM state_snapshots WHERE run_id=? AND state_version=?',
                                                 (state.run_id, state.state_version)).fetchone()[0]
        prediction = Prediction(selected_option=options[0].id,
                                probabilities={option.id: float(index == 0) for index, option in enumerate(options)})
        marker = service._marker(payload, intent['preview'], state, snapshot, options, 'call-synthetic-proof')
        with self.store.connection:
            self.store.insert('model_calls', call_id='call-synthetic-proof', run_id=state.run_id, step_id=state.step_id,
                              deployment_id=state.deployment_id, role='system1', status='error',
                              request_json=canonical(decision_request(state, options)),
                              response_json=prediction.model_dump_json(), created_at=now())
            self.store.connection.execute('UPDATE observations SET payload_json=?,created_at=? WHERE run_id=? AND kind=?',
                                          (canonical(marker), now(), state.run_id, MARKER))
            self.store.connection.execute('UPDATE decisions SET call_id=?,created_at=? WHERE run_id=?',
                                          ('call-synthetic-proof', now(), state.run_id))
        self.assertEqual(service._application(self.store.connection, intent, payload, job), (True, False))
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET status='ok' WHERE call_id='call-synthetic-proof'")
        self.assertEqual(service._application(self.store.connection, intent, payload, job), (True, True))
        with self.store.connection:
            self.store.connection.execute("UPDATE model_calls SET request_json='{}' WHERE call_id='call-synthetic-proof'")
        with self.assertRaises(ValueError):
            service._application(self.store.connection, intent, payload, job)
        with self.store.connection:
            self.store.connection.execute('UPDATE model_calls SET request_json=?,response_json=? WHERE call_id=?',
                                          (canonical(decision_request(state, options)), '{}', 'call-synthetic-proof'))
        with self.assertRaises(ValueError):
            service._application(self.store.connection, intent, payload, job)

    async def test_synthetic_examples_and_canonical_schemas(self):
        from aos.dataset import validator

        for name, model in (('preview', FailureGuidancePreview), ('start', FailureGuidanceStart),
                            ('report', FailureGuidanceReport)):
            fixture = json.loads((REPO_ROOT / 'examples' / ('failure_guidance_' + name + '.json')).read_text())
            validator('failure_guidance_' + name).validate(fixture)
            self.assertEqual(model.model_validate(fixture).model_dump(mode='json'), fixture)
            for field in ('causality_verified', 'gold', 'training_ready'):
                if field in fixture:
                    with self.assertRaises(ValueError):
                        model.model_validate(fixture | {field: True})
            if name == 'report':
                for changes in ({'guidance_applied': True}, {'model_request_verified': True},
                                {'context_binding_verified': False, 'model_request_verified': True,
                                 'guidance_applied': True}):
                    with self.assertRaises(ValueError):
                        model.model_validate(fixture | changes)
                    with self.assertRaises(jsonschema.ValidationError):
                        validator('failure_guidance_report').validate(fixture | changes)
                accepted = fixture | {'context_binding_verified': True, 'model_request_verified': True,
                                      'guidance_applied': True}
                model.model_validate(accepted)
                validator('failure_guidance_report').validate(accepted)


if __name__ == '__main__':
    unittest.main()
