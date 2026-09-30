import asyncio
import os
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from .browser_operator import BrowserOperator
from .contracts import AOSFault, Action, ErrorCode, Failure, Option, Phase, State, VISION_GOAL, canonical, digest, identifier, now
from .registries import DeploymentRegistry
from .vision import Capture, VISION_EXPECTED, VISION_SCOPE, VisionScene


class VisionOperator(BrowserOperator):
    def record_observation(self, state: State, kind: str, payload: dict, action_id=None) -> str:
        observation_id = identifier("observation")
        with self.store.connection:
            self.store.insert("observations", observation_id=observation_id, run_id=state.run_id,
                              step_id=state.step_id, action_id=action_id, kind=kind,
                              payload_json=canonical(payload), created_at=now())
        return observation_id

    def save_capture(self, state: State, capture: Capture) -> str:
        root = self.settings.database.parent / "vision-captures"
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / (capture.capture_id + ".png")
        payload = capture.image_bytes()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        artifact_id = identifier("artifact")
        with self.store.connection:
            self.store.insert("artifacts", artifact_id=artifact_id, run_id=state.run_id, step_id=state.step_id,
                              relative_path="vision-captures/" + path.name, media_type="image/png",
                              sha256=capture.sha256, size_bytes=len(payload), redaction_status="raw", created_at=now())
        return artifact_id

    async def describe(self, state: State, capture: Capture, capture_version: int, artifact_id: str) -> VisionScene:
        call_id = identifier("call") if self.supervisor.identity["real_model"] else None
        request = {"purpose": "synthetic_canvas_vision", "capture": capture.metadata(),
                   "state_version": capture_version, "artifact_id": artifact_id}
        if call_id:
            with self.store.connection:
                self.store.insert("model_calls", call_id=call_id, run_id=state.run_id, step_id=state.step_id,
                                  deployment_id=self.supervisor.identity["deployment_id"], role="system2",
                                  request_json=canonical(request), status="error", created_at=now())
        started = time.perf_counter()
        try:
            scene = await asyncio.wait_for(self.supervisor.describe(capture, capture_version), self.settings.supervisor_timeout_seconds)
            scene = VisionScene.model_validate(scene.model_dump() if isinstance(scene, VisionScene) else scene)
            scene.validate_capture(capture, capture_version)
        except asyncio.CancelledError:
            if call_id:
                with self.store.connection:
                    self.store.connection.execute("UPDATE model_calls SET status='cancelled' WHERE call_id=?", (call_id,))
            raise
        except (AOSFault, TimeoutError) as error:
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
            metrics = getattr(self.supervisor, "last_metrics", {})
            with self.store.connection:
                self.store.connection.execute("UPDATE model_calls SET status='ok',response_json=?,input_tokens=?,output_tokens=? WHERE call_id=?",
                                              (scene.model_dump_json(), metrics.get("input_tokens"), metrics.get("output_tokens"), call_id))
        if self.store.state(state.run_id) != state:
            raise AOSFault(ErrorCode.UI_CHANGED, "Ownership changed during vision inference")
        return scene

    def visual_action(self, state: State, tool: str, option: str) -> Action:
        arguments = {"capture_id": state.capture_id, "scene_sha256": state.scene_sha256, "element_id": option} if tool == "vision.click" else {}
        return Action(task_id=state.task_id, run_id=state.run_id, step_id=state.step_id,
                      action_id=identifier("action"), runtime_id=state.runtime_id,
                      state_version=state.state_version, owner_lease_id=state.owner_lease_id,
                      tool=tool, arguments=arguments, expected_effect="Select SAVE once in the synthetic canvas",
                      verification="independent_canvas_equals", deadline=time.time() + 10,
                      idempotency_key=identifier("intent"), selected_option=option)

    async def canvas(self, *, owner_lease_id: str | None = None,
                     on_created: Callable[[State], None] | None = None,
                     execution_gate: Callable[[Action], Awaitable[None]] | None = None,
                     resume_state: State | None = None) -> dict:
        if self.supervisor is None:
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Vision observer is required")
        for identity in (self.engine.identity, self.supervisor.identity):
            DeploymentRegistry(self.store).record_experiment(identity)
        state = State(task_id=identifier("task"), run_id=identifier("run"), step_id=identifier("step"),
                      runtime_id=self.runtime.runtime_id, deployment_id=self.engine.identity["deployment_id"],
                      supervisor_deployment_id=self.supervisor.identity["deployment_id"],
                      owner_lease_id=owner_lease_id or identifier("lease"), task_kind="vision_canvas", authorized_path=VISION_SCOPE,
                      authorized_content="SAVE", original_goal="Sentetik görselde SAVE düğmesini bir kez seç ve sonucu doğrula.",
                      normalized_goal=VISION_GOAL,
                      success_criteria=("Independent synthetic application reports SAVE and exactly one click",), max_attempts=1)
        state = self.prepare_run(state, {**self.engine.identity, "supervisor": self.supervisor.identity}, self.runtime.status(), resume_state)
        try:
            if on_created:
                on_created(state)
            state = self.advance(state, Phase.OBSERVE)
            capture = Capture.model_validate_json(self.gateway.observe(state))
            capture_version = state.state_version
            artifact_id = self.save_capture(state, capture)
            self.record_observation(state, "vision.capture", {**capture.metadata(), "state_version": capture_version,
                                                              "artifact_id": artifact_id})
            state = self.advance(state, Phase.SUPERVISOR)
            scene = await self.describe(state, capture, capture_version, artifact_id)
            self.record_observation(state, "vision.scene", scene.model_dump())
            if scene.needs_human:
                state = self.advance(state, Phase.WAITING_HUMAN)
                self.store.finish(state, "waiting_human", "unknown")
                return {**self.result(state, "waiting_human"), "vision": True}
            self.runtime.bind_scene(scene, capture_version)
            targets = sorted(scene.targets().items(), key=lambda item: item[1].label != "SAVE")
            options = [Option(id=element_id, label="Click the visible " + element.label + " button") for element_id, element in targets]
            options.append(Option(id="ask_human", label="Ask the user for help without clicking"))
            state = self.advance(state, Phase.DECIDE, capture_id=capture.capture_id,
                                 scene_sha256=digest(scene.model_dump()),
                                 observation="The image observer found two separate visible buttons labeled SAVE and CANCEL in a capture-bound scene. No click has occurred.")
            state, decision_id, prediction, allowed = await self.choose(state, options)
            if not allowed:
                state = self.advance(state, Phase.WAITING_HUMAN)
                self.store.finish(state, "waiting_human", "unknown")
                return {**self.result(state, "waiting_human"), "vision": True}
            state = self.advance(state, Phase.EXECUTE)
            action = self.visual_action(state, "vision.click", prediction.selected_option)
            if execution_gate:
                action = action.model_copy(update={"deadline": time.time() + 70})
                await execution_gate(action)
            self.gateway.execute(action, decision_id)
            verify_action = self.visual_action(state, "vision.verify", prediction.selected_option)
            outcome = self.gateway.execute(verify_action, decision_id)
            state = self.advance(state, Phase.VERIFY)
            observation_id = self.record_observation(state, "vision.outcome", outcome, verify_action.action_id)
            passed = canonical(outcome) == canonical(VISION_EXPECTED)
            with self.store.connection:
                self.store.insert("verifications", verification_id=identifier("verification"), run_id=state.run_id,
                                  step_id=state.step_id, action_id=action.action_id, criterion=state.success_criteria[0],
                                  method="independent_canvas_equals", result="passed" if passed else "failed",
                                  expected_json=canonical(VISION_EXPECTED), actual_json=canonical(outcome),
                                  evidence_refs_json=canonical([observation_id]), verifier="aos-visual-canvas-v1", created_at=now())
            state = self.advance(state, Phase.SUCCEEDED if passed else Phase.FAILED)
            self.store.finish(state, "succeeded" if passed else "failed", "passed" if passed else "failed")
            return {**self.result(state, state.phase.value.lower()), "verified": passed, "vision": True}
        except asyncio.CancelledError:
            self.interrupt(state)
            raise
        except (AOSFault, OSError, ValueError, TypeError, KeyError, TimeoutError, ValidationError) as error:
            fault = error if isinstance(error, AOSFault) else AOSFault(
                ErrorCode.TIMEOUT if isinstance(error, TimeoutError) else ErrorCode.INVALID_OUTPUT,
                "Bounded visual operation failed")
            state = self.store.state(state.run_id)
            if state.phase in {Phase.CANCELLED, Phase.FAILED, Phase.SUCCEEDED}:
                return {**self.result(state, state.phase.value.lower()), "error": fault.payload(), "vision": True}
            if state.owner != "AGENT" or state.phase == Phase.PAUSED:
                self.store.finish(state, "paused", "unknown")
                return {**self.result(state, "paused"), "error": fault.payload(), "vision": True}
            state = self.advance(state, Phase.FAILED, last_error=Failure.model_validate(fault.payload()))
            self.store.finish(state, "failed", "failed")
            return {**self.result(state, "failed"), "error": fault.payload(), "vision": True}
