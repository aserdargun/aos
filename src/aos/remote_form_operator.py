import asyncio
import hashlib
import math
import threading
import time
from collections.abc import Awaitable, Callable
from ssl import SSLContext

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import (AOSFault, Action, ErrorCode, Failure, Option, Phase,
                        REMOTE_FORM_SCOPE, State, canonical, digest, identifier, now)
from .registries import DeploymentRegistry
from .web_application_binding import (WebTaskAdmissionDraft, verify_web_task_binding)
from .web_https_form_transport import (ExactHTTPSFormRelay, ExactHTTPSFormTransport,
                                       WebHTTPSFormPlan, exact_form_fields, form_body,
                                       form_stage_arguments,
                                       verify_web_https_form_plan,
                                       verify_web_https_form_target_grant)
from .web_https_form_state_probe import (ExactHTTPSFormStateProbe, WebHTTPSFormStatePlan,
                                         form_state_action_arguments,
                                         form_state_request_sha256,
                                         verify_web_https_form_state_plan)


class SingleUseRequestPermit:
    def __init__(self, *, wall_clock=time.time, monotonic_clock=time.monotonic):
        self.wall_clock = wall_clock
        self.monotonic_clock = monotonic_clock
        self.lock = threading.Lock()
        self.pending = None

    def arm(self, request_sha256: str, action_deadline: float) -> None:
        if (not isinstance(request_sha256, str) or len(request_sha256) != 64
                or any(character not in '0123456789abcdef' for character in request_sha256)
                or type(action_deadline) not in (int, float)
                or not math.isfinite(action_deadline)):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form request permit identity differs')
        remaining = action_deadline - self.wall_clock()
        if not 0 < remaining <= 90:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form action deadline expired')
        with self.lock:
            if self.pending is not None:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form request permit already armed')
            self.pending = (request_sha256, self.monotonic_clock() + remaining)

    def consume(self, request_sha256: str) -> bool:
        with self.lock:
            pending, self.pending = self.pending, None
            return bool(pending is not None and pending[0] == request_sha256
                        and self.monotonic_clock() < pending[1])


