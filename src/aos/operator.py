import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import ValidationError

from .computer import ComputerGateway, WorkspaceRuntime
from .contracts import AOSFault, Action, ErrorCode, Failure, HELLO_CONTENT, HELLO_PATH, MISSING_FILE_SUMMARY, Option, Phase, Prediction, QUESTION, Settings, State, canonical, identifier, now
from .decision import DecisionEngine, decision_request
from .registries import DeploymentRegistry
from .storage import TrajectoryStore
from .supervisor import RecoveryPlan, Supervisor


logger = logging.getLogger("aos")


class HelloDecisionContext(Protocol):
    def apply(self, state: State, observation: str) -> str: ...
    def record(self, state: State, options: list[Option], call_id: str | None) -> None: ...
    def before_call(self) -> None: ...


class Operator:
    def __init__(self, settings: Settings, store: TrajectoryStore, runtime: WorkspaceRuntime, engine: DecisionEngine,
                 supervisor: Supervisor | None = None):
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.engine = engine
        self.gateway = ComputerGateway(store, runtime)
        self.supervisor = supervisor
        self.escalation_id: str | None = None
        self.interruption_phase = Phase.CANCELLED

    def prepare_run(self, state: State, deployment: dict, runtime: dict, resume_state: State | None) -> State:
        if resume_state is None:
            self.store.create_run(state, deployment, runtime)
            return state
        current = self.store.state(resume_state.run_id)
        binding = ('task_kind', 'runtime_id', 'deployment_id', 'supervisor_deployment_id',
                   'authorized_path', 'authorized_content', 'success_criteria')
        run = self.store.connection.execute('SELECT status FROM runs WHERE run_id=?', (current.run_id,)).fetchone()
        uncertain = self.store.connection.execute(
            "SELECT 1 FROM actions WHERE run_id=? AND status IN ('intent','running','uncertain')", (current.run_id,)).fetchone()
        if (current != resume_state or current.phase != Phase.PAUSED or current.owner != 'PAUSED'
                or not run or run['status'] != 'paused' or uncertain or not runtime.get('running')
                or any(getattr(current, key) != getattr(state, key) for key in binding)
                or current.owner_lease_id == state.owner_lease_id):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Resume requires a live, unchanged, safely paused run and fresh lease')
        resumed = current.advance(Phase.PAUSED, owner='AGENT', owner_lease_id=state.owner_lease_id,
                                  observation='Not yet observed after resume', capture_id=None, scene_sha256=None)
        self.store.save_state(current, resumed, resume=True)
        return resumed

    def interrupt(self, state: State) -> State:
        current = self.store.state(state.run_id)
        if current.phase not in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
            current = self.advance(current, self.interruption_phase, owner='PAUSED', owner_lease_id=identifier('revoked'))
            self.store.finish(current, self.interruption_phase.value.lower(), 'unknown')
        return current

    def settle_recovery(self, outcome: str) -> None:
        if self.escalation_id:
            with self.store.connection:
                self.store.connection.execute("UPDATE supervisor_escalations SET outcome=? WHERE escalation_id=?",
                                              (outcome, self.escalation_id))

    async def recovery_probe(self, state: State) -> State:
        if self.supervisor is None or state.recovery_attempts >= self.settings.max_supervisor_recoveries:
            raise AOSFault(ErrorCode.STUCK, "Recovery requires an available supervisor and remaining budget")
        state = self.advance(state, Phase.OBSERVE)
        try:
            self.gateway.observe(state)
        except FileNotFoundError:
            pass
        else:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Recovery probe requires an absent hello file; existing data is preserved")
        state = self.advance(state, Phase.DECIDE, observation="Synthetic recovery scenario: the initial plan reads the hello file before creating it.")
        decision_id = identifier("probe-decision")
        with self.store.connection:
            snapshot_id = self.store.snapshot(state)
            self.store.insert("decisions", decision_id=decision_id, run_id=state.run_id, step_id=state.step_id,
                              snapshot_id=snapshot_id, question="Synthetic fault probe: attempt the initial read-first plan",
                              options_json=canonical([{"id": "read_file", "label": "Attempt the initial read"},
                                                      {"id": "ask_human", "label": "Ask the user"}]),
                              probabilities_json=canonical({"read_file": 1.0, "ask_human": 0.0}),
                              selected_option="read_file", confidence=1.0, policy_result="allow", created_at=now())
        state = self.advance(state, Phase.POLICY)
        state = self.advance(state, Phase.EXECUTE)
        action = self.action(state, "filesystem.read", "read_file")
        try:
            self.gateway.execute(action, decision_id)
        except AOSFault as fault:
            if fault.code != ErrorCode.ELEMENT_MISSING:
                raise
        else:
            raise AOSFault(ErrorCode.UI_CHANGED, "Recovery probe precondition changed")
        observation_id = identifier("observation")
        evidence = [{"id": observation_id, "kind": "filesystem.read", "summary": MISSING_FILE_SUMMARY}]
        with self.store.connection:
            self.store.insert("observations", observation_id=observation_id, run_id=state.run_id, step_id=state.step_id,
                              action_id=action.action_id, kind="filesystem.error",
                              payload_json=canonical({"error": "ELEMENT_MISSING", "summary": evidence[0]["summary"]}), created_at=now())
        state = self.advance(state, Phase.SUPERVISOR, recovery_attempts=state.recovery_attempts + 1)
        call_id = identifier("call") if self.supervisor.identity["real_model"] else None
        self.escalation_id = identifier("escalation")
        request = {"problem": state.normalized_goal + " The initial read-first plan failed. Only creating this exact file and reading it back are authorized; no overwrite, deletion, host commands, or network actions are authorized.", "evidence": evidence}
        with self.store.connection:
            if call_id:
                self.store.insert("model_calls", call_id=call_id, run_id=state.run_id, step_id=state.step_id,
                                  deployment_id=self.supervisor.identity["deployment_id"], role="system2",
                                  request_json=canonical(request), status="error", created_at=now())
            self.store.insert("supervisor_escalations", escalation_id=self.escalation_id, run_id=state.run_id,
                              step_id=state.step_id, call_id=call_id, reason="ELEMENT_MISSING: read-first recovery probe",
                              evidence_refs_json=canonical([observation_id]), outcome="pending", created_at=now())
        started = time.perf_counter()
        try:
            plan = await asyncio.wait_for(self.supervisor.plan(**request), self.settings.supervisor_timeout_seconds)
            plan = RecoveryPlan.model_validate(plan)
            plan.validate_evidence(evidence)
        except (TimeoutError, AOSFault, ValidationError) as error:
            if call_id:
                with self.store.connection:
                    self.store.connection.execute("UPDATE model_calls SET status=?,latency_ms=? WHERE call_id=?",
                                                  ("timeout" if isinstance(error, TimeoutError) or isinstance(error, AOSFault) and error.code == ErrorCode.TIMEOUT else "error",
                                                   (time.perf_counter() - started) * 1000, call_id))
            raise
        with self.store.connection:
            if call_id:
                metrics = self.supervisor.last_metrics
                self.store.connection.execute("UPDATE model_calls SET status='ok',response_json=?,latency_ms=?,input_tokens=?,output_tokens=? WHERE call_id=?",
                                              (plan.model_dump_json(), (time.perf_counter() - started) * 1000,
                                               metrics.get("input_tokens"), metrics.get("output_tokens"), call_id))
            self.store.connection.execute("UPDATE supervisor_escalations SET diagnosis=?,corrected_plan_json=? WHERE escalation_id=?",
                                          (plan.diagnosis, plan.model_dump_json(), self.escalation_id))
        if plan.needs_human:
            return self.advance(state, Phase.WAITING_HUMAN)
        return self.advance(state, Phase.REPLAN, recovery_plan=tuple(step.action for step in plan.revised_plan),
                            plan_version=state.plan_version + 1)

    def advance(self, state: State, phase: Phase, **changes) -> State:
        context = getattr(self, 'task_decision_context', None)
        if phase == Phase.DECIDE and context is not None:
            changes['observation'] = context.apply(state, changes.get('observation', state.observation))
        updated = state.advance(phase, **changes)
        self.store.save_state(state, updated)
        logger.info(canonical({"event": "state", "run_id": state.run_id,
                               "phase": phase.value, "state_version": updated.state_version}))
        return updated

    async def decide_with_context(self, state: State, options: list[Option], context=None):
        context = context or getattr(self, 'task_decision_context', None)
        previous = getattr(self.engine, 'before_decision_dispatch', None)
        changed = False
        try:
            if context is not None:
                context.before_call()
                if hasattr(context, 'before_dispatch'):
                    def before_dispatch(request):
                        if previous is not None:
                            previous(request)
                        context.before_dispatch(request)

                    self.engine.before_decision_dispatch = before_dispatch
                    changed = True
            return await self.engine.decide(state, options)
        finally:
            if changed:
                self.engine.before_decision_dispatch = previous

    def action(self, state: State, tool: str, option: str) -> Action:
        arguments = {"path": HELLO_PATH}
        if tool == "filesystem.write":
            arguments["content"] = HELLO_CONTENT
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier("action"), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect="Exact authorized hello content",
                      deadline=time.time() + 10, idempotency_key=identifier("intent"), selected_option=option)

    async def hello(self, recovery_probe: bool = False, *, owner_lease_id: str | None = None,
                    on_created: Callable[[State], None] | None = None,
                    execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                    resume_state: State | None = None, verification_only: bool = False,
                    verification_guard: Callable[[], None] | None = None,
                    decision_context: HelloDecisionContext | None = None) -> dict:
        if decision_context is not None and getattr(self, 'task_decision_context', None) is not None:
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reviewed context lanes cannot be implicitly composed')
        if decision_context is not None and (resume_state is not None or recovery_probe or verification_only):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Reviewed context requires a separately authorized fresh hello run')
        if verification_only and (resume_state is None or recovery_probe or self.supervisor is not None
                                  or not callable(execution_gate) or not callable(verification_guard)):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification-only continuation requires a resumed run and explicit read guards')
        DeploymentRegistry(self.store).record_experiment(self.engine.identity)
        if self.supervisor:
            DeploymentRegistry(self.store).record_experiment(self.supervisor.identity)
        state = State(task_id=identifier("task"), run_id=identifier("run"), step_id=identifier("step"),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity["deployment_id"],
                      owner_lease_id=owner_lease_id or identifier("lease"),
                      supervisor_deployment_id=self.supervisor.identity["deployment_id"] if self.supervisor else None)
        deployment = dict(self.engine.identity)
        if self.supervisor:
            deployment["supervisor"] = self.supervisor.identity
        state = self.prepare_run(state, deployment, {**self.runtime.status(), "recovery_probe": recovery_probe}, resume_state)
        call_id = None
        try:
            if on_created:
                on_created(state)
            if recovery_probe:
                state = await self.recovery_probe(state)
                if state.phase == Phase.WAITING_HUMAN:
                    self.settle_recovery("human_required")
                    self.store.finish(state, "waiting_human", "unknown")
                    return self.result(state, "waiting_human")
            state = self.advance(state, Phase.OBSERVE)
            try:
                existing = self.gateway.observe(state)
                if verification_only and existing != HELLO_CONTENT:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification-only continuation cannot repair conflicting content')
                observation = "File already contains the exact authorized content." if existing == HELLO_CONTENT else "File exists with different content. Overwriting is not authorized by this harness."
                first = Option(id="read_file", label="Read the existing file and independently verify exact content")
                if existing != HELLO_CONTENT:
                    first = Option(id="ask_human", label="Ask the user to resolve the existing conflicting file")
            except FileNotFoundError:
                if verification_only:
                    raise AOSFault(ErrorCode.UNSAFE_ACTION, 'Verification-only continuation cannot recreate a missing file') from None
                observation = "File is absent. Creating the exact requested file is authorized."
                first = Option(id="write_file", label="Create the authorized hello file with the exact requested content")
            options = [first, Option(id="ask_supervisor", label="Ask the supervisor for help")]
            if first.id != "ask_human":
                options.append(Option(id="ask_human", label="Ask the user for help"))
            if decision_context is not None:
                observation = decision_context.apply(state, observation)
            state = self.advance(state, Phase.DECIDE, observation=observation)
            snapshot_id = self.store.connection.execute(
                "SELECT snapshot_id FROM state_snapshots WHERE run_id=? AND state_version=?",
                (state.run_id, state.state_version),
            ).fetchone()[0]
            started = time.perf_counter()
            call_id = identifier("call") if self.engine.identity["real_model"] else None
            active_context = decision_context or getattr(self, 'task_decision_context', None)
            with self.store.connection:
                if active_context is not None and not self.store.connection.in_transaction:
                    self.store.connection.execute('BEGIN IMMEDIATE')
                if call_id:
                    self.store.insert("model_calls", call_id=call_id, run_id=state.run_id, step_id=state.step_id,
                                      deployment_id=state.deployment_id, role="system1", status="error",
                                      request_json=canonical(decision_request(state, options)), created_at=now())
                if active_context is not None:
                    active_context.record(state, options, call_id)
            try:
                pending_decision = self.decide_with_context(state, options, active_context)
                prediction = await asyncio.wait_for(pending_decision, self.settings.model_timeout_seconds)
                prediction = Prediction.model_validate(prediction)
                prediction.validate_options(options)
            except (ValidationError, TypeError, ValueError):
                raise AOSFault(ErrorCode.INVALID_OUTPUT, "Decision output failed validation") from None
            finally:
                if call_id:
                    with self.store.connection:
                        self.store.connection.execute("UPDATE model_calls SET latency_ms=? WHERE call_id=?",
                                                      ((time.perf_counter() - started) * 1000, call_id))
            confidence = prediction.probabilities[prediction.selected_option]
            allowed = prediction.selected_option in {"write_file", "read_file"} and confidence >= self.settings.execute_min
            decision_id = identifier("decision")
            state = self.advance(state, Phase.POLICY)
            with self.store.connection:
                if call_id:
                    metrics = getattr(self.engine, "last_metrics", {})
                    self.store.connection.execute(
                        "UPDATE model_calls SET status='ok',response_json=?,input_tokens=?,peak_vram_bytes=? WHERE call_id=?",
                        (prediction.model_dump_json(), metrics.get("input_tokens"), metrics.get("peak_vram_bytes"), call_id),
                    )
                self.store.insert("decisions", decision_id=decision_id, run_id=state.run_id, step_id=state.step_id,
                                  snapshot_id=snapshot_id, call_id=call_id, question=QUESTION,
                                  options_json=canonical([option.model_dump() for option in options]),
                                  probabilities_json=canonical(prediction.probabilities), selected_option=prediction.selected_option,
                                  confidence=confidence, policy_result="allow" if allowed else "escalate", created_at=now())
            if not allowed:
                state = self.advance(state, Phase.WAITING_HUMAN)
                self.store.finish(state, "waiting_human", "unknown")
                self.settle_recovery("human_required")
                return self.result(state, "waiting_human")
            state = self.advance(state, Phase.EXECUTE)
            tool = "filesystem.write" if prediction.selected_option == "write_file" else "filesystem.read"
            action = self.action(state, tool, prediction.selected_option)
            if execution_gate:
                action = action.model_copy(update={"deadline": time.time() + (60 if verification_only else 70)})
                await execution_gate(action)
            self.gateway.execute(action, decision_id)
            read_action = self.action(state, "filesystem.read", prediction.selected_option)
            if verification_only:
                read_action = read_action.model_copy(update={"deadline": time.time() + 60})
                await execution_gate(read_action)
            readback = self.gateway.execute(read_action, decision_id)["content"]
            if verification_only:
                verification_guard()
            state = self.advance(state, Phase.VERIFY)
            observation_id = identifier("observation")
            passed = readback == HELLO_CONTENT
            with self.store.connection:
                self.store.insert("observations", observation_id=observation_id, run_id=state.run_id,
                                  step_id=state.step_id, action_id=read_action.action_id, kind="filesystem.read",
                                  payload_json=canonical({"content": readback}), created_at=now())
                self.store.insert("verifications", verification_id=identifier("verification"), run_id=state.run_id,
                                  step_id=state.step_id, action_id=action.action_id, criterion=state.success_criteria[0],
                                  method="independent_read_equals", result="passed" if passed else "failed",
                                  expected_json=canonical(HELLO_CONTENT), actual_json=canonical(readback),
                                  evidence_refs_json=canonical([observation_id]), verifier="aos-exact-bytes-v1", created_at=now())
            state = self.advance(state, Phase.SUCCEEDED if passed else Phase.FAILED)
            self.store.finish(state, "succeeded" if passed else "failed", "passed" if passed else "failed")
            self.settle_recovery("recovered" if passed else "failed")
            return {**self.result(state, state.phase.value.lower()), "verified": passed}
        except asyncio.CancelledError:
            state = self.interrupt(state)
            with self.store.connection:
                self.store.connection.execute("UPDATE model_calls SET status='cancelled' WHERE run_id=? AND status='error'",
                                              (state.run_id,))
            raise
        except (AOSFault, OSError, UnicodeError, TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT if isinstance(error, ValidationError) else ErrorCode.TOOL_FAILURE,
                "Bounded hello operation failed",
            )
            state = self.store.state(state.run_id)
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, "failed", "failed")
            self.settle_recovery("failed")
            logger.error(canonical({"event": "failure", "run_id": state.run_id, **fault.payload()}))
            return {**self.result(state, "failed"), "error": fault.payload()}

    def result(self, state: State, status: str) -> dict:
        calls = self.store.connection.execute("SELECT count(*) FROM model_calls WHERE run_id=? AND role='system2'",
                                              (state.run_id,)).fetchone()[0]
        return {"run_id": state.run_id, "status": status, "real_model": self.engine.identity["real_model"],
                "system2_calls": calls, "recovery_attempts": state.recovery_attempts,
                "real_supervisor": self.supervisor.identity["real_model"] if self.supervisor else False}
