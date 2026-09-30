"""One-shot host HTTPS entry relay for an explicitly confirmed task draft."""

import base64
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import threading
import time
from typing import Literal

from pydantic import Field

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .lifecycle import private_directory
from .web_application import Checksum, WebApplicationProfiles, canonical_origin
from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskAdmissionDraft,
                                      WebTaskContract, verify_web_readonly_routes,
                                      verify_web_task_binding)
from .web_https_preflight import (WebHTTPSReadOnlyRouteReport, _fetch,
                                  fetch_web_readonly_route)


MAX_REQUEST_BYTES = 4096
MAX_ATTEMPTS = 8
MAX_WAIT_SECONDS = 30


class WebHTTPSRelayReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['one_shot_host_https_entry_relay'] = 'one_shot_host_https_entry_relay'
    status: Literal['entry_response_served'] = 'entry_response_served'
    profile_sha256: Checksum
    binding_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=65536)
    request_attempts: int = Field(ge=1, le=MAX_ATTEMPTS)
    browser_identity_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class ExactEntryRelay:
    def __init__(self, profiles: WebApplicationProfiles, profile_sha256: str,
                 confirm_sha256: str, draft: WebTaskAdmissionDraft,
                 confirm_binding_sha256: str, *, tls_context: ssl.SSLContext | None = None):
        if (not isinstance(draft, WebTaskAdmissionDraft)
                or not isinstance(confirm_binding_sha256, str)
                or confirm_binding_sha256 != draft.binding_sha256
                or profile_sha256 != confirm_sha256):
            raise ValueError('https_relay_requires_exact_confirmation')
        checked = verify_web_task_binding(profiles, draft)
        profile = profiles.get(profile_sha256)
        origin, local = canonical_origin(profile.entry_url)
        if (checked.profile_sha256 != profile_sha256
                or 'browser.navigate' not in checked.task.tools
                or profile.environment not in {'staging', 'production'}
                or local or not origin.startswith('https://')
                or checked.task.entry_url != profile.entry_url):
            raise ValueError('https_relay_requires_remote_entry_task')
        self.profiles = profiles
        self.profile_sha256 = profile_sha256
        self.draft = checked
        self.entry_url = profile.entry_url
        self.tls_context = tls_context
        self.client_token = secrets.token_hex(32)
        self._serve_lock = threading.Lock()
        self._served = False

    def _reply(self, connection: socket.socket, payload: dict) -> None:
        encoded = (canonical(payload) + '\n').encode()
        if len(encoded) > 100000:
            raise ValueError('https_relay_reply_limit')
        connection.sendall(encoded)

    def _request(self, connection: socket.socket, deadline: float) -> dict:
        content = bytearray()
        while len(content) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('https_relay_request_deadline')
            connection.settimeout(min(5, remaining))
            chunk = connection.recv(1)
            if not chunk:
                break
            content.extend(chunk)
            if chunk == b'\n':
                break
        if len(content) > MAX_REQUEST_BYTES or not content.endswith(b'\n'):
            raise ValueError('https_relay_request_limit')
        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('https_relay_duplicate_request_field')
                value[key] = item
            return value
        request = json.loads(content, object_pairs_hook=unique_pairs)
        if (not isinstance(request, dict)
                or set(request) != {'method', 'url', 'binding_sha256', 'client_token'}
                or request['method'] != 'GET'
                or request['url'] != self.entry_url
                or request['binding_sha256'] != self.draft.binding_sha256
                or not isinstance(request['client_token'], str)
                or re.fullmatch('[a-f0-9]{64}', request['client_token']) is None
                or not hmac.compare_digest(request['client_token'], self.client_token)):
            raise ValueError('https_relay_request_outside_entry')
        return request

    def serve(self, socket_path: Path, *, wait_seconds: float = MAX_WAIT_SECONDS,
              stop_event: threading.Event | None = None) -> WebHTTPSRelayReport:
        socket_path = Path(socket_path).absolute()
        if (not socket_path.parent.is_relative_to(REPO_ROOT / 'data')
                or socket_path.name in {'', '.', '..'}
                or isinstance(wait_seconds, bool)
                or not 0 < wait_seconds <= MAX_WAIT_SECONDS):
            raise ValueError('https_relay_private_data_socket_required')
        with self._serve_lock:
            if self._served:
                raise ValueError('https_relay_is_one_shot')
            self._served = True
        directory = private_directory(socket_path.parent)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            listener.bind(str(socket_path))
            metadata = socket_path.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(socket_path, 0o600, follow_symlinks=False)
            metadata = socket_path.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError('https_relay_socket_identity')
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise ValueError('https_relay_socket_changed')
            listener.listen(1)
            deadline = time.monotonic() + wait_seconds
            attempts = 0
            while attempts < MAX_ATTEMPTS:
                if stop_event is not None and stop_event.is_set():
                    raise TimeoutError('https_relay_stopped')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                listener.settimeout(min(remaining, 0.25) if stop_event is not None else remaining)
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue
                attempts += 1
                with connection:
                    try:
                        self._request(connection, deadline)
                    except (OSError, ValueError, TypeError, UnicodeError):
                        try:
                            self._reply(connection, {'error': 'denied'})
                        except OSError:
                            pass
                        continue
                    try:
                        verify_web_task_binding(self.profiles, self.draft)
                        report, body = _fetch(self.profiles, self.profile_sha256,
                                              self.profile_sha256, tls_context=self.tls_context)
                        if self.profiles.get(self.profile_sha256).entry_url != self.entry_url:
                            raise ValueError('https_relay_profile_changed')
                        self._reply(connection, {'status': 200, 'content_type': 'text/html',
                                                 'body_base64': base64.b64encode(body).decode(),
                                                 'response_sha256': report.response_sha256})
                    except (OSError, ValueError, TypeError, UnicodeError, TimeoutError,
                            ssl.SSLError, http.client.HTTPException):
                        try:
                            self._reply(connection, {'error': 'fetch_failed'})
                        except OSError:
                            pass
                        raise
                    return WebHTTPSRelayReport(profile_sha256=self.profile_sha256,
                                               binding_sha256=self.draft.binding_sha256,
                                               response_sha256=report.response_sha256,
                                               response_bytes=report.response_bytes,
                                               request_attempts=attempts)
            raise TimeoutError('https_relay_expired_without_entry')
        finally:
            listener.close()
            if identity is not None:
                try:
                    current = socket_path.lstat()
                    if (current.st_dev, current.st_ino) == identity:
                        os.unlink(socket_path)
                except FileNotFoundError:
                    pass
            os.close(directory)


