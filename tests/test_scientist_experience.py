"""Synthetic owner-bound history and context readback; not remote experiment acceptance."""

import asyncio
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch

import jsonschema

from aos.contracts import REPO_ROOT
from aos.scientist_cpu_capability import cpu_capability_sha256
from aos.scientist_experience import EXPERIENCE_BOUND, ScientistExperience, ScientistExperienceReadback, verify_scientist_experience
from aos.scientist_lab import ScientistLabStart
from aos.scientist_protocol import ScientistReport
from aos.scientist_transport import ScientistAdmissionError
import test_scientist_cpu_session as sessions


def experience_fixture():
    return json.loads((REPO_ROOT / 'examples/scientist_experience.json').read_text())


def context_usage(request, *, count):
    context = {'schema': 'field-study-context.v1', 'intent': request.field_intent.model_dump(mode='json'),
        'snapshot_sha256': 'd' * 64, 'asset_identity': 'user_supplied', 'use': 'advisory-only'}
    return {'field_context_sha256': cpu_capability_sha256(context),
        **{key: value for key, value in context.items() if key != 'schema'},
        'status': 'context-bound' if count else 'admitted-only', 'context_bound_proposal_count': count}


class ScientistExperienceTests(unittest.TestCase):
    def setUp(self):
        self.wire = experience_fixture()
        self.report = ScientistReport(run_id=self.wire['run_id'], report_sha256=self.wire['report_sha256'],
            report={'run_id': self.wire['run_id'], 'status': 'completed'}, verified_at='synthetic')
        self.request = ScientistLabStart.model_validate(json.loads(
            (REPO_ROOT / 'examples/scientist_lab_context_start.json').read_text()))
        self.bare = self.request.model_copy(update={'field_intent': None, 'prior_experience': None})

    def verify(self, wire=None, request=None):
        return verify_scientist_experience(json.dumps(self.wire if wire is None else wire).encode(),
            report=self.report, request=self.bare if request is None else request)

    def test_synthetic_canonical_schema_and_independent_boundary_flags(self):
        for model, name in ((ScientistExperience, 'scientist_experience'),
                            (ScientistExperienceReadback, 'scientist_experience_readback')):
            schema = json.loads((REPO_ROOT / ('schemas/' + name + '.schema.json')).read_text())
            self.assertEqual(schema, model.model_json_schema() | {'$schema': 'https://json-schema.org/draft/2020-12/schema'})
            jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(self.wire, json.loads((REPO_ROOT / 'schemas/scientist_experience.schema.json').read_text()))
        result = self.verify()
        self.assertTrue(result.independent_report_verified)
        self.assertFalse(result.execution_authorized)
        self.assertEqual(result.ledger_verification, 'scientist-reported-not-independently-replayed')
        self.assertTrue(result.experience.records[0].history_eligibility.eligible)
        self.assertFalse(result.experience.records[0].training_eligibility.eligible)

    def test_report_identity_training_holdout_unknown_and_duplicate_records_rejected(self):
        invalid = [self.wire | {field: value} for field, value in (
            ('schema', 'unknown'), ('run_id', '22222222-2222-2222-2222-222222222222'),
            ('report_sha256', 'f' * 64), ('purpose', 'baseline'), ('training_started', True),
            ('training_started', 0), ('holdout_included', 0), ('record_limit', 201), ('extra', 'synthetic'))]
        invalid.append(self.wire | {'records': self.wire['records'] * 2})
        for field, value in (('kind', 'baseline'), ('status', 'crashed'), ('experiment_sha256', None),
                             ('score', float('nan')), ('history_eligibility', {'eligible': 1, 'reasons': []}),
                             ('training_eligibility', {'eligible': True, 'reasons': []})):
            candidate = deepcopy(self.wire)
            candidate['records'][0][field] = value
            invalid.append(candidate)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.verify(candidate)
        for raw in (b'{"schema":1,"schema":2}', b' ' * (EXPERIENCE_BOUND + 1)):
            with self.assertRaises(ValueError):
                verify_scientist_experience(raw, report=self.report, request=self.bare)

    def test_context_usage_requires_exact_original_intent_and_selected_source(self):
        for count in (0, 1):
            field = context_usage(self.request, count=count)
            prior = {'snapshot_sha256': 'e' * 64, 'source_run_id': self.request.prior_experience.source_run_id,
                'source_report_sha256': self.request.prior_experience.source_report_sha256,
                'record_count': len(self.request.prior_experience.records), 'context_bound_proposal_count': count,
                'status': 'context-bound' if count else 'admitted-only', 'scope': 'historical-advisory-only'}
            wire = self.wire | {'field_context_usage': field, 'prior_findings_usage': prior}
            self.assertEqual(self.verify(wire, self.request).experience.prior_findings_usage.status, prior['status'])
            invalid = [wire | {'field_context_usage': None}, wire | {'prior_findings_usage': None},
                wire | {'field_context_usage': field | {'field_context_sha256': '0' * 64}},
                wire | {'field_context_usage': field | {'intent': field['intent'] | {'asset_id': 'different'}}},
                wire | {'prior_findings_usage': prior | {'record_count': 8}},
                wire | {'prior_findings_usage': prior | {'source_report_sha256': '0' * 64}},
                wire | {'prior_findings_usage': prior | {'status': 'admitted-only' if count else 'context-bound'}}]
            for changed in invalid:
                with self.assertRaises(ValueError):
                    self.verify(changed, self.request)
            with self.assertRaises(ValueError):
                self.verify(wire, self.bare)
        with self.assertRaises(ValueError):
            self.verify(self.wire, self.request)


class ScientistExperienceConsoleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = sessions.ScientistCpuSessionTests.asyncSetUp
    asyncTearDown = sessions.ScientistCpuSessionTests.asyncTearDown
    login = sessions.ScientistCpuSessionTests.login
    post = sessions.ScientistCpuSessionTests.post

    def transport(self, method, path, body, deadline, *, bound):
        if path.endswith('/experience'):
            self.requests.append((method, path))
            self.assertEqual((method, body, bound), ('GET', b'', EXPERIENCE_BOUND))
            value = experience_fixture() | {'run_id': self.remote_run, 'report_sha256': self.report_sha}
            value.update(getattr(self, 'experience_changes', {}))
            if getattr(self, 'status_drift', False):
                self.remote_state = 'failed'
            return json.dumps(value).encode()
        return sessions.ScientistCpuSessionTests.transport(self, method, path, body, deadline, bound=bound)

    async def start(self):
        await self.login()
        approval = (await self.post('propose', self.proposal)).json()
        self.assertEqual((await self.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': approval['envelope_sha256'], 'accept': True})).status_code, 200)
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 200)
        self.local_run = approval['envelope']['task']['request']['external_run_id']
        self.remote_state = 'completed'
        self.requests.clear()

    async def test_exact_current_job_status_report_experience_status_no_additional_effects(self):
        self.assertEqual((await self.post('experience', {'run_id': 'unknown'})).status_code, 401)
        await self.start()
        response = await self.post('experience', {'run_id': self.local_run})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['run_id'], self.remote_run)
        self.assertEqual(response.json()['experience']['schema'], 'run-experience.v1')
        run_reads = [path for method, path in self.requests if path.startswith('/v1/runs/')]
        self.assertEqual(run_reads, ['/v1/runs/' + self.remote_run, '/v1/runs/' + self.remote_run + '/report',
            '/v1/runs/' + self.remote_run + '/experience', '/v1/runs/' + self.remote_run])
        self.assertTrue(all(method == 'GET' for method, _path in self.requests))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_actions').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_readbacks').fetchone()[0], 0)
        self.assertIsNone(self.fixture.client.uncertain_action_id)

    async def test_unknown_cross_session_nonterminal_and_report_drift_fail_closed(self):
        await self.start()
        for payload in ({'run_id': self.remote_run}, {'run_id': 'unknown'}, {'run_id': self.local_run, 'owner': 'foreign'}):
            self.assertNotEqual((await self.post('experience', payload)).status_code, 200)
        self.assertEqual(self.requests, [])
        with patch.object(self.controller, 'session_id', 'foreign-session'):
            self.assertEqual((await self.post('experience', {'run_id': self.local_run})).status_code, 409)
        with patch.object(self.fixture.client, 'principal_id', 'foreign-owner'):
            self.assertEqual((await self.post('experience', {'run_id': self.local_run})).status_code, 409)
        self.assertEqual(self.requests, [])
        self.remote_state = 'running'
        self.assertEqual((await self.post('experience', {'run_id': self.local_run})).status_code, 409)
        self.assertFalse(any(path.endswith('/experience') for _method, path in self.requests))
        self.remote_state = 'completed'
        self.experience_changes = {'report_sha256': 'f' * 64}
        self.assertEqual((await self.post('experience', {'run_id': self.local_run})).status_code, 409)
        self.experience_changes = {}
        self.status_drift = True
        self.assertEqual((await self.post('experience', {'run_id': self.local_run})).status_code, 409)
        self.assertTrue(all(method == 'GET' for method, _path in self.requests))

    async def test_pending_history_read_does_not_block_inventory_and_rejects_generation_drift(self):
        await self.start()
        started, release = threading.Event(), threading.Event()
        original = self.fixture.client._request
        def request(method, path, *arguments, **options):
            if path.endswith('/experience'):
                started.set()
                if not release.wait(5):
                    raise TimeoutError()
            return original(method, path, *arguments, **options)
        self.fixture.client._request = request
        pending = asyncio.create_task(self.post('experience', {'run_id': self.local_run}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            self.assertEqual((await asyncio.wait_for(self.client.get('/api/scientist/jobs'), 0.5)).status_code, 200)
            with self.store.connection:
                self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1 WHERE session_id=?',
                    (self.controller.session_id,))
            release.set()
            self.assertEqual((await pending).status_code, 409)
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertIsNone(self.fixture.client.uncertain_action_id)

    async def test_cancel_keeps_read_worker_tracked_without_uncertain_effect_or_replay(self):
        await self.start()
        started, release = threading.Event(), threading.Event()
        original = self.fixture.client._request
        def request(method, path, *arguments, **options):
            if path.endswith('/experience'):
                started.set()
                if not release.wait(5):
                    raise TimeoutError()
            return original(method, path, *arguments, **options)
        self.fixture.client._request = request
        pending = asyncio.create_task(self.post('experience', {'run_id': self.local_run}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(self.fixture.client.local_cleanup_pending)
            with self.assertRaises(ScientistAdmissionError):
                await self.service.close_async(timeout_seconds=1)
            self.assertTrue(self.fixture.client.local_cleanup_pending)
            self.assertIsNone(self.fixture.client.uncertain_action_id)
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            await self.service.close_async(timeout_seconds=1)
        self.assertFalse(self.fixture.client.local_cleanup_pending)
        self.assertTrue(all(method == 'GET' for method, _path in self.requests))


if __name__ == '__main__':
    unittest.main()
