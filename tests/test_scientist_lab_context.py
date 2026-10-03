import copy
import json
import sqlite3
import unittest
from unittest.mock import Mock

import jsonschema
from pydantic import ValidationError

from aos.contracts import REPO_ROOT, canonical, digest
from aos.scientist_lab import ScientistLabAction, ScientistLabStart, ScientistLabTask
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_cpu_session as session_fixtures


def context_fixture():
    return json.loads((REPO_ROOT / 'examples/scientist_lab_context_start.json').read_text())


class ScientistLabContextTests(unittest.TestCase):
    def test_synthetic_example_matches_model_and_canonical_schema(self):
        value = context_fixture()
        schema = json.loads((REPO_ROOT / 'schemas/scientist_lab_start.schema.json').read_text())
        jsonschema.validate(value, schema)
        self.assertEqual(ScientistLabStart.model_validate(value).model_dump(mode='json'), value)

    def test_legacy_and_explicit_null_keep_original_bytes(self):
        legacy = context_fixture()
        legacy.pop('field_intent')
        legacy.pop('prior_experience')
        expected = json.dumps(legacy, ensure_ascii=False, separators=(',', ':'))
        for value in (legacy, legacy | {'field_intent': None, 'prior_experience': None}):
            request = ScientistLabStart.model_validate(value)
            self.assertEqual(request.model_dump_json(), expected)
            self.assertEqual(request.model_dump(mode='json'), legacy)
            self.assertEqual(canonical(request.model_dump(mode='json')), canonical(legacy))

    def test_strict_context_bounds_and_no_server_fields(self):
        invalid = []
        for field, value in [('asset_id', ''), ('asset_id', ' '), ('asset_id', 123),
                             ('asset_id', 'a' * 129), ('objective', 'a' * 601),
                             ('objective', 'bad\ntext'), ('objective', 'bad\x7ftext'),
                             ('goal_kind', 'execute')]:
            candidate = context_fixture()
            candidate['field_intent'][field] = value
            invalid.append(candidate)
        for field in ('field_context', 'prior_findings', 'dataset', 'algorithms', 'scores'):
            invalid.append(context_fixture() | {field: {}})
        for field in ('source_run_id', 'source_report_sha256'):
            candidate = context_fixture()
            candidate['prior_experience'][field] = 'invalid'
            invalid.append(candidate)
        for records in ([], context_fixture()['prior_experience']['records'] * 2,
                        context_fixture()['prior_experience']['records'] * 9):
            candidate = context_fixture()
            candidate['prior_experience']['records'] = records
            invalid.append(candidate)
        candidate = context_fixture()
        candidate['prior_experience']['records'][0]['score'] = 1.0
        invalid.append(candidate)
        invalid.append(context_fixture() | {'track': 'anomaly'})
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                ScientistLabStart.model_validate(candidate, strict=True)

    def test_normalization_eight_records_and_history_without_mode_intent(self):
        value = context_fixture()
        value['field_intent']['asset_id'] = '  synthetic-pump-01  '
        self.assertEqual(ScientistLabStart.model_validate(value).field_intent.asset_id, 'synthetic-pump-01')
        value.pop('field_intent')
        value['track'] = 'anomaly'
        template = value['prior_experience']['records'][0]
        value['prior_experience']['records'] = [template | {'experiment_id': 'exp_' + format(index, '032x')}
                                              for index in range(8)]
        self.assertEqual(len(ScientistLabStart.model_validate(value).prior_experience.records), 8)

    def test_each_context_identity_changes_approved_request_hash(self):
        original = context_fixture()
        original_hash = digest(ScientistLabStart.model_validate(original).model_dump(mode='json'))
        for path, value in [(['field_intent', 'asset_id'], 'synthetic-other'),
                            (['field_intent', 'goal_kind'], 'digital_twin'),
                            (['field_intent', 'objective'], 'Different synthetic question'),
                            (['prior_experience', 'source_report_sha256'], 'd' * 64),
                            (['prior_experience', 'source_run_id'], '33333333-3333-3333-3333-333333333333'),
                            (['prior_experience', 'records', 0, 'experiment_id'], 'exp_' + '4' * 32),
                            (['prior_experience', 'records', 0, 'experiment_sha256'], 'e' * 64),
                            (['prior_experience', 'records', 0, 'trajectory_sha256'], 'f' * 64)]:
            changed = copy.deepcopy(original)
            target = changed
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = value
            self.assertNotEqual(digest(ScientistLabStart.model_validate(changed).model_dump(mode='json')),
                                original_hash)


class ScientistLabContextSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = session_fixtures.ScientistCpuSessionTests()
        await self.session.asyncSetUp()
        self.addAsyncCleanup(self.session.asyncTearDown)
        self.posted = []
        original_transport = self.session.transport

        def transport(method, path, body, deadline, *, bound):
            if method == 'POST':
                self.posted.append(body)
            return original_transport(method, path, body, deadline, bound=bound)

        self.session.fixture.client._request = Mock(side_effect=transport)
        await self.session.login()
        fixture = context_fixture()
        self.proposal = self.session.proposal | {field: fixture[field]
            for field in ('field_intent', 'prior_experience')}

    async def approve(self, approval):
        response = await self.session.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': approval['envelope_sha256'], 'accept': True})
        self.assertEqual(response.status_code, 200, response.text)

    async def test_context_reaches_exact_durable_approved_body_only(self):
        response = await self.session.post('propose', self.proposal)
        self.assertEqual(response.status_code, 200, response.text)
        approval = response.json()
        self.assertEqual((await self.session.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertEqual(self.posted, [])
        request = approval['envelope']['task']['request']
        for field in ('field_intent', 'prior_experience'):
            self.assertEqual(request[field], self.proposal[field])
        self.assertEqual(approval['envelope']['action']['arguments'], {'request_sha256': digest(request)})
        await self.approve(approval)
        executed = await self.session.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(self.posted, [canonical(request).encode()])
        row = self.session.store.connection.execute('SELECT state,body FROM scientist_lab_actions').fetchone()
        self.assertEqual(tuple(row), ('acknowledged', canonical(request)))

    async def test_changed_context_cannot_consume_original_approval(self):
        approval = (await self.session.post('propose', self.proposal)).json()
        row = self.session.store.connection.execute('SELECT task_json FROM scientist_lab_actions').fetchone()
        task = json.loads(row['task_json'])
        task['request']['field_intent']['objective'] = 'Different synthetic objective'
        with self.assertRaises(sqlite3.IntegrityError):
            with self.session.store.connection:
                self.session.store.connection.execute('UPDATE scientist_lab_actions SET task_json=?',
                    (ScientistLabTask.model_validate(task).model_dump_json(),))
        response = await self.session.post('approve', {'action_id': approval['action_id'],
            'envelope_sha256': digest(approval['envelope'] | {'task': task}), 'accept': True})
        self.assertEqual(response.status_code, 409)
        await self.approve(approval)
        with self.assertRaises(ScientistAdmissionError):
            self.session.service.journal.execute(self.session.fixture.client,
                ScientistLabTask.model_validate(task),
                ScientistLabAction.model_validate(approval['envelope']['action']))
        self.assertEqual(self.posted, [])

    async def test_invalid_context_duplicates_and_budget_never_post(self):
        invalid = [self.proposal | {'field_context': {}},
                   self.proposal | {'track': 'anomaly'},
                   self.proposal | {'field_intent': {'asset_id': 'synthetic'}},
                   self.proposal | {'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 1}}]
        for value in invalid:
            response = await self.session.post('propose', value)
            self.assertIn(response.status_code, (400, 409), response.text)
        raw = json.dumps(self.proposal).replace('"asset_id":', '"asset_id":"other", "asset_id":')
        response = await self.session.client.post('/api/scientist/propose',
            headers=self.session.headers | {'Content-Type': 'application/json'}, content=raw)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.posted, [])
        self.assertEqual(self.session.store.connection.execute('SELECT count(*) FROM scientist_lab_actions').fetchone()[0], 0)

    async def test_legacy_persisted_pending_envelope_and_readback_remain_usable(self):
        approval = (await self.session.post('propose', self.session.proposal)).json()
        row = self.session.store.connection.execute('SELECT task_json FROM scientist_lab_actions').fetchone()
        legacy_json = row['task_json']
        self.assertNotIn('field_intent', legacy_json)
        self.assertNotIn('prior_experience', legacy_json)
        self.assertIn('"lab_run_id":null', legacy_json)
        self.assertEqual(ScientistLabTask.model_validate_json(legacy_json).model_dump_json(), legacy_json)
        await self.approve(approval)
        self.assertEqual((await self.session.post('execute', {'action_id': approval['action_id']})).status_code, 200)
        self.session.remote_state = 'completed'
        run_id = approval['envelope']['task']['request']['external_run_id']
        saved = await self.session.post('save_report', {'run_id': run_id,
            'expected_report_sha256': self.session.report_sha})
        self.assertEqual(saved.status_code, 200, saved.text)
        readback = await self.session.post('read_saved_report', {'run_id': run_id,
            'readback_id': saved.json()['readback_id']})
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(self.session.store.connection.execute('SELECT task_json FROM scientist_lab_actions').fetchone()[0], legacy_json)
