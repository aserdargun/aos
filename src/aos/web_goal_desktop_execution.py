"""Trusted-host parameter binding into the existing finite desktop form path."""

import os

from .contracts import canonical, digest
from .site_skill_form_recipe_audit import SiteSkillFormRecipeAuditReport
from .web_goal_execution_binding import (
    WebGoalExecutionAuthority, WebGoalExecutionBinding,
    WebGoalExecutionSource, _typed,
    confirm_web_goal_execution_binding, read_web_goal_whole_record)
from .web_goal_execution_journal import WebGoalExecutionJournal
from .workspace_identity import open_existing_workspace, workspace_identity


class DesktopParameterWebGoalExecution:
    def __init__(self, manager, binding, journal, *, manager_session,
                 current_source, source_auditor):
        binding = WebGoalExecutionBinding.model_validate_json(canonical(
            binding.model_dump(mode='json') if isinstance(binding, WebGoalExecutionBinding)
            else binding))
        workspace_path = manager.settings.workspace.absolute()
        journal_path = journal.directory.absolute() if isinstance(journal, WebGoalExecutionJournal) else None
        if (not isinstance(journal, WebGoalExecutionJournal)
                or journal_path == workspace_path or workspace_path in journal_path.parents
                or journal_path in workspace_path.parents
                or not callable(current_source) or not callable(source_auditor)
                or manager.reserved or manager.closed or manager.restart_quiesced
                or manager.remote_form_owned_fixture is None
                or getattr(manager.remote_form_owned_fixture, 'record_mode', False) is not True
                or manager._owned_skill_reuse is not None
                or manager.remote_form_owned_candidate_session is not None
                or manager.remote_form_public_plan_sha256 is not None
                or manager.remote_form_cookie is not None):
            raise ValueError('parameter_web_goal_host_configuration_invalid')
        self.manager = manager
        self.binding = binding
        self.journal = journal
        self.manager_session = manager_session
        self.source = current_source
        self.source_auditor = source_auditor
        self.workspace_fd = open_existing_workspace(manager.settings.workspace)
        self.workspace = workspace_identity(manager.settings.workspace, self.workspace_fd)
        self.intent_sha256 = None
        self.confirmation = None
        self.runner = None
        self.starting = False
        self.run_identity = None
        self.job_id = None
        self.receipt = None
        try:
            self._configuration_current()
        except BaseException:
            self.close()
            raise

    @property
    def reserved(self):
        return self.journal.reserved and not self.starting

    def authority(self):
        manager = self.manager
        control = manager.controller.state()
        descriptor = open_existing_workspace(manager.settings.workspace)
        try:
            current_workspace = workspace_identity(manager.settings.workspace, descriptor)
        finally:
            os.close(descriptor)
        if (manager.closed or manager.restart_quiesced or self.workspace_fd is None
                or workspace_identity(manager.settings.workspace, self.workspace_fd) != self.workspace
                or current_workspace != self.workspace):
            raise ValueError('parameter_web_goal_authority_unavailable')
        return WebGoalExecutionAuthority(
            manager_session=self.manager_session,
            desktop_session_id=manager.controller.session_id,
            runtime_id=control['runtime_id'], lease_id=control['lease_id'],
            generation=control['generation'], owner=control['owner'], status=control['status'])

    def _configuration_current(self):
        manager = self.manager
        binding = self.binding
        if (self.authority() != binding.authority
                or canonical(_typed(WebGoalExecutionSource, self.source()).model_dump(mode='json'))
                != canonical(binding.source.model_dump(mode='json'))
                or manager.remote_entry_profile_sha256 != binding.source.profile_sha256
                or digest(manager.remote_entry_task.model_dump(mode='json')) != binding.source.task_sha256
                or digest(manager.remote_form_plan.model_dump(mode='json')) != binding.invocation.form_plan_sha256
                or digest(manager.remote_form_state_plan.model_dump(mode='json')) != binding.invocation.state_plan_sha256
                or manager.remote_form_skill_invocation_sha256
                != digest(binding.invocation.model_dump(mode='json'))
                or canonical(manager.remote_form_skill_invocation)
                != canonical(binding.invocation.model_dump(mode='json'))
                or dict(manager.remote_form_fields) != binding.oracle.expected_fields
                or manager.remote_form_state_plan.state_url != binding.oracle.readback_url):
            raise ValueError('parameter_web_goal_host_configuration_changed')
        manager.remote_form_owned_target.assert_plan(
            binding.source.profile_sha256, binding.invocation.form_plan_sha256)
        manager.remote_form_owned_target.assert_state_plan(binding.invocation.state_plan_sha256)

    def check_job(self, job_id):
        if self.intent_sha256 is None or job_id != self.job_id:
            raise ValueError('parameter_web_goal_job_not_admitted')
        self._configuration_current()

    def start(self, *, confirm_sha256, human_confirmation, lease_id, generation):
        manager = self.manager
        if (self.intent_sha256 is not None or manager.reserved or manager.closed
                or manager.restart_quiesced or lease_id != self.binding.authority.lease_id
                or type(generation) is not int or generation != self.binding.authority.generation):
            raise ValueError('parameter_web_goal_requires_fresh_review')
        self._configuration_current()
        confirmation = confirm_web_goal_execution_binding(
            self.binding, confirm_sha256=confirm_sha256, human_confirmation=human_confirmation,
            current_source=self.source, current_authority=self.authority)
        intent = self.journal.begin(self.binding, confirmation,
                                    current_source=self.source, current_authority=self.authority)
        self.intent_sha256 = intent
        self.confirmation = confirmation
        self.starting = True
        try:
            result = manager.start(lease_id, generation, 'browser_remote_form', False, False)
            self.job_id = result['job_id']
            return result | {'intent_sha256': intent, 'binding_sha256': digest(
                self.binding.model_dump(mode='json')), 'independently_verified': False}
        finally:
            self.starting = False

    def preview(self, *, lease_id, generation):
        if (self.manager.reserved or self.intent_sha256 is not None
                or lease_id != self.binding.authority.lease_id
                or type(generation) is not int or generation != self.binding.authority.generation):
            raise ValueError('parameter_web_goal_preview_unavailable')
        self._configuration_current()
        binding = self.binding.model_dump(mode='json')
        return {'schema_version': '1.0', 'binding': binding,
                'binding_canonical': canonical(binding), 'binding_sha256': digest(binding),
                'execution_authorized': False, 'training_ready': False,
                'gpu_release_verified': False}

    def operation(self, operator, job_id):
        from .web_goal_execution_runner import WebGoalExecutionRunner

        if (self.runner is not None or self.intent_sha256 is None
                or self.confirmation is None or job_id != self.job_id):
            raise ValueError('parameter_web_goal_dispatch_not_admitted')
        self._configuration_current()

        def bind_run(intent, identity, **callbacks):
            if (callbacks.get('current_source') is not self.source
                    or callbacks.get('current_authority') != self.authority
                    or callbacks.get('expected_execution_runtime_id') != operator.runtime.runtime_id
                    or set(callbacks) != {'current_source', 'current_authority', 'expected_execution_runtime_id'}):
                raise ValueError('parameter_web_goal_run_callbacks_changed')
            self.journal.bind_run(intent, identity,
                                  expected_execution_runtime_id=operator.runtime.runtime_id,
                                  current_source=self.source,
                                  current_authority=self.authority)
            self.run_identity = dict(identity)

        self.runner = WebGoalExecutionRunner(
            operator, self.binding, self.confirmation, intent_sha256=self.intent_sha256,
            current_source=self.source, current_authority=self.authority, bind_run=bind_run)
        return self.runner.form

    def finish(self, job_id):
        manager = self.manager
        row = manager.store.connection.execute(
            'SELECT run_id,status FROM desktop_tasks WHERE job_id=? AND session_id=?',
            (job_id, manager.controller.session_id)).fetchone()
        if (job_id != self.job_id or self.run_identity is None or row is None
                or row['status'] != 'succeeded' or row['run_id'] != self.run_identity['run_id']):
            raise ValueError('parameter_web_goal_terminal_binding_changed')
        self._configuration_current()
        audit = _typed(SiteSkillFormRecipeAuditReport, self.source_auditor(row['run_id']))
        if (audit.run_ref != digest({'run_id': row['run_id']})
                or audit.invocation_sha256 != self.run_identity['skill_invocation_sha256']
                or any(getattr(audit, field) != getattr(self.binding.invocation, field)
                       for field in ('profile_sha256', 'task_sha256', 'form_plan_sha256',
                                     'state_plan_sha256', 'skill_sha256', 'recipe_sha256',
                                     'skill_plan_sha256', 'case_inputs_sha256', 'case_key',
                                     'field_binding_sha256'))):
            raise ValueError('parameter_web_goal_independent_audit_changed')
        readback = read_web_goal_whole_record(
            self.binding, self.confirmation, current_source=self.source,
            current_authority=self.authority,
            host_readback=lambda request: manager.remote_form_owned_fixture.read_whole_record(
                profile_sha256=request['profile_sha256'],
                form_plan_sha256=self.binding.invocation.form_plan_sha256,
                state_plan_sha256=self.binding.invocation.state_plan_sha256))
        self.receipt = self.journal.finish(
            self.intent_sha256, run_identity=self.run_identity, terminal_status='succeeded',
            recipe_audit=audit.model_dump(mode='json'), readback=readback.model_dump(mode='json'),
            current_source=self.source, current_authority=self.authority)
        return self.receipt

    def status(self):
        if self.intent_sha256 is not None:
            return self.journal.inspect(self.intent_sha256)
        return {'available': True, 'status': 'review_required',
                'binding_sha256': digest(self.binding.model_dump(mode='json')),
                'confirm_sha256': self.binding.confirm_sha256,
                'field_count': len(self.binding.parameters),
                'unresolved_intent': self.journal.reserved,
                'independently_verified': False, 'training_ready': False,
                'gpu_release_verified': False}

    def close(self):
        if self.workspace_fd is not None:
            os.close(self.workspace_fd)
            self.workspace_fd = None
