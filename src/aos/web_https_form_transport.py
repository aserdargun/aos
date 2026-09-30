"""Bounded host HTTPS form transport; the caller owns the fresh approval gate."""

import base64
import hashlib
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
from typing import Callable, Literal
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import Field, field_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .lifecycle import private_directory
from .web_application import Checksum, WebApplicationProfile, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract
from .web_https_preflight import (DeadlineSocket, _bounded_public_addresses, _fetch,
                                  _remaining, validate_https_cookie_header)


MAX_FORM_BYTES = 4096
MAX_SOCKET_REQUEST_BYTES = 8192
MAX_SOCKET_ATTEMPTS = 24
FIELD_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')


def exact_form_fields(field_name: str | None, value: str | None,
                      fields: list[dict] | tuple[tuple[str, str], ...] | None = None
                      ) -> tuple[tuple[str, str], ...]:
    multi_field = fields is not None
    if fields is None:
        fields = ((field_name, value),)
    elif field_name is not None or value is not None or not isinstance(fields, (list, tuple)):
        raise ValueError('https_form_field_mode_conflict')
    normalized = []
    if not (2 if multi_field else 1) <= len(fields) <= 8:
        raise ValueError('https_form_field_count_invalid')
    for field in fields:
        if isinstance(field, dict) and set(field) == {'name', 'value'}:
            name, content = field['name'], field['value']
        elif isinstance(field, tuple) and len(field) == 2:
            name, content = field
        else:
            raise ValueError('https_form_field_shape_invalid')
        if (not isinstance(name, str) or FIELD_NAME.fullmatch(name) is None
                or not isinstance(content, str) or not 0 < len(content) <= 2048):
            raise ValueError('https_form_field_invalid')
        normalized.append((name, content))
    if len({name for name, _content in normalized}) != len(normalized):
        raise ValueError('https_form_field_duplicate')
    if len(urlencode(normalized).encode()) > MAX_FORM_BYTES:
        raise ValueError('https_form_body_too_large')
    return tuple(normalized)


def form_body(fields: tuple[tuple[str, str], ...]) -> bytes:
    return urlencode(fields).encode()


def parse_form_fields_document(content: bytes) -> tuple[tuple[str, str], ...]:
    if not isinstance(content, bytes) or len(content) > 8192:
        raise ValueError('https_form_fields_document_invalid')
    try:
        document = json.loads(content)
    except (UnicodeError, ValueError, TypeError) as error:
        raise ValueError('https_form_fields_document_invalid') from error
    if (not isinstance(document, dict) or set(document) != {
            'schema_version', 'fields', 'body_sha256', 'body_bytes',
            'execution_authorized', 'collection_authorized'}
            or document['schema_version'] != '1.0'
            or document['execution_authorized'] is not False
            or document['collection_authorized'] is not False
            or not isinstance(document['fields'], list)
            or type(document['body_bytes']) is not int
            or not isinstance(document['body_sha256'], str)
            or re.fullmatch('[a-f0-9]{64}', document['body_sha256']) is None
            or canonical(document).encode() != content):
        raise ValueError('https_form_fields_document_invalid')
    fields = exact_form_fields(None, None, document['fields'])
    body = form_body(fields)
    if (len(body) != document['body_bytes']
            or hashlib.sha256(body).hexdigest() != document['body_sha256']
            or any(not value.isprintable() for _name, value in fields)):
        raise ValueError('https_form_fields_document_body_invalid')
    return fields


class WebHTTPSFormPlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    entry_url: str = Field(min_length=1, max_length=2048)
    submit_url: str = Field(min_length=1, max_length=2048)
    receipt_url: str = Field(min_length=1, max_length=2048)
    body_sha256: Checksum
    body_bytes: int = Field(ge=1, le=MAX_FORM_BYTES)
    content_type: Literal['application/x-www-form-urlencoded'] = 'application/x-www-form-urlencoded'
    approval: Literal['fresh_per_action'] = 'fresh_per_action'
    status: Literal['draft'] = 'draft'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('entry_url', 'submit_url', 'receipt_url')
    @classmethod
    def canonical_url(cls, value):
        WebApplicationProfile.entry_is_canonical(value)
        return value

    @field_validator('execution_authorized', 'collection_authorized', mode='before')
    @classmethod
    def cannot_authorize(cls, value):
        if value is not False:
            raise ValueError('form_plan_cannot_authorize')
        return value


