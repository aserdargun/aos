import hashlib
from http.client import HTTPResponse
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import AOSFault, ErrorCode, REPO_ROOT, canonical
from aos.dataset import digest
from aos.owned_form_fixture import (
    ENTRY_BODY, HOST, RECEIPT_BODY, STATE_AFTER, STATE_BEFORE,
    OwnedFormFixture, OwnedFormFixtureTarget,
)
from aos.owned_form_invocation_session import (
    provision_owned_synthetic_form_invocation, verify_owned_form_invocation_manifest,
)
from aos.web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from aos.web_application_binding import WebTaskContract
from aos.web_https_form_state_probe import (
    ExactHTTPSFormStateProbe, form_state_request_sha256, plan_web_https_form_state,
)
from aos.web_https_form_transport import (
    ExactHTTPSFormRelay, ExactHTTPSFormTransport, plan_web_https_form,
)
from aos.desktop_tasks import DesktopScheduler


@unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL')
class OwnedFormFixtureIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='aos-owned-fixture-isolation-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.certificate = cls.directory / 'certificate.pem'
        cls.key = cls.directory / 'key.pem'
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-days', '1', '-subj', '/CN=' + HOST,
            '-addext', 'subjectAltName=DNS:' + HOST,
            '-keyout', str(cls.key), '-out', str(cls.certificate),
        ], check=True, capture_output=True, timeout=15)
        cls.certificate.chmod(0o600)
        cls.key.chmod(0o600)
        cls.certificate_sha256 = hashlib.sha256(cls.certificate.read_bytes()).hexdigest()

    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.origin = f'https://{HOST}:{self.port}'
        self.arguments = {
            'origin': self.origin,
            'profile_sha256': '1' * 64,
            'form_plan_sha256': '2' * 64,
            'state_plan_sha256': '3' * 64,
            'entry_url': self.origin + '/entry',
            'submit_url': self.origin + '/submit',
            'receipt_url': self.origin + '/receipt',
            'state_url': self.origin + '/state',
            'expected_body': b'message=alpha',
            'certificate_file': self.certificate,
            'key_file': self.key,
            'certificate_sha256': self.certificate_sha256,
        }

    def fixture(self):
        fixture = OwnedFormFixture(self.listener.fileno(), **self.arguments)
        self.addCleanup(fixture.close)
        return fixture

    def test_capability_cannot_be_constructed_or_serialized_by_caller(self):
        arguments = {key: value for key, value in self.arguments.items()
                     if key not in {'certificate_file', 'key_file', 'expected_body'}}
        with self.assertRaisesRegex(ValueError, 'owner_only'):
            OwnedFormFixtureTarget(object(), **arguments, port=self.port,
                                   certificate_pem=self.certificate.read_bytes(),
                                   request_token='synthetic-untrusted-token')
        with self.assertRaisesRegex(TypeError, 'not_serializable'):
            pickle.dumps(self.fixture().target)

    def test_wrong_plan_state_method_and_url_fail_before_connect(self):
        fixture = self.fixture()
        cases = [
            ('0' * 64, '2' * 64, None, 'GET', self.origin + '/entry'),
            ('1' * 64, '0' * 64, None, 'GET', self.origin + '/entry'),
            ('1' * 64, '2' * 64, None, 'GET', self.origin + '/state'),
            ('1' * 64, '2' * 64, '0' * 64, 'GET', self.origin + '/state'),
            ('1' * 64, '2' * 64, None, 'POST', self.origin + '/entry'),
            ('1' * 64, '2' * 64, None, 'GET', self.origin + '/off-plan'),
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                fixture.target.connect(*arguments, timeout=1)
        self.assertFalse(fixture._state.entry_seen)

    def test_revoked_target_denies_connection(self):
        target = self.fixture().target
        target.revoke()
        with self.assertRaises(ValueError):
            target.connect('1' * 64, '2' * 64, None, 'GET',
                           self.origin + '/entry', timeout=1)

    def request(self, fixture, method, path, body=b'', extra_headers=''):
        url = self.origin + path
        state_sha256 = '3' * 64 if path == '/state' else None
        token = fixture.target.request_authorization(method, url)
        with fixture.target.connect('1' * 64, '2' * 64, state_sha256,
                                    method, url, timeout=2) as secure:
            headers = (f'{method} {path} HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n'
                       f'X-AOS-Owned-Form: {token}\r\nConnection: close\r\n'
                       + extra_headers)
            if method == 'POST':
                headers += ('Content-Type: application/x-www-form-urlencoded\r\n'
                            f'Content-Length: {len(body)}\r\n')
            secure.sendall(headers.encode('ascii') + b'\r\n' + body)
            response = HTTPResponse(secure)
            try:
                response.begin()
                return response.status, response.read(), response.getheader('Location')
            finally:
                response.close()

    def test_exact_pinned_tls_sequence_completes_once_without_dns_override(self):
        fixture = self.fixture()
        self.assertEqual(self.request(fixture, 'GET', '/entry'), (200, ENTRY_BODY, None))
        self.assertEqual(self.request(fixture, 'GET', '/state'), (200, STATE_BEFORE, None))
        self.assertEqual(self.request(fixture, 'POST', '/submit', b'message=alpha'),
                         (303, b'', self.origin + '/receipt'))
        self.assertEqual(self.request(fixture, 'GET', '/receipt'), (200, RECEIPT_BODY, None))
        self.assertEqual(self.request(fixture, 'GET', '/state'), (200, STATE_AFTER, None))
        fixture.verify_complete()
        self.assertGreaterEqual(self.request(fixture, 'POST', '/submit', b'message=alpha')[0], 400)
        with self.assertRaisesRegex(ValueError, 'request_limit'):
            self.request(fixture, 'GET', '/entry')

    def test_disabling_hostname_verification_denies_connection(self):
        fixture = self.fixture()
        fixture.target.tls_context.check_hostname = False
        with self.assertRaisesRegex(ValueError, 'tls_verification_required'):
            self.request(fixture, 'GET', '/entry')
        self.assertFalse(fixture._state.entry_seen)

    def test_real_transport_and_state_probe_keep_separate_one_use_permits(self):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            profile_source = json.loads((REPO_ROOT / 'examples/web_application_profile.json').read_text())['profile']
            profile = WebApplicationProfile.model_validate({
                **profile_source, 'entry_url': self.origin + '/entry',
                'allowed_origins': [self.origin],
            })
            profile_sha256 = profile_report(profile).profile_sha256
            profiles = WebApplicationProfiles(Path(directory) / 'profiles')
            profiles.register(profile, confirm_sha256=profile_sha256)
            task_source = json.loads((REPO_ROOT / 'examples/web_application_binding.json').read_text())['task']
            task = WebTaskContract.model_validate({
                **task_source, 'profile_sha256': profile_sha256,
                'entry_url': profile.entry_url, 'allowed_origins': profile.allowed_origins,
            })
            body = b'message=alpha'
            plan = plan_web_https_form(
                profiles, task, submit_url=self.origin + '/submit',
                receipt_url=self.origin + '/receipt',
                body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
            plan_sha256 = digest(plan.model_dump())
            preview_transport = ExactHTTPSFormTransport(
                profiles, task, plan, plan_sha256, consume_approval=lambda _checksum: False,
                tls_context=ssl.create_default_context())
            state_plan = plan_web_https_form_state(
                preview_transport, state_url=self.origin + '/state',
                expected_before_sha256=hashlib.sha256(STATE_BEFORE).hexdigest(),
                expected_after_sha256=hashlib.sha256(STATE_AFTER).hexdigest())
            self.arguments.update(profile_sha256=profile_sha256,
                                  form_plan_sha256=plan_sha256,
                                  state_plan_sha256=digest(state_plan.model_dump()))
            fixture = self.fixture()
            permits = set()

            def consume(checksum):
                if checksum not in permits:
                    return False
                permits.remove(checksum)
                return True

            transport = ExactHTTPSFormTransport(
                profiles, task, plan, plan_sha256, consume_approval=consume,
                owned_form_target=fixture.target)
            probe = ExactHTTPSFormStateProbe(
                transport, state_plan, digest(state_plan.model_dump()), consume_approval=consume)
            operations = [
                (transport.open_entry, digest({'method': 'GET', 'url': plan.entry_url}), {}),
                (probe.observe_before, form_state_request_sha256(state_plan, 'before'), {}),
                (transport.submit, digest({'method': 'POST', 'url': plan.submit_url,
                                          'body_sha256': plan.body_sha256}), {'body': body}),
                (transport.read_receipt, digest({'method': 'GET', 'url': plan.receipt_url}), {}),
                (probe.observe_after, form_state_request_sha256(state_plan, 'after'), {}),
            ]
            for operation, checksum, arguments in operations:
                with self.assertRaisesRegex(ValueError, 'approval_denied'):
                    operation(confirm_request_sha256=checksum, **arguments)
                permits.add(checksum)
                result = operation(confirm_request_sha256=checksum, **arguments)
                self.assertEqual(permits, set())
            fixture.verify_complete()
            self.assertTrue(result.declared_response_transition_observed)
            self.assertFalse(result.site_outcome_verified)
            self.assertFalse(result.account_verified)

    def test_duplicate_authorization_header_does_not_consume_entry(self):
        fixture = self.fixture()
        token = fixture.target.request_authorization('GET', self.origin + '/entry')
        self.assertGreaterEqual(self.request(
            fixture, 'GET', '/entry', extra_headers=f'X-AOS-Owned-Form: {token}\r\n')[0], 400)
        self.assertFalse(fixture._state.entry_seen)

    def test_duplicate_host_header_does_not_consume_entry(self):
        fixture = self.fixture()
        self.assertGreaterEqual(self.request(
            fixture, 'GET', '/entry', extra_headers=f'Host: {HOST}:{self.port}\r\n')[0], 400)
        self.assertFalse(fixture._state.entry_seen)

    def test_uncredentialed_local_tls_client_cannot_consume_fixture(self):
        fixture = self.fixture()
        context = ssl.create_default_context(cadata=self.certificate.read_text())
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as connection:
            with context.wrap_socket(connection, server_hostname=HOST) as secure:
                secure.sendall((f'GET /entry HTTP/1.1\r\nHost: {HOST}:{self.port}'
                                '\r\nConnection: close\r\n\r\n').encode('ascii'))
                response = HTTPResponse(secure)
                try:
                    response.begin()
                    self.assertGreaterEqual(response.status, 400)
                    response.read()
                finally:
                    response.close()
        self.assertFalse(fixture._state.entry_seen)
        self.assertFalse(fixture._state.submitted)

    def test_failed_constructor_keeps_original_listener_without_fd_leak(self):
        baseline = len(os.listdir('/proc/self/fd'))
        for changes in (
                {'origin': 'https://wrong.aos.invalid:' + str(self.port)},
                {'certificate_sha256': '0' * 64},
                {'expected_body': b''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OwnedFormFixture(self.listener.fileno(), **(self.arguments | changes))
            self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)
            self.assertEqual(self.listener.getsockname()[1], self.port)

    def test_certificate_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            link = Path(directory) / 'linked.pem'
            link.symlink_to(self.certificate)
            with self.assertRaisesRegex(ValueError, 'private_file'):
                OwnedFormFixture(self.listener.fileno(),
                                 **(self.arguments | {'certificate_file': link}))

    def test_certificate_fifo_is_rejected_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            fifo = Path(directory) / 'certificate.pipe'
            os.mkfifo(fifo, 0o600)
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, 'private_file'):
                OwnedFormFixture(self.listener.fileno(),
                                 **(self.arguments | {'certificate_file': fifo}))
            self.assertLess(time.monotonic() - started, 1)

    def test_idle_plain_tcp_client_cannot_prevent_bounded_shutdown(self):
        fixture = self.fixture()
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        self.addCleanup(connection.close)
        started = time.monotonic()
        fixture.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(fixture._thread.is_alive())

    def test_partial_tls_request_cannot_prevent_bounded_shutdown(self):
        fixture = self.fixture()
        context = ssl.create_default_context(cadata=self.certificate.read_text())
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as connection:
            with context.wrap_socket(connection, server_hostname=HOST) as secure:
                secure.sendall(b'POST /submit HTTP/1.1\r\n')
                started = time.monotonic()
                fixture.close()
                self.assertLess(time.monotonic() - started, 4)
                self.assertFalse(fixture._thread.is_alive())

    def test_shutdown_releases_owned_listener_and_repeated_close_is_safe(self):
        fixture = self.fixture()
        self.listener.close()
        fixture.close()
        fixture.close()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as replacement:
            replacement.bind(('127.0.0.1', self.port))
        with self.assertRaises(ValueError):
            fixture.target.assert_plan('1' * 64, '2' * 64)

    def test_generated_source_bundle_matches_schema_private_modes_and_exact_hashes(self):
        schema = json.loads((REPO_ROOT / 'schemas/owned_form_invocation_session.schema.json').read_text())
        with tempfile.TemporaryDirectory(dir=self.directory) as directory:
            destination = Path(directory) / 'owned-form'
            bundle = provision_owned_synthetic_form_invocation(destination, self.port)
            jsonschema.Draft202012Validator(schema).validate(bundle['manifest'])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            for name, checksum in bundle['source_pins'].items():
                source = destination / name
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), checksum)
            self.assertEqual(hashlib.sha256((destination / 'manifest.json').read_bytes()).hexdigest(),
                             bundle['manifest_sha256'])
            self.assertTrue(all(value is False for value in bundle['manifest']['claims'].values()))
            with self.assertRaisesRegex(ValueError, 'bundle_target_invalid'):
                provision_owned_synthetic_form_invocation(destination, self.port)

    def test_manifest_schema_rejects_foreign_sources_and_execution_claims(self):
        schema = json.loads((REPO_ROOT / 'schemas/owned_form_invocation_session.schema.json').read_text())
        manifest = json.loads((REPO_ROOT / 'examples/owned_form_invocation_session.json').read_text())['manifest']
        validator = jsonschema.Draft202012Validator(schema)
        validator.validate(manifest)
        changes = [
            {'sources': manifest['sources'] | {'../foreign.json': 'a' * 64}},
            {'sources': {name: checksum for name, checksum in manifest['sources'].items()
                         if name != 'remote-form-plan.json'}},
            {'claims': manifest['claims'] | {'skill_executed': True}},
            {'claims': manifest['claims'] | {'training_ready': True}},
            {'synthetic': False},
            {'origin': 'https://public.example.com:443'},
            {'invocation_sha256': 'invalid'},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(jsonschema.ValidationError):
                validator.validate(manifest | change)

    def test_manifest_loader_rechecks_sources_and_rejects_nonboolean_claims(self):
        with tempfile.TemporaryDirectory(prefix='owned-manifest-test-', dir=REPO_ROOT / 'data') as directory:
            destination = Path(directory) / 'owned-form'
            bundle = provision_owned_synthetic_form_invocation(destination, self.port)
            expected = bundle['manifest']
            self.assertEqual(verify_owned_form_invocation_manifest(
                destination, bundle['manifest_sha256']), expected)
            manifest_file = destination / 'manifest.json'
            for change in (
                    {'claims': expected['claims'] | {'skill_executed': 0}},
                    {'profile_sha256': 'not-a-sha256'}):
                content = canonical(expected | change).encode('utf-8')
                manifest_file.write_bytes(content)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    verify_owned_form_invocation_manifest(
                        destination, hashlib.sha256(content).hexdigest())
            manifest_file.write_bytes(canonical(expected).encode('utf-8'))
            (destination / 'remote-form-value.txt').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'source_pin_changed'):
                verify_owned_form_invocation_manifest(destination, bundle['manifest_sha256'])

    def test_rejected_second_start_does_not_revoke_existing_owned_run(self):
        scheduler = object.__new__(DesktopScheduler)
        scheduler.restart_quiesced = False
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler._owned_form_lifecycle = 'running'
        scheduler.remote_form_owned_fixture = object()
        failure = AOSFault(ErrorCode.UNSAFE_ACTION, 'Synthetic admission rejected')
        with patch.object(scheduler, '_start', side_effect=failure), \
                patch.object(scheduler, '_consume_owned_form') as consume:
            with self.assertRaises(AOSFault):
                scheduler.start('synthetic-stale-lease', 1, 'browser_remote_form')
            consume.assert_not_called()
        self.assertEqual(scheduler._owned_form_lifecycle, 'running')

    def test_each_manual_audit_rechecks_sources_instead_of_trusting_cached_report(self):
        scheduler = object.__new__(DesktopScheduler)
        report = json.loads((REPO_ROOT / 'examples/site_skill_form_invocation_audit.json').read_text())['report']
        scheduler.owned_skill_planning = None
        run_id = 'synthetic-owned-audit-run'
        report['run_ref'] = digest({'run_id': run_id})
        scheduler.remote_form_owned_fixture = object()
        scheduler.remote_form_owned_manifest = {'invocation_sha256': report['invocation_sha256']}
        scheduler.remote_form_owned_auditor = Mock(return_value=report)
        scheduler.task = None
        scheduler.job_id = 'synthetic-owned-audit-job'
        scheduler._owned_form_source_job_id = scheduler.job_id
        scheduler.sequences = SimpleNamespace(reserved=False)
        scheduler.store = SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_arguments: SimpleNamespace(fetchone=lambda: {
                'kind': 'browser_remote_form', 'status': 'succeeded', 'run_id': run_id,
            })))
        scheduler._owned_form_lifecycle = 'completed'
        scheduler._owned_form_run_id = run_id
        scheduler._owned_form_audit = None
        self.assertEqual(scheduler.audit_owned_form_invocation()['status'], 'verified')
        self.assertEqual(scheduler.audit_owned_form_invocation()['status'], 'verified')
        self.assertEqual(scheduler.remote_form_owned_auditor.call_count, 2)

    def test_form_relay_binds_long_private_workspace_without_relaxing_authentication(self):
        with tempfile.TemporaryDirectory(prefix='owned-long-relay-', dir=REPO_ROOT / 'data') as directory:
            root = Path(directory)
            workspace = root / ('workspace-' + 'x' * 80)
            workspace.mkdir(mode=0o700)
            socket_path = workspace / 'relay.sock'
            self.assertGreater(len(os.fsencode(socket_path)), 107)
            bundle = provision_owned_synthetic_form_invocation(root / 'bundle', self.port)
            transport = ExactHTTPSFormTransport(
                bundle['profiles'], bundle['task'], bundle['form_plan'],
                bundle['form_plan_sha256'], consume_approval=lambda _checksum: False,
                tls_context=ssl.create_default_context())
            relay = ExactHTTPSFormRelay(transport)
            stopped = threading.Event()
            failures = []

            def serve():
                try:
                    relay.serve(socket_path, wait_seconds=2, stop_event=stopped)
                except Exception as error:
                    failures.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 1
                while thread.is_alive() and not socket_path.exists() and time.monotonic() < deadline:
                    stopped.wait(.01)
                self.assertTrue(socket_path.exists(), str(failures))
                descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(1)
                        client.connect(f'/proc/self/fd/{descriptor}/relay.sock')
                        client.sendall(b'{"token":"untrusted-synthetic-client"}\n')
                        self.assertEqual(client.recv(1024), b'{"error":"denied"}\n')
                finally:
                    os.close(descriptor)
                self.assertFalse(transport._entry_attempted)
            finally:
                stopped.set()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(socket_path.exists())
            self.assertTrue(all(isinstance(error, TimeoutError) for error in failures))


if __name__ == '__main__':
    unittest.main()
