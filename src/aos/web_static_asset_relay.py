"""One-use host relay for an exact HTTPS document and static assets."""

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
from .web_application import Checksum, WebApplicationProfiles
from .web_application_binding import WebTaskContract
from .web_https_preflight import _fetch
from .web_static_assets import WebStaticAssetPlan, fetch_web_static_asset
from .web_readonly_data import (WebReadOnlyDataBundlePlan, fetch_web_readonly_bundle_asset,
                                fetch_web_readonly_data,
                                verify_web_bundle_plan)


MAX_BUNDLE_ATTEMPTS = 32
MAX_BUNDLE_WAIT_SECONDS = 30
MAX_BUNDLE_REQUEST_BYTES = 4096
MAX_BUNDLE_REPLY_BYTES = 1500000


class WebStaticBundleRelayReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['one_shot_host_https_static_bundle_relay'] = 'one_shot_host_https_static_bundle_relay'
    status: Literal['bundle_served'] = 'bundle_served'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    entry_response_sha256: Checksum
    asset_response_sha256: list[Checksum] = Field(min_length=1, max_length=8)
    request_attempts: int = Field(ge=2, le=MAX_BUNDLE_ATTEMPTS)
    browser_identity_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class WebReadOnlyDataBundleRelayReport(TypedModel):
    schema_version: Literal['2.0'] = '2.0'
    mode: Literal['one_shot_host_https_readonly_data_bundle_relay'] = 'one_shot_host_https_readonly_data_bundle_relay'
    status: Literal['bundle_served'] = 'bundle_served'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    entry_response_sha256: Checksum
    asset_response_sha256: list[Checksum] = Field(min_length=1, max_length=8)
    data_response_sha256: list[Checksum] = Field(min_length=1, max_length=4)
    request_attempts: int = Field(ge=3, le=MAX_BUNDLE_ATTEMPTS)
    browser_identity_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class ExactStaticBundleRelay:
    def __init__(self, profiles: WebApplicationProfiles, task: WebTaskContract,
                 plan: WebStaticAssetPlan | WebReadOnlyDataBundlePlan,
                 confirm_plan_sha256: str, *,
                 tls_context: ssl.SSLContext | None = None):
        checked = verify_web_bundle_plan(profiles, task, plan)
        plan_sha256 = digest(checked.model_dump())
        if not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256:
            raise ValueError('static_bundle_requires_exact_plan_confirmation')
        self.profiles = profiles
        self.task = WebTaskContract.model_validate(task.model_dump())
        self.plan = checked
        self.plan_sha256 = plan_sha256
        self.entry_url = self.task.entry_url
        self.tls_context = tls_context
        self.client_token = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._armed = False
        self._served = False
        self._entry_used = False
        self._asset_reports = {}
        self._data_reports = {}

    @staticmethod
    def _verify_plan(profiles, task, plan):
        return verify_web_bundle_plan(profiles, task, plan)

    def arm(self) -> None:
        self._verify_plan(self.profiles, self.task, self.plan)
        with self._lock:
            if self._armed or self._entry_used:
                raise ValueError('static_bundle_already_armed')
            self._armed = True

    def _request(self, connection: socket.socket, deadline: float) -> int | None:
        content = bytearray()
        while len(content) <= MAX_BUNDLE_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('static_bundle_request_deadline')
            connection.settimeout(min(5, remaining))
            chunk = connection.recv(1)
            if not chunk:
                break
            content.extend(chunk)
            if chunk == b'\n':
                break
        if len(content) > MAX_BUNDLE_REQUEST_BYTES or not content.endswith(b'\n'):
            raise ValueError('static_bundle_request_limit')

        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('static_bundle_duplicate_request_field')
                value[key] = item
            return value

        request = json.loads(content, object_pairs_hook=unique_pairs)
        if (not isinstance(request, dict)
                or set(request) != {'method', 'url', 'plan_sha256', 'client_token', 'asset_index'}
                or request['method'] != 'GET'
                or request['plan_sha256'] != self.plan_sha256
                or not isinstance(request['client_token'], str)
                or re.fullmatch('[a-f0-9]{64}', request['client_token']) is None
                or not hmac.compare_digest(request['client_token'], self.client_token)):
            raise ValueError('static_bundle_request_outside_plan')
        asset_index = request['asset_index']
        if asset_index is None:
            if request['url'] != self.entry_url:
                raise ValueError('static_bundle_entry_url_changed')
        elif type(asset_index) is not int:
            raise ValueError('static_bundle_asset_url_changed')
        elif 0 <= asset_index < len(self.plan.assets):
            if request['url'] != self.plan.assets[asset_index].url:
                raise ValueError('static_bundle_asset_url_changed')
        elif (isinstance(self.plan, WebReadOnlyDataBundlePlan)
              and len(self.plan.assets) <= asset_index
              < len(self.plan.assets) + len(self.plan.data_resources)):
            if request['url'] != self.plan.data_resources[asset_index - len(self.plan.assets)].url:
                raise ValueError('static_bundle_data_url_changed')
        else:
            raise ValueError('static_bundle_asset_url_changed')
        return asset_index

    def _reply(self, connection: socket.socket, payload: dict) -> None:
        encoded = (canonical(payload) + '\n').encode()
        if len(encoded) > MAX_BUNDLE_REPLY_BYTES:
            raise ValueError('static_bundle_reply_limit')
        connection.sendall(encoded)

    def serve(self, socket_path: Path, *, wait_seconds: float = MAX_BUNDLE_WAIT_SECONDS,
              stop_event: threading.Event | None = None
              ) -> WebStaticBundleRelayReport | WebReadOnlyDataBundleRelayReport:
        socket_path = Path(socket_path).absolute()
        if (not socket_path.parent.is_relative_to(REPO_ROOT / 'data')
                or socket_path.name in {'', '.', '..'} or isinstance(wait_seconds, bool)
                or not 0 < wait_seconds <= MAX_BUNDLE_WAIT_SECONDS):
            raise ValueError('static_bundle_private_data_socket_required')
        with self._lock:
            if self._served:
                raise ValueError('static_bundle_is_one_shot')
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
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (metadata.st_dev, metadata.st_ino) != identity):
                raise ValueError('static_bundle_socket_identity')
            listener.listen(1)
            deadline = time.monotonic() + wait_seconds
            attempts = 0
            while attempts < MAX_BUNDLE_ATTEMPTS:
                if stop_event is not None and stop_event.is_set():
                    raise TimeoutError('static_bundle_stopped')
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
                        asset_index = self._request(connection, deadline)
                        with self._lock:
                            data_index = (asset_index - len(self.plan.assets)
                                          if asset_index is not None
                                          and asset_index >= len(self.plan.assets) else None)
                            if (not self._armed or asset_index is None and self._entry_used
                                    or asset_index is not None
                                    and (not self._entry_used
                                         or data_index is None and asset_index in self._asset_reports
                                         or data_index is not None and data_index in self._data_reports)):
                                raise ValueError('static_bundle_request_not_armed_or_replayed')
                            if asset_index is None:
                                self._entry_used = True
                            elif data_index is not None:
                                self._data_reports[data_index] = None
                            else:
                                self._asset_reports[asset_index] = None
                    except (OSError, ValueError, TypeError, UnicodeError):
                        try:
                            self._reply(connection, {'error': 'denied'})
                        except OSError:
                            pass
                        continue
                    try:
                        self._verify_plan(self.profiles, self.task, self.plan)
                        if asset_index is None:
                            report, body = _fetch(self.profiles, self.plan.profile_sha256,
                                                  self.plan.profile_sha256,
                                                  tls_context=self.tls_context,
                                                  target_url=self.entry_url)
                            content_type = 'text/html'
                            entry_response_sha256 = report.response_sha256
                        elif data_index is None:
                            fetch_asset = (fetch_web_readonly_bundle_asset
                                           if isinstance(self.plan, WebReadOnlyDataBundlePlan)
                                           else fetch_web_static_asset)
                            report, body = fetch_asset(
                                self.profiles, self.task, self.plan, self.plan_sha256,
                                asset_index, tls_context=self.tls_context)
                            content_type = report.content_type
                            self._asset_reports[asset_index] = report
                        else:
                            report, body = fetch_web_readonly_data(
                                self.profiles, self.task, self.plan, self.plan_sha256,
                                data_index, tls_context=self.tls_context)
                            content_type = report.content_type
                            self._data_reports[data_index] = report
                        self._reply(connection, {'status': 200, 'content_type': content_type,
                                                 'asset_index': asset_index,
                                                 'body_base64': base64.b64encode(body).decode(),
                                                 'response_sha256': report.response_sha256})
                    except (OSError, ValueError, TypeError, UnicodeError, TimeoutError,
                            ssl.SSLError, http.client.HTTPException):
                        try:
                            self._reply(connection, {'error': 'fetch_failed'})
                        except OSError:
                            pass
                        raise
                if (self._entry_used and len(self._asset_reports) == len(self.plan.assets)
                        and (not isinstance(self.plan, WebReadOnlyDataBundlePlan)
                             or len(self._data_reports) == len(self.plan.data_resources))):
                    values = dict(profile_sha256=self.plan.profile_sha256,
                                  task_sha256=self.plan.task_sha256,
                                  plan_sha256=self.plan_sha256,
                                  entry_response_sha256=entry_response_sha256,
                                  asset_response_sha256=[self._asset_reports[index].response_sha256
                                                         for index in range(len(self.plan.assets))],
                                  request_attempts=attempts)
                    if isinstance(self.plan, WebReadOnlyDataBundlePlan):
                        return WebReadOnlyDataBundleRelayReport(
                            **values,
                            data_response_sha256=[self._data_reports[index].response_sha256
                                                  for index in range(len(self.plan.data_resources))])
                    return WebStaticBundleRelayReport(**values)
            raise TimeoutError('static_bundle_expired_incomplete')
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
