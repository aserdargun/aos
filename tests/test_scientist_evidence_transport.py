"""Synthetic CPU bytes through the public reviewed schema, not GPU proof or admission."""

from copy import deepcopy
import hashlib
import json
import unittest

from aos.contracts import REPO_ROOT
from aos.scientist_evidence_transport import ScientistEvidenceCodec, SCHEMA, request_schema
from aos.scientist_protocol import scientist_request_sha256
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import BrokerPeer, ScientistAdmissionError

from test_scientist_admission_history import BOOT_ID, admission_capture_fixture
from test_scientist_decider_receipt import request_fixture


TRANSPORT_PIN = '7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7'
EVIDENCE_PIN = 'aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c'


def evidence_codec_fixture():
    raw = (REPO_ROOT / 'schemas/scientist_evidence_transport.schema.json').read_bytes()
    return ScientistEvidenceCodec(raw, transport_schema_sha256=TRANSPORT_PIN,
                                  evidence_schema_sha256=EVIDENCE_PIN)


def evidence_request_fixture(operation='capability'):
    infer = request_fixture()
    peer = BrokerPeer(pid=123, uid=1000, start_ticks=20, boot_id=BOOT_ID,
                      invocation_id='1' * 32, control_group='/synthetic/broker')
    binding = admission_capture_fixture(infer, peer)['admission_binding']
    request = {'schema': SCHEMA, 'version': 2, 'op': operation, 'control_id': 'd' * 32,
               'profile_id': infer.profile_id, 'deployment_digest': infer.deployment_digest,
               'target': {'request_id': infer.request_id, 'request_sha256': scientist_request_sha256(infer),
                          'original_peer_generation_sha256': digest(binding['caller_generation'])},
               'expected_capability_sha256': None if operation == 'capability' else 'e' * 64,
               'evidence_schema_sha256': EVIDENCE_PIN, 'transport_schema_sha256': TRANSPORT_PIN}
    return request, binding


def evidence_response_fixture(request, binding):
    if request['op'] == 'capability':
        capability = {'target': deepcopy(request['target']), 'control_binding': deepcopy(binding),
                      'operations': ['cancel', 'reconcile', 'status'], 'boot_id': BOOT_ID,
                      'issued_boottime': 100.125, 'expires_boottime': 160.125}
        data = {'capability': capability, 'admission': 'denied', 'reason_code': 'retained_target_only',
                'evidence_schema_sha256': EVIDENCE_PIN, 'transport_schema_sha256': TRANSPORT_PIN}
        fingerprint = digest(capability)
    else:
        data = {'schema': 'aos-scientist-terminal-evidence.v2', 'version': 2,
                'target': deepcopy(request['target']),
                'terminal_canonical': canonical({'synthetic_transport_only': 'kanıt değil', 'seconds': 1.0}),
                'allocation_canonical': None, 'drain_canonical': None,
                'no_admission_canonical': None, 'result_canonical': None}
        fingerprint = request['expected_capability_sha256']
    return {'schema': SCHEMA, 'version': 2, 'control_id': request['control_id'], 'op': request['op'],
            'ok': True, 'capability_sha256': fingerprint, 'data': data, 'error': None}


