import asyncio
import json
import ssl
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from aos.browser import BrowserRuntime
from aos.contracts import AOSFault, ErrorCode, Settings, canonical, digest, identifier, now
from aos.decision import FixtureDecisionEngine
from aos.remote_form_operator import RemoteFormOperator
from aos.storage import TrajectoryStore
from aos.web_application_binding import WebRuntimePin, bind_web_task
from aos.web_goal_execution_binding import build_web_goal_execution_binding, confirm_web_goal_execution_binding
from aos.web_goal_execution_runner import WebGoalExecutionRunner
from aos.web_https_form_state_probe import form_state_request_sha256
from test_web_goal_execution_binding import example, source_fixture


class FixtureBrowserBackend(BrowserRuntime):
    def __init__(self, pin, sources, events):
        self.runtime_id = pin.runtime_id
        self.sources = sources
        self.events = events
        self.relay = None
        self.probe = None
        self.running = False

    def status(self):
        return {'runtime_id': self.runtime_id, 'running': self.running, 'real_execution': False,
                'kind': 'fixture_browser_backend'}

    def attach_relay(self, relay, _draft, **arguments):
        self.relay = relay
        self.relay.transport._entry_response_sha256 = 'a' * 64
        self.events.append(('relay', arguments))

    def attach_state_probe(self, probe):
        self.probe = probe

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    @property
    def relay_report(self):
        form = self.sources['form_plan']
        return SimpleNamespace(profile_sha256=form.profile_sha256, task_sha256=form.task_sha256,
            plan_sha256=digest(form.model_dump(mode='json')), entry_response_sha256='a' * 64,
            receipt_url_sha256=digest({'url': form.receipt_url}), receipt_response_sha256='b' * 64,
            submit_request_sha256=digest({'method': 'POST', 'url': form.submit_url, 'body_sha256': form.body_sha256}))

    def perform(self, tool, _arguments):
        self.events.append(('tool', tool))
        form, state = self.sources['form_plan'], self.sources['state_plan']
        requests = {
            'browser.form.open': digest({'method': 'GET', 'url': form.entry_url}),
            'browser.form.submit': digest({'method': 'POST', 'url': form.submit_url, 'body_sha256': form.body_sha256}),
            'browser.form.receipt': digest({'method': 'GET', 'url': form.receipt_url}),
            'browser.form.state_before': form_state_request_sha256(state, 'before'),
            'browser.form.state_after': form_state_request_sha256(state, 'after'),
        }
        if tool in requests:
            if not self.relay.transport.consume_approval(requests[tool]):
                raise AssertionError('Synthetic backend request permit denied')
        if tool == 'browser.form.open':
            return {'url': form.entry_url}
        if tool in {'browser.form.receipt', 'browser.form.observe'}:
            return {'url': form.receipt_url, 'title_sha256': 'c' * 64, 'heading_sha256': 'd' * 64}
        if tool == 'browser.form.state_after':
            return {'state_plan_sha256': digest(state.model_dump(mode='json')),
                'form_plan_sha256': digest(form.model_dump(mode='json')),
                'before_response_sha256': state.expected_before_sha256, 'after_response_sha256': state.expected_after_sha256,
                'receipt_response_sha256': 'b' * 64, 'site_outcome_verified': False, 'account_verified': False}
        return {'synthetic': True}


class WebGoalExecutionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = source_fixture(self.root / 'crm', 'synthetic-crm-note', 'contact_name', 'Ada', 'Call tomorrow')
        self.binding = build_web_goal_execution_binding(**self.sources)
        self.authority = [self.binding.authority]
        self.source = [self.binding.source]
        self.confirmation = confirm_web_goal_execution_binding(self.binding, confirm_sha256=self.binding.confirm_sha256,
            human_confirmation=True, current_source=lambda: self.source[0], current_authority=lambda: self.authority[0])
        self.events = []
        self.pin = WebRuntimePin.model_validate(example('web_application_binding.json')['draft']['runtime'])
        self.runtime = FixtureBrowserBackend(self.pin, self.sources, self.events)
        self.store = TrajectoryStore(self.root / 'trajectory.sqlite')
        self.addCleanup(self.store.close)
        self.settings = Settings(workspace=self.root, database=self.root / 'trajectory.sqlite')
        draft = bind_web_task(self.sources['profiles'], self.sources['task'], self.pin)
        self.operator = RemoteFormOperator(self.settings, self.store, self.runtime, FixtureDecisionEngine(),
            self.sources['profiles'], draft, self.sources['form_plan'], None, None, ssl.create_default_context(),
            state_plan=self.sources['state_plan'], fields=[{'name': name, 'value': value}
                for name, value in self.binding.oracle.expected_fields.items()],
            skill_invocation=self.binding.invocation.model_dump(mode='json'),
            skill_invocation_sha256=digest(self.binding.invocation.model_dump(mode='json')))
        with self.store.connection:
            self.store.insert('desktop_sessions', session_id='synthetic-session', runtime_id='synthetic-runtime',
                image_id='synthetic-image', owner='AGENT', lease_id='synthetic-lease', generation=2, status='running',
                created_at=now(), updated_at=now())
        self.runner = self.make_runner()

    def bind_run(self, intent_sha256, identity, **_callbacks):
        self.events.append(('bind', intent_sha256, identity))
        return {'synthetic': True}

    def make_runner(self, **changes):
        return WebGoalExecutionRunner(self.operator, self.binding, self.confirmation,
            **({'intent_sha256': 'a' * 64, 'current_source': lambda: self.source[0],
                'current_authority': lambda: self.authority[0], 'bind_run': self.bind_run} | changes))

    def created(self, state):
        self.events.append(('created', state.run_id))
        with self.store.connection:
            self.store.insert('desktop_tasks', job_id='synthetic-job', session_id='synthetic-session', run_id=state.run_id,
                kind='browser_remote_form', lease_id='synthetic-lease', generation=2, status='running', real_model=0,
                runtime_id=self.runtime.runtime_id, created_at=now(), updated_at=now())

    async def approve_fixture_action(self, action):
        self.events.append(('gate', action.tool))
        with self.store.connection:
            self.store.insert('desktop_approvals', approval_id=identifier('approval'), job_id='synthetic-job',
                envelope_json=canonical(action.model_dump(mode='json')), action_sha256=digest(action.model_dump(mode='json')),
                expires_at=action.deadline, status='consumed', created_at=now(), updated_at=now())

    async def test_actual_finite_operator_gateway_and_sqlite_run_bind_before_tools(self):
        self.assertNotEqual(self.binding.authority.runtime_id, self.runtime.runtime_id)
        result = await self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created,
                                        execution_gate=self.approve_fixture_action)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(result['real_model'])
        self.assertEqual(self.runner.status, 'awaiting_independent_verification')
        self.assertEqual(self.runner.approved_stages, 6)
        self.assertEqual(self.runner.run_identity['run_id'], result['run_id'])
        self.assertLess(next(index for index, event in enumerate(self.events) if event[0] == 'bind'),
                        next(index for index, event in enumerate(self.events) if event[0] == 'tool'))
        actions = self.store.connection.execute('SELECT tool,status FROM actions WHERE run_id=?', (result['run_id'],)).fetchall()
        self.assertEqual(len(actions), 7)
        self.assertTrue(all(action['status'] == 'ok' for action in actions))
        self.assertEqual(sum(action['tool'] == 'browser.form.submit' for action in actions), 1)
        with self.assertRaises(AOSFault):
            await self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created,
                                   execution_gate=self.approve_fixture_action)

    async def test_source_or_control_changes_during_approval_prevent_tools_and_retry(self):
        async def change_control(action):
            await self.approve_fixture_action(action)
            self.authority[0] = self.binding.authority.model_copy(update={'generation': 3})

        with self.assertRaises(ValueError):
            await self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created, execution_gate=change_control)
        self.assertEqual(self.runner.status, 'uncertain')
        self.assertFalse(any(event[0] == 'tool' for event in self.events))
        self.assertIsNotNone(self.runner.run_identity)

    async def test_changed_exact_fields_or_invocation_are_rejected_before_run(self):
        self.operator.form_fields = (('contact_name', 'Other'), ('note', 'Call tomorrow'))
        with self.assertRaises(AOSFault):
            self.make_runner()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    async def test_source_change_during_runtime_start_prevents_first_tool(self):
        def change_source():
            self.runtime.running = True
            self.source[0] = self.binding.source.model_copy(update={'review_sha256': 'f' * 64})

        self.runtime.start = change_source
        with self.assertRaises(ValueError):
            await self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created,
                                   execution_gate=self.approve_fixture_action)
        self.assertEqual(self.runner.status, 'uncertain')
        self.assertIsNotNone(self.runner.run_identity)
        self.assertFalse(any(event[0] == 'tool' for event in self.events))

    async def test_source_change_after_dispatched_tool_prevents_next_stage(self):
        original_perform = self.runtime.perform

        def change_source(tool, arguments):
            result = original_perform(tool, arguments)
            self.source[0] = self.binding.source.model_copy(update={'review_sha256': 'f' * 64})
            return result

        self.runtime.perform = change_source
        with self.assertRaises(ValueError):
            await self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created,
                                   execution_gate=self.approve_fixture_action)
        self.assertEqual(self.runner.status, 'uncertain')
        self.assertEqual([event[1] for event in self.events if event[0] == 'tool'], ['browser.form.open'])

    async def test_cancel_retains_created_identity_and_uncertainty_without_tools(self):
        waiting = asyncio.Event()

        async def gate(_action):
            waiting.set()
            await asyncio.Event().wait()

        running = asyncio.create_task(self.runner.form(owner_lease_id='synthetic-lease', on_created=self.created, execution_gate=gate))
        await asyncio.wait_for(waiting.wait(), 2)
        self.assertTrue(await self.runner.cancel(timeout_seconds=1))
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(self.runner.reserved)
        self.assertIsNotNone(self.runner.run_identity)
        self.assertEqual(self.runner.status, 'uncertain')
        self.assertFalse(any(event[0] == 'tool' for event in self.events))

    async def test_failed_durable_run_binding_cannot_execute_or_hide_run(self):
        def fail_binding(*_arguments, **_keywords):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic journal failure')

        runner = self.make_runner(bind_run=fail_binding)
        result = await runner.form(owner_lease_id='synthetic-lease', on_created=self.created,
                                   execution_gate=self.approve_fixture_action)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(runner.status, 'uncertain')
        self.assertEqual(runner.run_identity['run_id'], result['run_id'])
        self.assertFalse(any(event[0] == 'tool' for event in self.events))
