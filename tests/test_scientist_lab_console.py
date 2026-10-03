import asyncio
from pathlib import Path
import threading
import time
import tempfile
import shutil
from uuid import uuid4
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.contracts import REPO_ROOT, Settings
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_lab import ScientistLabClient
from aos.scientist_lab_service import ScientistLabService
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError
from aos.storage import TrajectoryStore
import test_scientist_lab as lab_fixtures


class ScientistLabConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = TrajectoryStore(self.root / 'synthetic.sqlite3')
        runtime = SimpleNamespace(runtime_id='runtime-synthetic', pins={'image_id': 'synthetic-image'})
        self.controller = DesktopController(self.store, runtime)
        self.server = lab_fixtures.SyntheticLabServer()
        token = self.root / 'synthetic.token'
        token.write_text('synthetic-test-token\n')
        token.chmod(0o600)
        self.remote = ScientistLabClient(self.server.url, token, principal_id='synthetic-aos',
                                          allowed_suites=frozenset({'synthetic.allowed.v1'}))
        self.service = ScientistLabService(self.controller, self.remote, authorization_context_sha256='a' * 64,
                                           program_version='director.v1',
                                           verify_capability=lambda *arguments: None)
        self.app = create_console(self.controller, 'synthetic-console-token', 'http://testserver', self.root,
                                  scientist_lab=self.service, web_profiles_root=self.root / 'profiles')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.headers = {'Origin': 'http://testserver'}
        await self.client.post('/api/login', headers=self.headers, json={'token': 'synthetic-console-token'})
        self.proposal = {'suite': 'synthetic.allowed.v1', 'track': 'anomaly', 'program_version': 'director.v1',
                         'budget': {'experiments': 1, 'wall_seconds': 30, 'model_tokens': 100}}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.close()
        self.store.close()
        self.temporary.cleanup()

    async def post(self, operation, value):
        return await self.client.post('/api/scientist/' + operation, headers=self.headers, json=value)

    async def approve(self, approval):
        return await self.post('approve', {key: approval[key] for key in ('action_id', 'envelope_sha256')} | {'accept': True})

    def prepared_project_scope(self):
        name = 'console-fixture-' + uuid4().hex
        directory = REPO_ROOT / 'data' / ('local-app-project-' + name)
        directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, directory)
        assets = directory / 'ui'
        assets.mkdir(mode=0o700)
        (assets / 'index.html').write_text('SYNTHETIC_PROJECT_UI')
        return {'project': name, 'port': 18766}, assets

    async def test_authenticated_console_full_synthetic_lab_lifecycle(self):
        approval = (await self.post('propose', self.proposal)).json()
        self.assertEqual(self.server.requests, [])
        denied = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual((await self.approve(approval)).status_code, 200)
        start = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(start.status_code, 200, start.text)
        run_id = approval['envelope']['task']['request']['external_run_id']
        status = await self.post('status', {'run_id': run_id})
        self.assertEqual(status.json()['state'], 'queued')
        self.assertEqual((await self.post('report', {'run_id': run_id})).status_code, 400)
        stop = (await self.post('stop', {'run_id': run_id})).json()
        self.assertEqual((await self.approve(stop)).status_code, 200)
        result = await self.post('execute', {'action_id': stop['action_id']})
        self.assertEqual(result.json()['state'], 'stop_requested')
        self.server.state = 'stopped'
        report = await self.post('report', {'run_id': run_id})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()['run_id'], self.server.run_id)
        inventory = (await self.client.get('/api/scientist/jobs')).json()
        self.assertEqual(len(inventory['jobs']), 1)
        self.assertEqual([action['state'] for action in inventory['jobs'][0]['actions']], ['acknowledged', 'acknowledged'])
        self.assertFalse(inventory['joint_runtime_admitted'])

    def persist_inference(self, controller, request_id):
        current = controller.state()
        binding = ScientistIntentBinding(session_id=current['session_id'], runtime_id=current['runtime_id'],
            owner=current['owner'], lease_id=current['lease_id'], generation=current['generation'],
            authorization_context_sha256='a' * 64)
        journal = ScientistIntentJournal(self.store, binding)
        request = ScientistTurnRequest(request_id=request_id, profile_id='aos.decider.turn.v1',
            deployment_digest='b' * 64, payload={'synthetic_private_prompt': 'DO_NOT_EXPOSE_SYNTHETIC_PROMPT'})
        peer = BrokerPeer(1234, 1000, 1, 'synthetic-private-boot', 'c' * 32, '/synthetic-private')
        journal.persist_intent(scientist_request_frame(request)[:-1], scientist_request_sha256(request),
            time.monotonic() + 60, peer)
        return journal, request, peer

    async def test_explicit_report_retention_and_offline_authenticated_history(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        started = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(started.status_code, 200, started.text)
        run_id = approval['envelope']['task']['request']['external_run_id']
        self.server.state = 'completed'
        report = await self.post('report', {'run_id': run_id})
        self.assertEqual(report.status_code, 200, report.text)
        history = (await self.client.get('/api/scientist/jobs')).json()['jobs'][0]['readbacks']
        self.assertEqual(history['items'], [])
        wrong = await self.post('save_report', {'run_id': run_id, 'expected_report_sha256': '0' * 64})
        self.assertEqual(wrong.status_code, 409, wrong.text)
        saved = await self.post('save_report', {'run_id': run_id,
                                               'expected_report_sha256': report.json()['report_sha256']})
        self.assertEqual(saved.status_code, 200, saved.text)
        metadata = saved.json()
        self.assertTrue(metadata['historical'])
        self.assertFalse(metadata['gpu_release_verified'])
        self.assertNotIn('record', metadata)
        inventory = (await self.client.get('/api/scientist/jobs')).json()['jobs'][0]['readbacks']
        self.assertEqual(inventory['items'], [metadata])
        requests = list(self.server.requests)
        retained = await self.post('read_saved_report', {'run_id': run_id, 'readback_id': metadata['readback_id']})
        self.assertEqual(retained.status_code, 200, retained.text)
        self.assertEqual(retained.json()['record']['report'], report.json())
        self.assertEqual(self.server.requests, requests)
        foreign = await self.client.post('/api/scientist/read_saved_report',
            headers={'Origin': 'http://foreign.invalid'},
            json={'run_id': run_id, 'readback_id': metadata['readback_id']})
        self.assertEqual(foreign.status_code, 403)
        invalid = await self.post('save_report', {'run_id': run_id, 'expected_report_sha256': 'not-a-hash'})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.server.requests, requests)

    async def test_named_manager_scope_is_pinned_copied_metadata_not_authority(self):
        scope, assets = self.prepared_project_scope()
        expected = dict(scope)
        app = create_console(self.controller, 'synthetic-console-token', 'http://127.0.0.1:18766',
                             self.root, manager_scope=scope, manager_session='app-' + 'a' * 32)
        scope['project'] = 'changed-after-construction'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url='http://127.0.0.1:18766') as client:
            identification = await client.get('/api/session')
            self.assertEqual(identification.json(), {'authenticated': False,
                'manager_scope': expected, 'manager_session': 'app-' + 'a' * 32})
            self.assertEqual(identification.headers['cache-control'], 'no-store')
            self.assertNotIn('set-cookie', identification.headers)
            for protected in ('/api/tasks', '/api/state', '/api/scientist/jobs'):
                self.assertEqual((await client.get(protected)).status_code, 401)
            self.assertEqual((await client.get('/api/session', headers={'Host': '127.0.0.1:8765'})).status_code, 403)
            login = await client.post('/api/login', headers={'Origin': 'http://127.0.0.1:18766'},
                                      json={'token': 'synthetic-console-token'})
            self.assertEqual(login.status_code, 200)
            self.assertEqual((await client.get('/api/session')).json(), {'authenticated': True,
                'manager_scope': expected, 'manager_session': 'app-' + 'a' * 32})
            response = await client.get('/api/tasks')
            self.assertEqual(response.json()['manager_scope'], expected)
            self.assertEqual(response.json()['manager_session'], 'app-' + 'a' * 32)
            self.assertFalse(response.json()['available'])
            self.assertEqual((await client.get('/ui/')).text, 'SYNTHETIC_PROJECT_UI')
        for invalid in ({'project': '../default', 'port': 18766},
                        {'project': 'learning-demo', 'port': True},
                        {'project': 'learning-demo', 'port': 8765},
                        {'project': 'learning-demo', 'port': 18767}):
            with self.assertRaises(ValueError):
                create_console(self.controller, 'synthetic-console-token', 'http://127.0.0.1:18766',
                               self.root, manager_scope=invalid)

    async def test_default_and_named_console_cookies_survive_shared_host_login_logout(self):
        default = create_console(self.controller, 'synthetic-default-token', 'http://127.0.0.1:8765', self.root)
        scope, assets = self.prepared_project_scope()
        named = create_console(self.controller, 'synthetic-project-token', 'http://127.0.0.1:18766',
                               self.root, manager_scope=scope)
        async with (httpx.AsyncClient(transport=httpx.ASGITransport(app=default),
                                      base_url='http://127.0.0.1:8765') as ordinary,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=named),
                                      base_url='http://127.0.0.1:18766') as project):
            project.cookies.jar = ordinary.cookies.jar
            self.assertEqual((await ordinary.post('/api/login', headers={'Origin': 'http://127.0.0.1:8765'},
                             json={'token': 'synthetic-default-token'})).status_code, 200)
            self.assertEqual((await project.post('/api/login', headers={'Origin': 'http://127.0.0.1:18766'},
                             json={'token': 'synthetic-project-token'})).status_code, 200)
            self.assertTrue((await ordinary.get('/api/session')).json()['authenticated'])

            self.assertTrue((await project.get('/api/session')).json()['authenticated'])
            self.assertEqual((await project.post('/api/logout', headers={'Origin': 'http://127.0.0.1:18766'},
                             json={})).status_code, 200)
            self.assertFalse((await project.get('/api/session')).json()['authenticated'])
            self.assertTrue((await ordinary.get('/api/session')).json()['authenticated'])

    def test_named_project_ui_rejects_shared_missing_and_symlinked_artifacts(self):
        scope, assets = self.prepared_project_scope()
        for invalid in ('desktop-session-' + 'a' * 32, True, '../outside'):
            with self.assertRaises(ValueError):
                create_console(self.controller, 'synthetic-token', 'http://127.0.0.1:18766',
                               self.root, manager_scope=scope, manager_session=invalid)
        with self.assertRaises(ValueError):
            create_console(self.controller, 'synthetic-token', 'http://127.0.0.1:18766',
                           self.root, manager_scope=scope, ui_root=REPO_ROOT / 'ui/dist')
        (assets / 'index.html').unlink()
        with self.assertRaises(ValueError):
            create_console(self.controller, 'synthetic-token', 'http://127.0.0.1:18766',
                           self.root, manager_scope=scope)
        outside = self.root / 'synthetic-ui.html'
        outside.write_text('SYNTHETIC_OUTSIDE_UI')
        (assets / 'index.html').symlink_to(outside)
        with self.assertRaises(ValueError):
            create_console(self.controller, 'synthetic-token', 'http://127.0.0.1:18766',
                           self.root, manager_scope=scope)
    async def test_readonly_inference_inventory_redacts_prompts_peer_and_control_secrets(self):
        self.persist_inference(self.controller, 'a' * 32)
        changes = self.store.connection.total_changes
        response = await self.client.get('/api/scientist/jobs')
        inference = response.json()['inference']
        self.assertEqual(inference['unresolved_count'], 1)
        self.assertTrue(inference['admission_blocked'])
        self.assertFalse(inference['gpu_release_verified'])
        self.assertFalse(inference['joint_runtime_admitted'])
        self.assertEqual(inference['intents'][0]['state'], 'pending')
        self.assertEqual(inference['intents'][0]['request_id'], 'a' * 32)
        for private in ['DO_NOT_EXPOSE_SYNTHETIC_PROMPT', 'synthetic-private-boot',
                        'synthetic-private', self.controller.state()['lease_id'], 'synthetic-test-token']:
            self.assertNotIn(private, response.text)
        self.assertEqual(self.store.connection.total_changes, changes)
        self.assertEqual(self.server.requests, [])

    async def test_prior_session_unresolved_intent_blocks_without_exposing_its_identity(self):
        other = DesktopController(self.store, SimpleNamespace(runtime_id='runtime-other', pins={'image_id': 'synthetic-image'}))
        self.persist_inference(other, 'd' * 32)
        inventory = (await self.client.get('/api/scientist/jobs')).json()['inference']
        self.assertEqual(inventory['other_session_count'], 1)
        self.assertEqual(inventory['current_session_count'], 0)
        self.assertTrue(inventory['admission_blocked'])
        self.assertEqual(inventory['intents'], [])

    async def test_empty_inference_inventory_does_not_claim_gpu_release(self):
        inventory = (await self.client.get('/api/scientist/jobs')).json()['inference']
        self.assertFalse(inventory['admission_blocked'])
        self.assertFalse(inventory['gpu_release_verified'])
        self.assertFalse(inventory['configured'])

    async def test_new_session_cannot_admit_lab_start_while_prior_session_effect_is_unresolved(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.server.mode = 'lost_ack'
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        fresh_controller = DesktopController(self.store, self.controller.runtime)
        fresh_client = ScientistLabClient(self.server.url, self.remote.token_file,
            principal_id=self.remote.principal_id, allowed_suites=self.remote.allowed_suites)
        fresh_service = ScientistLabService(fresh_controller, fresh_client,
            authorization_context_sha256='a' * 64, program_version='director.v1',
            verify_capability=lambda *arguments: None)
        with self.assertRaises(ScientistAdmissionError):
            fresh_service.propose(suite='synthetic.allowed.v1', track='anomaly',
                budget=self.service._load_action(approval['action_id'])[1].request.budget,
                program_version='director.v1')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_jobs').fetchone()[0], 1)
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)
        inventory = (await self.client.get('/api/scientist/jobs')).json()['inference']
        self.assertEqual(inventory['unresolved_lab_effect_count'], 1)
        self.assertTrue(inventory['admission_blocked'])
        scheduler = create_scientist_desktop_scheduler(fresh_controller,
            Settings(workspace=self.root / 'unused', database=self.root / 'synthetic.sqlite3'),
            {'synthetic_cpu_fixture': True}, self.root / 'absent.sock', confirm_runtime=lambda profiles: None)
        try:
            current = fresh_controller.state()
            with self.assertRaises(ScientistAdmissionError):
                scheduler.start(current['lease_id'], current['generation'])
            self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        finally:
            await scheduler.close()

    async def test_receipt_metadata_is_visible_without_releasing_gpu_or_exposing_response(self):
        journal, request, peer = self.persist_inference(self.controller, 'a' * 32)
        unit = 'swapp-aos-gpu-turn-' + 'e' * 32 + '.service'
        receipt = ScientistTurnReceipt(version=1, request_id=request.request_id,
            profile_id=request.profile_id, deployment_digest=request.deployment_digest,
            generation={'unit': unit, 'invocation_id': 'f' * 32, 'main_pid': 1234,
                        'control_group': '/synthetic/' + unit},
            response={'synthetic_private_response': 'DO_NOT_EXPOSE_SYNTHETIC_RESPONSE'}, usage={})
        journal.record_receipt(receipt, peer)
        response = await self.client.get('/api/scientist/jobs')
        inventory = response.json()['inference']
        self.assertEqual(inventory['intents'][0]['worker_unit'], unit)
        self.assertEqual(inventory['intents'][0]['state'], 'receipt_recorded')
        self.assertTrue(inventory['admission_blocked'])
        self.assertFalse(inventory['gpu_release_verified'])
        self.assertNotIn('DO_NOT_EXPOSE_SYNTHETIC_RESPONSE', response.text)

    async def test_anonymous_wrong_origin_and_host_do_not_create_intent(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver') as anonymous:
            self.assertEqual((await anonymous.get('/api/scientist/jobs')).status_code, 401)
            self.assertEqual((await anonymous.post('/api/scientist/propose', headers=self.headers, json=self.proposal)).status_code, 401)
        self.assertEqual((await self.client.post('/api/scientist/propose', headers={'Origin': 'http://attacker.invalid'}, json=self.proposal)).status_code, 403)
        self.assertEqual((await self.client.get('/api/scientist/jobs', headers={'Host': 'attacker.invalid'})).status_code, 403)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM scientist_lab_jobs').fetchone()[0], 0)

    async def test_default_capability_denies_before_proposal_or_rpc(self):
        default = ScientistLabService(self.controller, self.remote, authorization_context_sha256='a' * 64,
                                      program_version='director.v1')
        app = create_console(self.controller, 'synthetic-console-token', 'http://testserver', self.root,
                             scientist_lab=default, web_profiles_root=self.root / 'profiles')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            await client.post('/api/login', headers=self.headers, json={'token': 'synthetic-console-token'})
            response = await client.post('/api/scientist/propose', headers=self.headers, json=self.proposal)
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.server.requests, [])

    async def test_real_controller_takeover_invalidates_approved_lab_effect(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.controller.control('take-control')
        response = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.server.requests, [])

    async def test_lost_ack_durable_intent_and_changed_envelope_rejected(self):
        approval = (await self.post('propose', self.proposal)).json()
        altered = approval | {'envelope_sha256': 'f' * 64}
        self.assertEqual((await self.approve(altered)).status_code, 409)
        await self.approve(approval)
        self.server.mode = 'lost_ack'
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertEqual(len(self.server.requests), 1)
        inventory = (await self.client.get('/api/scientist/jobs')).json()
        self.assertEqual(inventory['jobs'][0]['actions'][0]['state'], 'intent')

    async def test_bounded_requests_no_alias_or_extra_fields(self):
        for operation, value in [('propose', self.proposal | {'gpu_owner': 'invented'}),
                                 ('propose', self.proposal | {'budget': {'experiments': True, 'wall_seconds': 30, 'model_tokens': 100}}),
                                 ('execute', {'action_id': {}}), ('approve', {'action_id': 'unknown', 'envelope_sha256': 'a' * 64, 'accept': 1})]:
            self.assertEqual((await self.post(operation, value)).status_code, 400)
        self.assertEqual((await self.client.post('/api/scientist/propose?alias=x', headers=self.headers, json=self.proposal)).status_code, 400)
        self.assertEqual(self.server.requests, [])

    async def test_console_cannot_bind_another_controller_service(self):
        other = SimpleNamespace(state=self.controller.state)
        with self.assertRaisesRegex(ValueError, 'this console desktop controller'):
            create_console(other, 'synthetic-console-token', 'http://testserver', self.root,
                           scientist_lab=self.service)

    async def test_fresh_human_takeover_can_inspect_and_request_stop_without_reusing_agent_approval(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        start = await self.post('execute', {'action_id': approval['action_id']})
        self.assertEqual(start.status_code, 200)
        run_id = approval['envelope']['task']['request']['external_run_id']
        stale_stop = (await self.post('stop', {'run_id': run_id})).json()
        await self.approve(stale_stop)
        self.controller.control('take-control')
        self.assertEqual((await self.post('execute', {'action_id': stale_stop['action_id']})).status_code, 409)
        self.assertEqual((await self.post('status', {'run_id': run_id})).status_code, 200)
        self.assertEqual((await self.post('propose', self.proposal)).status_code, 409)
        fresh_stop = await self.post('stop', {'run_id': run_id})
        self.assertEqual(fresh_stop.status_code, 200, fresh_stop.text)
        fresh = fresh_stop.json()
        self.assertEqual(fresh['envelope']['task']['binding']['owner'], 'HUMAN')
        self.assertNotEqual(fresh['action_id'], stale_stop['action_id'])
        self.assertEqual((await self.approve(fresh)).status_code, 200)
        self.assertEqual((await self.post('execute', {'action_id': fresh['action_id']})).json()['state'], 'stop_requested')
        self.assertEqual(self.service.approval(stale_stop['action_id'])['rejection_reason'], 'stale_controller')

    async def test_expired_unattempted_stop_does_not_block_fresh_approval(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        await self.post('execute', {'action_id': approval['action_id']})
        run_id = approval['envelope']['task']['request']['external_run_id']
        old = (await self.post('stop', {'run_id': run_id})).json()
        await self.approve(old)
        with patch('aos.scientist_lab_service.time.time', return_value=old['expires_at'] + 1):
            fresh = await self.post('stop', {'run_id': run_id})
            self.assertEqual(fresh.status_code, 200)
        self.assertEqual(self.service.approval(old['action_id'])['rejection_reason'], 'approval_expired')
        self.assertEqual((await self.post('execute', {'action_id': old['action_id']})).status_code, 409)
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)

    async def test_late_old_stop_ack_cannot_close_new_human_authority_or_enable_retry(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        await self.post('execute', {'action_id': approval['action_id']})
        run_id = approval['envelope']['task']['request']['external_run_id']
        stop = (await self.post('stop', {'run_id': run_id})).json()
        await self.approve(stop)
        self.server.mode = 'hold_stop'
        pending = asyncio.create_task(self.post('execute', {'action_id': stop['action_id']}))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 1))
        self.controller.control('take-control')
        self.server.release.set()
        self.assertEqual((await pending).status_code, 409)
        self.assertEqual(self.controller.state()['owner'], 'HUMAN')
        self.assertEqual(self.service.approval(stop['action_id'])['state'], 'intent')
        self.assertEqual((await self.post('status', {'run_id': run_id})).status_code, 200)
        self.assertEqual((await self.post('stop', {'run_id': run_id})).status_code, 409)
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 2)

    async def test_held_lab_effect_keeps_inventory_responsive_and_sqlite_callbacks_on_host_thread(self):
        host_thread = threading.get_ident()
        def capability(*arguments):
            self.assertEqual(threading.get_ident(), host_thread)
        self.service.capability = capability
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.post('execute', {'action_id': approval['action_id']}))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 1))
        self.assertFalse(pending.done())
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
        started = time.monotonic()
        inventory = await asyncio.wait_for(self.client.get('/api/scientist/jobs'), .3)
        self.assertLess(time.monotonic() - started, .3)
        self.assertEqual(inventory.status_code, 200)
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.server.release.set()
        self.assertEqual((await pending).status_code, 200)
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'acknowledged')
        self.assertFalse(self.remote.local_cleanup_pending)

    async def test_cancelled_http_wait_retains_durable_intent_and_never_replays_remote_start(self):
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda current, context: loop_errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.post('execute', {'action_id': approval['action_id']}))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 1))
        started = time.monotonic()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertLess(time.monotonic() - started, .3)
        self.assertTrue(self.remote.local_cleanup_pending)
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
        self.server.release.set()
        deadline = time.monotonic() + 1
        while self.remote.local_cleanup_pending and time.monotonic() < deadline:
            await asyncio.sleep(.005)
        self.assertFalse(self.remote.local_cleanup_pending)
        self.assertIsNotNone(self.remote.uncertain_action_id)
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertEqual((await self.post('propose', self.proposal)).status_code, 409)
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
        self.assertEqual(loop_errors, [])

    async def test_local_close_waits_for_owned_http_and_keeps_uncertain_intent(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.post('execute', {'action_id': approval['action_id']}))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 1))
        closing = asyncio.create_task(self.service.close_async(timeout_seconds=1))
        await asyncio.sleep(.01)
        self.assertFalse(closing.done())
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
        self.server.release.set()
        await closing
        self.assertEqual((await pending).status_code, 409)
        self.assertFalse(self.remote.local_cleanup_pending)
        self.assertEqual(self.service._active_controls, set())
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.assertEqual((await self.post('propose', self.proposal)).status_code, 409)
        self.assertEqual(len([request for request in self.server.requests if request[0] == 'POST']), 1)

    async def test_failed_local_close_does_not_allow_new_work_and_can_be_rechecked(self):
        approval = (await self.post('propose', self.proposal)).json()
        await self.approve(approval)
        self.server.mode = 'hold_start'
        pending = asyncio.create_task(self.post('execute', {'action_id': approval['action_id']}))
        self.assertTrue(await asyncio.to_thread(self.server.entered.wait, 1))
        with self.assertRaises(ScientistAdmissionError):
            await self.remote.close_async(timeout_seconds=1)
        self.assertTrue(self.remote.local_cleanup_pending)
        self.assertEqual((await self.post('execute', {'action_id': approval['action_id']})).status_code, 409)
        self.server.release.set()
        await self.remote.close_async(timeout_seconds=1)
        self.assertEqual((await pending).status_code, 409)
        self.assertEqual(self.service.approval(approval['action_id'])['state'], 'intent')
