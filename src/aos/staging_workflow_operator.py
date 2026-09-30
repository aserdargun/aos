import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .contracts import (AOSFault, Action, ErrorCode, Failure, Option, Phase, State,
                        STAGING_WORKFLOW_GOAL, STAGING_WORKFLOW_MESSAGE,
                        STAGING_WORKFLOW_SCOPE, canonical, identifier, now)
from .local_navigation_operator import LocalNavigationOperator
from .registries import DeploymentRegistry


def staging_outcome(observed: dict) -> dict:
    return {'page': observed['page'], 'heading': observed['heading'],
            'value': observed['value'], 'receipt': observed['receipt'],
            'submissions': observed['submissions'],
            'controls': [{'role': element['role'], 'label': element['label']}
                         for element in observed.get('elements', [])]}


EXPECTED_OUTCOMES = (
    {'page': 'app', 'heading': 'Synthetic app', 'value': '', 'receipt': '',
     'submissions': 0, 'controls': [{'role': 'link', 'label': 'Draft'}]},
    {'page': 'draft', 'heading': 'Synthetic draft', 'value': '', 'receipt': '',
     'submissions': 0, 'controls': [{'role': 'textbox', 'label': 'Message'},
                                  {'role': 'button', 'label': 'Save draft'}]},
    {'page': 'draft', 'heading': 'Synthetic draft', 'value': STAGING_WORKFLOW_MESSAGE,
     'receipt': '', 'submissions': 0,
     'controls': [{'role': 'textbox', 'label': 'Message'},
                  {'role': 'button', 'label': 'Save draft'}]},
    {'page': 'receipt', 'heading': 'Synthetic receipt', 'value': '',
     'receipt': 'Saved locally.', 'submissions': 1, 'controls': []},
)
STAGES = (
    ('browser.staging.open', 'open_app',
     'Open the only authorized synthetic App page now, then verify its heading and Draft link',
     'The unchanged synthetic entry form is visible. No local App page was opened.'),
    ('browser.staging.follow', 'follow_draft', 'Follow only the freshly observed Draft link',
     'The verified App page has one Draft link.'),
    ('browser.staging.fill', 'fill_message', 'Fill Message with exactly Hello from the local agent.',
     'The verified Draft page has an empty Message field and has not been submitted.'),
    ('browser.staging.submit', 'submit_draft', 'Submit the exact filled Draft form once and verify its receipt',
     'The verified Draft page contains the exact message and has not been submitted.'),
)


class StagingWorkflowOperator(LocalNavigationOperator):
    def staging_action(self, state: State, tool: str, option: str, arguments: dict) -> Action:
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier('action'), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments,
                      expected_effect='Only the fixed synthetic local staging workflow',
                      verification='independent_staging_state_equals', deadline=time.time() + 70,
                      idempotency_key=identifier('intent'), selected_option=option)

    async def workflow(self, *, owner_lease_id: str | None = None,
                       on_created: Callable[[State], None] | None = None,
                       execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                       resume_state: State | None = None) -> dict:
        if owner_lease_id is None or not callable(execution_gate):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Staging workflow requires a live lease and per-action approval')
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier('task'), run_id=identifier('run'), step_id=identifier('step'),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity['deployment_id'],
                      owner_lease_id=owner_lease_id, task_kind='browser_staging_workflow',
                      authorized_path=STAGING_WORKFLOW_SCOPE,
                      authorized_content=STAGING_WORKFLOW_MESSAGE,
                      original_goal='Yerel sentetik uygulamada Draft bağlantısını aç, sabit mesajı kaydet ve makbuzu doğrula.',
                      normalized_goal=STAGING_WORKFLOW_GOAL,
                      success_criteria=('Exactly one authorized local form submission with an independently read receipt',),
                      max_attempts=4)
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), resume_state)
        try:
            if on_created:
                on_created(state)
            for attempt in range(4):
                state = self.advance(state, Phase.OBSERVE)
                observed = json.loads(self.gateway.observe(state))
                self.observation(state, observed)
                completed = self.store.connection.execute(
                    "SELECT count(*) FROM actions JOIN verifications USING(action_id) "
                    "WHERE actions.run_id=? AND actions.tool IN "
                    "('browser.staging.open','browser.staging.follow','browser.staging.fill','browser.staging.submit') "
                    "AND actions.status='ok' AND verifications.result='passed'",
                    (state.run_id,)).fetchone()[0]
                if not 0 <= completed < 4:
                    raise AOSFault(ErrorCode.UI_CHANGED, 'Synthetic staging action count differs')
                if completed == 0:
                    if observed != {'page': 'entry', 'value': '', 'receipt': '', 'submissions': 0}:
                        raise AOSFault(ErrorCode.UI_CHANGED, 'Synthetic staging entry differs')
                elif staging_outcome(observed) != EXPECTED_OUTCOMES[completed - 1]:
                    raise AOSFault(ErrorCode.UI_CHANGED, 'Synthetic staging page changed before action')
                tool, option, label, summary = STAGES[completed]
                arguments = {} if completed == 0 else {
                    'snapshot_id': observed['snapshot_id'],
                    'element_id': observed['elements'][0 if completed in (1, 2) else 1]['element_id']}
                if completed == 2:
                    arguments['value'] = STAGING_WORKFLOW_MESSAGE
                options = [Option(id=option, label=label), Option(id='ask_human', label='Ask the user for help')]
                state = self.advance(state, Phase.DECIDE, observation=summary)
                state, decision_id, prediction, allowed = await self.choose(state, options)
                if not allowed:
                    state = self.advance(state, Phase.WAITING_HUMAN)
                    self.store.finish(state, 'waiting_human', 'unknown')
                    return self.result(state, 'waiting_human')
                state = self.advance(state, Phase.EXECUTE)
                action = self.staging_action(state, tool, prediction.selected_option, arguments)
                await execution_gate(action)
                self.gateway.execute(action, decision_id)
                verify_action = self.staging_action(state, 'browser.staging.snapshot',
                                                    prediction.selected_option, {})
                actual = self.gateway.execute(verify_action, decision_id)
                state = self.advance(state, Phase.VERIFY)
                evidence_id = self.observation(state, actual, verify_action.action_id)
                expected = EXPECTED_OUTCOMES[completed]
                measured = staging_outcome(actual)
                passed = canonical(measured) == canonical(expected)
                with self.store.connection:
                    self.store.insert('verifications', verification_id=identifier('verification'),
                                      run_id=state.run_id, step_id=state.step_id, action_id=action.action_id,
                                      criterion=state.success_criteria[0] if completed == 3 else label,
                                      method='independent_staging_state_equals',
                                      result='passed' if passed else 'failed',
                                      expected_json=canonical(expected), actual_json=canonical(measured),
                                      evidence_refs_json=canonical([evidence_id]),
                                      verifier='aos-synthetic-staging-v1', created_at=now())
                if not passed:
                    raise AOSFault(ErrorCode.TOOL_FAILURE, 'Independent staging verification failed')
                if completed == 3:
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
                'Bounded synthetic staging workflow failed')
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), 'error': fault.payload()}
            if state.owner != 'AGENT' or state.phase == Phase.PAUSED:
                self.store.finish(state, 'paused', 'unknown')
                return {**self.result(state, 'paused'), 'error': fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, 'failed', 'failed')
            return {**self.result(state, 'failed'), 'error': fault.payload()}
