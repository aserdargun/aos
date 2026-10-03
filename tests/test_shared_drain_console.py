import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import jsonschema

from aos.contracts import REPO_ROOT, Settings, digest
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.scientist_lab import ScientistLabClient
from aos.scientist_lab_service import ScientistLabService
from aos.shared_drain import SavedSharedDrainObservation, SharedDrainReceipt, read_shared_drain_receipt
from aos.storage import TrajectoryStore


class SharedDrainConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='synthetic-shared-drain-')
        self.root = Path(self.temporary.name)
        self.database = self.root / 'synthetic.sqlite3'
        self.store = TrajectoryStore(self.database)
        runtime = SimpleNamespace(runtime_id='runtime-synthetic', pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.scheduler = DesktopScheduler(self.controller,
            Settings(workspace=self.root / 'workspace', database=self.database), FixtureDecisionEngine())
        token = self.root / 'synthetic.token'
        token.write_text('synthetic-local-test-token\n')
        token.chmod(0o600)
        self.remote = ScientistLabClient('http://127.0.0.1:54321', token, principal_id='synthetic-aos',
            allowed_suites=frozenset({'synthetic.allowed.v1'}))
        self.service = ScientistLabService(self.controller, self.remote,
            authorization_context_sha256='a' * 64, program_version='director.v1',
            verify_capability=lambda *arguments: None)
        self.app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
            scheduler=self.scheduler, scientist_lab=self.service, web_profiles_root=self.root / 'profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.headers = {'Origin': 'http://testserver'}
        response = await self.client.post('/api/login', headers=self.headers, json={'token': 'synthetic-token'})
        self.assertEqual(response.status_code, 200)
        state = self.controller.state()
        self.request = {'request_id': 'synthetic-drain', **{field: state[field] for field in (
            'session_id', 'runtime_id', 'owner', 'lease_id', 'generation')}}
        self.proposal = {'suite': 'synthetic.allowed.v1', 'track': 'anomaly', 'program_version': 'director.v1',
                         'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 100}}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.service.close_async()
        await self.scheduler.close()
        self.store.close()
        self.temporary.cleanup()

    async def drain(self):
        return await self.client.post('/api/shared/drain', headers=self.headers, json=self.request)

    async def test_idle_receipt_is_canonical_idempotent_readable_and_not_gpu_authority(self):
        response = await self.drain()
        self.assertEqual(response.status_code, 200, response.text)
        saved = SavedSharedDrainObservation.model_validate(response.json(), strict=True)
        self.assertEqual(saved.receipt.process.pid, os.getpid())
        self.assertTrue(saved.receipt.observation.admission_closed)
        self.assertTrue(saved.receipt.observation.local_controls_drained)
        self.assertFalse(saved.receipt.observation.gpu_release_verified)
        self.assertFalse(saved.receipt.observation.native_gpu_excluded)
        self.assertFalse(saved.receipt.observation.remote_jobs_stopped_verified)
        repeated = await self.drain()
        self.assertEqual(repeated.json(), response.json())
        readback = await self.client.get('/api/shared/drain/receipts/' + saved.receipt_id)
        self.assertEqual(readback.json(), response.json())
        connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)
        try:
            offline = read_shared_drain_receipt(connection, session_id=self.controller.session_id,
                event_id=saved.receipt_id, expected_sha256=saved.receipt_sha256)
            self.assertEqual(offline, saved)
            with self.assertRaises(ValueError):
                read_shared_drain_receipt(connection, session_id=self.controller.session_id,
                    event_id=saved.receipt_id, expected_sha256='0' * 64)
        finally:
            connection.close()
        for name, model, value in [('receipt', SharedDrainReceipt, saved.receipt.model_dump(mode='json')),
                                  ('saved_observation', SavedSharedDrainObservation, saved.model_dump(mode='json'))]:
            schema = json.loads((REPO_ROOT / ('schemas/shared_drain_' + name + '.schema.json')).read_text())
            self.assertEqual({key: value for key, value in schema.items() if key != '$schema'}, model.model_json_schema())
            jsonschema.Draft202012Validator(schema).validate(value)
        self.assertEqual(saved.receipt_sha256, digest(saved.receipt.model_dump(mode='json')))
        for path, payload in [('/api/control', {'command': 'resume'}),
                              ('/api/restart/release', {'session_id': self.controller.session_id}),
                              ('/api/scientist/propose', self.proposal)]:
            rejected = await self.client.post(path, headers=self.headers, json=payload)
            self.assertEqual(rejected.status_code, 409, rejected.text)

    async def test_unauthenticated_stale_and_malformed_requests_never_latch(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver') as client:
            response = await client.post('/api/shared/drain', headers=self.headers, json=self.request)
            self.assertEqual(response.status_code, 401)
        for change, expected in [({'generation': 1}, 409), ({'generation': True}, 400),
                                 ({'unknown': True}, 400), ({'owner': 'HUMAN'}, 400)]:
            response = await self.client.post('/api/shared/drain', headers=self.headers,
                json={**self.request, **change})
            self.assertEqual(response.status_code, expected, response.text)
        duplicate = json.dumps(self.request)[:-1] + ',"generation":0}'
        response = await self.client.post('/api/shared/drain', headers=self.headers, content=duplicate)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.scheduler.restart_quiesced)
        self.assertFalse(self.service.shared_drain_status()['admission_closed'])

    async def test_request_body_race_cannot_admit_lab_start_after_drain(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        payload = json.dumps(self.proposal).encode()
        async def delayed_body():
            yield payload[:1]
            entered.set()
            await release.wait()
            yield payload[1:]
        pending = asyncio.create_task(self.client.post('/api/scientist/propose', headers=self.headers,
            content=delayed_body()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            drained = await self.drain()
            self.assertEqual(drained.status_code, 200, drained.text)
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 2)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_jobs').fetchone()[0], 0)

    async def test_pending_lab_approval_blocks_local_drain_but_rejection_can_resolve_it(self):
        proposal = await self.client.post('/api/scientist/propose', headers=self.headers, json=self.proposal)
        self.assertEqual(proposal.status_code, 200, proposal.text)
        approval = proposal.json()
        first = (await self.drain()).json()
        self.assertTrue(first['receipt']['observation']['admission_closed'])
        self.assertFalse(first['receipt']['observation']['local_controls_drained'])
        self.assertIn('lab.pending_actions', first['receipt']['observation']['blockers'])
        denied = await self.client.post('/api/scientist/approve', headers=self.headers,
            json={'action_id': approval['action_id'], 'envelope_sha256': approval['envelope_sha256'], 'accept': True})
        self.assertEqual(denied.status_code, 409, denied.text)
        rejected = await self.client.post('/api/scientist/approve', headers=self.headers,
            json={'action_id': approval['action_id'], 'envelope_sha256': approval['envelope_sha256'], 'accept': False})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        second = (await self.drain()).json()
        self.assertTrue(second['receipt']['observation']['local_controls_drained'])
        self.assertNotEqual(first['receipt_sha256'], second['receipt_sha256'])
        historical = await self.client.get('/api/shared/drain/receipts/' + first['receipt_id'])
        self.assertEqual(historical.json(), first)

    async def test_receipt_write_failure_keeps_both_latches_closed(self):
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('Synthetic write failure')):
            response = await self.drain()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.scheduler.restart_quiesced)
        self.assertTrue(self.service.shared_drain_status()['admission_closed'])
        recovered = await self.drain()
        self.assertEqual(recovered.status_code, 200, recovered.text)
        saved = recovered.json()
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_events SET payload_json=? WHERE event_id=?',
                (' ' + json.dumps(saved['receipt']), saved['receipt_id']))
        response = await self.client.get('/api/shared/drain/receipts/' + saved['receipt_id'])
        self.assertEqual(response.status_code, 404)

    async def test_final_seal_requires_idle_then_cannot_reopen_any_control(self):
        proposal = await self.client.post('/api/scientist/propose', headers=self.headers, json=self.proposal)
        approval = proposal.json()
        blocked = await self.client.post('/api/shared/drain/seal', headers=self.headers, json=self.request)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertFalse(self.service.cleanup_controls_closed)
        self.assertTrue(self.scheduler.restart_quiesced)
        rejected = await self.client.post('/api/scientist/approve', headers=self.headers,
            json={'action_id': approval['action_id'], 'envelope_sha256': approval['envelope_sha256'], 'accept': False})
        self.assertEqual(rejected.status_code, 200, rejected.text)
        sealed = await self.client.post('/api/shared/drain/seal', headers=self.headers, json=self.request)
        self.assertEqual(sealed.status_code, 200, sealed.text)
        result = sealed.json()
        observation = result['receipt']['observation']
        self.assertTrue(observation['cleanup_controls_closed'])
        self.assertTrue(observation['local_controls_drained'])
        self.assertFalse(observation['gpu_release_verified'])
        repeated = await self.client.post('/api/shared/drain/seal', headers=self.headers, json=self.request)
        self.assertEqual(repeated.json(), result)
        denied = await self.client.post('/api/scientist/propose', headers=self.headers, json=self.proposal)
        self.assertEqual(denied.status_code, 409, denied.text)
        historical = await self.client.get('/api/shared/drain/receipts/' + result['receipt_id'])
        self.assertEqual(historical.json(), result)

    async def test_final_seal_receipt_failure_keeps_all_admission_closed(self):
        self.assertEqual((await self.drain()).status_code, 200)
        with patch.object(self.store, 'insert', side_effect=sqlite3.OperationalError('Synthetic write failure')):
            response = await self.client.post('/api/shared/drain/seal', headers=self.headers, json=self.request)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(self.service.cleanup_controls_closed)
        self.assertTrue(self.scheduler.restart_quiesced)
        recovered = await self.client.post('/api/shared/drain/seal', headers=self.headers, json=self.request)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        with self.store.connection:
            self.store.connection.execute('UPDATE desktop_sessions SET generation=generation+1 WHERE session_id=?',
                (self.controller.session_id,))
        stale = await self.drain()
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(self.service.cleanup_controls_closed)
