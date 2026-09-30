import asyncio
import json
import os
import signal
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .contracts import AOSFault, ErrorCode, Option, Prediction, QUESTION, REPO_ROOT, State, digest


class DecisionEngine(Protocol):
    identity: dict
    async def decide(self, state: State, options: list[Option]) -> Prediction: ...


def decision_request(state: State, options: list[Option]) -> dict:
    request = {"state": state.normalized_goal + "\nObservation: " + state.observation,
               "question": QUESTION, "options": [option.model_dump() for option in options]}
    if state.recovery_plan:
        request["state"] += "\nValidated recovery plan: " + ", ".join(state.recovery_plan)
    return request


class FixtureDecisionEngine:
    identity = {"deployment_id": "fixture-decision-v1", "kind": "deterministic_fixture", "real_model": False}

    async def decide(self, state: State, options: list[Option]) -> Prediction:
        return Prediction(selected_option=options[0].id,
                          probabilities={option.id: 1.0 if index == 0 else 0.0 for index, option in enumerate(options)})


class DeciderEngine:
    def __init__(self, manifest: Path, python: Path, timeout: float = 120):
        self.manifest = manifest.resolve(strict=True)
        self.python = python.absolute()
        self.timeout = timeout
        self.pins = json.loads(self.manifest.read_text())
        self.identity = {"deployment_id": "decider-" + digest(self.pins), "kind": "decider_native_worker",
                         "real_model": True, "pins": self.pins}
        self.last_metrics: dict = {}
        self.last_request: dict | None = None
        self.last_response: dict | None = None
        self.before_decision_dispatch = None

    async def decide(self, state: State, options: list[Option]) -> Prediction:
        self.last_request = None
        self.last_response = None
        request = decision_request(state, options)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            str(self.python), str(REPO_ROOT / "services/decider/worker.py"), str(self.manifest),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                 "TOKENIZERS_PARALLELISM": "false"}, start_new_session=True,
        ))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        try:
            payload = json.dumps(request).encode()
            if self.before_decision_dispatch is not None:
                self.before_decision_dispatch(deepcopy(request))
            self.last_request = deepcopy(request)
            stdout, _ = await asyncio.wait_for(process.communicate(payload), self.timeout)
            if process.returncode or len(stdout) > 65536:
                raise AOSFault(ErrorCode.MODEL_FAILURE, "Pinned local Decider worker failed; inspect its installation")
            prediction = self.parse_response(stdout, options)
            self.last_response = prediction.model_dump(mode='json')
            return prediction
        except BaseException as error:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(error, TimeoutError):
                raise AOSFault(ErrorCode.TIMEOUT, "Decider worker timed out; no action executed") from None
            raise

    def parse_response(self, stdout: bytes, options: list[Option]) -> Prediction:
        try:
            response = json.loads(stdout)
            if response["deployment_digest"] != digest(self.pins):
                raise ValueError("identity mismatch")
            self.last_metrics = response["metrics"]
            prediction = Prediction.model_validate(response["prediction"])
            prediction.validate_options(options)
            return prediction
        except (ValueError, KeyError, TypeError, ValidationError):
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Invalid Decider worker response") from None
