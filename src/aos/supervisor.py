import asyncio
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import tempfile
import time
from typing import Literal, Protocol

from pydantic import Field, ValidationError

from .contracts import AOSFault, ErrorCode, REPO_ROOT, TypedModel, canonical, digest


RECOVERY_ACTIONS = ("observe_workspace", "create_authorized_file", "verify_exact_content")


class RecoveryStep(TypedModel):
    order: int = Field(ge=1, le=3)
    action: Literal["observe_workspace", "create_authorized_file", "verify_exact_content"]
    expected_result: str = Field(min_length=1, max_length=300)


def constrain_plan_schema(schema: dict) -> None:
    schema.pop("items", None)
    entries = [{"type": "object", "properties": {
        "order": {"const": index}, "action": {"const": action},
        "expected_result": {"type": "string", "minLength": 1, "maxLength": 300}},
        "required": ["order", "action", "expected_result"], "additionalProperties": False}
        for index, action in enumerate(RECOVERY_ACTIONS, start=1)]
    schema["anyOf"] = [{"const": []}, {"type": "array", "prefixItems": entries, "minItems": 3, "maxItems": 3}]


class RecoveryPlan(TypedModel):
    diagnosis: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(min_length=1, max_length=4)
    assumptions: list[str] = Field(max_length=3)
    revised_plan: list[RecoveryStep] = Field(max_length=3, json_schema_extra=constrain_plan_schema)
    verification_criteria: list[Literal["exact_file_content"]] = Field(min_length=1, max_length=1)
    needs_human: bool

    def validate_evidence(self, evidence: list[dict]) -> None:
        if set(self.evidence_refs) != {item["id"] for item in evidence}:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, "Supervisor cited unknown or incomplete evidence")
        if self.needs_human:
            if self.revised_plan:
                raise AOSFault(ErrorCode.INVALID_OUTPUT, "Human-required plan cannot authorize actions")
        elif ([step.order for step in self.revised_plan] != [1, 2, 3]
              or tuple(step.action for step in self.revised_plan) != RECOVERY_ACTIONS):
            raise AOSFault(ErrorCode.UNSAFE_ACTION, "Recovery plan must observe, create within scope, and verify")


class Supervisor(Protocol):
    identity: dict
    last_metrics: dict
    async def plan(self, problem: str, evidence: list[dict]) -> RecoveryPlan: ...


def verify_artifacts(root: Path, expected: dict[str, str]) -> None:
    root = root.resolve(strict=True)
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    if actual != set(expected):
        raise AOSFault(ErrorCode.MODEL_FAILURE, "Bonsai artifact file set differs from the pinned deployment")
    for relative, checksum in expected.items():
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Artifact resolves outside its pinned directory")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise AOSFault(ErrorCode.MODEL_FAILURE, "Bonsai artifact SHA-256 mismatch")


