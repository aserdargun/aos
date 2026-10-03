"""Fenced host composition around the existing finite HTTPS form operator."""

import asyncio
import inspect
import math
import time

from .contracts import Action, AOSFault, ErrorCode, State, canonical, digest
from .remote_form_operator import RemoteFormOperator
from .site_skill_form_recipe import recipe_operator_steps
from .web_goal_execution_binding import (WebGoalExecutionBinding, WebGoalExecutionConfirmation,
                                         _current, _typed, fields_for_parameters)
from .web_https_form_state_probe import form_state_action_arguments
from .web_https_form_transport import form_stage_arguments


def _require(condition):
    if not condition:
        raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reviewed parameter task source or control changed')


class _FencedExecutionGateway:
    def __init__(self, runner, gateway):
        self.runner = runner
        self.gateway = gateway

    def execute(self, action, decision_id):
        self.runner._assert_dispatch(action)
        result = self.gateway.execute(action, decision_id)
        self.runner._assert_current()
        return result


class WebGoalExecutionRunner:
    def __init__(self, operator, binding, confirmation, *, intent_sha256,
                 current_source, current_authority, bind_run):
        _require(type(operator) is RemoteFormOperator
                 and type(intent_sha256) is str and len(intent_sha256) == 64
                 and all(character in '0123456789abcdef' for character in intent_sha256)
                 and callable(current_source) and callable(current_authority) and callable(bind_run))
        self.operator = operator
        self.binding = _typed(WebGoalExecutionBinding, binding)
        self.confirmation = _typed(WebGoalExecutionConfirmation, confirmation)
        self.intent_sha256 = intent_sha256
        self.current_source = current_source
        self.current_authority = current_authority
        self.bind_run = bind_run
        self.runtime_id = operator.runtime.runtime_id
        self.engine_identity = canonical(operator.engine.identity)
        self.run_identity = None
        self.status = 'ready'
        self.action_ids = set()
        self.approved_actions = {}
        self.readback_dispatched = False
        self.idempotency_keys = set()
        self.approved_stages = 0
        self.started = False
        self.task = None
        self._assert_current()

    @property
    def reserved(self):
        return self.started

    def _assert_current(self):
        binding = self.binding
        confirmation = self.confirmation
        operator = self.operator
        fields = fields_for_parameters(binding.parameters, binding.field_bindings)
        _require(confirmation.binding_sha256 == digest(binding.model_dump(mode='json'))
                 and confirmation.source_sha256 == digest(binding.source.model_dump(mode='json'))
                 and confirmation.authority == binding.authority)
        _current(binding, self.current_source, self.current_authority)
        _require(operator.runtime.runtime_id == self.runtime_id
                 and operator.draft.runtime.runtime_id == self.runtime_id
                 and canonical(operator.engine.identity) == self.engine_identity
                 and operator.draft.profile_sha256 == binding.source.profile_sha256
                 and operator.draft.task.profile_sha256 == binding.source.profile_sha256
                 and digest(operator.draft.task.model_dump(mode='json')) == binding.source.task_sha256
                 and digest(operator.plan.model_dump(mode='json')) == binding.invocation.form_plan_sha256
                 and operator.state_plan is not None
                 and digest(operator.state_plan.model_dump(mode='json')) == binding.invocation.state_plan_sha256
                 and operator.state_plan.state_url == binding.oracle.readback_url
                 and operator.form_fields == fields
                 and operator.field_selector == (fields[0][0] if len(fields) == 1 else tuple(name for name, _value in fields))
                 and operator.field_name == (fields[0][0] if len(fields) == 1 else None)
                 and operator.value == (fields[0][1] if len(fields) == 1 else None)
                 and canonical(operator.skill_invocation) == canonical(binding.invocation.model_dump(mode='json'))
                 and operator.skill_invocation_sha256 == digest(binding.invocation.model_dump(mode='json')))
        profile = operator.profiles.get(binding.source.profile_sha256)
        _require((profile.application_key, profile.tenant_key, profile.account_role)
                 == (binding.oracle.scope.application_id, binding.oracle.scope.tenant_id,
                     binding.oracle.scope.account_role))

    def _created(self, state, callback):
        self._assert_current()
        _require(isinstance(state, State) and self.run_identity is None
                 and state.runtime_id == self.runtime_id
                 and state.owner_lease_id == self.binding.authority.lease_id
                 and state.owner == 'AGENT'
                 and state.deployment_id == self.operator.engine.identity['deployment_id']
                 and state.skill_invocation_sha256 == self.operator.skill_invocation_sha256
                 and state.task_kind == 'browser_remote_form')
        identity = {field: getattr(state, field) for field in (
            'task_id', 'run_id', 'runtime_id', 'deployment_id', 'owner_lease_id', 'skill_invocation_sha256')}
        self.run_identity = identity
        self.status = 'binding_run'
        if callback is not None:
            result = callback(state)
            _require(not inspect.isawaitable(result))
        result = self.bind_run(self.intent_sha256, dict(identity),
                               expected_execution_runtime_id=self.runtime_id,
                               current_source=self.current_source, current_authority=self.current_authority)
        _require(not inspect.isawaitable(result))
        self._assert_current()
        self.status = 'running'

    def _assert_action(self, action):
        self._assert_current()
        _require(isinstance(action, Action) and self.run_identity is not None
                 and self.approved_stages < len(self.binding.invocation.steps))
        tool, choice, stage, _label, _step_key = recipe_operator_steps(self.binding.invocation)[self.approved_stages]
        expected = (form_state_action_arguments(self.operator.state_plan, self.operator.draft.binding_sha256,
                    'before' if choice == 'read_state_before' else 'after', self.operator.cookie_sha256)
                    if stage is None else form_stage_arguments(self.operator.plan,
                    self.operator.draft.binding_sha256, self.operator.field_selector, stage, self.operator.cookie_sha256))
        state = self.operator.store.state(action.run_id)
        _require(action.task_id == self.run_identity['task_id']
                 and action.run_id == self.run_identity['run_id']
                 and action.runtime_id == self.runtime_id
                 and action.owner_lease_id == self.binding.authority.lease_id
                 and action.tool == tool and action.selected_option == choice
                 and canonical(action.arguments) == canonical(expected)
                 and state.owner == 'AGENT' and state.owner_lease_id == action.owner_lease_id
                 and state.runtime_id == action.runtime_id and state.state_version == action.state_version
                 and state.step_id == action.step_id
                 and type(action.deadline) in (int, float) and math.isfinite(action.deadline)
                 and time.time() < action.deadline
                 and action.action_id not in self.action_ids
                 and action.idempotency_key not in self.idempotency_keys)

    async def _gate(self, action, callback):
        self._assert_action(action)
        action_hash = digest(action.model_dump(mode='json'))
        await callback(action)
        self._assert_action(action)
        _require(digest(action.model_dump(mode='json')) == action_hash)
        self.action_ids.add(action.action_id)
        self.approved_actions[action.action_id] = action_hash
        self.idempotency_keys.add(action.idempotency_key)
        self.approved_stages += 1

    def _assert_dispatch(self, action):
        self._assert_current()
        _require(isinstance(action, Action) and self.run_identity is not None)
        if action.action_id in self.approved_actions:
            _require(self.approved_actions[action.action_id] == digest(action.model_dump(mode='json')))
            return
        expected = form_stage_arguments(self.operator.plan, self.operator.draft.binding_sha256,
                                        self.operator.field_selector, 4, self.operator.cookie_sha256)
        state = self.operator.store.state(action.run_id)
        _require(not self.readback_dispatched and self.approved_stages == 5
                 and action.tool == 'browser.form.observe' and action.selected_option == 'read_receipt'
                 and canonical(action.arguments) == canonical(expected)
                 and action.task_id == self.run_identity['task_id']
                 and action.run_id == self.run_identity['run_id']
                 and action.runtime_id == self.runtime_id
                 and action.owner_lease_id == self.binding.authority.lease_id
                 and state.owner == 'AGENT' and state.owner_lease_id == action.owner_lease_id
                 and state.runtime_id == action.runtime_id and state.state_version == action.state_version
                 and state.step_id == action.step_id and math.isfinite(action.deadline)
                 and time.time() < action.deadline)
        self.readback_dispatched = True

    async def _execute(self, owner_lease_id, on_created, execution_gate):
        original_gateway = self.operator.gateway
        self.operator.gateway = _FencedExecutionGateway(self, original_gateway)
        try:
            result = await RemoteFormOperator.form(self.operator,
                owner_lease_id=owner_lease_id, on_created=lambda state: self._created(state, on_created),
                execution_gate=lambda action: self._gate(action, execution_gate), resume_state=None)
            self._assert_current()
            _require(type(result) is dict and self.run_identity is not None
                     and result.get('run_id') == self.run_identity['run_id'])
            self.status = ('awaiting_independent_verification' if result.get('status') == 'succeeded'
                           and self.approved_stages == len(self.binding.invocation.steps) else 'uncertain')
            return result
        except BaseException:
            self.status = 'uncertain'
            raise
        finally:
            self.operator.gateway = original_gateway

    async def form(self, *, owner_lease_id=None, on_created=None, execution_gate=None, resume_state=None):
        _require(not self.started and resume_state is None and callable(execution_gate)
                 and (on_created is None or callable(on_created))
                 and owner_lease_id == self.binding.authority.lease_id)
        self._assert_current()
        self.started = True
        self.status = 'dispatching'
        self.task = asyncio.create_task(self._execute(owner_lease_id, on_created, execution_gate))
        try:
            return await self.task
        except BaseException:
            self.status = 'uncertain'
            raise

    async def cancel(self, timeout_seconds=5.0):
        _require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds) and 0 < timeout_seconds <= 30)
        if self.task is not None and not self.task.done():
            self.task.cancel()
            completed, pending = await asyncio.wait({self.task}, timeout=timeout_seconds)
            if pending:
                self.status = 'cleanup_pending'
                return False
            for task in completed:
                if not task.cancelled():
                    task.exception()
        if self.started:
            self.status = 'uncertain'
        return True