class WebHTTPSFormTransportReport(TypedModel):
    schema_version: Literal['1.1'] = '1.1'
    mode: Literal['bounded_host_https_form_transport'] = 'bounded_host_https_form_transport'
    status: Literal['receipt_response_read'] = 'receipt_response_read'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    entry_response_sha256: Checksum
    submit_request_sha256: Checksum
    submit_response_status: Literal[302, 303]
    receipt_url_sha256: Checksum
    receipt_response_sha256: Checksum
    receipt_bytes: int = Field(ge=0, le=65536)
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def plan_web_https_form(profiles: WebApplicationProfiles, task: WebTaskContract, *,
                        submit_url: str, receipt_url: str, body_sha256: str,
                        body_bytes: int) -> WebHTTPSFormPlan:
    task = WebTaskContract.model_validate(task.model_dump())
    profile = profiles.get(task.profile_sha256)
    plan = WebHTTPSFormPlan(profile_sha256=task.profile_sha256,
                            task_sha256=digest(task.model_dump()), entry_url=task.entry_url,
                            submit_url=submit_url, receipt_url=receipt_url,
                            body_sha256=body_sha256, body_bytes=body_bytes)
    origin, local = canonical_origin(task.entry_url)
    if (profile.environment not in {'staging', 'production'} or local
            or task.task_key not in profile.task_keys or task.entry_url != profile.entry_url
            or origin not in task.allowed_origins
            or not set(task.allowed_origins).issubset(profile.allowed_origins)
            or not {'browser.navigate', 'browser.fill', 'browser.click'}.issubset(task.tools)
            or task.max_pages < 2 or task.max_navigations < 2 or task.max_actions < 4
            or submit_url == task.entry_url or receipt_url == submit_url
            or any(canonical_origin(url)[0] != origin for url in (submit_url, receipt_url))):
        raise ValueError('https_form_outside_confirmed_task')
    return plan


def verify_web_https_form_plan(profiles: WebApplicationProfiles, task: WebTaskContract,
                               plan: WebHTTPSFormPlan) -> WebHTTPSFormPlan:
    checked = WebHTTPSFormPlan.model_validate(plan.model_dump())
    if plan_web_https_form(profiles, task, submit_url=checked.submit_url,
                           receipt_url=checked.receipt_url, body_sha256=checked.body_sha256,
                           body_bytes=checked.body_bytes) != checked:
        raise ValueError('https_form_plan_binding_changed')
    return checked


def form_stage_arguments(plan: WebHTTPSFormPlan, binding_sha256: str,
                         field_name: str | tuple[str, ...], stage: int,
                         cookie_sha256: str | None = None) -> dict:
    if (type(stage) is not int or stage not in range(5)
            or isinstance(field_name, str) and FIELD_NAME.fullmatch(field_name) is None
            or isinstance(field_name, tuple) and (not 2 <= len(field_name) <= 8
                or len(set(field_name)) != len(field_name)
                or any(not isinstance(name, str) or FIELD_NAME.fullmatch(name) is None
                       for name in field_name))
            or not isinstance(field_name, (str, tuple))):
        raise ValueError('https_form_invalid_action_stage')
    url = (plan.entry_url if stage in (0, 1) else
           plan.submit_url if stage == 2 else plan.receipt_url)
    arguments = {'profile_sha256': plan.profile_sha256,
                 'binding_sha256': binding_sha256,
                 'plan_sha256': digest(plan.model_dump()),
                 'stage': stage, 'url': url}
    if cookie_sha256 is not None:
        if not isinstance(cookie_sha256, str) or re.fullmatch('[a-f0-9]{64}', cookie_sha256) is None:
            raise ValueError('https_form_invalid_cookie_hash')
        arguments['cookie_sha256'] = cookie_sha256
    if stage in (1, 2):
        if isinstance(field_name, str):
            arguments['field_name'] = field_name
        else:
            arguments['field_names'] = ','.join(field_name)
    if stage == 2:
        arguments['body_sha256'] = plan.body_sha256
    return arguments


