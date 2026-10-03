"""Synthetic CPU original-budget checks; no Scientist execution or GPU proof."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT
from aos.scientist_admission_history import ScientistAdmissionHistory, ScientistAdmissionRecord
from aos.scientist_budget_witness import (
    ScientistBudgetWitnessVerifier, ScientistOriginalBudgetWitness, budget_witness_schema,
    ScientistRetainedTerminalVerifier,
)
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_terminal as terminal_fixture
import test_scientist_release_proof as release_fixture


class ScientistBudgetWitnessTests(unittest.TestCase):
    capture = terminal_fixture.ScientistTerminalTests.capture
    validate_output = terminal_fixture.ScientistTerminalTests.validate_output
    rows = terminal_fixture.ScientistTerminalTests.rows

    def setUp(self):
        terminal_fixture.ScientistTerminalTests.setUp(self)
        binding = self.original.admission_binding
        budget = terminal_fixture.terminal_budget_fixture()
        self.witness = {
            'schema': 'aos-scientist-original-budget-witness.v1', 'version': 1,
            'target': {'request_id': self.request.request_id,
                'request_sha256': self.original.request_sha256,
                'original_peer_generation_sha256': digest(binding.caller_generation.model_dump(mode='json'))},
            'profile_id': self.request.profile_id, 'deployment_digest': self.request.deployment_digest,
            'profile_config_sha256': binding.profile_pin.config_sha256,
            'response_schema_sha256': binding.profile_pin.response_schema_sha256,
            'original_admission_binding_sha256': self.original.admission_binding_sha256,
            'original_cleanup_authorization_sha256': None, 'allocation_binding_sha256': 'a' * 64,
            'budget_canonical': canonical(budget), 'budget_sha256': digest(budget)}
        self.source = Mock(return_value=None)

    def verifier(self, **changes):
        return ScientistBudgetWitnessVerifier(self.history,
            **({'schema_sha256': digest(budget_witness_schema()), 'verify_source': self.source} | changes))

    def verify(self, witness=None, **options):
        return self.verifier(**options).verify(self.request, canonical(witness or self.witness).encode('utf-8'))

    def with_budget(self, changes):
        witness = deepcopy(self.witness)
        budget = json.loads(witness['budget_canonical']) | changes
        witness.update(budget_canonical=canonical(budget), budget_sha256=digest(budget))
        return witness

    def assert_no_resolution(self):
        self.output.assert_not_called()
        self.proof.assert_not_called()
        self.resolver.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')

    def test_full_schema_exact_equality_and_valid_witness(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_original_budget_witness.schema.json').read_text())
        self.assertEqual(schema, budget_witness_schema())
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(self.witness)
        model = ScientistOriginalBudgetWitness.model_validate(self.witness, strict=True)
        self.assertEqual(model.model_dump(mode='json', by_alias=True), self.witness)
        before = self.rows()
        changes = self.store.connection.total_changes
        budget = self.verify()
        self.assertEqual(budget.model_dump(mode='json'), terminal_fixture.terminal_budget_fixture())
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.connection.total_changes, changes)
        self.assert_no_resolution()

    def test_default_source_denies_without_resolution_or_sql_changes(self):
        verifier = ScientistBudgetWitnessVerifier(self.history, schema_sha256=digest(budget_witness_schema()))
        before = self.rows()
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, canonical(self.witness).encode())
        self.assertEqual(self.rows(), before)
        self.assert_no_resolution()

    def test_wrong_schema_pin_legacy_history_or_noncallable_source_denied(self):
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(schema_sha256='f' * 64)
        with self.assertRaises(ScientistAdmissionError):
            ScientistBudgetWitnessVerifier(ScientistAdmissionHistory(self.store),
                schema_sha256=digest(budget_witness_schema()), verify_source=self.source)
        with self.assertRaises(TypeError):
            self.verifier(verify_source=None)
        self.source.assert_not_called()

    def test_version2_reader_cannot_adopt_historical_version1_record(self):
        legacy = ScientistAdmissionRecord.model_validate(
            self.original.model_dump(mode='json') | {'schema_version': '1.0'}, strict=True)
        with patch.object(self.history, 'read', return_value=(legacy, self.original_sha)):
            with self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.source.assert_not_called()
        self.assert_no_resolution()

    def test_current_source_revocation_on_first_or_second_observation_denies(self):
        before = self.rows()
        for results in ([ScientistAdmissionError('Synthetic source revoked')],
                        [None, ScientistAdmissionError('Synthetic source revoked')], [True], [None, False]):
            self.source.reset_mock(side_effect=True)
            self.source.side_effect = results
            with self.subTest(observations=len(results)), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assertEqual(self.rows(), before)
            self.assert_no_resolution()

    def test_wrong_exact_original_target_profile_hashes_and_cleanup_binding_deny_before_source(self):
        for field, value in [('profile_id', 'aos.bonsai.vision.v1'), ('deployment_digest', 'f' * 64),
                             ('profile_config_sha256', 'f' * 64), ('response_schema_sha256', 'f' * 64),
                             ('original_admission_binding_sha256', None),
                             ('original_admission_binding_sha256', 'f' * 64),
                             ('original_cleanup_authorization_sha256', 'f' * 64)]:
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(self.witness | {field: value})
        for field in ('request_id', 'request_sha256', 'original_peer_generation_sha256'):
            witness = deepcopy(self.witness)
            witness['target'][field] = 'f' * (32 if field == 'request_id' else 64)
            with self.subTest(target_field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(witness)
        self.source.assert_not_called()

    def test_unknown_fields_protocol_lexemes_duplicate_nonfinite_and_noncanonical_bytes_deny(self):
        raw = canonical(self.witness).encode()
        for invalid in (raw + b'\n', b' ' + raw, b'[]', b'\xff', b'{"value":NaN}', b'{"value":Infinity}',
                        raw[:-1] + b',"version":1}', b'{}' * 17000):
            with self.subTest(raw=invalid[:30]), self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, invalid)
        for changes in ({'version': True}, {'version': 1.0}, {'version': '1'}, {'unknown': False},
                        {'schema': 'unreviewed'}, {'allocation_binding_sha256': 'A' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verify(self.witness | changes)
        witness = deepcopy(self.witness)
        witness['target']['extra'] = 'not allowed'
        with self.assertRaises(ScientistAdmissionError):
            self.verify(witness)
        self.source.assert_not_called()

    def test_budget_canonical_hash_closed_fields_and_integer_limits_are_exact(self):
        for changes in ({'budget_sha256': 'f' * 64},
                        {'budget_canonical': ' ' + self.witness['budget_canonical']},
                        {'budget_canonical': self.witness['budget_canonical'][:-1] + ',"max_output_tokens":512}'},
                        {'budget_canonical': '{"clock":NaN}'}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verify(self.witness | changes)
        for field, value in [('context_tokens', True), ('context_tokens', 16384.0), ('context_tokens', 16385),
                             ('max_output_tokens', 513), ('activation_seconds', '20'),
                             ('total_seconds', 29), ('queue_seconds', 59), ('unknown', 0)]:
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError):
                self.verify(self.with_budget({field: value}))
        self.source.assert_not_called()

    def test_boot_capture_envelope_and_assigned_phase_deadlines_are_bound(self):
        invalid = [('boot_id', '22222222-2222-2222-2222-222222222222'),
                   ('admitted_boottime', None), ('admitted_boottime', 99), ('envelope_deadline', None),
                   ('envelope_deadline', 101), ('activation_deadline', 101), ('activation_deadline', 132),
                   ('inference_deadline', 181), ('total_deadline', 120), ('queue_deadline', 181),
                   ('queue_deadline', 101)]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError):
                self.verify(self.with_budget({field: value}))
        self.source.assert_not_called()

    def test_no_allocation_requires_null_phases_and_allocated_budget_requires_all_phases(self):
        witness = self.with_budget(dict.fromkeys(('activation_deadline', 'inference_deadline', 'total_deadline')))
        witness['allocation_binding_sha256'] = None
        self.assertIsNone(self.verify(witness).activation_deadline)
        for field in ('activation_deadline', 'inference_deadline', 'total_deadline'):
            changed = self.with_budget({field: None})
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(changed)
        with self.assertRaises(ScientistAdmissionError):
            self.verify(self.witness | {'allocation_binding_sha256': None})
        with self.assertRaises(ScientistAdmissionError):
            self.verify(witness | {'allocation_binding_sha256': 'a' * 64})

    def test_source_receives_detached_copies_and_cannot_mutate_verified_budget_or_original_sql(self):
        before = self.rows()
        original_request = self.request.model_dump(mode='json')
        observations = []

        def mutate(request, original, witness):
            observations.append((request.model_dump(mode='json'), original.model_dump(mode='json'),
                                 witness.model_dump(mode='json', by_alias=True)))
            request.payload.clear()
            original.admission_binding.profile_pin.__dict__['config_sha256'] = 'f' * 64
            witness.target.__dict__['request_id'] = 'f' * 32
            witness.__dict__['budget_canonical'] = '{}'

        self.source.side_effect = mutate
        budget = self.verify()
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0][2], self.witness)
        self.assertEqual(budget.model_dump(mode='json'), terminal_fixture.terminal_budget_fixture())
        self.assertEqual(self.request.model_dump(mode='json'), original_request)
        self.assertEqual(self.rows(), before)
        self.assert_no_resolution()

    def retained_fixture(self):
        allocation, drain = release_fixture.release_preimages_fixture(self.terminal)
        evidence = release_fixture.release_envelope_fixture(self.terminal, self.result, allocation, drain)
        witness = self.witness | {'allocation_binding_sha256': digest(allocation)}
        retained_allocation, retained_drain = deepcopy(allocation), deepcopy(drain)

        def synthetic_retained_physical(original, _terminal, observed_allocation, observed_drain, no_admission):
            self.assertEqual(original, self.original)
            self.assertEqual(observed_allocation, retained_allocation)
            self.assertEqual(observed_drain, retained_drain)
            self.assertIsNone(no_admission)

        physical = Mock(side_effect=synthetic_retained_physical)
        verifier = ScientistRetainedTerminalVerifier(self.verifier(),
            evidence_schema_bytes=(REPO_ROOT / 'schemas/scientist_release_proof.schema.json').read_bytes(),
            evidence_schema_sha256=release_fixture.EVIDENCE_PIN,
            descriptor_sha256=terminal_fixture.TERMINAL_DESCRIPTOR_SHA256,
            terminal_schema_sha256=terminal_fixture.terminal_schema_sha256(),
            validate_result=self.output, verify_resolver=self.resolver, verify_physical=physical)
        return verifier, witness, evidence, physical

    def test_retained_budget_and_release_proof_composition_preserves_sql_without_resolution(self):
        verifier, witness, evidence, physical = self.retained_fixture()
        before = self.rows()
        terminal = verifier.verify(self.request, witness_bytes=canonical(witness).encode(),
                                   evidence_bytes=canonical(evidence).encode())
        self.assertEqual(terminal.allocation_binding_sha256, witness['allocation_binding_sha256'])
        self.assertEqual(self.source.call_count, 4)
        physical.assert_called_once()
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')

    def test_retained_allocation_mismatch_denies_before_physical_callbacks(self):
        verifier, witness, evidence, physical = self.retained_fixture()
        witness['allocation_binding_sha256'] = 'f' * 64
        before = self.rows()
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, witness_bytes=canonical(witness).encode(),
                            evidence_bytes=canonical(evidence).encode())
        physical.assert_not_called()
        self.assertEqual(self.rows(), before)
        self.assert_no_resolution()

    def test_retained_source_revocation_after_physical_proof_denies_publication(self):
        verifier, witness, evidence, physical = self.retained_fixture()
        before = self.rows()
        revoked = {'value': False}
        retained_physical = physical.side_effect

        def source(*arguments):
            if revoked['value']:
                raise ScientistAdmissionError('Synthetic retained source revoked after physical fixture')

        def physical_observation(*arguments):
            retained_physical(*arguments)
            revoked['value'] = True

        self.source.side_effect = source
        physical.side_effect = physical_observation
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, witness_bytes=canonical(witness).encode(),
                            evidence_bytes=canonical(evidence).encode())
        physical.assert_called_once()
        self.assertEqual(self.source.call_count, 3)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0], 'pending')
