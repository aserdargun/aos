"""Synthetic v3 public-schema interoperability; no physical or current authority."""

from copy import deepcopy
import hashlib
import json
import unittest

from aos.contracts import REPO_ROOT
from aos.scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec, SCHEMA, request_schema
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

from test_scientist_evidence_transport import evidence_codec_fixture, EVIDENCE_PIN
from test_scientist_release_proof import release_preimages_fixture, release_envelope_fixture
import test_scientist_budget_witness as budget_cases


TRANSPORT_PIN = 'cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070'


class ScientistRetainedEvidenceTransportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = budget_cases.ScientistBudgetWitnessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.schema = (REPO_ROOT / 'schemas/scientist_retained_evidence_transport.schema.json').read_bytes()
        allocation, drain = release_preimages_fixture(self.fixture.terminal)
        evidence = release_envelope_fixture(self.fixture.terminal, self.fixture.result, allocation, drain)
        witness = deepcopy(self.fixture.witness)
        witness['allocation_binding_sha256'] = digest(allocation)
        self.capability = {'target': deepcopy(witness['target']),
            'control_binding': self.fixture.original.admission_binding.model_dump(mode='json'),
            'operations': ['cancel', 'reconcile', 'status'], 'boot_id': self.fixture.terminal['recorded_boot_id'],
            'issued_boottime': 100.125, 'expires_boottime': 160.125}
        self.request = {'schema': SCHEMA, 'version': 3, 'op': 'reconcile', 'control_id': 'd' * 32,
            'profile_id': witness['profile_id'], 'deployment_digest': witness['deployment_digest'],
            'target': deepcopy(witness['target']), 'expected_capability_sha256': digest(self.capability),
            'evidence_schema_sha256': EVIDENCE_PIN, 'transport_schema_sha256': TRANSPORT_PIN}
        self.response = {'schema': SCHEMA, 'version': 3, 'op': 'reconcile', 'control_id': 'd' * 32,
            'ok': True, 'capability_sha256': digest(self.capability), 'error': None,
            'data': {'evidence': evidence, 'original_budget_witness': witness}}

    def codec(self, **changes):
        options = {'transport_schema_sha256': TRANSPORT_PIN, 'evidence_schema_sha256': EVIDENCE_PIN,
                   'expected_capability': self.capability}
        return ScientistRetainedEvidenceCodec(self.schema, **(options | changes))

    def decode(self, response=None, request=None, **options):
        codec = self.codec(**options)
        return codec.decode_response(canonical(response or self.response).encode(),
                                     codec.encode_request(request or self.request))

    def capability_exchange(self):
        request = self.request | {'op': 'capability', 'expected_capability_sha256': None}
        response = self.response | {'op': 'capability', 'data': {
            'capability': deepcopy(self.capability), 'admission': 'denied', 'reason_code': 'retained_target_only',
            'evidence_schema_sha256': EVIDENCE_PIN, 'transport_schema_sha256': TRANSPORT_PIN}}
        return request, response

    def test_exact_public_schema_pin_and_local_request_schema(self):
        self.assertEqual(hashlib.sha256(self.schema).hexdigest(),
                         'd75c00384650e21f9c3c13a4bb1bcb2aad7009a4ae0580bc644959f4edc68f3d')
        self.assertEqual(digest(json.loads(self.schema)), TRANSPORT_PIN)
        self.assertEqual(json.loads((REPO_ROOT / 'schemas/scientist_retained_evidence_transport_request.schema.json').read_bytes()),
                         request_schema())

    def test_real_original_fixture_and_unicode_preimages_preserved_without_callbacks_or_sql_writes(self):
        before = canonical(self.response), self.fixture.rows(), self.fixture.store.connection.total_changes
        self.assertEqual(self.decode(), self.response)
        self.assertIn('yaz_şimdi', self.decode()['data']['evidence']['result_canonical'])
        self.assertEqual((canonical(self.response), self.fixture.rows(), self.fixture.store.connection.total_changes), before)
        for callback in (self.fixture.source, self.fixture.output, self.fixture.resolver, self.fixture.proof):
            callback.assert_not_called()

    def test_capability_requires_no_retained_preimage_but_never_grants_admission(self):
        request, response = self.capability_exchange()
        self.assertEqual(self.decode(response, request, expected_capability=None), response)
        response['data']['admission'] = 'enabled'
        with self.assertRaises(ScientistAdmissionError):
            self.decode(response, request)

    def test_successful_reconcile_requires_exact_frozen_independent_capability(self):
        with self.assertRaises(ScientistAdmissionError):
            self.decode(expected_capability=None)
        supplied = deepcopy(self.capability)
        codec = self.codec(expected_capability=supplied)
        supplied['control_binding']['policy_sha256'] = 'f' * 64
        self.assertEqual(codec.decode_response(canonical(self.response).encode(), codec.encode_request(self.request)), self.response)
        with self.assertRaises(ScientistAdmissionError):
            self.decode(expected_capability=supplied)

    def test_legacy_codec_and_v3_codec_reject_each_others_wire_without_fallback(self):
        old = evidence_codec_fixture()
        with self.assertRaises(ScientistAdmissionError):
            old.decode_request(self.codec().encode_request(self.request))
        for fields in ({'schema': 'aos-scientist-control-evidence.v2', 'version': 2},
                       {'version': 3.0}, {'version': True}, {'transport_schema_sha256': old.transport_schema_sha256},
                       {'op': 'cancel'}, {'unknown': True}):
            with self.subTest(fields=fields), self.assertRaises(ScientistAdmissionError):
                self.codec().encode_request(self.request | fields)

    def test_correlations_unknown_nested_fields_and_integer_lexemes_deny(self):
        for kind in ('id', 'version', 'op', 'unknown', 'witness_version', 'target', 'profile', 'pin'):
            response = deepcopy(self.response)
            if kind == 'id': response['control_id'] = 'f' * 32
            elif kind == 'version': response['version'] = 3.0
            elif kind == 'op': response['op'] = 'capability'
            elif kind == 'unknown': response['data']['original_budget_witness']['authority'] = True
            elif kind == 'witness_version': response['data']['original_budget_witness']['version'] = 1.0
            elif kind == 'target': response['data']['original_budget_witness']['target']['request_sha256'] = 'f' * 64
            elif kind == 'profile': response['data']['original_budget_witness']['profile_id'] = 'aos.bonsai.vision.v1'
            else: response['data']['original_budget_witness']['profile_config_sha256'] = 'f' * 64
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError): self.decode(response)

    def test_witness_budget_not_reconstructed_from_terminal_or_hash(self):
        for kind in ('hash', 'changed', 'null', 'unknown', 'deadline'):
            response = deepcopy(self.response)
            witness = response['data']['original_budget_witness']
            budget = json.loads(witness['budget_canonical'])
            if kind == 'hash': witness['budget_sha256'] = 'f' * 64
            elif kind == 'null': witness['budget_canonical'] = 'null'
            else:
                budget['unknown' if kind == 'unknown' else 'queue_deadline'] = True if kind == 'unknown' else 190
                witness.update(budget_canonical=canonical(budget), budget_sha256=digest(budget))
                if kind == 'deadline':
                    terminal = json.loads(response['data']['evidence']['terminal_canonical'])
                    terminal['original_budget'] = budget
                    terminal['receipt_sha256'] = digest({key: value for key, value in terminal.items() if key != 'receipt_sha256'})
                    response['data']['evidence']['terminal_canonical'] = canonical(terminal)
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError): self.decode(response)

    def test_original_authority_allocation_and_terminal_hash_cannot_be_swapped(self):
        for kind in ('admission', 'cleanup', 'allocation', 'missing', 'terminal_hash', 'allocation_owner'):
            response = deepcopy(self.response)
            witness, evidence = response['data']['original_budget_witness'], response['data']['evidence']
            if kind in ('admission', 'cleanup'):
                witness['original_' + kind + ('_binding_sha256' if kind == 'admission' else '_authorization_sha256')] = 'f' * 64
            elif kind == 'allocation': witness['allocation_binding_sha256'] = 'f' * 64
            elif kind == 'missing': evidence['allocation_canonical'] = None
            elif kind == 'terminal_hash':
                terminal = json.loads(evidence['terminal_canonical']); terminal['receipt_sha256'] = 'f' * 64
                evidence['terminal_canonical'] = canonical(terminal)
            else:
                allocation = json.loads(evidence['allocation_canonical']); allocation['original_principal']['pid'] += 1
                evidence['allocation_canonical'] = canonical(allocation)
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError): self.decode(response)

    def test_closed_error_returns_metadata_without_capability_or_replay_permission(self):
        error = self.response | {'ok': False, 'capability_sha256': None, 'data': None,
                                 'error': {'code': 'busy', 'retryable': True}}
        self.assertEqual(self.decode(error, expected_capability=None), error)
        error['error']['retryable'] = False
        with self.assertRaises(ScientistAdmissionError): self.decode(error)

    def test_noncanonical_duplicate_invalid_utf8_remote_refs_and_pins_deny(self):
        codec = self.codec()
        for raw in (b'{"x":1,"x":1}', b'\xff', canonical(self.response).encode() + b'\n', b' ' * (128 * 1024)):
            with self.assertRaises(ScientistAdmissionError): codec.decode_response(raw, codec.encode_request(self.request))
        for field in ('budget_canonical',):
            response = deepcopy(self.response)
            response['data']['original_budget_witness'][field] += '\n'
            with self.assertRaises(ScientistAdmissionError): self.decode(response)
        response = deepcopy(self.response)
        response['data']['evidence']['result_canonical'] = '{"x":"\\u015f"}'
        with self.assertRaises(ScientistAdmissionError): self.decode(response)
        schema = json.loads(self.schema)
        schema['$defs']['unreachable'] = {'$ref': 'https://invalid.example/schema'}
        with self.assertRaises(ScientistAdmissionError):
            ScientistRetainedEvidenceCodec(canonical(schema).encode(), transport_schema_sha256=digest(schema),
                                            evidence_schema_sha256=EVIDENCE_PIN)
        with self.assertRaises(ScientistAdmissionError): self.codec(transport_schema_sha256='f' * 64)

    def test_cleanup_capability_binds_original_admission_without_current_authority_claim(self):
        binding = deepcopy(self.capability['control_binding'])
        grant = {'schema': 'aos-scientist-cleanup-grant.v1', 'version': 1, 'target': deepcopy(self.request['target']),
            'profile_id': self.request['profile_id'], 'deployment_digest': self.request['deployment_digest'],
            'caller_generation': deepcopy(binding['caller_generation']), 'resolver_generation': deepcopy(binding['server_generation']),
            'policy_sha256': '3' * 64, 'source_fingerprints': {'aos': '4' * 64, 'scientist': '5' * 64},
            'control_schema_sha256': '6' * 64, 'operations': ['reconcile'], 'original_admission_binding': binding,
            'original_admission_binding_sha256': digest(binding), 'original_cleanup_authorization': None,
            'original_cleanup_authorization_sha256': None}
        capability = {'cleanup_grant': grant, 'cleanup_grant_sha256': digest(grant),
            'boot_id': binding['server_generation']['boot_id'], 'issued_boottime': 100.125, 'expires_boottime': 160.125}
        request = self.request | {'expected_capability_sha256': digest(capability)}
        response = self.response | {'capability_sha256': digest(capability)}
        self.assertEqual(self.decode(response, request, expected_capability=capability), response)
        grant['original_admission_binding_sha256'] = 'f' * 64
        capability['cleanup_grant_sha256'] = digest(grant)
        with self.assertRaises(ScientistAdmissionError):
            self.decode(response | {'capability_sha256': digest(capability)},
                        request | {'expected_capability_sha256': digest(capability)}, expected_capability=capability)
