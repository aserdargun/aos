"""Separately authorized manual-skill reuse through the existing task executor."""

import asyncio
import math
import os

from .contracts import canonical, digest
from .owned_parameter_skill_reuse import OwnedParameterSkillReuseSession
from .web_goal_desktop_execution import DesktopParameterWebGoalExecution
from .web_goal_execution_binding import (
    WebGoalExecutionAuthority, WebGoalExecutionBinding, WebGoalExecutionSource,
    confirm_web_goal_execution_binding,
)
from .web_goal_execution_journal import MAX_INTENTS, WebGoalExecutionJournal, _directory_identity
from .workspace_identity import open_existing_workspace, workspace_identity


class OwnedParameterSkillReuseExecution:
    def __init__(self, manager, reuse_session, journal, *, manager_session, prepare_manager,
                 cleanup_timeout_seconds=10.0):
        workspace = manager.settings.workspace.absolute()
        if (not isinstance(reuse_session, OwnedParameterSkillReuseSession)
                or not isinstance(journal, WebGoalExecutionJournal)
                or not callable(prepare_manager)
                or journal.directory == workspace or workspace in journal.directory.parents
                or journal.directory in workspace.parents
                or manager.closed or manager.restart_quiesced
                or type(cleanup_timeout_seconds) not in (int, float)
                or not math.isfinite(cleanup_timeout_seconds)
                or not 0 < cleanup_timeout_seconds <= 10):
            raise ValueError('owned_parameter_reuse_execution_configuration_invalid')
        self.manager = manager
        self.controller = manager.controller
        self.store = manager.store
        self.connection = manager.store.connection
        self.engine = manager.engine
        self.engine_identity = canonical(manager.engine.identity)
        self.settings = manager.settings
        self.reuse_session = reuse_session
        self.journal = journal
        self.manager_session = manager_session
        self.prepare_manager = prepare_manager
        self.workspace_fd = open_existing_workspace(workspace)
        self.workspace = workspace_identity(workspace, self.workspace_fd)
        self.child_manager = None
        self.execution = None
        self.admission = None
        self.binding = None
        self.intent_sha256 = None
        self.job_id = None
        self.starting = False
        self._dispatch_starting = False
        self.transitioning = False
        self.transition_blocked = False
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self.cleanup_task = None

    @property
    def reserved(self):
        return (self.transitioning or self.transition_blocked
                or not self._dispatch_starting and self.journal.reserved
                or self.child_manager is not None and self.child_manager.reserved)

    @property
    def config(self):
        return self.child_manager

    def authority(self):
        manager = self.manager
        descriptor = open_existing_workspace(manager.settings.workspace)
        try:
            current = workspace_identity(manager.settings.workspace, descriptor)
        finally:
            os.close(descriptor)
        if (self.workspace_fd is None or manager.closed or manager.restart_quiesced
                or manager.controller is not self.controller or manager.store is not self.store
                or manager.store.connection is not self.connection
                or manager.engine is not self.engine or manager.settings is not self.settings
                or canonical(manager.engine.identity) != self.engine_identity
                or current != self.workspace
                or workspace_identity(manager.settings.workspace, self.workspace_fd) != self.workspace):
            raise ValueError('owned_parameter_reuse_authority_unavailable')
        control = manager.controller.state()
        return WebGoalExecutionAuthority(
            manager_session=self.manager_session, desktop_session_id=manager.controller.session_id,
            runtime_id=control['runtime_id'], lease_id=control['lease_id'],
            generation=control['generation'], owner=control['owner'], status=control['status'])

    def _available(self, lease_id, generation):
        if (self.intent_sha256 is not None or self.manager.reserved or self.reserved
                or self.manager.closed or self.manager.restart_quiesced):
            raise ValueError('owned_parameter_reuse_requires_fresh_execution')
        try:
            with self.journal._locked() as (descriptor, workspace, identity):
                if self.journal._inventory(descriptor, workspace, identity):
                    raise ValueError('owned_parameter_reuse_existing_history_requires_live_predecessor')
        except FileNotFoundError:
            if self.journal._journal_identity is not None:
                raise ValueError('owned_parameter_reuse_execution_history_missing') from None
        authority = self.authority()
        if (lease_id != authority.lease_id or type(generation) is not int
                or generation != authority.generation):
            raise ValueError('owned_parameter_reuse_control_changed')
        return authority

    def _source(self, admission):
        self.authority()
        original = self.reuse_session.current_source(admission)
        if (digest(self.manager.remote_form_owned_manifest) != original['manifest_sha256']
                or self.manager.remote_entry_profile_sha256 != original['profile_sha256']
                or self.manager.remote_form_skill_invocation_sha256 != original['source_invocation_sha256']
                or self.manager.settings.database.absolute()
                != self.reuse_session.release_session.review_session.candidate_session.database):
            raise ValueError('owned_parameter_reuse_parent_source_changed')
        return WebGoalExecutionSource.model_validate_json(canonical({
            **{key: original[key] for key in (
                'source_run_ref', 'source_invocation_sha256', 'candidate_sha256',
                'review_sha256', 'release_sha256', 'selection_sha256',
                'profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256')},
            'reuse_admission_sha256': digest(admission),
            'skill_plan_sha256': admission['skill_plan_sha256'],
            'case_inputs_sha256': admission['case_inputs_sha256'],
        })).model_dump(mode='json')

    def _binding(self, admission, authority, transition=None):
        bundle = self.reuse_session.bundle(admission)
        source = self._source(admission)
        value = {
            'schema_version': '1.0', 'kind': 'web_goal_parameter_execution_binding',
            'synthetic': True, 'authority': authority.model_dump(mode='json'), 'source': source,
            'catalog_sha256': digest(admission['source']),
            'proposal_sha256': digest(admission if transition is None else transition),
            'skill_ref': bundle['skill_store'].get(bundle['skill_sha256']).skill_key,
            'case_key': admission['case_key'],
            'parameters': admission['parameters'],
            'parameter_variant_sha256': admission['parameter_variant_sha256'],
            'field_bindings': admission['field_bindings'],
            'form_body_sha256': admission['form_body_sha256'],
            'form_body_bytes': len(bundle['body']), 'invocation': admission['invocation'],
            'oracle': {
                'schema_version': '1.0', 'kind': 'web_goal_whole_record_contract',
                'scope': admission['source']['scope'], 'profile_sha256': source['profile_sha256'],
                'task_sha256': source['task_sha256'], 'source_run_ref': source['source_run_ref'],
                'case_key': admission['case_key'], 'readback_url': bundle['state_plan'].state_url,
                'expected_fields': dict(bundle['fields']),
                'expected_record_sha256': admission['expected_record_sha256'],
                'maximum_reported_posts': 1, 'ambiguous_effect': 'stop_without_retry',
            },
            **{key: False for key in ('execution_authorized', 'execution_performed',
                'site_outcome_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified')},
        }
        value['confirm_sha256'] = digest(value)
        return WebGoalExecutionBinding.model_validate_json(canonical(value)), bundle

    def preview(self, release_sha256, selection_sha256, parameters, *, lease_id, generation):
        authority = self._available(lease_id, generation)
        admission, checksum = self.reuse_session.preview(release_sha256, selection_sha256, parameters)
        binding, _bundle = self._binding(admission, authority)
        self._available(lease_id, generation)
        value = binding.model_dump(mode='json')
        return {
            'schema_version': '1.0', 'admission': admission,
            'admission_canonical': canonical(admission), 'admission_sha256': checksum,
            'binding': value, 'binding_canonical': canonical(value), 'binding_sha256': digest(value),
            'confirm_sha256': binding.confirm_sha256,
            'execution_authorized': False, 'execution_performed': False,
            'activation_authorized': False, 'training_ready': False, 'gpu_release_verified': False,
        }

    def start(self, admission, *, confirm_sha256, human_confirmation, lease_id, generation):
        authority = self._available(lease_id, generation)
        binding, bundle = self._binding(admission, authority)
        admission = bundle['admission']
        current_source = lambda: self._source(admission)
        confirmation = confirm_web_goal_execution_binding(
            binding, confirm_sha256=confirm_sha256, human_confirmation=human_confirmation,
            current_source=current_source, current_authority=self.authority)
        return self._publish_and_start(binding, bundle, confirmation, current_source,
                                       lease_id, generation)

    def _publish_and_start(self, binding, bundle, confirmation, current_source, lease_id, generation):
        admission = bundle['admission']
        intent = self.journal.begin(binding, confirmation,
            current_source=current_source, current_authority=self.authority)
        self.intent_sha256 = intent
        self.admission = admission
        self.binding = binding
        self.child_manager = None
        self.execution = None
        self.job_id = None
        self.starting = True
        child = None
        try:
            child = self.prepare_manager(bundle)
            if (child is self.manager or child.controller is not self.manager.controller
                    or child.store is not self.manager.store or child.engine is not self.manager.engine
                    or child.settings != self.manager.settings
                    or child.remote_form_owned_fixture is self.manager.remote_form_owned_fixture
                    or child.remote_form_owned_target is self.manager.remote_form_owned_target
                    or child.parameter_web_goal_execution is not None
                    or child.owned_parameter_project_execution is not None):
                raise ValueError('owned_parameter_reuse_child_identity_changed')
            self.child_manager = child
            execution = DesktopParameterWebGoalExecution(
                child, binding, self.journal, manager_session=self.manager_session,
                current_source=current_source,
                source_auditor=lambda run_id: self.reuse_session.audit(
                    admission, self.manager.settings.database, run_id))
            self.execution = execution
            execution.intent_sha256 = intent
            execution.confirmation = confirmation
            child.parameter_web_goal_execution = execution
            execution.starting = True
            try:
                execution._configuration_current()
                self._dispatch_starting = True
                result = child.start(lease_id, generation, 'browser_remote_form', False, False)
                execution.job_id = result['job_id']
                self.job_id = result['job_id']
                return result | {'intent_sha256': intent,
                    'admission_sha256': digest(admission),
                    'binding_sha256': digest(binding.model_dump(mode='json')),
                    'independently_verified': False, 'training_ready': False,
                    'gpu_release_verified': False}
            finally:
                self._dispatch_starting = False
                execution.starting = False
        except BaseException:
            if child is not None and child is not self.manager:
                fixture = getattr(child, 'remote_form_owned_fixture', None)
                if fixture is not None and fixture is not self.manager.remote_form_owned_fixture:
                    fixture.close()
            raise
        finally:
            self.starting = False

    def _predecessor(self, intent_sha256, receipt_sha256):
        with self.journal._locked() as (descriptor, workspace, identity):
            records = self.journal._inventory(descriptor, workspace, identity)
            self._validate_transitions(records)
            if len(records) >= MAX_INTENTS:
                raise ValueError('owned_parameter_reuse_execution_history_full')
            entry = self.journal._entry(records, intent_sha256)
            if (entry['accepted'] is None or digest(entry['accepted']) != receipt_sha256
                    or entry['bound'] is None):
                raise ValueError('owned_parameter_reuse_predecessor_not_accepted')
            return entry

    def _predecessor_readback(self, expected):
        """Check immutable proof without retaking an enclosing journal mutation lock."""
        journal = self.journal
        parent = open_existing_workspace(journal.directory.parent)
        descriptor = None
        try:
            descriptor = open_existing_workspace(journal.directory)
            journal._linked(descriptor, parent)
            if (workspace_identity(journal.directory.parent, parent).model_dump(mode='json')
                    != journal._workspace_identity
                    or _directory_identity(descriptor) != journal._journal_identity):
                raise ValueError('owned_parameter_reuse_predecessor_directory_changed')
            records = journal._inventory(descriptor, journal._workspace_identity, journal._journal_identity)
            self._validate_transitions(records)
            intent_sha256 = digest(expected['intent'])
            keys = {'intent': expected['intent']['confirmation_sha256'],
                    'bound': intent_sha256, 'accepted': intent_sha256}
            for stage, checksum in keys.items():
                if journal._read(descriptor, checksum + '.' + stage + '.json', stage) != expected[stage]:
                    raise ValueError('owned_parameter_reuse_predecessor_changed')
            journal._validate_terminal(expected['intent'], expected['bound'], expected['accepted'])
            journal._linked(descriptor, parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def _next_ready(self, previous_intent_sha256, previous_receipt_sha256,
                    lease_id, generation, *, during_transition=False, cleaned=False):
        if (self.transition_blocked or self.transitioning != during_transition
                or self.intent_sha256 != previous_intent_sha256
                or self.execution is None or self.child_manager is None
                or self.journal.reserved or self.manager.busy or self.manager.paused
                or not during_transition and self.manager.reserved):
            raise ValueError('owned_parameter_reuse_next_task_unavailable')
        authority = self.authority()
        if (lease_id != authority.lease_id or type(generation) is not int
                or generation != authority.generation):
            raise ValueError('owned_parameter_reuse_control_changed')
        entry = self._predecessor(previous_intent_sha256, previous_receipt_sha256)
        child = self.child_manager
        execution = self.execution
        row = self.store.connection.execute(
            'SELECT run_id,status FROM desktop_tasks WHERE job_id=? AND session_id=?',
            (self.job_id, self.controller.session_id)).fetchone()
        pending = self.store.connection.execute(
            "SELECT count(*) FROM desktop_approvals WHERE job_id=? AND status IN ('pending','approved')",
            (self.job_id,)).fetchone()[0]
        if (execution.manager is not child or execution.job_id != self.job_id
                or execution.intent_sha256 != previous_intent_sha256
                or execution.receipt != entry['accepted']
                or execution.run_identity != entry['bound']['run_identity']
                or digest(self.binding.model_dump(mode='json')) != entry['intent']['binding_sha256']
                or child.task is None or not child.task.done() or child.task.cancelled()
                or child.task.exception() is not None or child.busy or child.paused
                or row is None or row['status'] != 'succeeded'
                or row['run_id'] != entry['bound']['run_identity']['run_id']
                or pending or child.answer is not None
                or child.active_runtime is not None
                or not child.remote_form_owned_fixture._closed
                or child.remote_form_owned_target._active
                or cleaned and (not child.closed or child.completed_runtime is not None)):
            raise ValueError('owned_parameter_reuse_previous_task_not_settled')
        self._source(self.admission)
        return authority

    @staticmethod
    def _transition(admission, previous_intent_sha256, previous_receipt_sha256):
        return OwnedParameterSkillReuseExecution._transition_for_hash(
            digest(admission), previous_intent_sha256, previous_receipt_sha256)

    @staticmethod
    def _transition_for_hash(admission_sha256, previous_intent_sha256, previous_receipt_sha256):
        return {
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_reuse_next_task',
            'previous_intent_sha256': previous_intent_sha256,
            'previous_receipt_sha256': previous_receipt_sha256,
            'admission_sha256': admission_sha256,
        }

    def _validate_transitions(self, records):
        roots = []
        parents = {}
        for checksum, entry in records.items():
            binding = entry['intent']['binding']
            admission_sha256 = binding['source']['reuse_admission_sha256']
            if binding['proposal_sha256'] == admission_sha256:
                roots.append(checksum)
                continue
            matches = [previous for previous, predecessor in records.items()
                       if predecessor['accepted'] is not None
                       and digest(self._transition_for_hash(admission_sha256, previous,
                           digest(predecessor['accepted']))) == binding['proposal_sha256']]
            if len(matches) != 1 or matches[0] == checksum:
                raise ValueError('owned_parameter_reuse_transition_history_changed')
            parents[checksum] = matches[0]
        if len(roots) != 1 or len(set(parents.values())) != len(parents):
            raise ValueError('owned_parameter_reuse_transition_history_changed')
        for checksum in parents:
            seen = set()
            while checksum in parents and checksum not in seen:
                seen.add(checksum)
                checksum = parents[checksum]
            if checksum != roots[0]:
                raise ValueError('owned_parameter_reuse_transition_history_changed')

    def next_preview(self, release_sha256, selection_sha256, parameters, *,
                     previous_intent_sha256, previous_receipt_sha256, lease_id, generation):
        authority = self._next_ready(previous_intent_sha256, previous_receipt_sha256,
                                     lease_id, generation)
        admission, checksum = self.reuse_session.preview(release_sha256, selection_sha256, parameters)
        if admission['parameters'] == self.admission['parameters']:
            raise ValueError('owned_parameter_reuse_next_parameters_unchanged')
        transition = self._transition(admission, previous_intent_sha256, previous_receipt_sha256)
        binding, _bundle = self._binding(admission, authority, transition)
        self._next_ready(previous_intent_sha256, previous_receipt_sha256, lease_id, generation)
        value = binding.model_dump(mode='json')
        return {
            'schema_version': '1.0', 'admission': admission,
            'admission_canonical': canonical(admission), 'admission_sha256': checksum,
            'binding': value, 'binding_canonical': canonical(value), 'binding_sha256': digest(value),
            'confirm_sha256': binding.confirm_sha256,
            'predecessor': {'intent_sha256': previous_intent_sha256,
                            'receipt_sha256': previous_receipt_sha256},
            'transition': transition, 'transition_sha256': digest(transition),
            'execution_authorized': False, 'execution_performed': False,
            'activation_authorized': False, 'training_ready': False, 'gpu_release_verified': False,
        }

    async def next_start(self, admission, *, previous_intent_sha256, previous_receipt_sha256,
                         confirm_sha256, human_confirmation, lease_id, generation):
        authority = self._next_ready(previous_intent_sha256, previous_receipt_sha256,
                                     lease_id, generation)
        bundle = self.reuse_session.bundle(admission)
        admission = bundle['admission']
        if admission['parameters'] == self.admission['parameters']:
            raise ValueError('owned_parameter_reuse_next_parameters_unchanged')
        transition = self._transition(admission, previous_intent_sha256, previous_receipt_sha256)
        binding, bundle = self._binding(admission, authority, transition)
        predecessor = self._predecessor(previous_intent_sha256, previous_receipt_sha256)

        def current_source():
            self._predecessor_readback(predecessor)
            return self._source(admission)

        confirmation = confirm_web_goal_execution_binding(
            binding, confirm_sha256=confirm_sha256, human_confirmation=human_confirmation,
            current_source=current_source, current_authority=self.authority)
        self.transitioning = True
        try:
            self.cleanup_task = asyncio.create_task(self.child_manager.close())
            self.cleanup_task.add_done_callback(self._observe_cleanup)
            await self.wait_cleanup()
            current = self._next_ready(previous_intent_sha256, previous_receipt_sha256,
                lease_id, generation, during_transition=True, cleaned=True)
            if current != authority:
                raise ValueError('owned_parameter_reuse_control_changed')
            fresh_binding, bundle = self._binding(admission, current, transition)
            if fresh_binding != binding:
                raise ValueError('owned_parameter_reuse_next_confirmation_changed')
            self.cleanup_task = None
            result = self._publish_and_start(binding, bundle, confirmation, current_source,
                                              lease_id, generation)
            return result | {
                'predecessor': {'intent_sha256': previous_intent_sha256,
                                'receipt_sha256': previous_receipt_sha256},
                'transition_sha256': digest(transition),
            }
        except BaseException:
            self.transition_blocked = True
            raise
        finally:
            self.transitioning = False

    def _observe_cleanup(self, task):
        if task.cancelled() or task.exception() is not None:
            self.transition_blocked = True

    async def wait_cleanup(self):
        if self.cleanup_task is None:
            raise ValueError('owned_parameter_reuse_cleanup_not_started')
        try:
            await asyncio.wait_for(asyncio.shield(self.cleanup_task),
                                   timeout=self.cleanup_timeout_seconds)
        except BaseException:
            self.transition_blocked = True
            raise

    def read(self, intent_sha256):
        return self.journal.inspect(intent_sha256)

    def check_job(self, job_id):
        if self.execution is None:
            raise ValueError('owned_parameter_reuse_execution_not_started')
        self.execution.check_job(job_id)

    def operation(self, operator, job_id):
        self.check_job(job_id)
        return self.execution.operation(operator, job_id)

    def finish(self, job_id):
        self.check_job(job_id)
        return self.execution.finish(job_id)

    def status(self):
        if self.intent_sha256 is not None:
            return self.journal.inspect(self.intent_sha256)
        return {'available': self.workspace_fd is not None and not self.reserved, 'status': 'review_required',
                'unresolved_intent': self.journal.reserved, 'independently_verified': False,
                'training_ready': False, 'gpu_release_verified': False}

    def close(self):
        if self.execution is not None:
            self.execution.close()
        if self.child_manager is not None and self.child_manager is not self.manager:
            fixture = getattr(self.child_manager, 'remote_form_owned_fixture', None)
            if fixture is not None and fixture is not self.manager.remote_form_owned_fixture:
                fixture.close()
        if self.workspace_fd is not None:
            os.close(self.workspace_fd)
            self.workspace_fd = None
