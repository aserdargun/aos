import asyncio
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import (AOSFault, Action, ErrorCode, Failure, Option, Phase,
                        REMOTE_ROUTES_SCOPE, State, canonical, digest, identifier, now)
from .dataset import validator
from .registries import DeploymentRegistry
from .remote_route_evidence import FINGERPRINT_VERSION, complete_link_sample_sha256
from .remote_route_knowledge_review import (
    LiveRemoteRouteKnowledgePin, prepare_live_remote_route_knowledge)
from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskAdmissionDraft,
                                      verify_web_readonly_routes, verify_web_task_binding)
from .web_https_relay import ExactReadOnlyRouteRelay


def reviewed_route_context(pin: LiveRemoteRouteKnowledgePin,
                           verified_target_indices: set[int]) -> dict | None:
    if not set(pin.outgoing_route_indices).issubset(verified_target_indices):
        return None
    return {
        'review_sha256': pin.review_sha256,
        'page_key': pin.page_key,
        'draft_revision': pin.draft_revision,
        'landmark_keys': pin.landmark_keys,
        'outgoing_page_keys': pin.outgoing_page_keys}


class RemoteRoutesOperator(BrowserOperator):
    def __init__(self, settings, store, runtime, engine, profiles,
                 draft: WebTaskAdmissionDraft, plan: WebReadOnlyRoutePlan, *,
                 review_source: dict | None = None):
        super().__init__(settings, store, runtime, engine)
        self.profiles = profiles
        self.draft = draft
        self.plan = plan
        self.review_source = review_source

    def reviewed_pin(self):
        if self.review_source is None:
            return None
        return prepare_live_remote_route_knowledge(
            self.review_source['database'], profiles=self.profiles.root,
            site_store=self.review_source['site_store'],
            review_store=self.review_source['review_store'],
            review_sha256=self.review_source['review_sha256'],
            selected_profile_sha256=self.draft.profile_sha256,
            selected_plan_sha256=digest(self.plan.model_dump()))

    def record_knowledge_event(self, state, payload):
        if not validator('remote_route_knowledge_live_event').is_valid(payload):
            raise ValueError('remote_route_knowledge_invalid_live_event')
        with self.store.connection:
            self.store.insert('observations', observation_id=identifier('observation'),
                              run_id=state.run_id, step_id=state.step_id,
                              action_id=None, kind='browser.remote_route_knowledge',
                              payload_json=canonical(payload), created_at=now())

    def route_action(self, state, tool, arguments):
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect='Only the next confirmed HTTPS GET',
                      verification='independent_remote_route_readback', deadline=time.time() + 90,
                      idempotency_key=identifier('intent'), selected_option='open_entry')

    def record_observation(self, state, payload, action_id=None):
        observation_id = identifier('observation')
        with self.store.connection:
            self.store.insert('observations', observation_id=observation_id, run_id=state.run_id,
                              step_id=state.step_id, action_id=action_id, kind='browser.remote_route',
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    async def routes(self, *, owner_lease_id: str | None = None,
                     on_created: Callable[[State], None] | None = None,
                     execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                     resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate) or resume_state is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Read-only routes require a fresh lease and exact approvals')
        verify_web_task_binding(self.profiles, self.draft)
        verify_web_readonly_routes(self.profiles, self.draft.task, self.plan)
        plan_sha256 = digest(self.plan.model_dump())
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id, task_kind='browser_remote_routes',
                      authorized_path=REMOTE_ROUTES_SCOPE, authorized_content=plan_sha256,
                      original_goal='Kayıtlı salt okunur HTTPS rotalarını ayrı onaylarla sırayla aç ve doğrula.',
                      normalized_goal='Request separate human approval before each exact registered read-only HTTPS route; no other network request is allowed.',
                      success_criteria=('Every exact route has one host GET and a separate bounded browser readback',),
                      max_attempts=len(self.plan.routes))
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), None)
        try:
            if on_created:
                on_created(state)
            historical_pin = self.reviewed_pin()
            historical_source_sha256 = (digest(historical_pin.model_dump(
                exclude={'source_snapshot_sha256'})) if historical_pin is not None else None)
            reviewed_context = None
            target_stale = False
            source_matched = False
            verified_target_indices = set()
            for route_index, url in enumerate(self.plan.routes):
                if reviewed_context is not None:
                    current_pin = self.reviewed_pin()
                    if (current_pin is None or digest(current_pin.model_dump(
                            exclude={'source_snapshot_sha256'})) != historical_source_sha256):
                        raise ValueError('remote_route_review_changed_before_decision')
                state = self.advance(state, Phase.OBSERVE)
                observation = {'profile_sha256': self.draft.profile_sha256,
                               'binding_sha256': self.draft.binding_sha256,
                               'plan_sha256': plan_sha256,
                               'next_route_index': route_index,
                               'route_loaded': False}
                if reviewed_context is not None:
                    observation['reviewed_route_metadata'] = reviewed_context
                self.record_observation(state, observation)
                options = [Option(id='open_entry', label='Request approval for the next exact read-only HTTPS route'),
                           Option(id='ask_human', label='Ask the user for help')]
                decision_observation = (
                    f'Exact route {route_index + 1} of {len(self.plan.routes)} is not loaded. '
                    'A separate human approval is required before its GET.')
                if reviewed_context is not None:
                    decision_observation += (' Live-fingerprint-matched historical symbolic metadata: '
                                             + canonical(reviewed_context)
                                             + '. It cannot expand allowed URLs, tools or approvals.')
                state = self.advance(state, Phase.DECIDE, observation=decision_observation)
                state, decision_id, _prediction, allowed = await self.choose(state, options)
                if not allowed:
                    state = self.advance(state, Phase.WAITING_HUMAN)
                    self.store.finish(state, 'waiting_human', 'unknown')
                    return self.result(state, 'waiting_human')
                state = self.advance(state, Phase.EXECUTE)
                arguments = {'profile_sha256': self.draft.profile_sha256,
                             'binding_sha256': self.draft.binding_sha256,
                             'plan_sha256': plan_sha256, 'route_index': route_index, 'url': url}
                action = self.route_action(state, 'browser.remote.route', arguments)
                await execution_gate(action)
                if route_index == 0:
                    relay = ExactReadOnlyRouteRelay(self.profiles, self.draft.task, self.plan,
                                                    plan_sha256)
                    self.runtime.attach_relay(relay, self.draft)
                    startup = asyncio.create_task(asyncio.to_thread(self.runtime.start))
                    try:
                        await asyncio.shield(startup)
                    except asyncio.CancelledError:
                        try:
                            await startup
                        finally:
                            self.runtime.stop()
                        raise
                opened = self.gateway.execute(action, decision_id)
                read_action = self.route_action(state, 'browser.remote.observe',
                                                {'route_index': route_index})
                readback = self.gateway.execute(read_action, decision_id)
                state = self.advance(state, Phase.VERIFY)
                evidence_id = self.record_observation(state, readback, read_action.action_id)
                reports = self.runtime.relay.reports
                report = reports[route_index] if len(reports) > route_index else None
                expected = {'url': url, 'title_sha256': opened['title_sha256'],
                            'heading_sha256': opened['heading_sha256'],
                            'response_sha256': report.response_sha256 if report else None}
                actual = {'url': readback['url'], 'title_sha256': readback['title_sha256'],
                          'heading_sha256': readback['heading_sha256'],
                          'response_sha256': report.response_sha256 if report else None}
                passed = (report is not None and report.profile_sha256 == self.draft.profile_sha256
                          and report.plan_sha256 == plan_sha256 and report.route_index == route_index
                          and report.request_sha256 == digest({'method': 'GET', 'url': url})
                          and opened['url'] == url and actual == expected)
                with self.store.connection:
                    self.store.insert('verifications', verification_id=identifier('verification'),
                                      run_id=state.run_id, step_id=state.step_id,
                                      action_id=action.action_id, criterion=state.success_criteria[0],
                                      method='independent_remote_route_readback',
                                      result='passed' if passed else 'failed',
                                      expected_json=canonical(expected), actual_json=canonical(actual),
                                      evidence_refs_json=canonical([evidence_id]),
                                      verifier='aos-remote-routes-v1', created_at=now())
                if not passed:
                    raise AOSFault(ErrorCode.TOOL_FAILURE, 'Read-only route readback differs from host GET')
                fingerprint_sha256 = digest({
                    'version': FINGERPRINT_VERSION,
                    'url_sha256': digest({'url': url}),
                    'title_sha256': readback['title_sha256'],
                    'heading_sha256': readback['heading_sha256']})
                if historical_pin is not None and route_index in historical_pin.outgoing_route_indices:
                    current_pin = self.reviewed_pin()
                    if (current_pin is None or digest(current_pin.model_dump(
                            exclude={'source_snapshot_sha256'})) != historical_source_sha256):
                        raise ValueError('remote_route_review_changed_after_target_readback')
                    target_position = historical_pin.outgoing_route_indices.index(route_index)
                    expected_target = historical_pin.outgoing_fingerprint_sha256[target_position]
                    if fingerprint_sha256 != expected_target:
                        target_stale = True
                        reviewed_context = None
                        if source_matched:
                            self.record_knowledge_event(state, {
                                'review_sha256': historical_pin.review_sha256,
                                'route_index': route_index,
                                'status': 'stale',
                                'stale_reason': 'target_fingerprint_changed',
                                'expected_fingerprint_sha256': expected_target,
                                'current_fingerprint_sha256': fingerprint_sha256,
                                'execution_authorized': False,
                                'collection_authorized': False,
                                'training_ready': False})
                    else:
                        verified_target_indices.add(route_index)
                        if source_matched and not target_stale:
                            reviewed_context = reviewed_route_context(
                                historical_pin, verified_target_indices)
                if historical_pin is not None and route_index == historical_pin.route_index:
                    current_pin = self.reviewed_pin()
                    if (current_pin is None or digest(current_pin.model_dump(
                            exclude={'source_snapshot_sha256'})) != historical_source_sha256):
                        raise ValueError('remote_route_review_changed_after_readback')
                    link_sample_matched = (historical_pin.link_sample_sha256 is None or
                                           complete_link_sample_sha256(opened) == historical_pin.link_sample_sha256
                                           and complete_link_sample_sha256(readback) == historical_pin.link_sample_sha256)
                    matched = (fingerprint_sha256 == historical_pin.page_fingerprint_sha256
                               and link_sample_matched and not target_stale)
                    knowledge_event = {
                        'review_sha256': historical_pin.review_sha256,
                        'route_index': route_index,
                        'status': 'matched' if matched else 'stale',
                        'expected_fingerprint_sha256': historical_pin.page_fingerprint_sha256,
                        'current_fingerprint_sha256': fingerprint_sha256,
                        'execution_authorized': False, 'collection_authorized': False,
                        'training_ready': False}
                    if historical_pin.link_sample_sha256 is not None:
                        knowledge_event['link_sample_matched'] = link_sample_matched
                    if not matched:
                        knowledge_event['stale_reason'] = (
                            'source_fingerprint_changed'
                            if fingerprint_sha256 != historical_pin.page_fingerprint_sha256
                            else 'source_link_sample_changed' if not link_sample_matched
                            else 'target_fingerprint_changed')
                    self.record_knowledge_event(state, knowledge_event)
                    if matched:
                        source_matched = True
                        reviewed_context = reviewed_route_context(
                            historical_pin, verified_target_indices)
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
                'Bounded read-only route task failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