def verify_web_https_form_target_grant(plan: WebHTTPSFormPlan,
                                       confirm_public_plan_sha256: str | None,
                                       owned_form_target=None) -> bool:
    synthetic = urlsplit(plan.entry_url).hostname.endswith('.invalid')
    if owned_form_target is not None:
        owned_form_target.assert_plan(plan.profile_sha256, digest(plan.model_dump()))
        if (not synthetic or confirm_public_plan_sha256 is not None
                or (plan.entry_url, plan.submit_url, plan.receipt_url) != (
                    owned_form_target.entry_url, owned_form_target.submit_url,
                    owned_form_target.receipt_url)):
            raise ValueError('owned_form_fixture_requires_exact_synthetic_plan')
        return False
    if ((confirm_public_plan_sha256 is None) != synthetic
            or confirm_public_plan_sha256 is not None
            and confirm_public_plan_sha256 != digest(plan.model_dump())):
        raise ValueError('https_form_public_target_requires_exact_plan_grant')
    return not synthetic


class ExactHTTPSFormTransport:
    def __init__(self, profiles: WebApplicationProfiles, task: WebTaskContract,
                 plan: WebHTTPSFormPlan, confirm_plan_sha256: str, *,
                 consume_approval: Callable[[str], bool],
                 tls_context: ssl.SSLContext | None = None,
                 confirm_public_plan_sha256: str | None = None,
                 cookie_header: str | None = None,
                 confirm_cookie_sha256: str | None = None,
                 owned_form_target=None):
        checked = verify_web_https_form_plan(profiles, task, plan)
        self.plan_sha256 = digest(checked.model_dump())
        if (confirm_plan_sha256 != self.plan_sha256 or not callable(consume_approval)
                or (owned_form_target is None and tls_context is None)
                or (owned_form_target is not None
                    and tls_context not in (None, owned_form_target.tls_context))
                or tls_context is not None and (tls_context.verify_mode != ssl.CERT_REQUIRED
                                                or not tls_context.check_hostname)):
            raise ValueError('https_form_requires_exact_plan_and_approval_gate')
        public = verify_web_https_form_target_grant(
            checked, confirm_public_plan_sha256, owned_form_target)
        if owned_form_target is not None and (cookie_header is not None
                                               or confirm_cookie_sha256 is not None):
            raise ValueError('owned_form_fixture_cookie_not_supported')
        if (cookie_header is None) != (confirm_cookie_sha256 is None):
            raise ValueError('https_form_cookie_requires_exact_hash')
        if cookie_header is not None:
            validate_https_cookie_header(cookie_header)
            if (not public or hashlib.sha256(cookie_header.encode('ascii')).hexdigest()
                    != confirm_cookie_sha256):
                raise ValueError('https_form_cookie_requires_public_exact_hash')
        self.profiles = profiles
        self.task = WebTaskContract.model_validate(task.model_dump())
        self.plan = checked
        self.consume_approval = consume_approval
        self.owned_form_target = owned_form_target
        self.tls_context = (owned_form_target.tls_context if owned_form_target is not None
                            else tls_context)
        self.public_plan_sha256 = confirm_public_plan_sha256
        self._cookie_header = cookie_header
        self.cookie_sha256 = confirm_cookie_sha256
        self._lock = threading.Lock()
        self._entry_attempted = False
        self._entry_response_sha256 = None
        self._submit_attempted = False
        self._submitted = False
        self._submit_response_status = None
        self._receipt_attempted = False
        self._receipt_response_sha256 = None

    def verify_plan_identity(self) -> None:
        if digest(self.plan.model_dump()) != self.plan_sha256:
            raise ValueError('https_form_plan_identity_changed')
        self.verify_cookie_identity()

    def verify_cookie_identity(self) -> None:
        if self._cookie_header is None:
            if self.cookie_sha256 is not None:
                raise ValueError('https_form_cookie_identity_changed')
        elif (validate_https_cookie_header(self._cookie_header) != self._cookie_header
              or self.public_plan_sha256 != self.plan_sha256
              or hashlib.sha256(self._cookie_header.encode('ascii')).hexdigest()
              != self.cookie_sha256):
            raise ValueError('https_form_cookie_identity_changed')

    def open_entry(self, *, confirm_request_sha256: str) -> bytes:
        request_sha256 = digest({'method': 'GET', 'url': self.plan.entry_url})
        if confirm_request_sha256 != request_sha256:
            raise ValueError('https_form_requires_exact_entry_confirmation')
        with self._lock:
            if self._entry_attempted or self._submit_attempted:
                raise ValueError('https_form_entry_is_one_shot')
            self.verify_plan_identity()
            verify_web_https_form_plan(self.profiles, self.task, self.plan)
            verify_web_https_form_target_grant(
                self.plan, self.public_plan_sha256, self.owned_form_target)
            if self.consume_approval(request_sha256) is not True:
                raise ValueError('https_form_entry_approval_denied')
            self._entry_attempted = True
        report, body = _fetch(self.profiles, self.plan.profile_sha256,
                              self.plan.profile_sha256, tls_context=self.tls_context,
                              target_url=self.plan.entry_url,
                              cookie_header=self._cookie_header,
                              owned_form_target=self.owned_form_target,
                              **({'owned_form_plan_sha256': self.plan_sha256}
                                 if self.owned_form_target is not None else {}))
        with self._lock:
            self._entry_response_sha256 = report.response_sha256
        return body

    def submit(self, body: bytes, *, confirm_request_sha256: str) -> None:
        if (type(body) is not bytes or len(body) != self.plan.body_bytes
                or hashlib.sha256(body).hexdigest() != self.plan.body_sha256):
            raise ValueError('https_form_body_differs_from_plan')
        request_sha256 = digest({'method': 'POST', 'url': self.plan.submit_url,
                                 'body_sha256': self.plan.body_sha256})
        if confirm_request_sha256 != request_sha256:
            raise ValueError('https_form_requires_exact_request_confirmation')
        with self._lock:
            if self._entry_response_sha256 is None or self._submit_attempted:
                raise ValueError('https_form_requires_one_entry_before_submit')
            self.verify_plan_identity()
            verify_web_https_form_plan(self.profiles, self.task, self.plan)
            verify_web_https_form_target_grant(
                self.plan, self.public_plan_sha256, self.owned_form_target)
            if self.consume_approval(request_sha256) is not True:
                raise ValueError('https_form_submit_approval_denied')
            self._submit_attempted = True
        submit_response_status = self._post_once(body)
        with self._lock:
            self._submit_response_status = submit_response_status
            self._submitted = True

    def read_receipt(self, *, confirm_request_sha256: str
                     ) -> tuple[WebHTTPSFormTransportReport, bytes]:
        receipt_request_sha256 = digest({'method': 'GET', 'url': self.plan.receipt_url})
        if confirm_request_sha256 != receipt_request_sha256:
            raise ValueError('https_form_requires_exact_receipt_confirmation')
        with self._lock:
            if (not self._submitted or self._receipt_attempted
                    or self._submit_response_status not in (302, 303)):
                raise ValueError('https_form_requires_one_submit_before_receipt')
            self.verify_plan_identity()
            verify_web_https_form_plan(self.profiles, self.task, self.plan)
            verify_web_https_form_target_grant(
                self.plan, self.public_plan_sha256, self.owned_form_target)
            if self.consume_approval(receipt_request_sha256) is not True:
                raise ValueError('https_form_receipt_approval_denied')
            self._receipt_attempted = True
            entry_response_sha256 = self._entry_response_sha256
        receipt, receipt_body = _fetch(self.profiles, self.plan.profile_sha256,
                                       self.plan.profile_sha256, tls_context=self.tls_context,
                                       target_url=self.plan.receipt_url,
                                       cookie_header=self._cookie_header,
                                       owned_form_target=self.owned_form_target,
                                       **({'owned_form_plan_sha256': self.plan_sha256}
                                          if self.owned_form_target is not None else {}))
        with self._lock:
            self._receipt_response_sha256 = receipt.response_sha256
        report = WebHTTPSFormTransportReport(
            profile_sha256=self.plan.profile_sha256, task_sha256=self.plan.task_sha256,
            plan_sha256=self.plan_sha256, entry_response_sha256=entry_response_sha256,
            submit_request_sha256=digest({'method': 'POST', 'url': self.plan.submit_url,
                                          'body_sha256': self.plan.body_sha256}),
            submit_response_status=self._submit_response_status,
            receipt_url_sha256=digest({'url': self.plan.receipt_url}),
            receipt_response_sha256=receipt.response_sha256,
            receipt_bytes=receipt.response_bytes)
        return report, receipt_body

    def _post_once(self, body: bytes) -> Literal[302, 303]:
        parsed = urlsplit(self.plan.submit_url)
        context = self.tls_context if self.tls_context is not None else ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError('https_form_requires_verified_tls')
        deadline = time.monotonic() + min(self.task.max_seconds, 10)
        if self.owned_form_target is None:
            addresses = _bounded_public_addresses(parsed.hostname, parsed.port or 443, deadline)
            connection = socket.create_connection((addresses[0], parsed.port or 443),
                                                  timeout=_remaining(deadline))
        else:
            connection = self.owned_form_target.connect(
                self.plan.profile_sha256, self.plan_sha256, None, 'POST',
                self.plan.submit_url, _remaining(deadline))
        with connection:
            connection.settimeout(_remaining(deadline))
            secure_connection = (connection if self.owned_form_target is not None else
                                 context.wrap_socket(connection, server_hostname=parsed.hostname))
            with secure_connection as secure:
                secure.settimeout(_remaining(deadline))
                owned_authorization = (
                    f'X-AOS-Owned-Form: {self.owned_form_target.request_authorization("POST", self.plan.submit_url)}\r\n'
                    if self.owned_form_target is not None else '')
                request = (f'POST {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n'
                           'Accept: text/html\r\nAccept-Encoding: identity\r\n'
                           + owned_authorization
                           + (f'Cookie: {self._cookie_header}\r\n' if self._cookie_header is not None else '')
                           + 'Content-Type: application/x-www-form-urlencoded\r\n'
                           f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode('ascii')
                secure.sendall(request + body)
                response = http.client.HTTPResponse(DeadlineSocket(secure, deadline))
                response.begin()
                location = response.getheader('Location')
                if (response.status not in (302, 303) or not isinstance(location, str)
                        or urljoin(self.plan.submit_url, location) != self.plan.receipt_url
                        or response.getheader('Content-Encoding') not in (None, 'identity')):
                    raise ValueError('https_form_unexpected_submit_response')
                return response.status


class ExactHTTPSFormRelay:
    def __init__(self, transport: ExactHTTPSFormTransport):
        if not isinstance(transport, ExactHTTPSFormTransport):
            raise ValueError('https_form_relay_requires_exact_transport')
        self.transport = transport
        self.client_token = secrets.token_hex(32)
        self._lock = threading.Lock()
        self._started = False

    def _request(self, connection: socket.socket, deadline: float) -> dict:
        content = bytearray()
        while len(content) <= MAX_SOCKET_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('https_form_relay_deadline')
            connection.settimeout(min(5, remaining))
            chunk = connection.recv(1)
            if not chunk:
                break
            content.extend(chunk)
            if chunk == b'\n':
                break
        if len(content) > MAX_SOCKET_REQUEST_BYTES or not content.endswith(b'\n'):
            raise ValueError('https_form_relay_request_limit')

        def unique_pairs(pairs):
            record = {}
            for key, value in pairs:
                if key in record:
                    raise ValueError('https_form_relay_duplicate_field')
                record[key] = value
            return record

        request = json.loads(content, object_pairs_hook=unique_pairs)
        if (not isinstance(request, dict)
                or not isinstance(request.get('method'), str)
                or request['method'] not in {'GET', 'POST'}
                or set(request) != ({'method', 'url', 'plan_sha256', 'client_token'}
                                    | ({'body_base64'} if request['method'] == 'POST' else set()))
                or request['plan_sha256'] != self.transport.plan_sha256
                or not isinstance(request['client_token'], str)
                or re.fullmatch('[a-f0-9]{64}', request['client_token']) is None
                or not hmac.compare_digest(request['client_token'], self.client_token)):
            raise ValueError('https_form_relay_request_outside_plan')
        return request

    def serve(self, socket_path: Path, *, wait_seconds: float = 30,
              stop_event: threading.Event | None = None) -> WebHTTPSFormTransportReport:
        socket_path = Path(socket_path).absolute()
        if (not socket_path.parent.is_relative_to(REPO_ROOT / 'data')
                or socket_path.name in {'', '.', '..'}
                or isinstance(wait_seconds, bool) or not 0 < wait_seconds <= 30):
            raise ValueError('https_form_relay_private_socket_required')
        with self._lock:
            if self._started:
                raise ValueError('https_form_relay_cannot_restart')
            self._started = True
        directory = private_directory(socket_path.parent)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        identity = None
        try:
            listener.bind(f'/proc/self/fd/{directory}/{socket_path.name}')
            metadata = socket_path.lstat()
            identity = (metadata.st_dev, metadata.st_ino)
            os.chmod(socket_path, 0o600, follow_symlinks=False)
            metadata = socket_path.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (metadata.st_dev, metadata.st_ino) != identity):
                raise ValueError('https_form_relay_socket_identity')
            listener.listen(1)
            deadline = time.monotonic() + wait_seconds
            attempts = 0
            while attempts < MAX_SOCKET_ATTEMPTS:
                if stop_event is not None and stop_event.is_set():
                    raise TimeoutError('https_form_relay_stopped')
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
                        method, url = request['method'], request['url']
                        plan = self.transport.plan
                        if method == 'GET' and url == plan.entry_url:
                            body = self.transport.open_entry(confirm_request_sha256=digest({
                                'method': 'GET', 'url': url}))
                            reply = {'status': 200, 'body_base64': base64.b64encode(body).decode(),
                                     'response_sha256': hashlib.sha256(body).hexdigest()}
                        elif method == 'POST' and url == plan.submit_url:
                            encoded = request['body_base64']
                            if not isinstance(encoded, str) or len(encoded) > 5500:
                                raise ValueError('https_form_relay_body_limit')
                            body = base64.b64decode(encoded, validate=True)
                            self.transport.submit(body, confirm_request_sha256=digest({
                                'method': 'POST', 'url': url, 'body_sha256': plan.body_sha256}))
                            reply = {'status': 204}
                        elif method == 'GET' and url == plan.receipt_url:
                            report, body = self.transport.read_receipt(
                                confirm_request_sha256=digest({'method': 'GET', 'url': url}))
                            reply = {'status': 200, 'body_base64': base64.b64encode(body).decode(),
                                     'response_sha256': hashlib.sha256(body).hexdigest()}
                        else:
                            raise ValueError('https_form_relay_wrong_route')
                        encoded = (canonical(reply) + '\n').encode()
                        if len(encoded) > 100000:
                            raise ValueError('https_form_relay_reply_limit')
                        connection.sendall(encoded)
                        if method == 'GET' and url == plan.receipt_url:
                            return report
                    except (OSError, ValueError, TypeError, KeyError, UnicodeError,
                            TimeoutError, ssl.SSLError, http.client.HTTPException):
                        try:
                            connection.sendall(b'{"error":"denied"}\n')
                        except OSError:
                            pass
                        if (self.transport._entry_attempted or self.transport._submit_attempted
                                or self.transport._receipt_attempted):
                            raise
            raise TimeoutError('https_form_relay_expired')
        finally:
            listener.close()
            if identity is not None:
                try:
                    metadata = socket_path.lstat()
                    if (metadata.st_dev, metadata.st_ino) == identity:
                        os.unlink(socket_path)
                except FileNotFoundError:
                    pass
            os.close(directory)
