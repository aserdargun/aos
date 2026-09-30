import asyncio
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import (AOSFault, Action, ErrorCode, Failure, Option, Phase,
                        REMOTE_STATIC_ASSETS_SCOPE, State, canonical, digest, identifier, now)
from .dataset import validator
from .registries import DeploymentRegistry
from .remote_readonly_data_change import FINGERPRINT_VERSION
from .remote_readonly_data_knowledge_review import (
    LiveRemoteReadonlyDataKnowledgeEvent,
    prepare_live_remote_readonly_data_knowledge)
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_static_asset_relay import ExactStaticBundleRelay
from .web_static_assets import WebStaticAssetPlan
from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_bundle_plan


class RemoteStaticAssetsOperator(BrowserOperator):
    def __init__(self, settings, store, runtime, engine, profiles,
                 draft: WebTaskAdmissionDraft,
                 plan: WebStaticAssetPlan | WebReadOnlyDataBundlePlan, *, tls_context=None,
                 review_source: dict | None = None):
        super().__init__(settings, store, runtime, engine)
        self.profiles = profiles
        self.draft = draft
        self.plan = plan
        self.is_data_bundle = isinstance(plan, WebReadOnlyDataBundlePlan)
        self.tls_context = tls_context
        if review_source is not None and not self.is_data_bundle:
            raise ValueError('remote_json_review_requires_v2_plan')
        self.review_source = review_source

    def reviewed_pin(self):
        if self.review_source is None:
            return None
        return prepare_live_remote_readonly_data_knowledge(
            self.review_source['database'], profiles=self.profiles.root,
            site_store=self.review_source['site_store'],
            review_store=self.review_source['review_store'],
            review_sha256=self.review_source['review_sha256'],
            selected_profile_sha256=self.draft.profile_sha256,
            selected_plan_sha256=digest(self.plan.model_dump()))

    def record_knowledge_event(self, state, payload):
        event = LiveRemoteReadonlyDataKnowledgeEvent.model_validate(payload)
        report = event.model_dump(exclude_none=True)
        validator('remote_readonly_data_knowledge_live_event').validate(report)
        with self.store.connection:
            self.store.insert('observations', observation_id=identifier('observation'),
                              run_id=state.run_id, step_id=state.step_id,
                              action_id=None, kind='browser.remote_json_knowledge',
                              payload_json=canonical(report), created_at=now())

    def static_action(self, state, tool, arguments):
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments,
                      expected_effect=('Only the confirmed HTTPS entry, static assets and exact JSON GETs'
                                       if self.is_data_bundle else
                                       'Only the confirmed HTTPS entry and static assets'),
                      verification=('independent_readonly_data_bundle_readback'
                                    if self.is_data_bundle else
                                    'independent_static_bundle_readback'),
                      deadline=time.time() + 90, idempotency_key=identifier('intent'),
                      selected_option='open_entry')

    def record_observation(self, state, payload, action_id=None):
        observation_id = identifier('observation')
        with self.store.connection:
            self.store.insert('observations', observation_id=observation_id,
                              run_id=state.run_id, step_id=state.step_id,
                              action_id=action_id,
                              kind=('browser.remote_readonly_data' if self.is_data_bundle
                                    else 'browser.remote_static_assets'),
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    async def assets(self, *, owner_lease_id: str | None = None,
                     on_created: Callable[[State], None] | None = None,
                     execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                     resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate) or resume_state is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION,
                           'Static bundle requires a fresh lease and exact approval')
        verify_web_task_binding(self.profiles, self.draft)
        verify_web_bundle_plan(self.profiles, self.draft.task, self.plan)
        plan_sha256 = digest(self.plan.model_dump())
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'),
                      step_id=identifier('step'), runtime_id=self.runtime.runtime_id,
                      deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id,
                      task_kind='browser_remote_static_assets',
                      authorized_path=REMOTE_STATIC_ASSETS_SCOPE,
                      authorized_content=plan_sha256,
                      original_goal=('Kayıtlı HTTPS girişini, exact JS/CSS ve JSON GET paketini yalnız açık onaydan sonra aç ve doğrula.'
                                     if self.is_data_bundle else
                                     'Kayıtlı HTTPS girişini ve exact JS/CSS paketini yalnız açık onaydan sonra aç ve doğrula.'),
                      normalized_goal=('Request human approval for one exact registered HTTPS entry, listed static assets and JSON GETs; deny all other requests.'
                                       if self.is_data_bundle else
                                       'Request human approval for one exact registered HTTPS entry and its listed static assets; deny all other requests.'),
                      success_criteria=(('Exact entry, every static asset and JSON resource have one host GET and stable bounded browser readback'
                                         if self.is_data_bundle else
                                         'Exact entry and every static asset has one host GET and stable bounded browser readback'),),
                      max_attempts=1)
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), None)
        try:
            if on_created:
                on_created(state)
            historical_pin = self.reviewed_pin()
            historical_source_sha256 = (digest(historical_pin.model_dump(
                exclude={'source_snapshot_sha256'})) if historical_pin is not None else None)
            state = self.advance(state, Phase.OBSERVE)
            self.record_observation(state, {
                'profile_sha256': self.draft.profile_sha256,
                'binding_sha256': self.draft.binding_sha256,
                'plan_sha256': plan_sha256,
                'asset_count': len(self.plan.assets),
                'data_count': len(self.plan.data_resources) if self.is_data_bundle else 0,
                'entry_loaded': False})
            options = [Option(id='open_entry', label='Request approval for the exact HTTPS static bundle'),
                       Option(id='ask_human', label='Ask the user for help')]
            state = self.advance(state, Phase.DECIDE, observation=(
                'The exact task, static asset plan and live MCP runtime are pinned. '
                'No page or asset has been fetched. One human approval is required for the full bundle.'))
            state, decision_id, _prediction, allowed = await self.choose(state, options)
            if not allowed:
                state = self.advance(state, Phase.WAITING_HUMAN)
                self.store.finish(state, 'waiting_human', 'unknown')
                return self.result(state, 'waiting_human')
            state = self.advance(state, Phase.EXECUTE)
            action = self.static_action(state, 'browser.static.open', {
                'profile_sha256': self.draft.profile_sha256,
                'binding_sha256': self.draft.binding_sha256,
                'plan_sha256': plan_sha256,
                'entry_url': self.draft.task.entry_url})
            await execution_gate(action)
            relay = ExactStaticBundleRelay(self.profiles, self.draft.task, self.plan,
                                           plan_sha256, tls_context=self.tls_context)
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
            read_action = self.static_action(state, 'browser.static.observe', {})
            readback = self.gateway.execute(read_action, decision_id)
            state = self.advance(state, Phase.VERIFY)
            evidence_id = self.record_observation(state, readback, read_action.action_id)
            report = self.runtime.relay_report
            response = (None if report is None else {
                'entry_response_sha256': report.entry_response_sha256,
                'asset_response_sha256': report.asset_response_sha256,
                **({'data_response_sha256': report.data_response_sha256}
                   if self.is_data_bundle else {})})
            expected = {'url': self.draft.task.entry_url,
                        'title_sha256': opened['title_sha256'],
                        'heading_sha256': opened['heading_sha256'],
                        'responses': response}
            actual = {'url': readback['url'],
                      'title_sha256': readback['title_sha256'],
                      'heading_sha256': readback['heading_sha256'],
                      'responses': response}
            passed = (report is not None
                      and report.profile_sha256 == self.draft.profile_sha256
                      and report.task_sha256 == self.plan.task_sha256
                      and report.plan_sha256 == plan_sha256
                      and len(report.asset_response_sha256) == len(self.plan.assets)
                      and (not self.is_data_bundle
                           or len(report.data_response_sha256) == len(self.plan.data_resources))
                      and report.request_attempts == (len(self.plan.assets) + 1
                                                     + (len(self.plan.data_resources)
                                                        if self.is_data_bundle else 0))
                      and opened['url'] == self.draft.task.entry_url
                      and actual == expected)
            with self.store.connection:
                self.store.insert('verifications',
                                  verification_id=identifier('verification'),
                                  run_id=state.run_id, step_id=state.step_id,
                                  action_id=action.action_id,
                                  criterion=state.success_criteria[0],
                                  method=('independent_readonly_data_bundle_readback'
                                          if self.is_data_bundle else
                                          'independent_static_bundle_readback'),
                                  result='passed' if passed else 'failed',
                                  expected_json=canonical(expected),
                                  actual_json=canonical(actual),
                                  evidence_refs_json=canonical([evidence_id]),
                                  verifier=('aos-remote-readonly-data-v1' if self.is_data_bundle
                                            else 'aos-remote-static-assets-v1'), created_at=now())
            if not passed:
                raise AOSFault(ErrorCode.TOOL_FAILURE,
                               'Static bundle readback differs from the confirmed host fetches')
            if historical_pin is not None:
                current_pin = self.reviewed_pin()
                if (current_pin is None or digest(current_pin.model_dump(
                        exclude={'source_snapshot_sha256'})) != historical_source_sha256):
                    raise ValueError('remote_json_review_changed_after_readback')
                current_fingerprint = digest({
                    'version': FINGERPRINT_VERSION,
                    'title_sha256': readback['title_sha256'],
                    'heading_sha256': readback['heading_sha256'],
                    'responses': response})
                matched = current_fingerprint == historical_pin.page_fingerprint_sha256
                self.record_knowledge_event(state, {
                    'review_sha256': historical_pin.review_sha256,
                    'status': 'matched' if matched else 'stale',
                    'expected_fingerprint_sha256': historical_pin.page_fingerprint_sha256,
                    'current_fingerprint_sha256': current_fingerprint,
                    **({'page_key': historical_pin.page_key,
                        'draft_revision': historical_pin.draft_revision,
                        'landmark_keys': historical_pin.landmark_keys} if matched else {})})
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
                'Bounded remote static bundle failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED,
                                 last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
