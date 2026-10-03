import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from aos.contracts import HELLO_CONTENT, Settings
from aos.desktop_control import DesktopController
from aos.desktop_console import create_console
from aos.decision import FixtureDecisionEngine
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_inventory import scientist_inference_inventory
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError
from aos.storage import TrajectoryStore
from test_desktop_tasks import FixtureDesktop
from test_scientist_transport import SyntheticBroker


class ScientistDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.settings = Settings(workspace=self.root / 'workspace', database=self.root / 'synthetic.sqlite3')
        self.runtime = FixtureDesktop(self.settings.workspace)
        self.runtime.start()
        self.store = TrajectoryStore(self.settings.database)
        self.controller = DesktopController(self.store, self.runtime)
        self.broker = None
        self.scheduler = None
        self.confirmed = True

    async def asyncTearDown(self):
        if self.scheduler is not None:
            await self.scheduler.close()
        if self.broker is not None:
            self.broker.release.set()
            self.broker.close()
        self.runtime.stop()
        self.store.close()
        self.temporary.cleanup()

    def build(self, mode='success'):
        def response(request):
            options = request['payload']['request']['options']
            selected = 'write_file'
            return {'deployment_digest': request['deployment_digest'], 'metrics': {'synthetic_cpu_fixture': True},
                    'prediction': {'selected_option': selected,
                                   'probabilities': {option['id']: float(option['id'] == selected) for option in options}}}
        self.broker = SyntheticBroker(self.root, mode, response_factory=response)
        authenticator = Mock()
        authenticator.authenticate.return_value = BrokerPeer(os.getpid(), os.getuid(), 1,
                                               'synthetic', 'c' * 32, '/synthetic')
        authenticator.still_current.return_value = True
        def confirm(profiles):
            if not self.confirmed:
                raise ScientistAdmissionError('Synthetic capability revoked')
            self.assertEqual(set(profiles), {'aos.decider.turn.v1'})
        self.scheduler = create_scientist_desktop_scheduler(self.controller, self.settings,
            {'synthetic_cpu_fixture': True, 'model_files': {'model.safetensors': 'a' * 64},
             'checkpoint_revision': 'synthetic-cpu-fixture', 'tokenizer_revision': 'synthetic-cpu-fixture'},
            self.broker.path, confirm_runtime=confirm,
            authenticator=authenticator, timeout_seconds=2)
        return self.scheduler

    def start(self):
        desktop = self.controller.state()
        return self.scheduler.start(desktop['lease_id'], desktop['generation'])

    async def approval(self):
        for attempt in range(300):
            approval = self.scheduler.status()['approval']
            if approval:
                return approval
            if self.scheduler.task.done():
                self.fail('Task ended before human approval')
            await asyncio.sleep(.005)
        self.fail('No approval produced')

    async def test_default_startup_rejects_unconfirmed_joint_runtime_without_native_worker(self):
        with self.assertRaises(ScientistAdmissionError):
            create_scientist_desktop_scheduler(self.controller, self.settings, {'synthetic': True},
                                              self.root / 'absent.sock')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)

    async def test_console_unresolved_receipt_denies_second_task_with_json_without_mutation(self):
        scheduler = self.build()
        self.start()
        approval = await self.approval()
        scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await scheduler.task
        before = self.store.connection.total_changes
        state = self.controller.state()
        app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
                             scheduler=scheduler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver',
                                     headers={'Origin': 'http://testserver'}) as client:
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post('/api/tasks', json={'kind': 'hello',
                'lease_id': state['lease_id'], 'generation': state['generation']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'reconciliation_required')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.store.connection.total_changes, before)
        self.assertEqual(len(scheduler.status()['jobs']), 1)
        self.assertEqual(self.store.connection.execute(
            'SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)

    async def test_console_admission_denial_does_not_disclose_private_exception(self):
        scheduler = self.build()
        scheduler.scientist_binding.confirm_runtime = Mock(
            side_effect=ScientistAdmissionError('synthetic-private-token-and-path'))
        state = self.controller.state()
        app = create_console(self.controller, 'synthetic-token', 'http://testserver', self.root,
                             scheduler=scheduler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver',
                                     headers={'Origin': 'http://testserver'}) as client:
            payload = {'kind': 'hello', 'lease_id': state['lease_id'], 'generation': state['generation']}
            self.assertEqual((await client.post('/api/tasks', json=payload)).status_code, 401)
            await client.post('/api/login', json={'token': 'synthetic-token'})
            response = await client.post('/api/tasks', json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'reconciliation_required')
        self.assertNotIn('synthetic-private-token-and-path', response.text)
        self.assertEqual(scheduler.status()['jobs'], [])

    async def test_actual_typed_task_socket_intent_human_approval_and_independent_file_readback(self):
        scheduler = self.build()
        self.start()
        approval = await self.approval()
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'receipt_recorded')
        scheduler.respond(approval['approval_id'], approval['action_sha256'], True)
        await asyncio.gather(scheduler.task, return_exceptions=True)
        self.assertEqual(scheduler.status()['jobs'][0]['status'], 'succeeded')
        self.assertEqual((self.runtime.root / 'hello.txt').read_text(), HELLO_CONTENT)
        self.assertEqual(self.store.connection.execute('SELECT result FROM verifications').fetchone()[0], 'passed')
        self.assertEqual(len(self.broker.requests), 1)
        with self.assertRaises(ScientistAdmissionError):
            self.start()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 1)

    async def test_revoke_before_job_acceptance_creates_no_task_or_inference(self):
        self.build()
        self.confirmed = False
        with self.assertRaises(ScientistAdmissionError):
            self.start()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        self.assertEqual(self.broker.requests, [])

    async def test_replaced_engine_or_native_auxiliary_models_cannot_bypass_broker(self):
        scheduler = self.build()
        original = scheduler.engine
        scheduler.engine = FixtureDecisionEngine()
        with self.assertRaises(ScientistAdmissionError):
            self.start()
        inventory = scientist_inference_inventory(self.controller, scheduler)
        self.assertTrue(inventory['admission_blocked'])
        self.assertFalse(inventory['model_binding_valid'])
        scheduler.engine = original
        with self.assertRaises(ScientistAdmissionError):
            scheduler.configure_owned_skill_planning(Mock())
        with self.assertRaises(ScientistAdmissionError):
            scheduler.configure_knowledge_answer(Mock(), Mock(), self.root / 'unused')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM desktop_tasks').fetchone()[0], 0)
        self.assertEqual(self.broker.requests, [])

    async def test_late_reply_after_human_takeover_cannot_publish_decision_or_execute(self):
        scheduler = self.build('hold')
        self.start()
        self.assertTrue(await asyncio.to_thread(self.broker.entered.wait, 1))
        self.controller.control('take-control')
        self.broker.release.set()
        await asyncio.gather(scheduler.task, return_exceptions=True)
        self.assertEqual(scheduler.status()['jobs'][0]['status'], 'failed')
        self.assertFalse((self.runtime.root / 'hello.txt').exists())
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)