class ExactReadOnlyRouteRelay:
    def __init__(self, profiles: WebApplicationProfiles, task: WebTaskContract,
                 plan: WebReadOnlyRoutePlan, confirm_plan_sha256: str, *,
                 tls_context: ssl.SSLContext | None = None):
        checked = verify_web_readonly_routes(profiles, task, plan)
        plan_sha256 = digest(checked.model_dump())
        if not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256:
            raise ValueError('readonly_relay_requires_exact_plan_confirmation')
        self.profiles = profiles
        self.task = WebTaskContract.model_validate(task.model_dump())
        self.plan = checked
        self.plan_sha256 = plan_sha256
        self.tls_context = tls_context
        self.client_token = secrets.token_hex(32)
        self._condition = threading.Condition()
        self._armed_index = None
        self._inflight = False
        self._failed = False
        self._started = False
        self._reports: list[WebHTTPSReadOnlyRouteReport] = []

    @property
    def reports(self) -> tuple[WebHTTPSReadOnlyRouteReport, ...]:
        with self._condition:
            return tuple(self._reports)

    def arm(self, route_index: int) -> None:
        verify_web_readonly_routes(self.profiles, self.task, self.plan)
        with self._condition:
            if (type(route_index) is not int or route_index != len(self._reports)
                    or route_index >= len(self.plan.routes) or self._armed_index is not None
                    or self._inflight or self._failed):
                raise ValueError('readonly_relay_requires_next_exact_route')
            self._armed_index = route_index

    def wait_for_route(self, route_index: int, timeout: float = 12) -> WebHTTPSReadOnlyRouteReport:
        if (type(route_index) is not int or not 0 <= route_index < len(self.plan.routes)
                or isinstance(timeout, bool) or not 0 < timeout <= 30):
            raise ValueError('readonly_relay_invalid_wait')
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._reports) <= route_index and not self._failed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if len(self._reports) <= route_index:
                raise TimeoutError('readonly_relay_route_not_served')
            return self._reports[route_index]

    def _request(self, connection: socket.socket, deadline: float) -> dict:
        content = bytearray()
        while len(content) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('readonly_relay_request_deadline')
            connection.settimeout(min(5, remaining))
            chunk = connection.recv(1)
            if not chunk:
                break
            content.extend(chunk)
            if chunk == b'\n':
                break
        if len(content) > MAX_REQUEST_BYTES or not content.endswith(b'\n'):
            raise ValueError('readonly_relay_request_limit')
        def unique_pairs(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError('readonly_relay_duplicate_request_field')
                values[key] = value
            return values
        request = json.loads(content, object_pairs_hook=unique_pairs)
        if (not isinstance(request, dict)
                or set(request) != {'method', 'url', 'plan_sha256', 'client_token'}
                or request['method'] != 'GET'
                or not isinstance(request['url'], str)
                or request['plan_sha256'] != self.plan_sha256
                or not isinstance(request['client_token'], str)
                or re.fullmatch('[a-f0-9]{64}', request['client_token']) is None
                or not hmac.compare_digest(request['client_token'], self.client_token)):
            raise ValueError('readonly_relay_request_outside_plan')
        return request

    def serve(self, socket_path: Path, *, wait_seconds: float = MAX_WAIT_SECONDS,
              stop_event: threading.Event | None = None) -> tuple[WebHTTPSReadOnlyRouteReport, ...]:
        socket_path = Path(socket_path).absolute()
        if (not socket_path.parent.is_relative_to(REPO_ROOT / 'data')
                or socket_path.name in {'', '.', '..'}
                or isinstance(wait_seconds, bool)
                or not 0 < wait_seconds <= self.task.max_seconds):
            raise ValueError('readonly_relay_private_data_socket_required')
        with self._condition:
            if self._started:
                raise ValueError('readonly_relay_cannot_restart')
            self._started = True
        directory = private_directory(socket_path.parent)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            listener.bind(str(socket_path))
            metadata = socket_path.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(socket_path, 0o600, follow_symlinks=False)
            metadata = socket_path.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (metadata.st_dev, metadata.st_ino) != identity):
                raise ValueError('readonly_relay_socket_identity')
            listener.listen(1)
            deadline = time.monotonic() + wait_seconds
            attempts = 0
            while attempts < MAX_ATTEMPTS * len(self.plan.routes):
                if stop_event is not None and stop_event.is_set():
                    raise TimeoutError('readonly_relay_stopped')
                with self._condition:
                    if len(self._reports) == len(self.plan.routes):
                        return tuple(self._reports)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                listener.settimeout(min(remaining, 0.25) if stop_event is not None else remaining)
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue
                attempts += 1
                with connection:
                    try:
                        request = self._request(connection, deadline)
                    except (OSError, ValueError, TypeError, UnicodeError):
                        try:
                            connection.sendall(b'{"error":"denied"}\n')
                        except OSError:
                            pass
                        continue
                    with self._condition:
                        route_index = self._armed_index
                        allowed = (route_index is not None and not self._inflight
                                   and route_index == len(self._reports)
                                   and request['url'] == self.plan.routes[route_index])
                        if allowed:
                            self._armed_index = None
                            self._inflight = True
                    if not allowed:
                        try:
                            connection.sendall(b'{"error":"denied"}\n')
                        except OSError:
                            pass
                        continue
                    try:
                        report, body = fetch_web_readonly_route(
                            self.profiles, self.task, self.plan, self.plan_sha256,
                            route_index, tls_context=self.tls_context)
                        encoded = (canonical({'status': 200, 'content_type': 'text/html',
                                              'body_base64': base64.b64encode(body).decode(),
                                              'response_sha256': report.response_sha256}) + '\n').encode()
                        if len(encoded) > 100000:
                            raise ValueError('readonly_relay_reply_limit')
                        connection.sendall(encoded)
                        with self._condition:
                            self._reports.append(report)
                            self._inflight = False
                            self._condition.notify_all()
                    except (OSError, ValueError, TypeError, UnicodeError, TimeoutError,
                            ssl.SSLError, http.client.HTTPException):
                        with self._condition:
                            self._failed = True
                            self._inflight = False
                            self._condition.notify_all()
                        try:
                            connection.sendall(b'{"error":"fetch_failed"}\n')
                        except OSError:
                            pass
                        raise
            raise TimeoutError('readonly_relay_expired_without_complete_plan')
        finally:
            with self._condition:
                if len(self._reports) != len(self.plan.routes):
                    self._failed = True
                    self._condition.notify_all()
            listener.close()
            if identity is not None:
                try:
                    current = socket_path.lstat()
                    if (current.st_dev, current.st_ino) == identity:
                        os.unlink(socket_path)
                except FileNotFoundError:
                    pass
            os.close(directory)
