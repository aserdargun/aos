import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree

import httpx

from aos.contracts import AOSFault, REPO_ROOT, Settings, State, canonical, digest, identifier, now
from aos.decision import FixtureDecisionEngine
from aos.desktop_console import create_console
from aos.desktop_control import DesktopController
from aos.desktop_tasks import DesktopScheduler
from aos.owned_form_fixture import HOST, OwnedFormFixture, owned_record_form_bodies
from aos.remote_form_operator import RemoteFormOperator
from aos.site_knowledge import SiteKnowledgeStore
from aos.site_skill import SiteSkillStore
from aos.site_skill_form_recipe import revalidate_site_skill_form_recipe_invocation
from aos.site_skill_form_recipe_audit import audit_site_skill_form_recipe_execution
from aos.storage import TrajectoryStore
from aos.web_application import WebApplicationProfiles, profile_report
from aos.web_application_binding import WebRuntimePin, bind_web_task
from aos.web_goal_desktop_execution import DesktopParameterWebGoalExecution
from aos.web_goal_execution_binding import (
    build_web_goal_execution_binding, confirm_web_goal_execution_binding,
)
from aos.web_goal_execution_journal import WebGoalExecutionJournal
from aos.web_goal_execution_runner import WebGoalExecutionRunner
from aos.web_https_form_state_probe import form_state_marker_sha256, form_state_request_sha256
from aos.web_https_form_transport import form_body, plan_web_https_form

from test_web_goal_execution_binding import source_fixture
from test_web_goal_execution_runner import FixtureBrowserBackend


class TLSFixtureBrowserBackend(FixtureBrowserBackend):
    def __init__(self, pin, sources, events):
        super().__init__(pin, sources, events)
        self.pin = pin
        self.report = None
        self.receipt_observation = None

    def runtime_pin(self):
        return self.pin

    def attach_relay(self, relay, draft, **arguments):
        self.relay = relay
        self.fields = tuple((item['name'], item['value']) for item in arguments['fields'])

    def attach_state_probe(self, probe):
        self.probe = probe
        probe.bind_submitted_fields(self.fields)

    @property
    def relay_report(self):
        return self.report

    def perform(self, tool, arguments):
        self.events.append(('tool', tool))
        plan = self.sources['form_plan']
        if tool == 'browser.form.open':
            self.relay.transport.open_entry(confirm_request_sha256=digest(
                {'method': 'GET', 'url': plan.entry_url}))
            return {'url': plan.entry_url}
        if tool == 'browser.form.fill':
            return {'synthetic_browser_fill': True}
        if tool == 'browser.form.state_before':
            self.probe.observe_before(confirm_request_sha256=form_state_request_sha256(
                self.sources['state_plan'], 'before'))
            return {'status': 'before_response_matched',
                'state_plan_sha256': digest(self.sources['state_plan'].model_dump(mode='json')),
                'response_sha256': self.sources['state_plan'].expected_before_sha256}
        if tool == 'browser.form.submit':
            self.relay.transport.submit(form_body(self.fields), confirm_request_sha256=digest({
                'method': 'POST', 'url': plan.submit_url, 'body_sha256': plan.body_sha256}))
            return {'synthetic_browser_submit': True}
        if tool == 'browser.form.receipt':
            self.report, receipt = self.relay.transport.read_receipt(confirm_request_sha256=digest(
                {'method': 'GET', 'url': plan.receipt_url}))
            document = ElementTree.fromstring(receipt)
            self.receipt_observation = {'url': plan.receipt_url,
                'title_sha256': hashlib.sha256(document.findtext('title').encode()).hexdigest(),
                'heading_sha256': hashlib.sha256(document.findtext('h1').encode()).hexdigest()}
            return dict(self.receipt_observation)
        if tool == 'browser.form.observe':
            return dict(self.receipt_observation)
        if tool == 'browser.form.state_after':
            return self.probe.observe_after(confirm_request_sha256=form_state_request_sha256(
                self.sources['state_plan'], 'after')).model_dump(mode='json')
        raise AssertionError('Unexpected synthetic browser operation')


class WebGoalDesktopExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.arguments = source_fixture(
            self.root / 'sources', 'synthetic-crm-note', 'contact_name', 'Ada', 'Call tomorrow')
        self.binding = build_web_goal_execution_binding(**self.arguments)
        self.source = self.binding.source
        self.control = self.binding.authority.model_dump(mode='json')
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute('''CREATE TABLE desktop_tasks(
            job_id TEXT, session_id TEXT, run_id TEXT, status TEXT)''')
        self.manager = SimpleNamespace(
            settings=SimpleNamespace(workspace=self.workspace), reserved=False,
            closed=False, restart_quiesced=False, _owned_skill_reuse=None,
            parameter_web_goal_execution=None,
            controller=SimpleNamespace(session_id=self.binding.authority.desktop_session_id,
                                       state=lambda: dict(self.control)),
            store=SimpleNamespace(connection=self.connection),
            remote_entry_profile_sha256=self.binding.source.profile_sha256,
            remote_entry_task=self.arguments['task'],
            remote_form_plan=self.arguments['form_plan'],
            remote_form_state_plan=self.arguments['state_plan'],
            remote_form_skill_invocation=self.binding.invocation.model_dump(mode='json'),
            remote_form_skill_invocation_sha256=digest(self.binding.invocation.model_dump(mode='json')),
            remote_form_fields=tuple(self.binding.oracle.expected_fields.items()),
            remote_form_owned_target=SimpleNamespace(assert_plan=Mock(), assert_state_plan=Mock()),
            remote_form_owned_fixture=SimpleNamespace(record_mode=True, read_whole_record=Mock()),
            remote_form_owned_candidate_session=None, remote_form_public_plan_sha256=None,
            remote_form_cookie=None, start=Mock(return_value={'job_id': 'synthetic-job'}))
        self.journal = WebGoalExecutionJournal(self.root / 'journal')
        self.auditor = Mock(side_effect=ValueError('synthetic independent audit unavailable'))

    def configure(self, **changes):
        arguments = dict(manager_session=self.binding.authority.manager_session,
                         current_source=lambda: self.source, source_auditor=self.auditor)
        arguments.update(changes)
        DesktopScheduler.configure_parameter_web_goal_execution(
            self.manager, self.binding, self.journal, **arguments)
        self.addCleanup(self.manager.parameter_web_goal_execution.close)
        return self.manager.parameter_web_goal_execution

    def start(self, service, **changes):
        arguments = dict(confirm_sha256=self.binding.confirm_sha256, human_confirmation=True,
                         lease_id=self.binding.authority.lease_id,
                         generation=self.binding.authority.generation)
        arguments.update(changes)
        return DesktopScheduler.start_parameter_web_goal_execution(self.manager, **arguments)

    def operator(self):
        operator = RemoteFormOperator.__new__(RemoteFormOperator)
        operator.runtime = SimpleNamespace(runtime_id='synthetic-child-runtime')
        operator.engine = FixtureDecisionEngine()
        operator.draft = SimpleNamespace(
            runtime=operator.runtime, profile_sha256=self.binding.source.profile_sha256,
            task=self.arguments['task'])
        operator.plan = self.arguments['form_plan']
        operator.state_plan = self.arguments['state_plan']
        operator.profiles = self.arguments['profiles']
        operator.form_fields = tuple(sorted(self.binding.oracle.expected_fields.items()))
        operator.field_selector = tuple(name for name, value in operator.form_fields)
        operator.field_name = None
        operator.value = None
        operator.gateway = Mock()
        operator.skill_invocation = self.binding.invocation.model_dump(mode='json')
        operator.skill_invocation_sha256 = self.manager.remote_form_skill_invocation_sha256
        return operator

    def state(self, operator):
        return State(task_kind='browser_remote_form', task_id=identifier('task'),
                     run_id=identifier('run'), step_id=identifier('step'),
                     runtime_id=operator.runtime.runtime_id,
                     deployment_id=operator.engine.identity['deployment_id'],
                     owner_lease_id=self.binding.authority.lease_id,
                     skill_invocation_sha256=operator.skill_invocation_sha256)

    def bound(self):
        service = self.configure()
        self.start(service)
        operator = self.operator()
        operation = service.operation(operator, service.job_id)
        state = self.state(operator)
        operation.__self__._created(state, None)
        return service, state

    def row(self, service, state, *, status='succeeded', run_id=None, session_id=None):
        self.connection.execute('DELETE FROM desktop_tasks')
        self.connection.execute('INSERT INTO desktop_tasks VALUES(?,?,?,?)', (
            service.job_id, session_id or self.manager.controller.session_id,
            state.run_id if run_id is None else run_id, status))

    def test_actual_scheduler_configuration_and_start_are_explicit_single_use(self):
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            DesktopScheduler.start_parameter_web_goal_execution(self.manager)
        service = self.configure()
        self.assertIsInstance(service, DesktopParameterWebGoalExecution)
        self.assertEqual(service.status()['field_count'], 2)
        self.assertFalse(service.status()['independently_verified'])
        with self.assertRaisesRegex(ValueError, 'already_configured'):
            self.configure()
        result = self.start(service)
        self.assertEqual(result['job_id'], 'synthetic-job')
        self.assertFalse(result['independently_verified'])
        self.manager.start.assert_called_once_with(
            self.binding.authority.lease_id, self.binding.authority.generation,
            'browser_remote_form', False, False)
        with self.assertRaises(ValueError):
            self.start(service)
        self.assertEqual(self.manager.start.call_count, 1)

    def test_start_requires_exact_review_and_control_before_manager_callback(self):
        service = self.configure()
        for changes in ({'confirm_sha256': '0' * 64}, {'human_confirmation': False},
                        {'human_confirmation': 1}, {'lease_id': 'foreign-lease'},
                        {'generation': 3}, {'generation': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.start(service, **changes)
        self.manager.start.assert_not_called()
        self.assertFalse(self.journal.reserved)
        self.assertFalse(self.journal.directory.exists())

    def test_exact_review_intent_is_durable_before_manager_start_callback(self):
        service = self.configure()
        observed = []

        def started(*arguments):
            reopened = WebGoalExecutionJournal(self.journal.directory)
            self.assertTrue(reopened.reserved)
            status = reopened.inspect(service.intent_sha256)
            self.assertEqual(status['status'], 'uncertain_before_run_binding')
            self.assertTrue(status['confirmation_consumed'])
            self.assertFalse(status['replay_authorized'])
            intent_path, = self.journal.directory.glob('*.intent.json')
            intent = json.loads(intent_path.read_text())
            self.assertEqual(intent['binding'], self.binding.model_dump(mode='json'))
            self.assertEqual(intent['confirmation'], service.confirmation.model_dump(mode='json'))
            self.assertEqual(intent_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(digest(intent), service.intent_sha256)
            observed.append(arguments)
            return {'job_id': 'synthetic-job'}

        self.manager.start.side_effect = started
        self.start(service)
        self.assertEqual(len(observed), 1)
        self.assertTrue(service.reserved)

    def test_failed_start_consumes_review_and_reopened_journal_blocks_replay(self):
        service = self.configure()
        self.manager.start.side_effect = RuntimeError('synthetic ambiguous dispatch')
        with self.assertRaises(RuntimeError):
            self.start(service)
        self.assertFalse(service.starting)
        self.assertTrue(service.reserved)
        self.assertTrue(WebGoalExecutionJournal(self.journal.directory).reserved)
        with self.assertRaises(ValueError):
            self.start(service)
        self.assertEqual(self.manager.start.call_count, 1)
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')

    def test_existing_unresolved_intent_denies_start_before_manager_callback(self):
        service = self.configure()
        confirmation = confirm_web_goal_execution_binding(
            self.binding, confirm_sha256=self.binding.confirm_sha256, human_confirmation=True,
            current_source=service.source, current_authority=service.authority)
        self.journal.begin(self.binding, confirmation,
                           current_source=service.source, current_authority=service.authority)
        with self.assertRaises(ValueError):
            self.start(service)
        self.manager.start.assert_not_called()

    def test_actual_ordinary_start_guard_denies_unreviewed_form_and_all_unresolved_admission(self):
        service = self.configure()
        with self.assertRaises(AOSFault):
            DesktopScheduler._start(self.manager, self.binding.authority.lease_id,
                                    self.binding.authority.generation, 'browser_remote_form')
        self.start(service)
        for kind in ('hello', 'browser_form', 'browser_remote_form'):
            with self.subTest(kind=kind), self.assertRaises(AOSFault):
                DesktopScheduler._start(self.manager, self.binding.authority.lease_id,
                                        self.binding.authority.generation, kind)

    def test_foreign_host_session_source_control_and_non_record_fixture_cannot_configure(self):
        for changes in ({'manager_session': 'foreign-manager'},
                        {'current_source': lambda: self.source.model_copy(update={'review_sha256': '0' * 64})},
                        {'current_source': None}, {'source_auditor': None}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.configure(**changes)
        for name, replacement in (('session_id', 'foreign-session'),):
            original = getattr(self.manager.controller, name)
            setattr(self.manager.controller, name, replacement)
            with self.assertRaises(ValueError):
                self.configure()
            setattr(self.manager.controller, name, original)
        for field, replacement in (('runtime_id', 'foreign-runtime'), ('generation', 3),
                                   ('owner', 'HUMAN'), ('status', 'paused')):
            original = self.control[field]
            self.control[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.configure()
            self.control[field] = original
        for mode in (False, 1, None):
            self.manager.remote_form_owned_fixture.record_mode = mode
            with self.subTest(record_mode=mode), self.assertRaises(ValueError):
                self.configure()
        self.manager.start.assert_not_called()

    def test_configure_rejects_changed_canonical_binding_schema_and_authority_claims(self):
        original = self.binding.model_dump(mode='json')
        for changed in ({'schema_version': '2.0'}, {'execution_authorized': True},
                        {'training_ready': True}, {'gpu_release_verified': True},
                        {'model_expanded_authority': True}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                DesktopScheduler.configure_parameter_web_goal_execution(
                    self.manager, original | changed, self.journal,
                    manager_session=self.binding.authority.manager_session,
                    current_source=lambda: self.source, source_auditor=self.auditor)
        self.assertIsNone(self.manager.parameter_web_goal_execution)
        self.manager.start.assert_not_called()
        self.assertFalse(self.journal.directory.exists())

    def test_private_journal_cannot_overlap_authorized_tool_workspace(self):
        for directory in (self.workspace, self.workspace / 'journal', self.root):
            self.journal = WebGoalExecutionJournal(directory)
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                self.configure()
        self.assertIsNone(self.manager.parameter_web_goal_execution)
        self.manager.start.assert_not_called()

    def test_canonical_dictionary_source_is_supported_without_expanding_authority(self):
        service = self.configure(current_source=lambda: self.source.model_dump(mode='json'))
        self.start(service)
        self.assertTrue(self.journal.reserved)
        self.assertFalse(service.status()['replay_authorized'])

    def test_changed_host_parameters_invocation_and_source_fail_before_intent(self):
        service = self.configure()
        replacements = (
            ('remote_entry_profile_sha256', '0' * 64),
            ('remote_form_fields', (('contact_name', 'Other'), ('note', 'Call tomorrow'))),
            ('remote_form_skill_invocation_sha256', '0' * 64),
            ('remote_form_skill_invocation', self.manager.remote_form_skill_invocation | {'synthetic': False}),
            ('remote_form_state_plan', self.arguments['state_plan'].model_copy(
                update={'state_url': 'https://synthetic-crm-note.aos.invalid/app/foreign'})))
        for field, replacement in replacements:
            original = getattr(self.manager, field)
            setattr(self.manager, field, replacement)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.start(service)
            setattr(self.manager, field, original)
        self.source = self.source.model_copy(update={'selection_sha256': '0' * 64})
        with self.assertRaises(ValueError):
            self.start(service)
        self.manager.start.assert_not_called()
        self.assertFalse(self.journal.directory.exists())

    def test_replaced_workspace_and_symlink_alias_fail_before_dispatch(self):
        service = self.configure()
        self.workspace.rename(self.root / 'old-workspace')
        self.workspace.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.start(service)
        self.workspace.rmdir()
        self.workspace.symlink_to(self.root / 'old-workspace', target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.start(service)
        self.manager.start.assert_not_called()

    def test_real_runner_callback_persists_exact_run_identity_before_effect(self):
        service = self.configure()
        self.start(service)
        operator = self.operator()
        operation = service.operation(operator, service.job_id)
        runner = operation.__self__
        self.assertIsInstance(runner, WebGoalExecutionRunner)
        state = self.state(operator)
        effects = []
        async def synthetic_form(actual_operator, *, on_created, **arguments):
            self.assertIs(actual_operator, operator)
            on_created(state)
            reopened = WebGoalExecutionJournal(self.journal.directory)
            status = reopened.inspect(service.intent_sha256)
            self.assertEqual(status['status'], 'awaiting_independent_verification')
            self.assertEqual(status['run_ref'], digest({'run_id': state.run_id}))
            self.assertEqual(service.run_identity, {field: getattr(state, field) for field in (
                'task_id', 'run_id', 'runtime_id', 'deployment_id', 'owner_lease_id',
                'skill_invocation_sha256')})
            effects.append('synthetic callback boundary')
            return {'run_id': state.run_id, 'status': 'cancelled'}

        with patch.object(RemoteFormOperator, 'form', synthetic_form):
            result = asyncio.run(operation(owner_lease_id=self.binding.authority.lease_id,
                                           execution_gate=AsyncMock()))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(effects), 1)
        self.assertTrue(self.journal.reserved)
        with self.assertRaises(ValueError):
            service.operation(operator, service.job_id)

    def test_callback_binding_failure_prevents_effect_and_preserves_reserved_intent(self):
        service = self.configure()
        self.start(service)
        operator = self.operator()
        operation = service.operation(operator, service.job_id)
        state = self.state(operator).model_copy(update={'owner_lease_id': 'foreign-lease'})
        effects = []

        async def synthetic_form(actual_operator, *, on_created, **arguments):
            on_created(state)
            effects.append('unreachable')
            return {'run_id': state.run_id, 'status': 'succeeded'}

        with patch.object(RemoteFormOperator, 'form', synthetic_form), self.assertRaises(AOSFault):
            asyncio.run(operation(owner_lease_id=self.binding.authority.lease_id,
                                  execution_gate=AsyncMock()))
        self.assertEqual(effects, [])
        self.assertEqual(operation.__self__.status, 'uncertain')
        self.assertEqual(service.status()['status'], 'uncertain_before_run_binding')
        self.assertTrue(self.journal.reserved)

    def test_finalization_requires_exact_succeeded_job_run_and_host_session_before_audit(self):
        service, state = self.bound()
        cases = ({'status': 'failed'}, {'status': 'running'},
                 {'run_id': identifier('run')}, {'session_id': 'foreign-session'})
        for changes in cases:
            self.row(service, state, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                service.finish(service.job_id)
        self.row(service, state)
        with self.assertRaises(ValueError):
            service.finish('foreign-job')
        self.auditor.assert_not_called()
        self.manager.remote_form_owned_fixture.read_whole_record.assert_not_called()
        self.assertTrue(self.journal.reserved)

    def test_failed_independent_source_audit_never_reads_or_accepts_record(self):
        service, state = self.bound()
        self.row(service, state)
        with self.assertRaisesRegex(ValueError, 'independent audit unavailable'):
            service.finish(service.job_id)
        self.auditor.assert_called_once_with(state.run_id)
        self.manager.remote_form_owned_fixture.read_whole_record.assert_not_called()
        self.assertTrue(self.journal.reserved)
        self.assertEqual(service.status()['status'], 'awaiting_independent_verification')
        self.assertEqual(list(self.journal.directory.glob('*.accepted.json')), [])

    def test_malformed_or_foreign_typed_audit_cannot_consume_one_use_record_reader(self):
        service, state = self.bound()
        self.row(service, state)
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_recipe_audit.json').read_text())['report']
        pins = {field: getattr(self.binding.invocation, field) for field in (
            'profile_sha256', 'task_sha256', 'form_plan_sha256', 'state_plan_sha256',
            'skill_sha256', 'recipe_sha256', 'skill_plan_sha256', 'case_inputs_sha256',
            'case_key', 'field_binding_sha256')}
        synthetic_report = report | pins | {
            'run_ref': digest({'run_id': state.run_id}),
            'invocation_sha256': service.run_identity['skill_invocation_sha256']}
        self.auditor.side_effect = None
        audits = ({}, report, synthetic_report | {'run_ref': '0' * 64},
                  synthetic_report | {'invocation_sha256': '0' * 64},
                  synthetic_report | {'profile_sha256': '0' * 64},
                  synthetic_report | {'recipe_sha256': '0' * 64},
                  synthetic_report | {'case_key': 'foreign-case'},
                  synthetic_report | {'executable_recipe_executed': 1},
                  synthetic_report | {'training_ready': True})
        for audit in audits:
            self.auditor.return_value = audit
            with self.subTest(audit=audit), self.assertRaises(ValueError):
                service.finish(service.job_id)
        self.manager.remote_form_owned_fixture.read_whole_record.assert_not_called()
        self.assertTrue(self.journal.reserved)
        self.assertEqual(list(self.journal.directory.glob('*.accepted.json')), [])

    def test_actual_scheduler_run_uses_real_runner_and_durable_child_binding_before_mock_effect(self):
        settings = Settings(workspace=self.workspace, database=self.root / 'trajectory.sqlite')
        store = TrajectoryStore(settings.database)
        self.addCleanup(store.close)
        parent_runtime = SimpleNamespace(runtime_id=identifier('desktop'),
                                         pins={'image_id': 'synthetic-image'})
        controller = DesktopController(store, parent_runtime)
        scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine())
        for field, value in vars(self.manager).items():
            if field.startswith('remote_'):
                setattr(scheduler, field, value)
        scheduler.remote_entry_profiles = self.arguments['profiles']
        scheduler.remote_form_field_name = None
        scheduler.remote_form_value = None
        scheduler.remote_form_tls_context = None
        scheduler.remote_form_cookie_sha256 = None
        scheduler.remote_form_public_state_plan_sha256 = None
        scheduler.remote_form_owned_target.revoke = Mock()
        scheduler.remote_form_owned_fixture.close = Mock()
        scheduler.remote_form_owned_fixture.verify_complete = Mock()
        control = controller.state()
        authority = self.binding.authority.model_copy(update={
            'desktop_session_id': controller.session_id, 'runtime_id': parent_runtime.runtime_id,
            'lease_id': control['lease_id'], 'generation': control['generation']})
        self.binding = build_web_goal_execution_binding(**(self.arguments | {'authority': authority}))
        self.manager = scheduler
        service = self.configure()
        scheduler.start = Mock(return_value={'job_id': 'synthetic-job'})
        self.start(service)
        scheduler.job_id = service.job_id
        store.insert('desktop_tasks', job_id=service.job_id, session_id=controller.session_id,
                     kind='browser_remote_form', lease_id=authority.lease_id,
                     generation=authority.generation, status='queued', real_model=0,
                     created_at=now(), updated_at=now())
        runtime_pin = WebRuntimePin.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_binding.json').read_text())['runtime'] | {
                'parent_runtime_id': parent_runtime.runtime_id})
        child_runtime = SimpleNamespace(runtime_id=runtime_pin.runtime_id,
                                        runtime_pin=Mock(return_value=runtime_pin), stop=Mock())
        draft = bind_web_task(self.arguments['profiles'], self.arguments['task'], runtime_pin)
        effects = []
        failures = []

        def update(job_id, status):
            if status == 'failed':
                failures.append(repr(sys.exception()))
            DesktopScheduler.update(scheduler, job_id, status)

        def initialize_operator(operator, *arguments, **keywords):
            configured = self.operator()
            operator.__dict__.update(configured.__dict__)
            operator.runtime = child_runtime
            operator.draft = draft
            operator.store = store

        async def synthetic_form(operator, *, on_created, **arguments):
            state = self.state(operator)
            store.create_run(state, operator.engine.identity, {'synthetic': True})
            on_created(state)
            self.assertIsInstance(service.runner, WebGoalExecutionRunner)
            self.assertEqual(service.run_identity['runtime_id'], child_runtime.runtime_id)
            self.assertNotEqual(service.run_identity['runtime_id'], authority.runtime_id)
            self.assertEqual(WebGoalExecutionJournal(self.journal.directory).inspect(
                service.intent_sha256)['status'], 'awaiting_independent_verification')
            effects.append('mock operator boundary')
            return {'run_id': state.run_id, 'status': 'succeeded'}

        with (patch('aos.desktop_form_mcp.DesktopHTTPSFormMCPRuntime', return_value=child_runtime),
              patch.object(RemoteFormOperator, '__init__', initialize_operator),
              patch.object(RemoteFormOperator, 'form', synthetic_form),
              patch.object(scheduler, 'update', update),
              patch.object(scheduler, 'check_remote_form_binding_preinsert'),
              patch.object(scheduler, 'check_remote_form_binding')):
            asyncio.run(scheduler.run(service.job_id, authority.lease_id, 'browser_remote_form'))
        self.assertEqual(effects, ['mock operator boundary'], failures)
        self.assertEqual(failures, ["ValueError('synthetic independent audit unavailable')"])
        self.auditor.assert_called_once_with(service.run_identity['run_id'])
        scheduler.remote_form_owned_fixture.read_whole_record.assert_not_called()
        row = store.connection.execute('SELECT status FROM desktop_tasks WHERE job_id=?',
                                       (service.job_id,)).fetchone()
        self.assertEqual(row['status'], 'failed')
        self.assertTrue(self.journal.reserved)
        self.assertEqual(list(self.journal.directory.glob('*.accepted.json')), [])

    def tls_sources(self, origin, bodies):
        original = self.arguments
        profile = original['profiles'].get(self.binding.source.profile_sha256).model_copy(update={
            'entry_url': origin + '/entry', 'allowed_origins': [origin]})
        profiles = WebApplicationProfiles(self.root / 'tls-profiles')
        profile_sha256 = profile_report(profile).profile_sha256
        profiles.register(profile, confirm_sha256=profile_sha256)
        task = original['task'].model_copy(update={'profile_sha256': profile_sha256,
            'entry_url': profile.entry_url, 'allowed_origins': [origin]})
        old_skill = original['store'].get(self.binding.source.skill_sha256)
        page = original['store'].pages.get(old_skill.page_draft_sha256).model_copy(update={
            'profile_sha256': profile_sha256, 'origin': origin, 'route_template': '/entry'})
        pages = SiteKnowledgeStore(self.root / 'tls-pages', profiles)
        page_sha256 = digest(page.model_dump(mode='json'))
        pages.register(page, confirm_sha256=page_sha256)
        skill = old_skill.model_copy(update={'profile_sha256': profile_sha256,
                                             'page_draft_sha256': page_sha256})
        skills = SiteSkillStore(self.root / 'tls-skills', profiles, pages)
        skill_sha256 = digest(skill.model_dump(mode='json'))
        skills.register(skill, confirm_sha256=skill_sha256)
        plan = original['plan'].model_copy(update={'profile_sha256': profile_sha256,
            'page_draft_sha256': page_sha256, 'skill_sha256': skill_sha256})
        inputs = original['inputs'].model_copy(update={'plan_sha256': digest(plan.model_dump(mode='json'))})
        form_plan = plan_web_https_form(profiles, task, submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', body_sha256=hashlib.sha256(bodies['form_body']).hexdigest(),
            body_bytes=len(bodies['form_body']))
        task_sha256 = digest(task.model_dump(mode='json'))
        state_plan = original['state_plan'].model_copy(update={
            'profile_sha256': profile_sha256, 'task_sha256': task_sha256,
            'form_plan_sha256': digest(form_plan.model_dump(mode='json')), 'state_url': origin + '/state',
            'expected_before_sha256': hashlib.sha256(bodies['before']).hexdigest(),
            'expected_after_sha256': hashlib.sha256(bodies['after']).hexdigest(),
            'expected_before_marker_sha256': form_state_marker_sha256(bodies['before'], 'outcome'),
            'expected_after_marker_sha256': form_state_marker_sha256(bodies['after'], 'outcome')})
        recipe = original['recipe'].model_copy(update={'profile_sha256': profile_sha256,
            'page_draft_sha256': page_sha256, 'skill_sha256': skill_sha256, 'task_sha256': task_sha256})
        source = original['source'].model_copy(update={'profile_sha256': profile_sha256,
            'task_sha256': task_sha256, 'skill_sha256': skill_sha256,
            'recipe_sha256': digest(recipe.model_dump(mode='json')),
            'skill_plan_sha256': digest(plan.model_dump(mode='json')),
            'case_inputs_sha256': digest(inputs.model_dump(mode='json'))})
        catalog_value = original['catalog'].model_dump(mode='json')
        catalog_value['profile_sha256'] = profile_sha256
        catalog_value['skills'][0].update({'skill_sha256': skill_sha256, 'task_sha256': task_sha256,
                                           'recipe_sha256': source.recipe_sha256})
        catalog = type(original['catalog']).model_validate(catalog_value)
        proposal = original['proposal'].model_copy(update={'catalog_sha256': digest(catalog.model_dump(mode='json'))})
        oracle = original['oracle'].model_copy(update={'profile_sha256': profile_sha256,
            'task_sha256': task_sha256, 'readback_url': state_plan.state_url})
        return original | {'store': skills, 'profiles': profiles, 'task': task, 'plan': plan,
            'inputs': inputs, 'form_plan': form_plan, 'state_plan': state_plan, 'recipe': recipe,
            'source': source, 'catalog': catalog, 'proposal': proposal, 'oracle': oracle}

    @unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL for synthetic TLS')
    def test_cpu_two_profiles_four_parameter_maps_real_operator_tls_and_snapshot_audit(self):
        root = self.root
        accepted = []
        cases = (
            ('synthetic-crm-note', 'contact_name', 'Ada', 'Call tomorrow'),
            ('synthetic-crm-note', 'contact_name', 'Grace', 'Send brief'),
            ('synthetic-inventory-note', 'item_code', 'SKU-42', 'Inspect stock'),
            ('synthetic-inventory-note', 'item_code', 'SKU-73', 'Check shelf'))
        for index, (application, identity_field, record_id, note) in enumerate(cases):
            with self.subTest(application=application, record_id=record_id):
                self.root = root / ('case-' + str(index))
                self.root.mkdir(mode=0o700)
                self.workspace = self.root / 'workspace'
                self.workspace.mkdir(mode=0o700)
                self.arguments = source_fixture(self.root / 'sources', application,
                                                 identity_field, record_id, note)
                self.binding = build_web_goal_execution_binding(**self.arguments)
                self.journal = WebGoalExecutionJournal(self.root / 'journal')
                accepted.append(self.execute_tls_source_case())
        self.assertEqual(len(accepted), 4)
        self.assertEqual(len({binding.source.profile_sha256 for binding in accepted}), 4)
        self.assertEqual({binding.oracle.scope.application_id for binding in accepted},
                         {'synthetic-crm-note', 'synthetic-inventory-note'})
        self.assertEqual({tuple(sorted(binding.oracle.expected_fields)) for binding in accepted},
                         {('contact_name', 'note'), ('item_code', 'note')})
        self.assertEqual(len({binding.parameter_variant_sha256 for binding in accepted}), 4)

    def execute_tls_source_case(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        origin = f'https://{HOST}:{listener.getsockname()[1]}'
        config = {'scope': self.binding.oracle.scope.model_dump(mode='json'),
            'fields': [{'name': name, 'label': name, 'value': value}
                       for name, value in sorted(self.binding.oracle.expected_fields.items())],
            'outcome_field': 'note', 'marker_id': 'outcome'}
        bodies = owned_record_form_bodies(config)
        sources = self.tls_sources(origin, bodies)
        settings = Settings(workspace=self.workspace, database=self.root / 'tls-trajectory.sqlite')
        store = TrajectoryStore(settings.database)
        self.addCleanup(store.close)
        parent = SimpleNamespace(runtime_id=identifier('desktop'), pins={'image_id': 'synthetic-image'})
        controller = DesktopController(store, parent)
        control = controller.state()
        authority = self.binding.authority.model_copy(update={'desktop_session_id': controller.session_id,
            'runtime_id': parent.runtime_id, 'lease_id': control['lease_id'], 'generation': control['generation']})
        sources['authority'] = authority
        binding = build_web_goal_execution_binding(**sources)
        certificate, key = self.root / 'certificate.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=' + HOST, '-addext', 'subjectAltName=DNS:' + HOST,
            '-keyout', str(key), '-out', str(certificate)], capture_output=True, check=True, timeout=15)
        certificate.chmod(0o600)
        key.chmod(0o600)
        fixture = OwnedFormFixture(listener.fileno(), origin=origin,
            profile_sha256=binding.source.profile_sha256,
            form_plan_sha256=binding.invocation.form_plan_sha256,
            state_plan_sha256=binding.invocation.state_plan_sha256,
            entry_url=origin + '/entry', submit_url=origin + '/submit',
            receipt_url=origin + '/receipt', state_url=origin + '/state',
            expected_body=bodies['form_body'], record_config=config,
            certificate_file=certificate, key_file=key,
            certificate_sha256=hashlib.sha256(certificate.read_bytes()).hexdigest())
        self.addCleanup(fixture.close)
        scheduler = DesktopScheduler(controller, settings, FixtureDecisionEngine())
        scheduler.remote_entry_profiles = sources['profiles']
        scheduler.remote_entry_profile_sha256 = binding.source.profile_sha256
        scheduler.remote_entry_task = sources['task']
        scheduler.remote_form_plan = sources['form_plan']
        scheduler.remote_form_fields = tuple(sorted(binding.oracle.expected_fields.items()))
        scheduler.remote_form_field_name = None
        scheduler.remote_form_value = None
        scheduler.remote_form_state_plan = sources['state_plan']
        scheduler.remote_form_tls_context = fixture.target.tls_context
        scheduler.remote_form_skill_invocation = binding.invocation.model_dump(mode='json')
        scheduler.remote_form_skill_invocation_sha256 = digest(binding.invocation.model_dump(mode='json'))
        scheduler.remote_form_owned_fixture = fixture
        scheduler.remote_form_owned_target = fixture.target
        scheduler._owned_form_lifecycle = 'ready'
        source_arguments = (sources['store'], sources['plan'], sources['inputs'], sources['case_key'],
            sources['profiles'], sources['task'], sources['form_plan'], sources['state_plan'],
            sources['field_bindings'], sources['recipe'])
        scheduler.remote_form_skill_revalidator = lambda: revalidate_site_skill_form_recipe_invocation(
            binding.invocation, *source_arguments)
        self.assertEqual(scheduler.remote_form_skill_revalidator(), binding.invocation)
        audits = []

        def independent_audit(run_id):
            report = audit_site_skill_form_recipe_execution(
                *source_arguments, binding.invocation, settings.database, run_id)
            audits.append(report)
            return report

        scheduler.configure_parameter_web_goal_execution(binding, self.journal,
            manager_session=authority.manager_session, current_source=lambda: binding.source,
            source_auditor=independent_audit)
        service = scheduler.parameter_web_goal_execution
        self.addCleanup(service.close)
        runtime_pin = WebRuntimePin.model_validate(json.loads(
            (REPO_ROOT / 'examples/web_application_binding.json').read_text())['runtime'] | {
                'parent_runtime_id': parent.runtime_id})
        events = []
        backend = TLSFixtureBrowserBackend(runtime_pin, sources, events)
        failures = []

        def update(job_id, status):
            if status == 'failed':
                failures.append(repr(sys.exception()))
            DesktopScheduler.update(scheduler, job_id, status)

        async def execute():
            try:
                scheduler.start_parameter_web_goal_execution(confirm_sha256=binding.confirm_sha256,
                    human_confirmation=True, lease_id=authority.lease_id, generation=authority.generation)
            except AOSFault as error:
                if error.__cause__ is not None:
                    raise error.__cause__
                raise
            for attempt in range(1000):
                if scheduler.task.done():
                    break
                row = store.connection.execute("SELECT * FROM desktop_approvals WHERE status='pending'").fetchone()
                if row is not None:
                    scheduler.respond(row['approval_id'], row['action_sha256'], True)
                await asyncio.sleep(.001)
            await asyncio.wait_for(scheduler.task, 5)

        with (patch('aos.desktop_form_mcp.DesktopHTTPSFormMCPRuntime', return_value=backend),
              patch.object(scheduler, 'update', update)):
            asyncio.run(execute())
        row = store.connection.execute('SELECT status,run_id FROM desktop_tasks WHERE job_id=?',
                                       (service.job_id,)).fetchone()
        self.assertEqual(row['status'], 'succeeded', failures)
        self.assertIsInstance(service.runner, WebGoalExecutionRunner)
        self.assertEqual(service.runner.approved_stages, 6)
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['run_ref'], digest({'run_id': row['run_id']}))
        self.assertEqual(audits[0]['consumed_approval_count'], 6)
        self.assertEqual(service.status()['status'], 'accepted_verified')
        self.assertFalse(self.journal.reserved)
        self.assertTrue(service.receipt['record_outcome_verified'])
        self.assertFalse(service.receipt['site_outcome_verified'])
        self.assertFalse(service.receipt['training_ready'] or service.receipt['gpu_release_verified'])
        self.assertFalse(scheduler.engine.identity['real_model'])
        self.assertFalse(backend.status()['real_execution'])
        self.assertEqual(fixture._state.committed_record, binding.oracle.expected_fields)
        self.assertEqual(sum(event == ('tool', 'browser.form.submit') for event in events), 1)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM actions').fetchone()[0], 7)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM human_interventions WHERE kind='approve'").fetchone()[0], 6)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM model_calls').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            scheduler.start_parameter_web_goal_execution(confirm_sha256=binding.confirm_sha256,
                human_confirmation=True, lease_id=authority.lease_id, generation=authority.generation)
        with self.assertRaises(AOSFault):
            scheduler.start(authority.lease_id, authority.generation, 'browser_remote_form')
        return binding

    def console(self):
        assets = self.root / 'assets'
        assets.mkdir()
        self.manager.start_parameter_web_goal_execution = lambda **arguments: (
            DesktopScheduler.start_parameter_web_goal_execution(self.manager, **arguments))
        origin = 'http://127.0.0.1:19483'
        return create_console(self.manager.controller, 'synthetic-token', origin, assets,
                              scheduler=self.manager), origin

    def test_authenticated_console_preview_start_and_content_free_journal_report(self):
        service = self.configure()
        app, origin = self.console()
        review = {'schema_version': '1.0', 'lease_id': self.binding.authority.lease_id,
                  'generation': self.binding.authority.generation}

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                prefix = '/api/tasks/parameter-web-goal/'
                denied = await client.post(prefix + 'preview', json=review)
                self.assertEqual(denied.status_code, 401)
                self.manager.start.assert_not_called()
                await client.post('/api/login', json={'token': 'synthetic-token'})
                foreign_origin = await client.post(prefix + 'preview', json=review,
                                                   headers={'Origin': 'https://foreign.aos.invalid'})
                self.assertEqual(foreign_origin.status_code, 403)
                preview = await client.post(prefix + 'preview', json=review)
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.json()['binding_canonical'],
                                 canonical(self.binding.model_dump(mode='json')))
                self.assertFalse(preview.json()['execution_authorized'])
                start = await client.post(prefix + 'start', json=review | {
                    'confirm_sha256': self.binding.confirm_sha256, 'human_confirmation': True})
                self.assertEqual(start.status_code, 202)
                self.assertFalse(start.json()['independently_verified'])
                report = await client.post(prefix + 'report', json={
                    'schema_version': '1.0', 'intent_sha256': service.intent_sha256})
                self.assertEqual(report.status_code, 200)
                self.assertEqual(report.json()['status'], 'uncertain_before_run_binding')
                self.assertFalse(report.json()['replay_authorized'])
                self.assertNotIn('Call tomorrow', report.text)
                self.assertNotIn('Ada', report.text)
                replay = await client.post(prefix + 'start', json=review | {
                    'confirm_sha256': self.binding.confirm_sha256, 'human_confirmation': True})
                self.assertEqual(replay.status_code, 409)
                self.assertEqual(self.manager.start.call_count, 1)

        asyncio.run(check())

    def test_console_exact_requests_reject_forged_claims_duplicates_and_oversize_before_start(self):
        self.configure()
        app, origin = self.console()
        review = {'schema_version': '1.0', 'lease_id': self.binding.authority.lease_id,
                  'generation': self.binding.authority.generation,
                  'confirm_sha256': self.binding.confirm_sha256, 'human_confirmation': True}

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                endpoint = '/api/tasks/parameter-web-goal/start'
                for change in ({'human_confirmation': 1}, {'human_confirmation': 'true'},
                               {'human_confirmation': False}, {'generation': True},
                               {'generation': '2'}, {'execution_authorized': True},
                               {'activation_authorized': True}, {'training_ready': True},
                               {'scope_authorization_verified': True}, {'parameters': {'note-text': 'secret'}},
                               {'schema_version': '2.0'}):
                    response = await client.post(endpoint, json=review | change)
                    self.assertEqual(response.status_code, 400, change)
                    self.assertNotIn('secret', response.text)
                raw = canonical(review).encode()
                duplicate = raw[:-1] + b',"human_confirmation":true}'
                self.assertEqual((await client.post(endpoint, content=duplicate)).status_code, 400)
                self.assertEqual((await client.post(endpoint, content=b' ' * 4097)).status_code, 413)
                self.assertEqual((await client.post(endpoint + '?extra=secret', json=review)).status_code, 400)
                self.assertEqual((await client.post('/api/tasks/parameter-web-goal/unknown',
                                                   json=review)).status_code, 400)
                self.manager.start.assert_not_called()
                self.assertFalse(self.journal.directory.exists())

        asyncio.run(check())

    def test_console_default_denial_and_stale_control_do_not_expose_private_errors(self):
        app, origin = self.console()
        review = {'schema_version': '1.0', 'lease_id': self.binding.authority.lease_id,
                  'generation': self.binding.authority.generation}

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=origin, headers={'Origin': origin}) as client:
                await client.post('/api/login', json={'token': 'synthetic-token'})
                prefix = '/api/tasks/parameter-web-goal/'
                denied = await client.post(prefix + 'preview', json=review)
                self.assertEqual(denied.status_code, 409)
                self.configure()
                self.control['generation'] += 1
                stale = await client.post(prefix + 'start', json=review | {
                    'confirm_sha256': self.binding.confirm_sha256, 'human_confirmation': True})
                self.assertEqual(stale.status_code, 409)
                self.assertNotIn(str(self.root), stale.text)
                self.assertNotIn('Call tomorrow', stale.text)
                self.assertNotIn(self.binding.confirm_sha256, stale.text)
                self.manager.start.assert_not_called()
                self.assertFalse(self.journal.directory.exists())

        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