class ScientistEvidenceTransportTests(unittest.TestCase):
    def setUp(self):
        self.codec = evidence_codec_fixture()
        self.request, self.binding = evidence_request_fixture()

    def decode(self, response, request=None):
        return self.codec.decode_response(canonical(response).encode(),
                                          self.codec.encode_request(request or self.request))

    def test_public_schema_exact_hash_and_local_request_schema(self):
        raw = (REPO_ROOT / 'schemas/scientist_evidence_transport.schema.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         '0a18309a4c19824b7753179220eda840e7a8a545aea09b54023cfcce15737f99')
        self.assertEqual(digest(json.loads(raw)), TRANSPORT_PIN)
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/scientist_evidence_transport_request.schema.json').read_bytes()),
                         request_schema())

    def test_original_capability_bytes_and_seconds_are_preserved_without_freshness_claim(self):
        response = evidence_response_fixture(self.request, self.binding)
        before = canonical(response)
        self.assertEqual(self.decode(response), response)
        self.assertEqual(canonical(response), before)
        self.assertEqual(self.decode(response)['data']['capability']['expires_boottime'], 160.125)
        self.assertFalse(self.codec.encode_request(self.request).endswith(b'\n'))

    def test_legacy_preimage_strings_are_transport_not_physical_proof(self):
        request, binding = evidence_request_fixture('reconcile')
        response = evidence_response_fixture(request, binding)
        decoded = self.decode(response, request)
        self.assertEqual(decoded, response)
        self.assertIn('kanıt değil', decoded['data']['terminal_canonical'])
        self.assertEqual(json.loads(decoded['data']['terminal_canonical'])['seconds'], 1.0)

    def test_request_wrong_version_operation_target_pins_and_unknown_are_denied(self):
        for field, value in [('version', 2.0), ('version', True), ('op', 'cancel'), ('target', None),
                             ('schema', 'aos-scientist-control.v1'), ('extra', True),
                             ('expected_capability_sha256', 'a' * 64),
                             ('evidence_schema_sha256', 'f' * 64), ('transport_schema_sha256', 'f' * 64)]:
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError):
                self.codec.encode_request({**self.request, field: value})

    def test_response_correlation_pin_nested_unknown_and_float_integer_denied(self):
        original = evidence_response_fixture(self.request, self.binding)
        for change in ('control_id', 'op', 'version', 'schema', 'fingerprint', 'pin', 'unknown', 'pid_float', 'admission'):
            response = deepcopy(original)
            if change in ('control_id', 'op', 'schema'):
                response[change] = 'foreign'
            elif change == 'version':
                response['version'] = 2.0
            elif change == 'fingerprint':
                response['capability_sha256'] = 'f' * 64
            elif change == 'pin':
                response['data']['evidence_schema_sha256'] = 'f' * 64
            elif change == 'admission':
                response['data']['admission'] = 'enabled'
            else:
                generation = response['data']['capability']['control_binding']['caller_generation']
                generation['unreviewed' if change == 'unknown' else 'pid'] = True if change == 'unknown' else 124.0
                response['capability_sha256'] = digest(response['data']['capability'])
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError):
                self.decode(response)

    def test_reconcile_requires_exact_target_capability_and_closed_container(self):
        request, binding = evidence_request_fixture('reconcile')
        original = evidence_response_fixture(request, binding)
        for change in ('target', 'capability', 'unknown', 'version', 'missing'):
            response = deepcopy(original)
            if change == 'target':
                response['data']['target']['request_sha256'] = 'f' * 64
            elif change == 'capability':
                response['capability_sha256'] = 'f' * 64
            elif change == 'unknown':
                response['data']['physical_release'] = True
            elif change == 'version':
                response['data']['version'] = 2.0
            else:
                response['data'].pop('terminal_canonical')
            with self.subTest(change=change), self.assertRaises(ScientistAdmissionError):
                self.decode(response, request)

    def test_noncanonical_duplicate_nonfinite_invalid_utf8_and_oversized_denied(self):
        valid = self.codec.encode_request(self.request)
        for raw in (valid + b'\n', b'{"x":1,"x":1}', b'{"x":NaN}', b'\xff', b' ' * 8192):
            with self.subTest(raw=raw[:30]), self.assertRaises(ScientistAdmissionError):
                self.codec.decode_request(raw)
        response = evidence_response_fixture(self.request, self.binding)
        for raw in (canonical(response).encode() + b'\n', b' ' * (128 * 1024)):
            with self.assertRaises(ScientistAdmissionError):
                self.codec.decode_response(raw, valid)

    def test_embedded_json_is_canonical_utf8_bounded_and_never_normalized(self):
        request, binding = evidence_request_fixture('reconcile')
        original = evidence_response_fixture(request, binding)
        for encoded in ('{"duplicate":1,"duplicate":2}', '{"x":Infinity}', '{"x": 1}',
                        '{"x":"\\u015f"}', '{"x":"' + 'a' * (96 * 1024) + '"}'):
            response = deepcopy(original)
            response['data']['result_canonical'] = encoded
            with self.subTest(encoded=encoded[:20]), self.assertRaises(ScientistAdmissionError):
                self.decode(response, request)

    def test_error_retryability_is_metadata_not_permission_to_retry(self):
        response = {'schema': SCHEMA, 'version': 2, 'control_id': self.request['control_id'],
                    'op': 'capability', 'ok': False, 'capability_sha256': None, 'data': None,
                    'error': {'code': 'unauthorized', 'retryable': False}}
        self.assertEqual(self.decode(response), response)
        response['error']['retryable'] = True
        with self.assertRaises(ScientistAdmissionError):
            self.decode(response)

    def test_remote_and_missing_references_open_objects_wrong_pin_denied_before_validation(self):
        original = json.loads((REPO_ROOT / 'schemas/scientist_evidence_transport.schema.json').read_bytes())
        for kind in ('remote', 'missing', 'open', 'pin'):
            schema = deepcopy(original)
            if kind == 'remote':
                schema['$defs']['unreachable'] = {'$ref': 'https://invalid.example/schema'}
            elif kind == 'missing':
                schema['$defs']['unreachable'] = {'$ref': '#/$defs/absent'}
            elif kind == 'open':
                schema['$defs']['unreachable'] = {'type': 'object', 'additionalProperties': True}
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                ScientistEvidenceCodec(canonical(schema).encode(),
                    transport_schema_sha256='f' * 64 if kind == 'pin' else digest(schema),
                    evidence_schema_sha256=EVIDENCE_PIN)

    def test_trailing_newline_nested_identity_is_denied_even_with_rehashed_capability(self):
        response = evidence_response_fixture(self.request, self.binding)
        response['data']['capability']['control_binding']['policy_sha256'] += '\n'
        response['capability_sha256'] = digest(response['data']['capability'])
        with self.assertRaises(ScientistAdmissionError):
            self.decode(response)

    def test_cleanup_grant_variant_retains_original_admission_and_scope(self):
        response = evidence_response_fixture(self.request, self.binding)
        grant = {'schema': 'aos-scientist-cleanup-grant.v1', 'version': 1,
                 'target': deepcopy(self.request['target']), 'profile_id': self.request['profile_id'],
                 'deployment_digest': self.request['deployment_digest'],
                 'caller_generation': deepcopy(self.binding['caller_generation']),
                 'resolver_generation': deepcopy(self.binding['server_generation']),
                 'policy_sha256': '3' * 64, 'source_fingerprints': {'aos': '4' * 64, 'scientist': '5' * 64},
                 'control_schema_sha256': '6' * 64, 'operations': ['reconcile'],
                 'original_admission_binding': deepcopy(self.binding),
                 'original_admission_binding_sha256': digest(self.binding),
                 'original_cleanup_authorization': None, 'original_cleanup_authorization_sha256': None}
        capability = {'cleanup_grant': grant, 'cleanup_grant_sha256': digest(grant), 'boot_id': BOOT_ID,
                      'issued_boottime': 100.125, 'expires_boottime': 160.125}
        response['data'].update(capability=capability, reason_code='cleanup_only')
        response['capability_sha256'] = digest(capability)
        self.assertEqual(self.decode(response), response)
        grant['original_admission_binding_sha256'] = 'f' * 64
        capability['cleanup_grant_sha256'] = digest(grant)
        response['capability_sha256'] = digest(capability)
        with self.assertRaises(ScientistAdmissionError):
            self.decode(response)
