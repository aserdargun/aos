import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import (AOSFault, Action, ErrorCode, Failure, LOCAL_NAVIGATION_GOAL,
                        LOCAL_NAVIGATION_SCOPE, Option, Phase, State, canonical, identifier, now)
from .registries import DeploymentRegistry


def page_outcome(observed: dict) -> dict:
    return {'page': observed['page'], 'heading': observed['heading'],
            'links': [{'role': element['role'], 'label': element['label']}
                      for element in observed['elements']]}


class LocalNavigationOperator(BrowserOperator):
    def observation(self, state: State, payload: dict, action_id=None) -> str:
        observation_id = identifier('observation')
        with self.store.connection:
            self.store.insert('observations', observation_id=observation_id, run_id=state.run_id,
                              step_id=state.step_id, action_id=action_id, kind='browser.local_navigation',
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    def navigation_action(self, state: State, tool: str, option: str, arguments: dict) -> Action:
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect='Only synthetic local Start to Details navigation',
                      verification='independent_local_page_equals', deadline=time.time() + 70,
                      idempotency_key=identifier('intent'), selected_option=option)

    async def navigate(self, *, owner_lease_id: str | None = None,
                       on_created: Callable[[State], None] | None = None,
                       execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                       resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Local navigation requires a live lease and per-action approval gate')
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id, task_kind='browser_local_navigation',
                      authorized_path=LOCAL_NAVIGATION_SCOPE, authorized_content='DETAILS',
                      original_goal='Yerel sentetik Start sayfasını aç, Details bağlantısını izle ve sonucu doğrula.',
                      normalized_goal=LOCAL_NAVIGATION_GOAL,
                      success_criteria=('Only the exact synthetic Details page after one observed link click',),
                      max_attempts=2)
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), resume_state)
        try:
            if on_created:
                on_created(state)
            for attempt in range(2):
                state = self.advance(state, Phase.OBSERVE)
                observed = json.loads(self.gateway.observe(state))
                self.observation(state, observed)
                completed_opens = self.store.connection.execute(
                    "SELECT count(*) FROM actions JOIN verifications USING(action_id) WHERE actions.run_id=? "
                    "AND tool='browser.fixture.open' AND status='ok' AND result='passed'",
                    (state.run_id,)).fetchone()[0]
                if observed['page'] == 'entry':
                    if completed_opens != 0 or (observed['value'], observed['receipt'], observed['submissions']) != ('', '', 0):
                        raise AOSFault(ErrorCode.UI_CHANGED, 'Local navigation entry differs')
                    ordinal, tool, option, arguments = 0, 'browser.fixture.open', 'open_start', {}
                    label = 'Open the only authorized Start page now, then verify its heading and Details link'
                    summary = 'The unchanged synthetic entry form is visible. No local page was opened.'
                elif observed['page'] == 'start':
                    if completed_opens != 1 or page_outcome(observed) != {
                            'page': 'start', 'heading': 'Synthetic start',
                            'links': [{'role': 'link', 'label': 'Details'}]}:
                        raise AOSFault(ErrorCode.UI_CHANGED, 'Local Start page lacks a verified opening or link')
                    target = observed['elements'][0]
                    ordinal, tool, option = 1, 'browser.fixture.follow', 'follow_details'
                    arguments = {'snapshot_id': observed['snapshot_id'], 'element_id': target['element_id']}
                    label = 'Follow only the fresh Details link on the synthetic Start page'
                    summary = 'The verified synthetic Start page has one observed Details link.'
                else:
                    raise AOSFault(ErrorCode.UI_CHANGED, 'Unexpected local navigation page')
                options = [Option(id=option, label=label), Option(id='ask_human', label='Ask the user for help')]
                state = self.advance(state, Phase.DECIDE, observation=summary)
                state, decision_id, prediction, allowed = await self.choose(state, options)
                if not allowed:
                    state = self.advance(state, Phase.WAITING_HUMAN)
                    self.store.finish(state, 'waiting_human', 'unknown')
                    return self.result(state, 'waiting_human')
                state = self.advance(state, Phase.EXECUTE)
                action = self.navigation_action(state, tool, prediction.selected_option, arguments)
                await execution_gate(action)
                self.gateway.execute(action, decision_id)
                verify_action = self.navigation_action(state, 'browser.fixture.snapshot', prediction.selected_option, {})
                actual = self.gateway.execute(verify_action, decision_id)
                state = self.advance(state, Phase.VERIFY)
                evidence_id = self.observation(state, actual, verify_action.action_id)
                expected = ({'page': 'start', 'heading': 'Synthetic start',
                             'links': [{'role': 'link', 'label': 'Details'}]} if ordinal == 0 else
                            {'page': 'details', 'heading': 'Synthetic details', 'links': []})
                measured = page_outcome(actual)
                passed = canonical(measured) == canonical(expected)
                with self.store.connection:
                    self.store.insert('verifications', verification_id=identifier('verification'), run_id=state.run_id,
                                      step_id=state.step_id, action_id=action.action_id,
                                      criterion='Exact synthetic Start page' if ordinal == 0 else state.success_criteria[0],
                                      method='independent_local_page_equals', result='passed' if passed else 'failed',
                                      expected_json=canonical(expected), actual_json=canonical(measured),
                                      evidence_refs_json=canonical([evidence_id]),
                                      verifier='aos-local-navigation-v1', created_at=now())
                if not passed:
                    raise AOSFault(ErrorCode.TOOL_FAILURE, 'Independent local page verification failed')
                if ordinal == 1:
                    break
            state = self.advance(state, Phase.SUCCEEDED)
            self.store.finish(state, 'succeeded', 'passed')
            return {**self.result(state, 'succeeded'), 'verified': True, 'vision': False}
        except asyncio.CancelledError:
            self.interrupt(state)
            raise
        except (AOSFault, OSError, ValueError, TypeError, KeyError, IndexError, TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT,
                'Bounded local navigation failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