def request_json(port: int, token: str, route: str, body: dict | None = None, timeout: float = 5) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("POST" if body is not None else "GET", route,
                           body=canonical(body) if body is not None else None,
                           headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read(65537)
        if response.status == 503:
            raise ConnectionError("Local model is still loading")
        if response.status != 200 or len(payload) > 65536:
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Local Bonsai server rejected the bounded request")
        return json.loads(payload)
    finally:
        connection.close()


class BonsaiSupervisor:
    def __init__(self, manifest: Path, timeout: float = 180):
        self.pins = json.loads(manifest.read_text())
        self.pins["recovery_schema_sha256"] = digest(RecoveryPlan.model_json_schema())
        self.pins["recovery_protocol"] = "aos-bonsai-recovery-v1"
        self.identity = {"deployment_id": "bonsai-" + digest(self.pins), "kind": "bonsai_native_supervisor",
                         "real_model": True, "pins": self.pins}
        self.timeout = timeout
        self.last_metrics: dict = {}
        self._verified_fingerprint: tuple | None = None
        self._pin_prewarm: asyncio.Task | None = None

    def artifact_fingerprint(self) -> tuple:
        try:
            paths = []
            for root_name in ("model_path", "runtime_path"):
                root = Path(self.pins[root_name])
                paths.append(root)
                paths.extend(sorted(root.resolve(strict=True).rglob("*")))
            paths.extend(Path(path) for path in sorted(self.pins["native_libraries"]))
            entries = []
            for path in paths:
                link = path.lstat()
                resolved = path.resolve(strict=True)
                target = resolved.stat()
                entries.append((str(path), str(resolved),
                                tuple(getattr(link, field) for field in
                                      ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")),
                                tuple(getattr(target, field) for field in
                                      ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink"))))
            return digest(self.pins), tuple(entries)
        except (OSError, KeyError, RuntimeError, ValueError) as error:
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Pinned Bonsai artifact inventory is unavailable") from error

    def verify_pins(self) -> None:
        if not self.pins.get("native_libraries"):
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Pinned native dependency hashes are required")
        fingerprint = self.artifact_fingerprint()
        if fingerprint == self._verified_fingerprint:
            return
        self._verified_fingerprint = None
        verify_artifacts(Path(self.pins["model_path"]), self.pins["model_files"])
        verify_artifacts(Path(self.pins["runtime_path"]), self.pins["runtime_files"])
        for filename, expected in self.pins["native_libraries"].items():
            with Path(filename).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise AOSFault(ErrorCode.MODEL_FAILURE, "Native runtime dependency changed; explicit revalidation required")
        server = Path(self.pins["server_path"])
        if not server.resolve().is_relative_to(Path(self.pins["runtime_path"]).resolve()):
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Server is outside the pinned runtime")
        if self.artifact_fingerprint() != fingerprint:
            raise AOSFault(ErrorCode.MODEL_FAILURE, "Pinned Bonsai artifacts changed during verification")
        self._verified_fingerprint = fingerprint

    def prewarm_pins_idle(self) -> None:
        if self._pin_prewarm is None:
            self._pin_prewarm = asyncio.create_task(asyncio.to_thread(self.verify_pins))

    async def close_pin_prewarm(self) -> None:
        task = self._pin_prewarm
        self._pin_prewarm = None
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def verify_pins_for_call(self) -> None:
        task = self._pin_prewarm
        if task is not None:
            try:
                await asyncio.shield(task)
            finally:
                if task.done() and self._pin_prewarm is task:
                    self._pin_prewarm = None
        await asyncio.to_thread(self.verify_pins)

    async def plan(self, problem: str, evidence: list[dict], *, request_context=None) -> RecoveryPlan:
        started = time.perf_counter()
        self.last_metrics = {}
        self.last_request = None
        self.last_response = None
        await self.verify_pins_for_call()
        pins_verified = time.perf_counter()
        server = Path(self.pins["server_path"])
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        token = secrets.token_urlsafe(32)
        process = None
        spawning = None
        model_root = Path(self.pins["model_path"])
        with tempfile.TemporaryDirectory(prefix="bonsai-call-", dir=REPO_ROOT / "runs") as directory:
            key_file = Path(directory) / "api-key"
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(token)
            command = [str(server), "--model", str(model_root / self.pins["weights_file"]),
                       "--mmproj", str(model_root / self.pins["projector_file"]),
                       "--host", "127.0.0.1", "--port", str(port), "--api-key-file", str(key_file),
                       "--alias", self.identity["deployment_id"], "--ctx-size", str(self.pins["context_tokens"]),
                       "--parallel", "1", "--n-gpu-layers", str(self.pins["gpu_layers"]),
                       "--flash-attn", "on", "--jinja", "--reasoning-budget", "0", "--no-webui",
                       "--no-warmup", "--no-context-shift", "--log-disable"]
            try:
                async with asyncio.timeout(self.timeout):
                    spawning = asyncio.create_task(asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                                  stderr=asyncio.subprocess.DEVNULL,
                                                                  env={"PATH": "/usr/bin:/bin"}, start_new_session=True))
                    process = await asyncio.shield(spawning)
                    while True:
                        if process.returncode is not None:
                            raise AOSFault(ErrorCode.MODEL_FAILURE, "Pinned Bonsai server exited before readiness")
                        try:
                            models = await asyncio.to_thread(request_json, port, token, "/v1/models")
                            if {model["id"] for model in models["data"]} != {self.identity["deployment_id"]}:
                                raise AOSFault(ErrorCode.MODEL_FAILURE, "Loaded Bonsai deployment identity differs")
                            break
                        except (ConnectionError, OSError):
                            await asyncio.sleep(0.2)
                    server_ready = time.perf_counter()
                    request = (self.request_body(problem, evidence) if request_context is None else
                               self.request_body(problem, evidence, request_context=request_context))
                    request_built = time.perf_counter()
                    before_model_call = getattr(self, 'before_model_call', None)
                    if before_model_call is not None:
                        before_model_call()
                    self.last_request = json.loads(canonical(request))
                    response = await asyncio.to_thread(request_json, port, token, "/v1/chat/completions", request, self.timeout)
                    response_received = time.perf_counter()
                    choice = response["choices"][0]
                    if choice["finish_reason"] != "stop":
                        raise AOSFault(ErrorCode.INVALID_OUTPUT, "Supervisor output was truncated")
                    plan = self.parse_response(choice["message"]["content"], evidence)
                    self.last_response = plan.model_dump(mode='json')
                    parsed = time.perf_counter()
                    self.last_metrics = {"latency_ms": (parsed - started) * 1000,
                                         "pin_verify_ms": (pins_verified - started) * 1000,
                                         "server_start_ms": (server_ready - pins_verified) * 1000,
                                         "request_build_ms": (request_built - server_ready) * 1000,
                                         "inference_http_ms": (response_received - request_built) * 1000,
                                         "response_parse_ms": (parsed - response_received) * 1000,
                                         "input_tokens": response.get("usage", {}).get("prompt_tokens"),
                                         "output_tokens": response.get("usage", {}).get("completion_tokens")}
                    return plan
            except TimeoutError:
                raise AOSFault(ErrorCode.TIMEOUT, "Bonsai request timed out; no tool execution authorized") from None
            except (ValueError, KeyError, IndexError, TypeError, ValidationError) as error:
                raise AOSFault(ErrorCode.INVALID_OUTPUT, "Malformed Bonsai response: " + type(error).__name__) from None
            finally:
                if process is None and spawning is not None:
                    process = await spawning
                if process is not None and process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()

    def parse_response(self, content: str, evidence: list[dict]) -> RecoveryPlan:
        plan = RecoveryPlan.model_validate_json(content)
        plan.validate_evidence(evidence)
        return plan

    def request_body(self, problem: str, evidence: list[dict]) -> dict:
        return {
                        "model": self.identity["deployment_id"], "temperature": self.pins["temperature"],
                        "max_tokens": self.pins["max_output_tokens"], "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                        "messages": [
                            {"role": "system", "content": "You are the AOS Supervisor. Diagnose the provided evidence and return only the requested JSON recovery plan. You cannot execute tools or expand authorization. Do not provide hidden reasoning. For a safely recoverable missing authorized hello file, first observe_workspace, then create_authorized_file, then verify_exact_content. If evidence is insufficient or the request exceeds authorization, set needs_human=true and revised_plan=[]. Cite the exact evidence ids. The only verification criterion is exact_file_content."},
                            {"role": "user", "content": canonical({"problem": problem, "evidence": evidence})},
                        ],
                        "response_format": {"type": "json_schema", "json_schema": {
                            "name": "aos_recovery_plan", "strict": True, "schema": RecoveryPlan.model_json_schema()}},
                    }
