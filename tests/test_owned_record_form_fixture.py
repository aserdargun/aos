import hashlib
from http.client import HTTPResponse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

from aos.owned_form_fixture import (HOST, OwnedFormFixture, extract_owned_whole_record,
                                   owned_record_form_bodies)


@unittest.skipUnless(shutil.which('openssl'), 'Requires local OpenSSL')
class OwnedRecordFormFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='aos-owned-record-fixture-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.certificate = cls.directory / 'certificate.pem'
        cls.key = cls.directory / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=' + HOST, '-addext', 'subjectAltName=DNS:' + HOST,
                        '-keyout', str(cls.key), '-out', str(cls.certificate)],
                       check=True, capture_output=True, timeout=15)
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
        self.record_config = {
            'scope': {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant',
                      'account_role': 'tester'},
            'fields': [{'name': 'contact_name', 'label': 'Contact name', 'value': 'Ada Lovelace'},
                       {'name': 'note', 'label': 'Note <safe & clear>', 'value': '<saved & checked>'}],
            'outcome_field': 'contact_name', 'marker_id': 'outcome'}
        self.bodies = owned_record_form_bodies(self.record_config)

    def fixture(self, **changes):
        arguments = {
            'origin': self.origin, 'profile_sha256': '1' * 64,
            'form_plan_sha256': '2' * 64, 'state_plan_sha256': '3' * 64,
            'entry_url': self.origin + '/entry', 'submit_url': self.origin + '/submit',
            'receipt_url': self.origin + '/receipt', 'state_url': self.origin + '/state',
            'expected_body': self.bodies['form_body'], 'record_config': self.record_config,
            'certificate_file': self.certificate, 'key_file': self.key,
            'certificate_sha256': self.certificate_sha256,
        } | changes
        fixture = OwnedFormFixture(self.listener.fileno(), **arguments)
        self.addCleanup(fixture.close)
        return fixture

    def request(self, fixture, method, path, body=b''):
        url = self.origin + path
        token = fixture.target.request_authorization(method, url)
        state_hash = '3' * 64 if path == '/state' else None
        with fixture.target.connect('1' * 64, '2' * 64, state_hash, method, url, timeout=2) as secure:
            headers = (f'{method} {path} HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n'
                       f'X-AOS-Owned-Form: {token}\r\nConnection: close\r\n')
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

    def complete_regular_sequence(self, fixture):
        self.assertEqual(self.request(fixture, 'GET', '/entry')[:2], (200, self.bodies['entry']))
        self.assertEqual(self.request(fixture, 'GET', '/state')[:2], (200, self.bodies['before']))
        self.assertEqual(self.request(fixture, 'POST', '/submit', self.bodies['form_body'])[0], 303)
        self.assertEqual(self.request(fixture, 'GET', '/receipt')[:2][0], 200)
        status, after, _ = self.request(fixture, 'GET', '/state')
        self.assertEqual(status, 200)
        self.assertEqual(after, self.bodies['after'])
        fixture.verify_complete()

    def test_host_owned_multi_field_submit_and_same_url_tls_readback(self):
        fixture = self.fixture()
        self.assertTrue(fixture.record_mode)
        self.assertIn(b'Note &lt;safe &amp; clear&gt;', self.bodies['entry'])
        self.assertIn(b'aos-whole-record', self.bodies['after'])
        self.complete_regular_sequence(fixture)
        self.assertEqual(fixture._state.committed_record, {
            'contact_name': 'Ada Lovelace', 'note': '<saved & checked>'})
        observed = fixture.read_whole_record(profile_sha256='1' * 64,
                                             form_plan_sha256='2' * 64,
                                             state_plan_sha256='3' * 64)
        self.assertEqual(json.loads(observed), {
            'schema_version': '1.0',
            'scope': {'application_id': 'synthetic-app', 'tenant_id': 'synthetic-tenant',
                      'account_role': 'tester'},
            'record': {'contact_name': 'Ada Lovelace', 'note': '<saved & checked>'},
            'reported_post_count': 1, 'effect_status': 'committed'})
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            fixture.read_whole_record(profile_sha256='1' * 64,
                                      form_plan_sha256='2' * 64, state_plan_sha256='3' * 64)

    def test_changed_field_set_is_rejected_without_commit(self):
        fixture = self.fixture()
        self.request(fixture, 'GET', '/entry')
        self.request(fixture, 'GET', '/state')
        status, _, _ = self.request(fixture, 'POST', '/submit', b'contact_name=Ada+Lovelace&note=altered')
        self.assertEqual(status, 409)
        self.assertFalse(fixture._state.submitted)
        self.assertIsNone(fixture._state.committed_record)

    def test_scope_and_plan_pins_are_required_before_extra_state_request(self):
        fixture = self.fixture()
        self.complete_regular_sequence(fixture)
        for changes in ({'profile_sha256': '0' * 64}, {'form_plan_sha256': '0' * 64},
                        {'state_plan_sha256': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fixture.read_whole_record(profile_sha256=changes.get('profile_sha256', '1' * 64),
                                          form_plan_sha256=changes.get('form_plan_sha256', '2' * 64),
                                          state_plan_sha256=changes.get('state_plan_sha256', '3' * 64))
        self.assertFalse(fixture._state.whole_record_seen)
        self.assertEqual(json.loads(fixture.read_whole_record(profile_sha256='1' * 64,
                          form_plan_sha256='2' * 64, state_plan_sha256='3' * 64))['reported_post_count'], 1)

    def test_repeated_post_and_duplicate_or_noncanonical_readback_reject(self):
        fixture = self.fixture()
        self.complete_regular_sequence(fixture)
        self.assertGreaterEqual(self.request(fixture, 'POST', '/submit', self.bodies['form_body'])[0], 400)
        with self.assertRaisesRegex(ValueError, 'readback_invalid'):
            extract_owned_whole_record(
                b'<script id="aos-whole-record" type="application/json">{}</script>'
                b'<script id="aos-whole-record" type="application/json">{}</script>')
        with self.assertRaisesRegex(ValueError, 'readback_invalid'):
            extract_owned_whole_record(
                b'<script id="aos-whole-record" type="application/json">{"a":1,"a":2}</script>')

    def test_default_scalar_fixture_still_has_no_extra_record_readback(self):
        scalar = OwnedFormFixture(self.listener.fileno(),
            origin=self.origin, profile_sha256='1' * 64, form_plan_sha256='2' * 64,
            state_plan_sha256='3' * 64, entry_url=self.origin + '/entry',
            submit_url=self.origin + '/submit', receipt_url=self.origin + '/receipt',
            state_url=self.origin + '/state', expected_body=b'message=alpha',
            certificate_file=self.certificate, key_file=self.key,
            certificate_sha256=self.certificate_sha256)
        self.addCleanup(scalar.close)
        self.assertFalse(scalar.record_mode)
        self.assertEqual(self.request(scalar, 'GET', '/entry')[:2],
                         (200, b'<html><title>Owned synthetic form</title><h1>Owned form</h1>'
                          b'<form method="post" action="/submit"><label for="message">Message</label>'
                          b'<input id="message" name="message" required>'
                          b'<button type="submit">Save draft</button></form></html>'))
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            scalar.read_whole_record(profile_sha256='1' * 64,
                                     form_plan_sha256='2' * 64, state_plan_sha256='3' * 64)
