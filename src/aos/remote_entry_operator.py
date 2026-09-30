import asyncio
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import (AOSFault, Action, ErrorCode, Failure, Option, Phase,
                        REMOTE_ENTRY_SCOPE, State, canonical, identifier, now)
from .registries import DeploymentRegistry
from .web_application_binding import WebTaskAdmissionDraft
from .web_https_relay import ExactEntryRelay


class RemoteEntryOperator(BrowserOperator):
    def __init__(self, settings, store, runtime, engine, profiles, draft: WebTaskAdmissionDraft):
        super().__init__(settings, store, runtime, engine)
        self.profiles = profiles
        self.draft = draft

    def entry_action(self, state, tool, arguments):
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect='Only the confirmed HTTPS entry GET',
                      verification='independent_remote_entry_readback', deadline=time.time() + 90,
                      idempotency_key=identifier('intent'), selected_option='open_entry')

    def record_observation(self, state, payload, action_id=None):
        observation_id = identifier('observation')
        with self.store.connection:
            self.store.insert('observations', observation_id=observation_id, run_id=state.run_id,
                              step_id=state.step_id, action_id=action_id, kind='browser.remote_entry',
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    async def entry(self, *, owner_lease_id: str | None = None,
                    on_created: Callable[[State], None] | None = None,
                    execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                    resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate) or resume_state is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Remote entry requires a fresh lease and exact approval')
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id, task_kind='browser_remote_entry',
                      authorized_path=REMOTE_ENTRY_SCOPE, authorized_content=self.draft.binding_sha256,
                      original_goal='Kayıtlı HTTPS girişini yalnız ayrı eylem onayından sonra bir kez aç ve parmak izini doğrula.',
                      normalized_goal='Request separate human approval for one exact registered HTTPS entry GET; no other action or network request is allowed.',
                      success_criteria=('Exact entry URL and stable bounded browser fingerprint after one verified host GET',),
                      max_attempts=1)
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), None)
        try:
            if on_created:
                on_created(state)
            state = self.advance(state, Phase.OBSERVE)
            self.record_observation(state, {'profile_sha256': self.draft.profile_sha256,
                                            'binding_sha256': self.draft.binding_sha256,
                                            'entry_loaded': False})
            options = [Option(id='open_entry', label='Request approval to open the exact registered entry once'),
                       Option(id='ask_human', label='Ask the user for help')]
            state = self.advance(state, Phase.DECIDE, observation=(
                'The registered profile, exact task binding, and live MCP runtime are confirmed. '
                'The page is not loaded. Opening requires a separate human approval before any network access.'))
            state, decision_id, prediction, allowed = await self.choose(state, options)
            if not allowed:
                state = self.advance(state, Phase.WAITING_HUMAN)
                self.store.finish(state, 'waiting_human', 'unknown')
                return self.result(state, 'waiting_human')
            state = self.advance(state, Phase.EXECUTE)
            arguments = {'profile_sha256': self.draft.profile_sha256,
                         'binding_sha256': self.draft.binding_sha256,
                         'entry_url': self.draft.task.entry_url}
            action = self.entry_action(state, 'browser.remote.open', arguments)
            await execution_gate(action)
            relay = ExactEntryRelay(self.profiles, self.draft.profile_sha256,
                                    action.arguments['profile_sha256'], self.draft,
                                    action.arguments['binding_sha256'])
            self.runtime.attach_relay(relay)
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
            read_action = self.entry_action(state, 'browser.remote.observe', {})
            readback = self.gateway.execute(read_action, decision_id)
            state = self.advance(state, Phase.VERIFY)
            evidence_id = self.record_observation(state, readback, read_action.action_id)
            report = self.runtime.relay_report
            expected = {'url': self.draft.task.entry_url,
                        'title_sha256': opened['title_sha256'],
                        'heading_sha256': opened['heading_sha256'],
                        'response_sha256': report.response_sha256 if report else None}
            actual = {'url': readback['url'], 'title_sha256': readback['title_sha256'],
                      'heading_sha256': readback['heading_sha256'],
                      'response_sha256': report.response_sha256 if report else None}
            passed = (report is not None and report.profile_sha256 == self.draft.profile_sha256
                      and report.binding_sha256 == self.draft.binding_sha256
                      and report.request_attempts == 1 and opened['url'] == expected['url']
                      and actual == expected)
            with self.store.connection:
                self.store.insert('verifications', verification_id=identifier('verification'),
                                  run_id=state.run_id, step_id=state.step_id, action_id=action.action_id,
                                  criterion=state.success_criteria[0], method='independent_remote_entry_readback',
                                  result='passed' if passed else 'failed', expected_json=canonical(expected),
                                  actual_json=canonical(actual), evidence_refs_json=canonical([evidence_id]),
                                  verifier='aos-remote-entry-v1', created_at=now())
            if not passed:
                raise AOSFault(ErrorCode.TOOL_FAILURE, 'Remote entry readback differs from the confirmed one-shot fetch')
            state = self.advance(state, Phase.SUCCEEDED)
            self.store.finish(state, 'succeeded', 'passed')
            return {**self.result(state, 'succeeded'), 'verified': True, 'vision': False}
        except asyncio.CancelledError:
            self.interrupt(state)
            raise
        except (AOSFault, OSError, ValueError, TypeError, KeyError, IndexError, TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT,
                'Bounded remote entry failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
