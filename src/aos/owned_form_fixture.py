"""Private, single-use HTTPS fixture capability for synthetic form tasks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from html.parser import HTMLParser
import hashlib
import json
from http.client import HTTPResponse
import os
import secrets
import socket
import ssl
import stat
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .contracts import canonical


HOST = 'w3-owned-form.aos.invalid'
ENTRY_BODY = (b'<html><title>Owned synthetic form</title><h1>Owned form</h1>'
              b'<form method="post" action="/submit"><label for="message">Message</label>'
              b'<input id="message" name="message" required>'
              b'<button type="submit">Save draft</button></form></html>')
RECEIPT_BODY = b'<html><title>Owned receipt</title><h1 id="receipt">Saved synthetic form</h1></html>'
STATE_BEFORE = b'<html><h1 id="outcome">Empty synthetic state</h1></html>'
STATE_AFTER = b'<html><h1 id="outcome">alpha</h1></html>'
_FACTORY_KEY = object()
WHOLE_RECORD_ID = 'aos-whole-record'


def _record_config(config):
    if type(config) is not dict or set(config) != {'scope', 'fields', 'outcome_field', 'marker_id'}:
        raise ValueError('owned_record_form_config_invalid')
    scope = config['scope']
    if (type(scope) is not dict or set(scope) != {'application_id', 'tenant_id', 'account_role'}
            or any(type(value) is not str or not 1 <= len(value) <= 64
                   or not value.isprintable() for value in scope.values())):
        raise ValueError('owned_record_form_scope_invalid')
    fields = config['fields']
    if not isinstance(fields, list) or not 2 <= len(fields) <= 8:
        raise ValueError('owned_record_form_fields_invalid')
    normalized = []
    names = set()
    for item in fields:
        if type(item) is not dict or set(item) != {'name', 'label', 'value'}:
            raise ValueError('owned_record_form_field_invalid')
        name, label, value = item['name'], item['label'], item['value']
        if (type(name) is not str or not name.isascii() or not name.replace('_', 'a').isalnum()
                or not name[0].isalpha() or len(name) > 64 or name in names
                or type(label) is not str or not 1 <= len(label) <= 80 or not label.isprintable()
                or type(value) is not str or not value or not value.isprintable()
                or len(value.encode('utf-8')) > 256):
            raise ValueError('owned_record_form_field_invalid')
        names.add(name)
        normalized.append({'name': name, 'label': label, 'value': value})
    outcome_field = config['outcome_field']
    if type(outcome_field) is not str or outcome_field not in names:
        raise ValueError('owned_record_form_outcome_field_invalid')
    if config['marker_id'] != 'outcome':
        raise ValueError('owned_record_form_marker_invalid')
    return {'scope': dict(scope), 'fields': normalized, 'outcome_field': outcome_field,
            'marker_id': 'outcome'}


def _whole_record_observation(config, actual_record=None):
    record = actual_record if actual_record is not None else {
        item['name']: item['value'] for item in config['fields']}
    return {'schema_version': '1.0', 'scope': config['scope'], 'record': record,
            'reported_post_count': 1, 'effect_status': 'committed'}


def _after_state(config, record=None):
    values = record if record is not None else {item['name']: item['value'] for item in config['fields']}
    outcome = values[config['outcome_field']]
    observation = canonical(_whole_record_observation(config, values))
    safe_json = observation.replace('&', r'\u0026').replace('<', r'\u003c').replace('>', r'\u003e')
    return (f'<html><h1 id="{config["marker_id"]}">{escape(outcome)}</h1>'
            f'<script id="{WHOLE_RECORD_ID}" type="application/json">{safe_json}</script></html>').encode()


def owned_record_form_bodies(record_config):
    config = _record_config(record_config)
    fields = ''.join(f'<label for="{escape(item["name"], quote=True)}">{escape(item["label"])}</label>'
                     f'<input id="{escape(item["name"], quote=True)}" name="{escape(item["name"], quote=True)}" required>'
                     for item in config['fields'])
    entry = (f'<html><title>Owned synthetic form</title><h1>Owned form</h1>'
             f'<form method="post" action="/submit">{fields}<button type="submit">Save draft</button></form></html>').encode()
    before = f'<html><h1 id="{config["marker_id"]}">Empty synthetic state</h1></html>'.encode()
    after = _after_state(config)
    form_body = urlencode([(item['name'], item['value']) for item in config['fields']]).encode('ascii')
    if len(entry) > 8192 or len(after) > 8192 or len(form_body) > 4096:
        raise ValueError('owned_record_form_size_invalid')
    return {'entry': entry, 'before': before, 'after': after, 'form_body': form_body}


class _WholeRecordParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.matches = 0
        self.payload = []
        self.in_record = False
        self.closed_record = False
        self.invalid = False

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if len(values) != len(attributes) and any(value == WHOLE_RECORD_ID for _, value in attributes):
            self.invalid = True
        if values.get('id') == WHOLE_RECORD_ID:
            self.matches += 1
            if self.matches != 1 or tag != 'script' or values.get('type') != 'application/json':
                self.invalid = True
            self.in_record = tag == 'script' and values.get('type') == 'application/json'

    def handle_endtag(self, tag):
        if self.in_record and tag == 'script':
            self.in_record = False
            self.closed_record = True

    def handle_data(self, data):
        if self.in_record:
            self.payload.append(data)

    def handle_entityref(self, _name):
        if self.in_record:
            self.invalid = True


def extract_owned_whole_record(html_body):
    if type(html_body) is not bytes or not 1 <= len(html_body) <= 8192:
        raise ValueError('owned_record_form_readback_invalid')
    try:
        parser = _WholeRecordParser()
        parser.feed(html_body.decode('utf-8', 'strict'))
        parser.close()
        if parser.matches != 1 or parser.invalid or parser.in_record or not parser.closed_record:
            raise ValueError('owned_record_form_readback_invalid')
        payload = ''.join(parser.payload).encode('utf-8')
        if not 1 <= len(payload) <= 4096:
            raise ValueError('owned_record_form_readback_invalid')
        value = json.loads(payload, object_pairs_hook=_unique_json_pairs)
        canonical_payload = canonical(value).replace('&', r'\u0026').replace('<', r'\u003c').replace('>', r'\u003e').encode('utf-8')
        if type(value) is not dict or canonical_payload != payload:
            raise ValueError('owned_record_form_readback_invalid')
        return canonical(value).encode('utf-8')
    except Exception:
        raise ValueError('owned_record_form_readback_invalid') from None


def _unique_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != HOST or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.port is None or parsed.path == ''):
        raise ValueError('owned_form_fixture_url_invalid')
    return 'https', parsed.hostname, parsed.port


class OwnedFormFixtureTarget:
    def __init__(self, capability, *, profile_sha256: str, form_plan_sha256: str,
                 state_plan_sha256: str, origin: str, entry_url: str, submit_url: str,
                 receipt_url: str, state_url: str, port: int, certificate_sha256: str,
                 certificate_pem: bytes, request_token: str):
        if capability is not _FACTORY_KEY:
            raise ValueError('owned_form_fixture_target_owner_only')
        self.profile_sha256 = profile_sha256
        self.form_plan_sha256 = form_plan_sha256
        self.state_plan_sha256 = state_plan_sha256
        self.origin = origin
        self.entry_url = entry_url
        self.submit_url = submit_url
        self.receipt_url = receipt_url
        self.state_url = state_url
        self.port = port
        self.certificate_sha256 = certificate_sha256
        self._certificate_der_sha256 = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(certificate_pem.decode('ascii'))).hexdigest()
        self._request_token = request_token
        self.tls_context = ssl.create_default_context(cadata=certificate_pem.decode('ascii'))
        if self.tls_context.verify_mode != ssl.CERT_REQUIRED or not self.tls_context.check_hostname:
            raise ValueError('owned_form_fixture_tls_verification_required')
        self._lock = threading.Lock()
        self._active = True
        self._requests = 0

    def __getstate__(self):
        raise TypeError('owned_form_fixture_target_not_serializable')

    def assert_plan(self, profile_sha256: str, form_plan_sha256: str) -> None:
        with self._lock:
            if (not self._active or profile_sha256 != self.profile_sha256
                    or form_plan_sha256 != self.form_plan_sha256):
                raise ValueError('owned_form_fixture_plan_binding_changed')

    def assert_state_plan(self, state_plan_sha256: str) -> None:
        with self._lock:
            if not self._active or state_plan_sha256 != self.state_plan_sha256:
                raise ValueError('owned_form_fixture_state_plan_binding_changed')

    def _assert_tls(self) -> None:
        if (self.tls_context.verify_mode != ssl.CERT_REQUIRED
                or not self.tls_context.check_hostname):
            raise ValueError('owned_form_fixture_tls_verification_required')

    def assert_request(self, profile_sha256: str, form_plan_sha256: str,
                       state_plan_sha256: str | None, method: str, url: str) -> None:
        with self._lock:
            if (not self._active or profile_sha256 != self.profile_sha256
                    or form_plan_sha256 != self.form_plan_sha256):
                raise ValueError('owned_form_fixture_target_inactive_or_mismatched')
            expected = {
                ('GET', self.entry_url), ('POST', self.submit_url),
                ('GET', self.receipt_url), ('GET', self.state_url)}
            if ((method, url) not in expected
                    or method == 'GET' and url == self.state_url
                    and state_plan_sha256 != self.state_plan_sha256
                    or url != self.state_url and state_plan_sha256 is not None
                    and state_plan_sha256 != self.state_plan_sha256):
                raise ValueError('owned_form_fixture_request_outside_exact_plan')
            parsed_origin = _url_origin(url)
            if parsed_origin != _url_origin(self.entry_url):
                raise ValueError('owned_form_fixture_request_origin_changed')
            self._requests += 1
            if self._requests > 6:
                raise ValueError('owned_form_fixture_request_limit')

    def request_authorization(self, method: str, url: str) -> str:
        with self._lock:
            if (not self._active or (method, url) not in {
                    ('GET', self.entry_url), ('POST', self.submit_url),
                    ('GET', self.receipt_url), ('GET', self.state_url)}):
                raise ValueError('owned_form_fixture_request_outside_exact_plan')
            return self._request_token

    def connect(self, profile_sha256: str, form_plan_sha256: str,
                state_plan_sha256: str | None, method: str, url: str,
                timeout: float) -> socket.socket:
        self._assert_tls()
        self.assert_request(profile_sha256, form_plan_sha256, state_plan_sha256,
                            method, url)
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=timeout)
        try:
            secure = self.tls_context.wrap_socket(connection,
                                                  server_hostname=urlsplit(url).hostname)
            if hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest() != self._certificate_der_sha256:
                secure.close()
                raise ValueError('owned_form_fixture_certificate_changed')
            return secure
        except Exception:
            connection.close()
            raise

    def revoke(self) -> None:
        with self._lock:
            self._active = False


class _FixtureState:
    def __init__(self, expected_body: bytes, request_token: str, state_after: bytes,
                 record_config=None):
        if not isinstance(expected_body, bytes) or not expected_body:
            raise ValueError('owned_form_fixture_body_required')
        if not isinstance(state_after, bytes) or not 1 <= len(state_after) <= 8192:
            raise ValueError('owned_form_fixture_after_state_required')
        self.expected_body = expected_body
        self.state_after = state_after
        self.record_config = record_config
        self.entry_body = ENTRY_BODY if record_config is None else owned_record_form_bodies(record_config)['entry']
        self.before_body = STATE_BEFORE if record_config is None else owned_record_form_bodies(record_config)['before']
        self.request_token = request_token
        self._lock = threading.Lock()
        self.entry_seen = False
        self.before_seen = False
        self.submitted = False
        self.receipt_seen = False
        self.after_seen = False
        self.whole_record_seen = False
        self.committed_record = None

    def complete(self) -> bool:
        with self._lock:
            return all((self.entry_seen, self.before_seen, self.submitted,
                        self.receipt_seen, self.after_seen))


class _OwnedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, listener: socket.socket, handler, context: ssl.SSLContext):
        address = listener.getsockname()
        super().__init__(address, handler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.ssl_context = context
        self.server_address = address
        self.server_port = address[1]
        self._active_sockets = set()
        self._socket_lock = threading.Lock()
        self._socket_timers = {}

    def get_request(self):
        connection, address = self.socket.accept()
        connection.settimeout(2.0)
        with self._socket_lock:
            self._active_sockets.add(connection)
        try:
            secure = self.ssl_context.wrap_socket(connection, server_side=True)
            secure.settimeout(2.0)
            timer = threading.Timer(8.0, secure.close)
            timer.daemon = True
            with self._socket_lock:
                self._active_sockets.discard(connection)
                self._active_sockets.add(secure)
                self._socket_timers[secure] = timer
            timer.start()
            return secure, address
        except Exception:
            with self._socket_lock:
                self._active_sockets.discard(connection)
            connection.close()
            raise

    def shutdown_request(self, request):
        with self._socket_lock:
            timer = self._socket_timers.pop(request, None)
            self._active_sockets.discard(request)
        if timer is not None:
            timer.cancel()
        super().shutdown_request(request)

    def interrupt_requests(self):
        with self._socket_lock:
            sockets = tuple(self._active_sockets)
            timers = tuple(self._socket_timers.values())
            self._active_sockets.clear()
            self._socket_timers.clear()
        for timer in timers:
            timer.cancel()
        for request in sockets:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()


def _handler(state: _FixtureState, origin: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'AOSOwnedFixture/1'
        sys_version = ''

        def log_message(self, _format, *_arguments):
            return

        def _send(self, status: int, body: bytes, *, location: str | None = None):
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            if location is not None:
                self.send_header('Location', location)
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.close_connection = True

        def _host_ok(self):
            return self.headers.get('Host') == urlsplit(origin).netloc

        def _authorized(self):
            auth_values = self.headers.get_all('X-AOS-Owned-Form', [])
            host_values = self.headers.get_all('Host', [])
            return (len(auth_values) == 1 and isinstance(auth_values[0], str)
                    and secrets.compare_digest(auth_values[0], state.request_token)
                    and len(host_values) == 1 and self._host_ok())

        def do_GET(self):
            with state._lock:
                if not self._authorized():
                    self._send(421, b'')
                elif self.path == '/entry' and not state.entry_seen and not state.submitted:
                    state.entry_seen = True
                    self._send(200, state.entry_body)
                elif self.path == '/state' and state.entry_seen and not state.before_seen and not state.submitted:
                    state.before_seen = True
                    self._send(200, state.before_body)
                elif self.path == '/receipt' and state.submitted and not state.receipt_seen:
                    state.receipt_seen = True
                    self._send(200, RECEIPT_BODY)
                elif self.path == '/state' and state.receipt_seen and not state.after_seen:
                    state.after_seen = True
                    if state.record_config is None:
                        self._send(200, state.state_after)
                    else:
                        self._send(200, _after_state(state.record_config, state.committed_record))
                elif (self.path == '/state' and state.after_seen and state.record_config is not None
                      and state.committed_record is not None and not state.whole_record_seen):
                    state.whole_record_seen = True
                    self._send(200, _after_state(state.record_config, state.committed_record))
                else:
                    self._send(404, b'')

        def do_POST(self):
            if not self._authorized():
                self._send(403, b'')
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                length = -1
            if length <= 0 or length > 4096:
                self._send(413, b'')
                return
            if self.headers.get('Transfer-Encoding') is not None:
                self._send(400, b'')
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._send(400, b'')
                return
            with state._lock:
                parsed_record = None
                if state.record_config is not None:
                    try:
                        pairs = parse_qsl(body.decode('ascii', 'strict'), keep_blank_values=True,
                                          strict_parsing=True, encoding='utf-8', errors='strict',
                                          max_num_fields=8)
                        parsed_record = _unique_json_pairs(pairs)
                    except Exception:
                        parsed_record = None
                    expected_names = {item['name'] for item in state.record_config['fields']}
                    expected_values = {item['name']: item['value'] for item in state.record_config['fields']}
                if (not self._host_ok() or self.path != '/submit'
                        or not state.before_seen or state.submitted
                        or body != state.expected_body
                        or state.record_config is not None and (parsed_record is None
                            or set(parsed_record) != expected_names or parsed_record != expected_values)
                        or self.headers.get('Content-Type') != 'application/x-www-form-urlencoded'):
                    self._send(409, b'')
                    return
                state.submitted = True
                if state.record_config is not None:
                    state.committed_record = dict(parsed_record)
                self._send(303, b'', location=origin + '/receipt')

        def do_HEAD(self):
            self._send(405, b'')

        def do_PUT(self):
            self._send(405, b'')

        def do_DELETE(self):
            self._send(405, b'')

    return Handler


class OwnedFormFixture:
    def __init__(self, listener_fd: int, *, origin: str, profile_sha256: str,
                 form_plan_sha256: str, state_plan_sha256: str, entry_url: str,
                 submit_url: str, receipt_url: str, state_url: str,
                 expected_body: bytes, certificate_file: Path, key_file: Path,
                 certificate_sha256: str, state_after: bytes | None = None,
                 record_config=None):
        if type(listener_fd) is not int or listener_fd < 0:
            raise ValueError('owned_form_fixture_listener_required')
        listener = socket.socket(fileno=os.dup(listener_fd))
        server = None
        try:
            address = listener.getsockname()
            if (listener.family != socket.AF_INET or listener.type & socket.SOCK_STREAM == 0
                    or address[0] != '127.0.0.1'
                    or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
                raise ValueError('owned_form_fixture_listener_mismatch')
            parsed_origin = urlsplit(origin)
            if (origin != f'https://{HOST}:{address[1]}'
                    or parsed_origin.hostname != HOST or parsed_origin.path not in ('', '/')
                    or any(_url_origin(url) != _url_origin(origin + '/entry') for url in
                           (entry_url, submit_url, receipt_url, state_url))
                    or (entry_url, submit_url, receipt_url, state_url) != (
                        origin + '/entry', origin + '/submit', origin + '/receipt', origin + '/state')):
                raise ValueError('owned_form_fixture_manifest_mismatch')
            certificate_file = Path(certificate_file)
            key_file = Path(key_file)
            certificate_pem = _private_file_bytes(certificate_file, 16384)
            key_pem = _private_file_bytes(key_file, 16384)
            if hashlib.sha256(certificate_pem).hexdigest() != certificate_sha256:
                raise ValueError('owned_form_fixture_manifest_mismatch')
            request_token = secrets.token_hex(32)
            normalized_record = None if record_config is None else _record_config(record_config)
            if normalized_record is not None:
                generated = owned_record_form_bodies(normalized_record)
                if expected_body != generated['form_body']:
                    raise ValueError('owned_record_form_expected_body_mismatch')
                if state_after is not None and state_after != generated['after']:
                    raise ValueError('owned_record_form_after_state_mismatch')
                state_after = generated['after']
            elif state_after is None:
                state_after = STATE_AFTER
            state = _FixtureState(expected_body, request_token, state_after, normalized_record)
            with tempfile.TemporaryDirectory(prefix='aos-owned-form-tls-') as temp_dir:
                os.chmod(temp_dir, 0o700)
                cert_path = Path(temp_dir) / 'fixture.pem'
                key_path = Path(temp_dir) / 'fixture-key.pem'
                cert_path.write_bytes(certificate_pem)
                key_path.write_bytes(key_pem)
                os.chmod(cert_path, 0o600)
                os.chmod(key_path, 0o600)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(str(cert_path), str(key_path))
            server = _OwnedHTTPServer(listener, _handler(state, origin), context)
            listener = None
            target = OwnedFormFixtureTarget(
                _FACTORY_KEY, profile_sha256=profile_sha256,
                form_plan_sha256=form_plan_sha256, state_plan_sha256=state_plan_sha256,
                origin=origin, entry_url=entry_url, submit_url=submit_url,
                receipt_url=receipt_url, state_url=state_url, port=address[1],
                certificate_sha256=certificate_sha256, certificate_pem=certificate_pem,
                request_token=request_token)
            self._state = state
            self._server = server
            self.origin = origin
            self.target = target
            self._thread = threading.Thread(target=server.serve_forever,
                                            kwargs={'poll_interval': 0.1},
                                            name='aos-owned-form-fixture', daemon=True)
            self._closed = False
            self._thread.start()
            self._readback_lock = threading.Lock()
            self._readback_used = False
        except BaseException:
            if server is not None:
                server.server_close()
            if listener is not None:
                listener.close()
            raise

    @property
    def record_mode(self) -> bool:
        return self._state.record_config is not None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.target.revoke()
        self._server.interrupt_requests()
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
            self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError('owned_form_fixture_shutdown_timeout')

    def verify_complete(self):
        if not self._state.complete():
            raise ValueError('owned_form_fixture_execution_incomplete')

    def read_whole_record(self, *, profile_sha256: str, form_plan_sha256: str,
                          state_plan_sha256: str) -> bytes:
        self.target.assert_plan(profile_sha256, form_plan_sha256)
        self.target.assert_state_plan(state_plan_sha256)
        with self._readback_lock:
            if self._readback_used or self._state.record_config is None or not self._state.after_seen:
                raise ValueError('owned_record_form_readback_unavailable')
            self._readback_used = True
        url = self.target.state_url
        token = self.target.request_authorization('GET', url)
        with self.target.connect(profile_sha256, form_plan_sha256, state_plan_sha256,
                                 'GET', url, timeout=2) as secure:
            request = (f'GET {urlsplit(url).path} HTTP/1.1\r\n'
                       f'Host: {urlsplit(url).netloc}\r\n'
                       f'X-AOS-Owned-Form: {token}\r\nConnection: close\r\n\r\n')
            secure.sendall(request.encode('ascii'))
            response = HTTPResponse(secure)
            try:
                response.begin()
                if response.status != 200:
                    raise ValueError('owned_record_form_readback_unavailable')
                payload = response.read(8193)
                if len(payload) > 8192:
                    raise ValueError('owned_record_form_readback_invalid')
            finally:
                response.close()
        result = extract_owned_whole_record(payload)
        if json.loads(result) != _whole_record_observation(
                self._state.record_config, self._state.committed_record):
            raise ValueError('owned_record_form_readback_mismatch')
        return result

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        self.close()


def _private_file_bytes(path: Path, limit: int) -> bytes:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0)
                             | getattr(os, 'O_CLOEXEC', 0))
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or metadata.st_size <= 0
                or metadata.st_size > limit):
            raise ValueError('owned_form_fixture_private_file_required')
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(4096, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
    except OSError as exc:
        raise ValueError('owned_form_fixture_private_file_unavailable') from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not content or len(content) > limit or len(content) != metadata.st_size:
        raise ValueError('owned_form_fixture_private_file_size_invalid')
    return bytes(content)
