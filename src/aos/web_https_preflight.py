"""Explicit host-side HTTPS entry probe; never a browser or task execution grant."""

import argparse
import hashlib
import html
from html.parser import HTMLParser
import http.client
import io
import ipaddress
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

from pydantic import Field
from typing import Literal

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_preflight import bounded_file
from .web_application import (Checksum, WebApplicationProfile,
                              WebApplicationProfiles, canonical_origin,
                              readonly_resource_url_parts)
from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskAdmissionDraft,
                                      WebTaskContract, verify_web_readonly_routes,
                                      verify_web_task_binding)


MAX_RESPONSE_BYTES = 65536
CONNECT_TIMEOUT_SECONDS = 5
PROBE_TIMEOUT_SECONDS = 10
_dns_slot = threading.Lock()
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def validate_https_cookie_header(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048 or not value.isascii():
        raise ValueError('https_cookie_invalid')
    names = set()
    for pair in value.split('; '):
        name, separator, cookie_value = pair.partition('=')
        if (separator != '=' or COOKIE_NAME.fullmatch(name) is None or name in names
                or any(not 0x21 <= ord(character) <= 0x7e
                       or character in {'"', ',', ';', '\\'} for character in cookie_value)):
            raise ValueError('https_cookie_invalid')
        names.add(name)
    return value


def reject_reflected_cookie(body: bytes, cookie_header: str | None) -> None:
    if cookie_header is None:
        return
    for pair in cookie_header.split('; '):
        _name, _separator, value = pair.partition('=')
        for text in (pair, value):
            if not text:
                continue
            variants = {text, html.escape(text, quote=True), quote(text, safe='')}
            if any(variant.encode('ascii') in body for variant in variants):
                raise ValueError('https_probe_cookie_reflected')


class DeadlineSocketReader(io.RawIOBase):
    def __init__(self, connection: ssl.SSLSocket, deadline: float):
        self.connection = connection
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('https_probe_deadline')
        self.connection.settimeout(min(CONNECT_TIMEOUT_SECONDS, remaining))
        return self.connection.recv_into(buffer)


class DeadlineSocket:
    def __init__(self, connection: ssl.SSLSocket, deadline: float):
        self.connection = connection
        self.deadline = deadline

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('https_probe_read_only_stream')
        return io.BufferedReader(DeadlineSocketReader(self.connection, self.deadline))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('https_probe_deadline')
    return min(CONNECT_TIMEOUT_SECONDS, remaining)


class WebHTTPSPreflightReport(TypedModel):
    schema_version: Literal['1.3'] = '1.3'
    mode: Literal['explicit_host_https_entry_probe'] = 'explicit_host_https_entry_probe'
    status: Literal['https_entry_reached'] = 'https_entry_reached'
    profile_sha256: Checksum
    origin_sha256: Checksum
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    entry_html_signals: 'EntryHTMLSignals | None'
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class WebBoundHTTPSPreflightReport(TypedModel):
    schema_version: Literal['1.3'] = '1.3'
    mode: Literal['explicit_host_bound_https_entry_probe'] = 'explicit_host_bound_https_entry_probe'
    status: Literal['https_entry_reached'] = 'https_entry_reached'
    profile_sha256: Checksum
    task_sha256: Checksum
    runtime_sha256: Checksum
    binding_sha256: Checksum
    origin_sha256: Checksum
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    entry_html_signals: 'EntryHTMLSignals | None'
    tls_hostname_verified: Literal[True] = True
    task_binding_verified: Literal[True] = True
    runtime_identity_verified: Literal[False] = False
    browser_connected: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class WebHTTPSReadOnlyRouteReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['explicit_bound_readonly_route_fetch'] = 'explicit_bound_readonly_route_fetch'
    status: Literal['https_route_reached'] = 'https_route_reached'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    route_index: int = Field(ge=0, le=7)
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class EntryHTMLSignals(TypedModel):
    form_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    post_form_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    cross_origin_form_action_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    unclassified_form_action_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    meta_refresh_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    cross_origin_meta_refresh_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    unclassified_meta_refresh_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    script_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    stylesheet_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    image_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    password_input_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    cross_origin_resource_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    unclassified_resource_count: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    static_html_only: Literal[True] = True
    browser_or_account_verified: Literal[False] = False


class _EntryHTMLSignalParser(HTMLParser):
    def __init__(self, entry_url: str):
        super().__init__(convert_charrefs=True)
        self.entry_url = entry_url
        entry = urlsplit(entry_url)
        self.entry_origin = (entry.scheme, entry.hostname,
                             entry.port or (443 if entry.scheme == 'https' else 80))
        self.counts = dict.fromkeys(('form_count', 'post_form_count',
                                     'cross_origin_form_action_count',
                                     'unclassified_form_action_count',
                                     'meta_refresh_count', 'cross_origin_meta_refresh_count',
                                     'unclassified_meta_refresh_count',
                                     'script_count', 'stylesheet_count', 'image_count',
                                     'password_input_count', 'cross_origin_resource_count',
                                     'unclassified_resource_count'), 0)
        self.form_actions: list[str | None] = []
        self.meta_refresh_contents: list[str | None] = []
        self.resource_references: list[str] = []
        self.has_base_href = False

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        values = dict(attributes)
        reference = None
        if tag == 'form':
            self.counts['form_count'] += 1
            if (values.get('method') or '').strip().lower() == 'post':
                self.counts['post_form_count'] += 1
            self.form_actions.append(values.get('action'))
        elif tag == 'base' and 'href' in values:
            self.has_base_href = True
        elif tag == 'meta' and (values.get('http-equiv') or '').strip().lower() == 'refresh':
            self.counts['meta_refresh_count'] += 1
            self.meta_refresh_contents.append(values.get('content'))
        elif tag == 'script':
            self.counts['script_count'] += 1
            reference = values.get('src')
        elif tag == 'link' and 'stylesheet' in (values.get('rel') or '').lower().split():
            self.counts['stylesheet_count'] += 1
            reference = values.get('href')
        elif tag == 'img':
            self.counts['image_count'] += 1
            reference = values.get('src')
        elif tag == 'input' and (values.get('type') or '').lower() == 'password':
            self.counts['password_input_count'] += 1
        if reference is not None:
            self.resource_references.append(reference)

    def reference_scope(self, reference: str) -> Literal['same', 'cross', 'unclassified']:
        if (not reference or any(ord(character) <= 32 or ord(character) == 127
                                 or character == '\\' for character in reference)):
            return 'unclassified'
        try:
            parsed_reference = urlsplit(reference)
            if self.has_base_href and not parsed_reference.scheme and not parsed_reference.netloc:
                return 'unclassified'
            resolved = urlsplit(urljoin(self.entry_url, reference))
            if (resolved.scheme not in {'http', 'https'} or not resolved.hostname
                    or resolved.username is not None or resolved.password is not None
                    or any(ord(character) >= 127 or character == '%'
                           for character in resolved.netloc)):
                return 'unclassified'
            port = resolved.port
            if port is not None and not 1 <= port <= 65535:
                return 'unclassified'
            origin = (resolved.scheme, resolved.hostname,
                      port if port is not None else (443 if resolved.scheme == 'https' else 80))
            return 'same' if origin == self.entry_origin else 'cross'
        except ValueError:
            return 'unclassified'

    def finish_references(self) -> None:
        for reference in self.resource_references:
            scope = self.reference_scope(reference)
            if scope == 'unclassified':
                self.counts['unclassified_resource_count'] += 1
            elif scope == 'cross':
                self.counts['cross_origin_resource_count'] += 1
        for action in self.form_actions:
            if not action:
                continue
            scope = self.reference_scope(action)
            if scope == 'unclassified':
                self.counts['unclassified_form_action_count'] += 1
            elif scope == 'cross':
                self.counts['cross_origin_form_action_count'] += 1
        for content in self.meta_refresh_contents:
            match = re.fullmatch(r'\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*(.*?)\s*',
                                 content or '', flags=re.IGNORECASE)
            if match is None:
                self.counts['unclassified_meta_refresh_count'] += 1
                continue
            reference = match.group(1)
            if reference.startswith(('"', "'")):
                if len(reference) < 2 or reference[-1] != reference[0]:
                    self.counts['unclassified_meta_refresh_count'] += 1
                    continue
                reference = reference[1:-1]
            scope = self.reference_scope(reference)
            if scope == 'unclassified':
                self.counts['unclassified_meta_refresh_count'] += 1
            elif scope == 'cross':
                self.counts['cross_origin_meta_refresh_count'] += 1


def entry_html_signals(entry_url: str, body: bytes) -> EntryHTMLSignals | None:
    try:
        parser = _EntryHTMLSignalParser(entry_url)
        parser.feed(body.decode('utf-8'))
        parser.close()
        parser.finish_references()
    except (UnicodeError, ValueError):
        return None
    return EntryHTMLSignals(**parser.counts)


def _public_addresses(host: str, port: int) -> list[str]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    selected = []
    for family, socktype, protocol, _name, address in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM:
            raise ValueError('https_probe_dns_answer_unsupported')
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global:
            raise ValueError('https_probe_dns_answer_not_public')
        if str(ip) not in selected:
            selected.append(str(ip))
    if not selected or len(selected) > 16:
        raise ValueError('https_probe_dns_answer_limit')
    return selected


def _bounded_public_addresses(host: str, port: int, deadline: float) -> list[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _dns_slot.acquire(timeout=remaining):
        raise TimeoutError('https_probe_dns_deadline')
    if deadline - time.monotonic() <= 0:
        _dns_slot.release()
        raise TimeoutError('https_probe_dns_deadline')
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            outcome = (_public_addresses(host, port), None)
        except Exception as error:
            outcome = (None, error)
        finally:
            _dns_slot.release()
        result.put(outcome)

    worker = threading.Thread(target=resolve, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _dns_slot.release()
        raise
    try:
        addresses, error = result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError('https_probe_dns_deadline') from None
    if error is not None:
        raise error
    return addresses


def _fetch(profiles: WebApplicationProfiles, profile_sha256: str, confirm_sha256: str,
           *, tls_context: ssl.SSLContext | None = None,
           target_url: str | None = None,
           cookie_header: str | None = None,
           allow_query: bool = False, owned_form_target=None,
           owned_form_plan_sha256: str | None = None,
           owned_form_state_plan_sha256: str | None = None) -> tuple[WebHTTPSPreflightReport, bytes]:
    origin, request_url, body = _fetch_response(
        profiles, profile_sha256, confirm_sha256, tls_context=tls_context,
        target_url=target_url, cookie_header=cookie_header,
        content_type='text/html', max_response_bytes=MAX_RESPONSE_BYTES,
        allow_query=allow_query, owned_form_target=owned_form_target,
        owned_form_plan_sha256=owned_form_plan_sha256,
        owned_form_state_plan_sha256=owned_form_state_plan_sha256)
    report = WebHTTPSPreflightReport(profile_sha256=profile_sha256,
                                     origin_sha256=digest({'origin': origin}),
                                     request_sha256=digest({'method': 'GET', 'url': request_url}),
                                     response_sha256=hashlib.sha256(body).hexdigest(),
                                     response_bytes=len(body),
                                     entry_html_signals=entry_html_signals(request_url, body))
    return report, body


def _fetch_response(profiles: WebApplicationProfiles, profile_sha256: str,
                    confirm_sha256: str, *, tls_context: ssl.SSLContext | None = None,
                    target_url: str | None = None, cookie_header: str | None = None,
                    content_type: str, max_response_bytes: int,
                    allow_query: bool = False, owned_form_target=None,
                    owned_form_plan_sha256: str | None = None,
                    owned_form_state_plan_sha256: str | None = None) -> tuple[str, str, bytes]:
    if (not isinstance(profile_sha256, str) or not isinstance(confirm_sha256, str)
            or profile_sha256 != confirm_sha256
            or content_type not in {'text/html', 'text/css', 'text/javascript',
                                    'application/javascript', 'application/json',
                                    'image/png', 'image/jpeg', 'image/webp', 'image/gif'}
            or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 1048576):
        raise ValueError('https_probe_requires_exact_profile_confirmation')
    profile = profiles.get(profile_sha256)
    request_url = target_url if target_url is not None else profile.entry_url
    if allow_query:
        if target_url is None or cookie_header is not None:
            raise ValueError('https_probe_query_requires_readonly_resource')
        base_url, query = readonly_resource_url_parts(request_url)
    else:
        WebApplicationProfile.entry_is_canonical(request_url)
        base_url, query = request_url, ''
    parsed = urlsplit(base_url)
    origin, local = canonical_origin(base_url)
    if (profile.environment not in {'staging', 'production'} or local or parsed.scheme != 'https'
            or origin != canonical_origin(profile.entry_url)[0]
            or origin not in profile.allowed_origins or not parsed.path.startswith('/')
            or parsed.fragment or parsed.username or parsed.password
            or parsed.hostname is None):
        raise ValueError('https_probe_requires_remote_profile_entry')
    port = parsed.port or 443
    if owned_form_target is not None:
        if (content_type != 'text/html' or cookie_header is not None or allow_query
                or owned_form_plan_sha256 is None
                or not parsed.hostname.endswith('.invalid')):
            raise ValueError('owned_form_fixture_request_scope_invalid')
        if tls_context is not None and tls_context is not owned_form_target.tls_context:
            raise ValueError('owned_form_fixture_tls_context_mismatch')
        context = owned_form_target.tls_context
    else:
        if owned_form_plan_sha256 is not None or owned_form_state_plan_sha256 is not None:
            raise ValueError('owned_form_fixture_target_required')
        context = tls_context if tls_context is not None else ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError('https_probe_requires_verified_tls')
    if cookie_header is not None:
        validate_https_cookie_header(cookie_header)
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    if owned_form_target is None:
        addresses = _bounded_public_addresses(parsed.hostname, port, deadline)
        connection = socket.create_connection((addresses[0], port), timeout=_remaining(deadline))
    else:
        connection = owned_form_target.connect(
            profile_sha256, owned_form_plan_sha256, owned_form_state_plan_sha256,
            'GET', request_url, _remaining(deadline))
    with connection:
        connection.settimeout(_remaining(deadline))
        secure_connection = (connection if owned_form_target is not None
                             else context.wrap_socket(connection, server_hostname=parsed.hostname))
        with secure_connection as secure:
            secure.settimeout(_remaining(deadline))
            request_target = parsed.path + ('?' + query if query else '')
            owned_authorization = (
                f'X-AOS-Owned-Form: {owned_form_target.request_authorization("GET", request_url)}\r\n'
                if owned_form_target is not None else '')
            request = (f'GET {request_target} HTTP/1.1\r\nHost: {parsed.netloc}\r\n'
                       f'Accept: {content_type}\r\nAccept-Encoding: identity\r\n'
                       + owned_authorization
                       + (f'Cookie: {cookie_header}\r\n' if cookie_header is not None else '')
                       + 'Connection: close\r\n\r\n').encode('ascii')
            secure.sendall(request)
            response = http.client.HTTPResponse(DeadlineSocket(secure, deadline))
            response.begin()
            if response.status != 200 or response.getheader('Content-Encoding') not in (None, 'identity'):
                raise ValueError('https_probe_response_not_plain_success')
            received_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if received_type != content_type:
                raise ValueError('https_probe_response_not_html' if content_type == 'text/html'
                                 else 'https_probe_response_content_type_mismatch')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > max_response_bytes):
                raise ValueError('https_probe_response_length_limit')
            body = bytearray()
            while True:
                _remaining(deadline)
                chunk = response.read(min(8192, max_response_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_response_bytes:
                    raise ValueError('https_probe_response_size_limit')
    response_body = bytes(body)
    reject_reflected_cookie(response_body, cookie_header)
    return origin, request_url, response_body


def fetch_web_readonly_route(profiles: WebApplicationProfiles, task: WebTaskContract,
                             plan: WebReadOnlyRoutePlan, confirm_plan_sha256: str,
                             route_index: int, *, tls_context: ssl.SSLContext | None = None
                             ) -> tuple[WebHTTPSReadOnlyRouteReport, bytes]:
    checked = verify_web_readonly_routes(profiles, task, plan)
    plan_sha256 = digest(checked.model_dump())
    if (not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256
            or type(route_index) is not int or not 0 <= route_index < len(checked.routes)):
        raise ValueError('readonly_route_requires_exact_plan_and_index')
    report, body = _fetch(profiles, checked.profile_sha256, checked.profile_sha256,
                          tls_context=tls_context, target_url=checked.routes[route_index],
                          allow_query=True)
    return WebHTTPSReadOnlyRouteReport(
        profile_sha256=checked.profile_sha256, task_sha256=checked.task_sha256,
        plan_sha256=plan_sha256, route_index=route_index,
        request_sha256=report.request_sha256, response_sha256=report.response_sha256,
        response_bytes=report.response_bytes), body


def _probe(profiles: WebApplicationProfiles, profile_sha256: str, confirm_sha256: str,
           *, tls_context: ssl.SSLContext | None = None) -> WebHTTPSPreflightReport:
    return _fetch(profiles, profile_sha256, confirm_sha256, tls_context=tls_context)[0]


def probe_https_entry(profiles: WebApplicationProfiles, profile_sha256: str,
                      confirm_sha256: str) -> WebHTTPSPreflightReport:
    return _probe(profiles, profile_sha256, confirm_sha256)


def _probe_bound(profiles: WebApplicationProfiles, profile_sha256: str,
                 confirm_sha256: str, draft: WebTaskAdmissionDraft,
                 confirm_binding_sha256: str, *,
                 tls_context: ssl.SSLContext | None = None) -> WebBoundHTTPSPreflightReport:
    if (not isinstance(draft, WebTaskAdmissionDraft)
            or not isinstance(confirm_binding_sha256, str)
            or confirm_binding_sha256 != draft.binding_sha256):
        raise ValueError('https_probe_requires_exact_binding_confirmation')
    checked = verify_web_task_binding(profiles, draft)
    if (checked.profile_sha256 != profile_sha256
            or 'browser.navigate' not in checked.task.tools):
        raise ValueError('https_probe_binding_outside_navigation_scope')
    report = _probe(profiles, profile_sha256, confirm_sha256, tls_context=tls_context)
    return WebBoundHTTPSPreflightReport(
        profile_sha256=report.profile_sha256, task_sha256=checked.task_sha256,
        runtime_sha256=checked.runtime_sha256, binding_sha256=checked.binding_sha256,
        origin_sha256=report.origin_sha256, request_sha256=report.request_sha256,
        response_sha256=report.response_sha256, response_bytes=report.response_bytes,
        entry_html_signals=report.entry_html_signals)


def probe_bound_https_entry(profiles: WebApplicationProfiles, profile_sha256: str,
                            confirm_sha256: str, draft: WebTaskAdmissionDraft,
                            confirm_binding_sha256: str) -> WebBoundHTTPSPreflightReport:
    return _probe_bound(profiles, profile_sha256, confirm_sha256, draft,
                        confirm_binding_sha256)


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit('HTTPS preflight unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Explicit host-only HTTPS entry probe')
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--profile-sha256', required=True)
    parser.add_argument('--confirm-sha256', required=True)
    parser.add_argument('--binding-file', type=Path)
    parser.add_argument('--confirm-binding-sha256')
    arguments = parser.parse_args(argv)
    try:
        if (arguments.binding_file is None) != (arguments.confirm_binding_sha256 is None):
            raise ValueError('https_probe_binding_options_incomplete')
        profiles = WebApplicationProfiles(arguments.profiles)
        if arguments.binding_file is None:
            report = probe_https_entry(profiles, arguments.profile_sha256, arguments.confirm_sha256)
        else:
            draft = WebTaskAdmissionDraft.model_validate_json(bounded_file(arguments.binding_file, 65536))
            report = probe_bound_https_entry(profiles, arguments.profile_sha256,
                                             arguments.confirm_sha256, draft,
                                             arguments.confirm_binding_sha256)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError,
            TimeoutError, ssl.SSLError, http.client.HTTPException):
        parser.exit(1, 'HTTPS preflight unavailable: scope, DNS, TLS, response or source rejected.\n')
    print(canonical(report.model_dump()))


if __name__ == '__main__':
    main()
