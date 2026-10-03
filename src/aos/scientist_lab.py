import asyncio
from copy import copy
import hashlib
import http.client
import ipaddress
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import Field

from .contracts import Action, TypedModel, canonical, digest
from .scientist_intents import ScientistIntentBinding
from .scientist_protocol import (
    ScientistReport, ScientistRunStatus, _reject_constant, _unique_object, verify_scientist_report,
)
from .scientist_transport import ScientistAdmissionError
from .scientist_async import ScientistHostBridge


class ScientistLabBudget(TypedModel):
    experiments: int = Field(ge=1, le=35)
    wall_seconds: int = Field(ge=1, le=14400)
    model_tokens: int = Field(ge=0, le=350000)


class ScientistLabStart(TypedModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    track: Literal['anomaly', 'mode']
    suite: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$')
    budget: ScientistLabBudget
    program_version: str = Field(min_length=1, max_length=64)
    external_task_id: str = Field(pattern=r'^task-[a-f0-9]{32}$')
    external_run_id: str = Field(pattern=r'^run-[a-f0-9]{32}$')
    external_action_id: str = Field(pattern=r'^action-[a-f0-9]{32}$')


class ScientistLabHandle(TypedModel):
    run_id: str = Field(pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
    state: Literal['queued', 'running', 'stop_requested', 'completed', 'stopped', 'failed']
    reused: bool


class ScientistLabTask(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    kind: Literal['scientist_lab'] = 'scientist_lab'
    binding: ScientistIntentBinding
    authority_url: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(min_length=1, max_length=128)
    state_version: int = Field(ge=0)
    request: ScientistLabStart
    lab_run_id: str | None = Field(default=None,
        pattern=r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


class ScientistLabAction(Action):
    tool: Literal['lab.start', 'lab.status', 'lab.stop', 'lab.report']


class ScientistLabPolicy:
    identity = {'kind': 'host_policy', 'deployment_id': 'scientist-lab-policy-v1', 'real_model': False}

    @staticmethod
    def check(task: ScientistLabTask, action: ScientistLabAction, *, authority_url: str,
              allowed_suites: frozenset[str], principal_id: str) -> None:
        request = task.request
        if (task.authority_url != authority_url or task.principal_id != principal_id
                or request.suite not in allowed_suites
                or action.task_id != request.external_task_id or action.run_id != request.external_run_id
                or action.runtime_id != task.binding.runtime_id or action.owner_lease_id != task.binding.lease_id
                or action.state_version != task.state_version or action.selected_option != action.tool
                or type(action.deadline) not in (int, float) or action.deadline <= time.time()):
            raise ScientistAdmissionError('Lab tool authority, scope, state or deadline differs')
        if action.tool == 'lab.start':
            expected = {'request_sha256': digest(request.model_dump(mode='json'))}
            if (task.binding.owner != 'AGENT' or task.lab_run_id is not None
                    or action.action_id != request.external_action_id
                    or action.idempotency_key != request.idempotency_key or action.arguments != expected):
                raise ScientistAdmissionError('Lab start does not match its exact task intent')
        elif task.lab_run_id is None or action.arguments != {'lab_run_id': task.lab_run_id}:
            raise ScientistAdmissionError('Lab control action does not match its bound remote run')


class ScientistLabUncertain(RuntimeError):
    pass


def _deny_authority(task: ScientistLabTask, action: ScientistLabAction) -> None:
    raise ScientistAdmissionError('Joint capability, principal and current task authority are not configured')


def _deny_effect(task: ScientistLabTask, action: ScientistLabAction, body: bytes) -> None:
    raise ScientistAdmissionError('Fresh human approval and durable Lab action intent are not configured')


def _object(data: bytes) -> dict:
    value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError('Lab API response must be one strict JSON object')
    return value


class ScientistLabClient:
    def __init__(self, base_url: str, token_file: Path, *, principal_id: str,
                 allowed_suites: frozenset[str], timeout_seconds: int = 3,
                 verify_authority: Callable[[ScientistLabTask, ScientistLabAction], None] = _deny_authority,
                 authorize_and_persist: Callable[[ScientistLabTask, ScientistLabAction, bytes], None] = _deny_effect):
        parsed = urlsplit(base_url)
        if (base_url != base_url.strip() or parsed.scheme != 'http'
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or parsed.hostname is None or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or not ipaddress.ip_address(parsed.hostname).is_loopback):
            raise ValueError('Lab API requires an explicit loopback HTTP authority and port')
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10:
            raise ValueError('Lab API control timeout must be an integer within 1..10 seconds')
        if (not principal_id or len(principal_id) > 128 or not isinstance(allowed_suites, frozenset)
                or not allowed_suites or not all(type(suite) is str for suite in allowed_suites)):
            raise ValueError('Lab API principal and explicit suite allowlist are required')
        self.host, self.port = parsed.hostname, parsed.port
        self.authority_url = base_url.rstrip('/')
        self.token_file = token_file
        self.principal_id = principal_id
        self.allowed_suites = allowed_suites
        self.timeout_seconds = timeout_seconds
        self.verify_authority = verify_authority
        self.authorize_and_persist = authorize_and_persist
        self._lock = threading.Lock()
        self._attempted: set[str] = set()
        self._uncertain_action_id: str | None = None
        self._async_active = None
        self._async_cancel_event = None
        self._closed = False

    @property
    def uncertain_action_id(self) -> str | None:
        return self._uncertain_action_id

    def _verify(self, task, action):
        ScientistLabPolicy.check(task, action, authority_url=self.authority_url,
                                 allowed_suites=self.allowed_suites, principal_id=self.principal_id)
        if self.verify_authority(task.model_copy(deep=True), action.model_copy(deep=True)) is not None:
            raise ScientistAdmissionError('Lab authority verifier must complete or raise')

    def _token(self) -> str:
        descriptor = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 1 <= info.st_size <= 4096):
                raise ScientistAdmissionError('Lab token must be a bounded private user-owned regular file')
            raw = os.read(descriptor, 4097)
            current = os.fstat(descriptor)
            stable = ('st_dev', 'st_ino', 'st_size', 'st_uid', 'st_mode', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')
            if (any(getattr(current, field) != getattr(info, field) for field in stable)
                    or self.token_file.lstat().st_ino != info.st_ino):
                raise ScientistAdmissionError('Lab token changed during read')
        finally:
            os.close(descriptor)
        token = raw.removesuffix(b'\n')
        if not token or len(token) > 4096 or any(character < 33 or character > 126 for character in token):
            raise ScientistAdmissionError('Lab token format is invalid')
        return token.decode('ascii')

    def _request(self, method: str, route: str, body: bytes, deadline: float, *, bound: int) -> bytes:
        cancelled = getattr(self, '_async_cancel_event', None)
        if cancelled is not None and cancelled.is_set():
            raise ScientistAdmissionError('Local Lab request was cancelled before dispatch')
        token = self._token()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Lab control deadline expired')
        connection = http.client.HTTPConnection(self.host, self.port, timeout=remaining)
        try:
            connection.request(method, route, body=body if method == 'POST' else None,
                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                         'Content-Type': 'application/json'})
            channel = connection.sock
            remaining = deadline - time.monotonic()
            if channel is None or remaining <= 0:
                raise TimeoutError('Lab request exceeded its control deadline')
            channel.settimeout(remaining)
            response = connection.getresponse()
            data = bytearray()
            while response.fp is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Lab readback exceeded its control deadline')
                channel.settimeout(remaining)
                chunk = response.read1(min(8192, bound + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > bound:
                    raise ValueError('Lab API response exceeds its bound')
            if len(data) > bound or response.status not in {200, 202}:
                raise ValueError('Lab API rejected the request or exceeded its response bound')
            if time.monotonic() >= deadline:
                raise TimeoutError('Lab control response exceeded deadline')
            _object(bytes(data))
            return bytes(data)
        finally:
            connection.close()

    def execute(self, task: ScientistLabTask, action: ScientistLabAction) -> ScientistLabHandle | ScientistRunStatus | ScientistReport:
        if self._closed:
            raise ScientistAdmissionError('Lab client is closed; no new controls are admitted')
        if self._async_active is not None:
            raise ScientistAdmissionError('Lab client has an active asynchronous control request')
        task = ScientistLabTask.model_validate(task.model_dump(), strict=True)
        action = ScientistLabAction.model_validate(action.model_dump(), strict=True)
        if not self._lock.acquire(blocking=False):
            raise ScientistAdmissionError('Lab client already has an active control request')
        attempted = False
        effect = action.tool in {'lab.start', 'lab.stop'}
        try:
            deadline = min(time.monotonic() + min(self.timeout_seconds, action.deadline - time.time()),
                           getattr(self, '_control_deadline', float('inf')))
            if effect and (self.uncertain_action_id is not None or action.action_id in self._attempted
                           or len(self._attempted) >= 256):
                raise ScientistAdmissionError('Lab uncertain effect or replay requires trusted reconciliation')
            self._verify(task, action)
            body = canonical(task.request.model_dump(mode='json')).encode() if action.tool == 'lab.start' else b'{}'
            if effect:
                self._token()
                if self.authorize_and_persist(task.model_copy(deep=True), action.model_copy(deep=True), body) is not None:
                    raise ScientistAdmissionError('Lab approval/intent writer must durably complete or raise')
                self._verify(task, action)
                self._attempted.add(action.action_id)
                attempted = True
            remote = task.lab_run_id
            if action.tool == 'lab.start':
                raw = self._request('POST', '/v1/runs', body, deadline, bound=65536)
                result = ScientistLabHandle.model_validate(_object(raw), strict=True)
                self._verify(task, action)
                readback = self._request('GET', f'/v1/runs/{result.run_id}', b'', deadline, bound=65536)
                observed = ScientistRunStatus.model_validate(_object(readback), strict=True)
                if (observed.run_id != result.run_id or observed.purpose == 'baseline'
                        or result.state in {'completed', 'stopped', 'failed'} and observed.state != result.state):
                    raise ValueError('Lab start acknowledgment failed independent owner-visible readback')
                result = ScientistLabHandle(run_id=result.run_id, state=observed.state, reused=result.reused)
            elif action.tool == 'lab.stop':
                raw = self._request('POST', f'/v1/runs/{remote}/stop', body, deadline, bound=65536)
                result = ScientistRunStatus.model_validate(_object(raw), strict=True)
                if (result.run_id != remote or result.purpose == 'baseline'
                        or not result.stop_requested and result.state not in {'completed', 'stopped', 'failed'}):
                    raise ValueError('Lab stop acknowledgment does not confirm a stop request or terminal state')
                self._verify(task, action)
                readback = self._request('GET', f'/v1/runs/{remote}', b'', deadline, bound=65536)
                observed = ScientistRunStatus.model_validate(_object(readback), strict=True)
                if (observed.run_id != remote or observed.purpose == 'baseline'
                        or not observed.stop_requested and observed.state not in {'completed', 'stopped', 'failed'}
                        or result.state in {'completed', 'stopped', 'failed'} and observed.state != result.state):
                    raise ValueError('Lab stop failed independent stop-request or terminal readback')
                result = observed
            elif action.tool == 'lab.status':
                raw = self._request('GET', f'/v1/runs/{remote}', body, deadline, bound=65536)
                result = ScientistRunStatus.model_validate(_object(raw), strict=True)
            else:
                before = self._request('GET', f'/v1/runs/{remote}', b'', deadline, bound=65536)
                status = ScientistRunStatus.model_validate(_object(before), strict=True)
                if status.purpose == 'baseline':
                    raise ValueError('Lab research tool cannot adopt a baseline report')
                if status.run_id != remote or status.state not in {'completed', 'stopped', 'failed'}:
                    raise ValueError('Lab report requires independently observed terminal status')
                self._verify(task, action)
                raw = self._request('GET', f'/v1/runs/{remote}/report', b'', deadline, bound=262144)
                result = verify_scientist_report(raw, status=status, expected_run_id=remote)
                self._verify(task, action)
                after = self._request('GET', f'/v1/runs/{remote}', b'', deadline, bound=65536)
                if ScientistRunStatus.model_validate(_object(after), strict=True) != status:
                    raise ValueError('Lab status changed during independent report verification')
            if remote is not None and result.run_id != remote:
                raise ValueError('Lab response belongs to another remote run')
            if isinstance(result, ScientistRunStatus) and result.purpose == 'baseline':
                raise ValueError('Lab research tool cannot adopt a baseline operation')
            self._verify(task, action)
            return result
        except BaseException as error:
            if attempted:
                self._uncertain_action_id = action.action_id
                if isinstance(error, Exception):
                    raise ScientistLabUncertain('Lab effect outcome is uncertain; do not repeat with a new action or key') from error
            raise
        finally:
            self._lock.release()

    @property
    def local_cleanup_pending(self):
        return self._async_active is not None and not self._async_active.done()

    async def execute_async(self, task, action):
        if self._closed or self._async_active is not None or self._lock.locked():
            raise ScientistAdmissionError('Lab client already has an active control request')
        task = ScientistLabTask.model_validate(task.model_dump(), strict=True)
        action = ScientistLabAction.model_validate(action.model_dump(), strict=True)
        cancelled = threading.Event()
        self._async_cancel_event = cancelled
        deadline = time.monotonic() + min(self.timeout_seconds, action.deadline - time.time())
        bridge = ScientistHostBridge(asyncio.get_running_loop(), cancelled, deadline)
        worker = copy(self)
        worker._control_deadline = deadline
        worker.verify_authority = lambda *arguments: bridge.invoke(self.verify_authority, *arguments)
        worker.authorize_and_persist = lambda *arguments: bridge.invoke(self.authorize_and_persist, *arguments)
        pending = asyncio.create_task(asyncio.to_thread(worker.execute, task, action))
        self._async_active = pending

        def completed(finished):
            if worker.uncertain_action_id is not None:
                self._uncertain_action_id = worker.uncertain_action_id
            if not finished.cancelled():
                finished.exception()
            if self._async_active is finished:
                self._async_active = None
                self._async_cancel_event = None

        pending.add_done_callback(completed)
        try:
            await asyncio.wait({pending})
            return pending.result()
        except asyncio.CancelledError:
            cancelled.set()
            if action.tool in {'lab.start', 'lab.stop'}:
                self._uncertain_action_id = action.action_id
            raise

    async def close_async(self, *, timeout_seconds=10):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10:
            raise ValueError('Local Lab cleanup timeout must be within 1..10 seconds')
        self._closed = True
        if self._async_cancel_event is not None:
            self._async_cancel_event.set()
        deadline = time.monotonic() + timeout_seconds
        pending = self._async_active
        if pending is not None:
            await asyncio.wait({pending}, timeout=max(0, deadline - time.monotonic()))
        while self._lock.locked() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        if self.local_cleanup_pending or self._lock.locked():
            raise ScientistAdmissionError('Local Lab HTTP cleanup is not proven; client remains closed')
