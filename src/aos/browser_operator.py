import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser import BROWSER_EXPECTED, BROWSER_SCOPE, BROWSER_VALUE
from .contracts import AOSFault, Action, BROWSER_GOAL, ErrorCode, Failure, Option, Phase, Prediction, QUESTION, State, canonical, identifier, now
from .decision import decision_request
from .operator import Operator
from .registries import DeploymentRegistry


class BrowserOperator(Operator):
    def browser_action(self, state: State, tool: str, option: str, arguments: dict) -> Action:
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier("action"), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect="Synthetic form exact local receipt",
                      verification="independent_dom_equals", deadline=time.time() + 10,
                      idempotency_key=identifier("intent"), selected_option=option)

    def observation(self, state: State, payload: dict, action_id=None) -> str:
        observation_id = identifier("observation")
        with self.store.connection:
            self.store.insert("observations", observation_id=observation_id, run_id=state.run_id,
                              step_id=state.step_id, action_id=action_id, kind="browser.dom",
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    async def choose(self, state: State, options: list[Option]) -> tuple[State, str, Prediction, bool]:
        adapter_identity = None
        if (self.engine.identity.get('kind') == 'owned_episode_adapter_runtime'
                or state.deployment_id.startswith('owned-adapter-')):
            from .owned_adapter_identity import validate_adapter_identity

            validate_adapter_identity(self.engine.identity)
            run = self.store.connection.execute('SELECT deployment_snapshot_json FROM runs WHERE run_id=?',
                                                (state.run_id,)).fetchone()
            if (run is None or state.deployment_id != self.engine.identity['deployment_id']
                    or json.loads(run['deployment_snapshot_json']) != self.engine.identity):
                raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Owned adapter run identity changed')
            adapter_identity = json.loads(canonical(self.engine.identity))
        snapshot_id = self.store.connection.execute(
            "SELECT snapshot_id FROM state_snapshots WHERE run_id=? AND state_version=?",
            (state.run_id, state.state_version),
        ).fetchone()[0]
        call_id = identifier("call") if self.engine.identity["real_model"] else None
        context = getattr(self, 'task_decision_context', None)
        if call_id or context is not None:
            with self.store.connection:
                if context is not None and not self.store.connection.in_transaction:
                    self.store.connection.execute('BEGIN IMMEDIATE')
                if call_id:
                    self.store.insert("model_calls", call_id=call_id, run_id=state.run_id, step_id=state.step_id,
                                  deployment_id=state.deployment_id, role="system1", status="error",
                                  request_json=canonical(decision_request(state, options)), created_at=now())
                if context is not None:
                    context.record(state, options, call_id)
        started = time.perf_counter()
        try:
            prediction = await asyncio.wait_for(self.decide_with_context(state, options), self.settings.model_timeout_seconds)
            prediction = Prediction.model_validate(prediction)
            prediction.validate_options(options)
        except asyncio.CancelledError:
            if call_id:
                with self.store.connection:
                    self.store.connection.execute("UPDATE model_calls SET status='cancelled' WHERE call_id=?", (call_id,))
            raise
        except (TimeoutError, AOSFault) as error:
            if call_id and (isinstance(error, TimeoutError) or error.code == ErrorCode.TIMEOUT):
                with self.store.connection:
                    self.store.connection.execute("UPDATE model_calls SET status='timeout' WHERE call_id=?", (call_id,))
            raise
        finally:
            if call_id:
                with self.store.connection:
                    self.store.connection.execute("UPDATE model_calls SET latency_ms=? WHERE call_id=?",
                                                  ((time.perf_counter() - started) * 1000, call_id))
        if call_id:
            metrics = getattr(self.engine, "last_metrics", {})
            if adapter_identity is not None and self.engine.identity != adapter_identity:
                raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Owned adapter inference identity changed')
            with self.store.connection:
                self.store.connection.execute(
                    "UPDATE model_calls SET status='ok',response_json=?,input_tokens=?,peak_vram_bytes=? WHERE call_id=?",
                    (prediction.model_dump_json(), metrics.get("input_tokens"), metrics.get("peak_vram_bytes"), call_id),
                )
                if adapter_identity is not None:
                    from .owned_adapter_identity import PROOF_KIND, adapter_inference_proof

                    call = self.store.connection.execute('SELECT * FROM model_calls WHERE call_id=?',
                                                         (call_id,)).fetchone()
                    proof = adapter_inference_proof(call, adapter_identity, metrics)
                    self.store.insert('observations', observation_id=identifier('observation'),
                                      run_id=state.run_id, step_id=state.step_id, action_id=None,
                                      kind=PROOF_KIND, payload_json=canonical(proof), created_at=now())
        if self.store.state(state.run_id) != state:
            raise AOSFault(ErrorCode.UI_CHANGED, "Ownership or state changed while deciding")
        confidence = prediction.probabilities[prediction.selected_option]
        allowed = prediction.selected_option == options[0].id and confidence >= self.settings.execute_min
        decision_id = identifier("decision")
        state = self.advance(state, Phase.POLICY)
        with self.store.connection:
            self.store.insert("decisions", decision_id=decision_id, run_id=state.run_id, step_id=state.step_id,
                              snapshot_id=snapshot_id, call_id=call_id, question=QUESTION,
                              options_json=canonical([option.model_dump() for option in options]),
                              probabilities_json=canonical(prediction.probabilities), selected_option=prediction.selected_option,
                              confidence=confidence, policy_result="allow" if allowed else "escalate", created_at=now())
        return state, decision_id, prediction, allowed

    async def form(self, *, owner_lease_id: str | None = None,
                   on_created: Callable[[State], None] | None = None,
                   execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                   resume_state: State | None = None) -> dict:
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        state = State(task_id=identifier("task"), run_id=identifier("run"), step_id=identifier("step"),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity["deployment_id"],
                      owner_lease_id=owner_lease_id or identifier("lease"), task_kind="browser_form", authorized_path=BROWSER_SCOPE,
                      authorized_content=BROWSER_VALUE, original_goal="Sentetik yerel formu doldur, kaydet ve sonucu doğrula.",
                      normalized_goal=BROWSER_GOAL,
                      success_criteria=("Exact message and receipt with exactly one local submission",), max_attempts=2)
        state = self.prepare_run(state, self.engine.identity, self.runtime.status(), resume_state)
        try:
            if on_created:
                on_created(state)
            for attempt in range(2):
                state = self.advance(state, Phase.OBSERVE)
                observed = json.loads(self.gateway.observe(state))
                self.observation(state, observed)
                completed_fills = self.store.connection.execute(
                    "SELECT count(*) FROM actions JOIN verifications USING(action_id) WHERE actions.run_id=? AND tool='browser.fill' AND status='ok' AND result='passed'",
                    (state.run_id,)).fetchone()[0]
                ordinal = 1 if observed['value'] == BROWSER_VALUE else 0
                if (completed_fills != ordinal or observed["value"] != ("" if ordinal == 0 else BROWSER_VALUE)
                        or observed["receipt"] != "" or observed["submissions"] != 0):
                    raise AOSFault(ErrorCode.UI_CHANGED, "Form precondition differs; no automatic replay")
                target_role = "textbox" if ordinal == 0 else "button"
                target = next(element for element in observed["elements"] if element["role"] == target_role)
                option = "fill_message" if ordinal == 0 else "submit_form"
                label = 'Fill the empty Message field with exactly "Hello from the local agent."' if ordinal == 0 else "Click Save locally to submit the correctly filled form once"
                summary = "The Message field is empty. Save locally is visible. Nothing has been submitted." if ordinal == 0 else 'Message contains exactly "Hello from the local agent.". Save locally is visible. Nothing has been submitted.'
                options = [Option(id=option, label=label), Option(id="ask_human", label="Ask the user for help")]
                state = self.advance(state, Phase.DECIDE, observation=summary)
                state, decision_id, prediction, allowed = await self.choose(state, options)
                if not allowed:
                    state = self.advance(state, Phase.WAITING_HUMAN)
                    self.store.finish(state, "waiting_human", "unknown")
                    return self.result(state, "waiting_human")
                state = self.advance(state, Phase.EXECUTE)
                arguments = {"snapshot_id": observed["snapshot_id"], "element_id": target["element_id"]}
                if ordinal == 0:
                    arguments["value"] = BROWSER_VALUE
                action = self.browser_action(state, "browser.fill" if ordinal == 0 else "browser.submit",
                                             prediction.selected_option, arguments)
                if execution_gate:
                    action = action.model_copy(update={"deadline": time.time() + 70})
                    await execution_gate(action)
                self.gateway.execute(action, decision_id)
                verify_action = self.browser_action(state, "browser.verify", prediction.selected_option, {})
                actual = self.gateway.execute(verify_action, decision_id)
                state = self.advance(state, Phase.VERIFY)
                evidence_id = self.observation(state, actual, verify_action.action_id)
                expected = {"value": BROWSER_VALUE, "receipt": "", "submissions": 0} if ordinal == 0 else BROWSER_EXPECTED
                passed = canonical(actual) == canonical(expected)
                with self.store.connection:
                    self.store.insert("verifications", verification_id=identifier("verification"), run_id=state.run_id,
                                      step_id=state.step_id, action_id=action.action_id,
                                      criterion="Exact field after fill" if ordinal == 0 else state.success_criteria[0],
                                      method="independent_dom_equals", result="passed" if passed else "failed",
                                      expected_json=canonical(expected), actual_json=canonical(actual),
                                      evidence_refs_json=canonical([evidence_id]), verifier="aos-browser-form-v1", created_at=now())
                if not passed:
                    raise AOSFault(ErrorCode.TOOL_FAILURE, "Independent DOM verification failed")
                if ordinal == 1:
                    break
            state = self.advance(state, Phase.SUCCEEDED)
            self.store.finish(state, "succeeded", "passed")
            return {**self.result(state, "succeeded"), "verified": True, "vision": False}
        except asyncio.CancelledError:
            self.interrupt(state)
            raise
        except (AOSFault, OSError, ValueError, TypeError, KeyError, StopIteration, TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT,
                "Bounded browser operation failed")
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), "error": fault.payload()}
            if state.owner != "AGENT" or state.phase == Phase.PAUSED:
                self.store.finish(state, "paused", "unknown")
                return {**self.result(state, "paused"), "error": fault.payload()}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, "failed", "failed")
            return {**self.result(state, "failed"), "error": fault.payload()}