class RemoteFormOperator(BrowserOperator):
    tools = ('browser.form.open', 'browser.form.fill', 'browser.form.submit',
             'browser.form.receipt')
    choices = ('open_entry', 'fill_form', 'submit_form', 'read_receipt')

    def __init__(self, settings, store, runtime, engine, profiles,
                 draft: WebTaskAdmissionDraft, plan: WebHTTPSFormPlan,
                 field_name: str, value: str, tls_context: SSLContext,
                 public_plan_sha256: str | None = None,
                 state_plan: WebHTTPSFormStatePlan | None = None,
                 public_state_plan_sha256: str | None = None,
                 cookie_header: str | None = None,
                 cookie_sha256: str | None = None,
                 skill_invocation=None, skill_invocation_sha256: str | None = None,
                 fields: list[dict] | None = None, owned_form_target=None):
        super().__init__(settings, store, runtime, engine)
        self.profiles = profiles
        self.draft = draft
        self.plan = plan
        self.field_name = field_name
        self.value = value
        self.form_fields = exact_form_fields(field_name, value, fields)
        self.field_selector = (field_name if fields is None else tuple(
            name for name, _content in self.form_fields))
        self.tls_context = tls_context
        self.public_plan_sha256 = public_plan_sha256
        self.state_plan = state_plan
        self.public_state_plan_sha256 = public_state_plan_sha256
        self.cookie_header = cookie_header
        self.cookie_sha256 = cookie_sha256
        self.skill_invocation = (None if skill_invocation is None else
                                 dict(skill_invocation))
        self.skill_invocation_sha256 = skill_invocation_sha256
        self.owned_form_target = owned_form_target

    def form_action(self, state, tool, choice, stage=None):
        if tool in {'browser.form.state_before', 'browser.form.state_after'}:
            arguments = form_state_action_arguments(
                self.state_plan, self.draft.binding_sha256,
                'before' if tool.endswith('before') else 'after',
                self.cookie_sha256)
        else:
            arguments = form_stage_arguments(
                self.plan, self.draft.binding_sha256, self.field_selector,
                4 if tool == 'browser.form.observe' else stage,
                self.cookie_sha256)
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments,
                      expected_effect='Only the next exact HTTPS form stage',
                      verification='independent_https_form_transport_readback',
                      deadline=time.time() + 90, idempotency_key=identifier('intent'),
                      selected_option=choice)

    def record_observation(self, state, payload, action_id=None):
        observation_id = identifier('observation')
        with self.store.connection:
            self.store.insert('observations', observation_id=observation_id,
                              run_id=state.run_id, step_id=state.step_id,
                              action_id=action_id, kind='browser.https_form',
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    async def form(self, *, owner_lease_id: str | None = None,
                   on_created: Callable[[State], None] | None = None,
                   execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                   resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate) or resume_state is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'HTTPS form requires a fresh lease and exact approvals')
        verify_web_task_binding(self.profiles, self.draft)
        verify_web_https_form_plan(self.profiles, self.draft.task, self.plan)
        verify_web_https_form_target_grant(
            self.plan, self.public_plan_sha256, self.owned_form_target)
        if self.cookie_header is not None or self.cookie_sha256 is not None:
            from .web_https_preflight import validate_https_cookie_header

            if (self.cookie_header is None or self.cookie_sha256 is None
                    or self.public_plan_sha256 != digest(self.plan.model_dump())
                    or hashlib.sha256(validate_https_cookie_header(
                        self.cookie_header).encode('ascii')).hexdigest() != self.cookie_sha256):
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form cookie differs from exact grant')
        if self.state_plan is not None:
            verify_web_https_form_state_plan(
                self.profiles, self.draft.task, self.plan, self.state_plan,
                self.tls_context,
                confirm_public_form_plan_sha256=self.public_plan_sha256,
                confirm_public_state_plan_sha256=self.public_state_plan_sha256,
                owned_form_target=self.owned_form_target)
        elif self.public_state_plan_sha256 is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'State plan grant has no plan')
        body = form_body(self.form_fields)
        if (hashlib.sha256(body).hexdigest() != self.plan.body_sha256
                or len(body) != self.plan.body_bytes):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form input differs from exact plan')
        if self.state_plan is not None:
            from .web_https_form_state_probe import verify_submitted_field_binding

            try:
                verify_submitted_field_binding(self.state_plan, self.plan, self.form_fields)
            except ValueError as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Submitted field does not match state readback plan') from error
        skill_invocation_sha256 = None
        recipe_invocation = None
        recipe_sha256 = None
        if self.skill_invocation is not None:
            from .site_skill_form_invocation import (
                SiteSkillFormInvocation, verify_site_skill_form_invocation_input)
            from .site_skill_form_recipe import (
                SiteSkillFormRecipeInvocation, recipe_operator_steps)

            try:
                if self.state_plan is None:
                    raise ValueError('skill_form_state_plan_required')
                invocation = verify_site_skill_form_invocation_input(
                    self.skill_invocation, skill_sha256=self.skill_invocation['skill_sha256'],
                    task=self.draft.task, form_plan=self.plan, state_plan=self.state_plan)
                if self.skill_invocation_sha256 != digest(invocation.model_dump(mode='json')):
                    raise ValueError('skill_form_invocation_hash_invalid')
                if isinstance(invocation, SiteSkillFormRecipeInvocation):
                    recipe_invocation = invocation
                    recipe_sha256 = invocation.recipe_sha256
                    recipe_steps = recipe_operator_steps(invocation)
                    steps = [(tool, operation, form_stage, label, step_key)
                             for tool, operation, form_stage, label, step_key in recipe_steps]
                elif isinstance(invocation, SiteSkillFormInvocation):
                    from .site_skill_form_invocation import FORM_SKILL_STAGES

                    if invocation.stages != FORM_SKILL_STAGES:
                        raise ValueError('skill_form_invocation_stage_order_invalid')
                else:
                    raise ValueError('skill_form_invocation_version_unsupported')
                skill_invocation_sha256 = self.skill_invocation_sha256
            except (ValueError, TypeError, KeyError) as error:
                raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Typed skill invocation differs') from error
        plan_sha256 = digest(self.plan.model_dump())
        if recipe_invocation is None:
            steps = [('browser.form.open', 'open_entry', 0, 'entry GET', None)]
            if self.state_plan is not None:
                steps.append(('browser.form.state_before', 'read_state_before', None,
                              'state-before GET', None))
            steps.extend((('browser.form.fill', 'fill_form', 1, 'form fill', None),
                          ('browser.form.submit', 'submit_form', 2, 'one form POST', None),
                          ('browser.form.receipt', 'read_receipt', 3, 'receipt GET', None)))
            if self.state_plan is not None:
                steps.append(('browser.form.state_after', 'read_state_after', None,
                              'state-after GET', None))
        request_hashes = {
            'browser.form.open': digest({'method': 'GET', 'url': self.plan.entry_url}),
            'browser.form.submit': digest({'method': 'POST', 'url': self.plan.submit_url,
                                           'body_sha256': self.plan.body_sha256}),
            'browser.form.receipt': digest({'method': 'GET', 'url': self.plan.receipt_url})}
        if self.state_plan is not None:
            request_hashes.update({
                'browser.form.state_before': form_state_request_sha256(
                    self.state_plan, 'before'),
                'browser.form.state_after': form_state_request_sha256(
                    self.state_plan, 'after')})
        permits = SingleUseRequestPermit()

        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'),
                      step_id=identifier('step'), runtime_id=self.runtime.runtime_id,
                      deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id, task_kind='browser_remote_form',
                      skill_invocation_sha256=skill_invocation_sha256,
                      authorized_path=REMOTE_FORM_SCOPE, authorized_content=plan_sha256,
                      original_goal='Kayıtlı HTTPS formunu ayrı onaylarla gönder ve sonucu oku.',
                      normalized_goal='Request a separate approval for each exact form and declared state readback stage; no other request is allowed.',
                      success_criteria=('One exact host POST and a separate browser receipt readback',),
                      max_attempts=len(steps))
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), None)
        opened = None
        try:
            if on_created:
                on_created(state)
            for position, (tool, choice, form_stage, label, step_key) in enumerate(steps):
                state = self.advance(state, Phase.OBSERVE)
                observation = {
                    'profile_sha256': self.draft.profile_sha256,
                    'binding_sha256': self.draft.binding_sha256,
                    'plan_sha256': plan_sha256, 'next_stage': position}
                if recipe_invocation is not None:
                    observation['recipe_step'] = {
                        'recipe_sha256': recipe_sha256,
                        'step_key': step_key,
                        'operation': choice,
                        'ordinal': position,
                    }
                self.record_observation(state, observation)
                options = [Option(id=choice, label='Request separate approval for exact ' + label),
                    Option(id='ask_human', label='Ask the user for help')]
                state = self.advance(state, Phase.DECIDE, observation=(
                    f'Exact HTTPS form stage {position + 1} of {len(steps)} is pending. '
                    'Separate action approval is required before execution.'))
                state, decision_id, _prediction, allowed = await self.choose(state, options)
                if not allowed:
                    state = self.advance(state, Phase.WAITING_HUMAN)
                    self.store.finish(state, 'waiting_human', 'unknown')
                    return self.result(state, 'waiting_human')
                state = self.advance(state, Phase.EXECUTE)
                action = self.form_action(state, tool, choice, form_stage)
                await execution_gate(action)
                approval = self.store.connection.execute(
                    "SELECT status FROM desktop_approvals WHERE job_id=(SELECT job_id FROM desktop_tasks WHERE run_id=?) "
                    "AND action_sha256=? ORDER BY rowid DESC LIMIT 1",
                    (state.run_id, digest(action.model_dump(mode='json')))).fetchone()
                if approval is None or approval['status'] != 'consumed':
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Form action approval was not consumed')
                if tool in request_hashes:
                    permits.arm(request_hashes[tool], action.deadline)
                if tool == 'browser.form.open':
                    transport = ExactHTTPSFormTransport(
                        self.profiles, self.draft.task, self.plan, plan_sha256,
                        consume_approval=permits.consume, tls_context=self.tls_context,
                        confirm_public_plan_sha256=self.public_plan_sha256,
                        cookie_header=self.cookie_header,
                        confirm_cookie_sha256=self.cookie_sha256,
                        owned_form_target=self.owned_form_target)
                    self.runtime.attach_relay(
                        ExactHTTPSFormRelay(transport), self.draft,
                        field_name=self.field_name, value=self.value,
                        fields=([{'name': name, 'value': content}
                                 for name, content in self.form_fields]
                                if self.field_name is None else None),
                        recipe_operations=(tuple(item.operation
                                                  for item in recipe_invocation.steps)
                                           if recipe_invocation is not None else None))
                    if self.state_plan is not None:
                        probe = ExactHTTPSFormStateProbe(
                            transport, self.state_plan, digest(self.state_plan.model_dump()),
                            consume_approval=permits.consume,
                            confirm_public_state_plan_sha256=self.public_state_plan_sha256)
                        self.runtime.attach_state_probe(probe)
                    startup = asyncio.create_task(asyncio.to_thread(self.runtime.start))
                    try:
                        await asyncio.shield(startup)
                    except asyncio.CancelledError:
                        try:
                            await startup
                        finally:
                            self.runtime.stop()
                        raise
                result = self.gateway.execute(action, decision_id)
                if tool == 'browser.form.open':
                    opened = result
                elif tool == 'browser.form.receipt':
                    read_action = self.form_action(
                        state, 'browser.form.observe', choice, 4)
                    readback = self.gateway.execute(read_action, decision_id)
                    state = self.advance(state, Phase.VERIFY)
                    evidence_id = self.record_observation(state, readback, read_action.action_id)
                    report = self.runtime.relay_report
                    expected = {'url': self.plan.receipt_url,
                                'title_sha256': result['title_sha256'],
                                'heading_sha256': result['heading_sha256'],
                                'plan_sha256': plan_sha256,
                                'submit_request_sha256': request_hashes['browser.form.submit']}
                    actual = {'url': readback['url'],
                              'title_sha256': readback['title_sha256'],
                              'heading_sha256': readback['heading_sha256'],
                              'plan_sha256': report.plan_sha256 if report else None,
                              'submit_request_sha256': report.submit_request_sha256 if report else None}
                    passed = (report is not None and opened is not None
                              and opened['url'] == self.plan.entry_url
                              and report.profile_sha256 == self.draft.profile_sha256
                              and report.task_sha256 == self.plan.task_sha256
                              and report.entry_response_sha256 == self.runtime.relay.transport._entry_response_sha256
                              and report.receipt_url_sha256 == digest({'url': self.plan.receipt_url})
                              and actual == expected)
                    with self.store.connection:
                        self.store.insert('verifications',
                                          verification_id=identifier('verification'),
                                          run_id=state.run_id, step_id=state.step_id,
                                          action_id=action.action_id,
                                          criterion=state.success_criteria[0],
                                          method='independent_https_form_transport_readback',
                                          result='passed' if passed else 'failed',
                                          expected_json=canonical(expected),
                                          actual_json=canonical(actual),
                                          evidence_refs_json=canonical([evidence_id]),
                                          verifier='aos-remote-form-v1', created_at=now())
                    if not passed:
                        raise AOSFault(ErrorCode.TOOL_FAILURE, 'Form transport readback differs')
                elif tool == 'browser.form.state_after':
                    state = self.advance(state, Phase.VERIFY)
                    evidence_id = self.record_observation(state, result, action.action_id)
                    receipt_report = self.runtime.relay_report
                    expected = {
                        'state_plan_sha256': digest(self.state_plan.model_dump()),
                        'form_plan_sha256': plan_sha256,
                        'before_response_sha256': self.state_plan.expected_before_sha256,
                        'after_response_sha256': self.state_plan.expected_after_sha256,
                        'receipt_response_sha256': (
                            receipt_report.receipt_response_sha256 if receipt_report else None)}
                    actual = {key: result.get(key) for key in expected}
                    passed = (receipt_report is not None and actual == expected
                              and result.get('site_outcome_verified') is False
                              and result.get('account_verified') is False)
                    with self.store.connection:
                        self.store.insert('verifications',
                                          verification_id=identifier('verification'),
                                          run_id=state.run_id, step_id=state.step_id,
                                          action_id=action.action_id,
                                          criterion='Declared HTTPS state response transition',
                                          method='declared_https_form_state_readback',
                                          result='passed' if passed else 'failed',
                                          expected_json=canonical(expected),
                                          actual_json=canonical(actual),
                                          evidence_refs_json=canonical([evidence_id]),
                                          verifier='aos-remote-form-state-v1', created_at=now())
                    if not passed:
                        raise AOSFault(ErrorCode.TOOL_FAILURE,
                                       'Declared state response differs from exact plan')
                if tool not in {'browser.form.receipt', 'browser.form.state_after'}:
                    state = self.advance(state, Phase.VERIFY)
            state = self.advance(state, Phase.SUCCEEDED)
            self.store.finish(state, 'succeeded', 'passed')
            return {**self.result(state, 'succeeded'), 'verified': True, 'vision': False}
        except asyncio.CancelledError:
            self.interrupt(state)
            raise
        except (AOSFault, OSError, ValueError, TypeError, KeyError, IndexError,
                TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT,
                'Bounded HTTPS form task failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
