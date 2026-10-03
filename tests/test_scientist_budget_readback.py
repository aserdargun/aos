"""Synthetic CPU independent budget readback; no Scientist runtime or GPU proof."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from aos.scientist_budget_witness import (
    ScientistBudgetWitnessVerifier, ScientistOriginalBudgetWitness, budget_witness_schema,
)
from aos.scientist_terminal import canonical, digest
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_budget_witness as budget_fixture
import test_scientist_retained_host_integration as host_fixture


class ScientistBudgetReadbackTests(unittest.TestCase):
    capture = budget_fixture.ScientistBudgetWitnessTests.capture
    validate_output = budget_fixture.ScientistBudgetWitnessTests.validate_output
    rows = budget_fixture.ScientistBudgetWitnessTests.rows
    with_budget = budget_fixture.ScientistBudgetWitnessTests.with_budget
    assert_no_resolution = budget_fixture.ScientistBudgetWitnessTests.assert_no_resolution

    def setUp(self):
        budget_fixture.ScientistBudgetWitnessTests.setUp(self)
        self.reader = Mock(side_effect=lambda request, original: deepcopy(self.witness))
        self.before_rows = self.rows()
        self.before_changes = self.store.connection.total_changes

    def verifier(self, **changes):
        return ScientistBudgetWitnessVerifier(self.history,
            **({'schema_sha256': digest(budget_witness_schema()),
                'verify_source': self.source, 'read_source': self.reader} | changes))

    def verify(self, witness=None, **changes):
        return self.verifier(**changes).verify(self.request,
            canonical(self.witness if witness is None else witness).encode('utf-8'))

    def assert_unchanged(self):
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.store.connection.total_changes, self.before_changes)
        self.assert_no_resolution()

    def test_independent_whole_witness_read_twice_with_authority_before_and_after(self):
        events = []

        def source(request, original, witness):
            events.append('authority')
            self.assertEqual(request, self.request)
            self.assertEqual(original, self.original)
            self.assertEqual(witness.model_dump(mode='json', by_alias=True), self.witness)

        def read(request, original):
            events.append('read')
            self.assertEqual(request, self.request)
            self.assertEqual(original, self.original)
            return deepcopy(self.witness)

        self.source.side_effect = source
        self.reader.side_effect = read
        budget = self.verify()
        self.assertEqual(events, ['authority', 'read', 'authority'] * 2)
        self.assertEqual(budget.model_dump(mode='json'), json.loads(self.witness['budget_canonical']))
        self.assert_unchanged()

    def test_reader_configuration_alone_never_grants_source_authority(self):
        verifier = ScientistBudgetWitnessVerifier(self.history,
            schema_sha256=digest(budget_witness_schema()), read_source=self.reader)
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, canonical(self.witness).encode())
        self.reader.assert_not_called()
        self.assert_unchanged()

    def test_source_revocation_denies_before_independent_read(self):
        self.source.side_effect = ScientistAdmissionError('Synthetic source authority missing')
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.reader.assert_not_called()
        self.assert_unchanged()

    def test_reader_noncallable_denied_at_construction(self):
        for reader in (True, False, {}, 1, 'untrusted'):
            with self.subTest(reader=reader), self.assertRaises(TypeError):
                self.verifier(read_source=reader)
        self.source.assert_not_called()
        self.assert_unchanged()

    def test_readback_requires_strict_complete_dictionary(self):
        values = [None, True, False, 1, [], canonical(self.witness),
                  canonical(self.witness).encode(), {},
                  ScientistOriginalBudgetWitness.model_validate(self.witness, strict=True),
                  self.witness | {'version': True}, self.witness | {'version': 1.0},
                  self.witness | {'version': '1'}, self.witness | {'unknown': False},
                  self.witness | {'schema': 'unreviewed'},
                  self.witness | {'target': []},
                  self.witness | {'allocation_binding_sha256': True}]
        for value in values:
            self.reader.reset_mock(side_effect=True)
            self.reader.return_value = value
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assert_unchanged()

    def test_independent_read_cannot_replace_any_original_identity_or_profile_field(self):
        for field, value in [('profile_id', 'aos.bonsai.vision.v1'),
                             ('deployment_digest', 'f' * 64),
                             ('profile_config_sha256', 'f' * 64),
                             ('response_schema_sha256', 'f' * 64),
                             ('original_admission_binding_sha256', 'f' * 64),
                             ('original_admission_binding_sha256', None),
                             ('original_cleanup_authorization_sha256', 'f' * 64)]:
            self.reader.side_effect = None
            self.reader.return_value = self.witness | {field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assert_unchanged()
        for field in self.witness['target']:
            changed = deepcopy(self.witness)
            changed['target'][field] = 'f' * (32 if field == 'request_id' else 64)
            self.reader.return_value = changed
            with self.subTest(target_field=field), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assert_unchanged()

    def test_allocation_hash_difference_denied_even_when_budget_matches(self):
        self.reader.side_effect = None
        self.reader.return_value = self.witness | {'allocation_binding_sha256': 'f' * 64}
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.assert_unchanged()

    def test_valid_alternative_budget_cannot_replace_original_readback(self):
        changed = self.with_budget({'activation_deadline': 122})
        self.reader.side_effect = None
        self.reader.return_value = changed
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.reader.return_value = deepcopy(self.witness)
        with self.assertRaises(ScientistAdmissionError):
            self.verify(changed)
        self.assert_unchanged()

    def test_budget_hash_or_canonical_difference_denied(self):
        for changes in ({'budget_sha256': 'f' * 64},
                        {'budget_canonical': ' ' + self.witness['budget_canonical']},
                        {'budget_canonical': '{}'}, {'budget_canonical': '{"clock":NaN}'}):
            self.reader.side_effect = None
            self.reader.return_value = self.witness | changes
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assert_unchanged()

    def test_oversized_independent_witness_denied(self):
        self.reader.side_effect = None
        self.reader.return_value = self.witness | {'budget_canonical': ' ' * 32769}
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.assert_unchanged()

    def test_read_provenance_exception_cannot_publish_budget(self):
        for error in (ScientistAdmissionError('Synthetic provenance missing'),
                      ValueError('Synthetic provenance malformed'), TypeError('Synthetic provenance invalid')):
            self.reader.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assert_unchanged()

    def test_authority_revocation_during_either_read_denies(self):
        for revoked_read in (1, 2):
            state = {'reads': 0, 'revoked': False}

            def source(request, original, witness):
                if state['revoked']:
                    raise ScientistAdmissionError('Synthetic authority revoked during read')

            def read(request, original):
                state['reads'] += 1
                state['revoked'] = state['reads'] == revoked_read
                return deepcopy(self.witness)

            self.source.reset_mock(side_effect=True)
            self.reader.reset_mock(side_effect=True)
            self.source.side_effect = source
            self.reader.side_effect = read
            with self.subTest(read=revoked_read), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assertEqual(self.reader.call_count, revoked_read)
            self.assertEqual(self.source.call_count, revoked_read * 2)
            self.assert_unchanged()

    def test_second_read_must_match_after_successful_first_read(self):
        self.reader.side_effect = [deepcopy(self.witness),
                                  self.witness | {'allocation_binding_sha256': 'f' * 64}]
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.assertEqual(self.reader.call_count, 2)
        self.assert_unchanged()

    def test_authority_return_value_cannot_grant_permission_before_or_after_read(self):
        for results, reads in (([True], 0), ([None, False], 1),
                               ([None, None, True], 1), ([None, None, None, True], 2)):
            self.source.reset_mock(side_effect=True)
            self.reader.reset_mock()
            self.source.side_effect = results
            with self.subTest(results=results), self.assertRaises(ScientistAdmissionError):
                self.verify()
            self.assertEqual(self.reader.call_count, reads)
            self.assert_unchanged()

    def test_changed_history_checksum_during_read_denies_without_writing_history(self):
        state = {'read': False}
        history_read = self.history.read

        def observed_history(request_id):
            original, checksum = history_read(request_id)
            return original, 'f' * 64 if state['read'] else checksum

        def read(request, original):
            state['read'] = True
            return deepcopy(self.witness)

        self.reader.side_effect = read
        with patch.object(self.history, 'read', side_effect=observed_history):
            with self.assertRaises(ScientistAdmissionError):
                self.verify()
        self.reader.assert_called_once()
        self.assert_unchanged()

    def test_unallocated_budget_requires_exact_null_phase_readback(self):
        witness = self.with_budget(dict.fromkeys(
            ('activation_deadline', 'inference_deadline', 'total_deadline')))
        witness['allocation_binding_sha256'] = None
        self.reader.side_effect = lambda request, original: deepcopy(witness)
        budget = self.verify(witness)
        self.assertIsNone(budget.activation_deadline)
        self.assertIsNone(budget.inference_deadline)
        self.assertIsNone(budget.total_deadline)
        self.assertEqual(self.reader.call_count, 2)
        self.assert_unchanged()

    def test_reader_and_authority_receive_detached_inputs(self):
        request_before = self.request.model_dump(mode='json')
        original_before = self.original.model_dump(mode='json')
        witness_before = deepcopy(self.witness)
        read_observations = []
        authority_observations = []

        def source(request, original, witness):
            authority_observations.append((request.model_dump(mode='json'),
                original.model_dump(mode='json'), witness.model_dump(mode='json', by_alias=True)))
            request.payload.clear()
            original.admission_binding.profile_pin.__dict__['config_sha256'] = 'f' * 64
            witness.__dict__['budget_canonical'] = '{}'

        def read(request, original):
            read_observations.append((request.model_dump(mode='json'), original.model_dump(mode='json')))
            request.payload.clear()
            original.admission_binding.profile_pin.__dict__['config_sha256'] = 'f' * 64
            return deepcopy(self.witness)

        self.source.side_effect = source
        self.reader.side_effect = read
        budget = self.verify()
        self.assertEqual(read_observations, [(request_before, original_before)] * 2)
        self.assertEqual(authority_observations, [(request_before, original_before, witness_before)] * 4)
        self.assertEqual(self.request.model_dump(mode='json'), request_before)
        self.assertEqual(self.original.model_dump(mode='json'), original_before)
        self.assertEqual(self.witness, witness_before)
        self.assertEqual(budget.model_dump(mode='json'), json.loads(witness_before['budget_canonical']))
        self.assert_unchanged()

    def test_legacy_explicit_authority_without_reader_keeps_two_checks(self):
        budget = self.verify(read_source=None)
        self.reader.assert_not_called()
        self.assertEqual(self.source.call_count, 2)
        self.assertEqual(budget.model_dump(mode='json'), json.loads(self.witness['budget_canonical']))
        self.assert_unchanged()

    def test_default_constructor_never_uses_automatic_reader(self):
        verifier = ScientistBudgetWitnessVerifier(self.history,
            schema_sha256=digest(budget_witness_schema()))
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, canonical(self.witness).encode())
        self.reader.assert_not_called()
        self.assert_unchanged()


class ScientistBudgetReadbackHostTests(unittest.TestCase):
    make_host = host_fixture.ScientistRetainedHostIntegrationTests.make_host
    exchange_pair = host_fixture.ScientistRetainedHostIntegrationTests.exchange_pair
    resolve = host_fixture.ScientistRetainedHostIntegrationTests.resolve
    assert_original_pending = host_fixture.ScientistRetainedHostIntegrationTests.assert_original_pending

    def setUp(self):
        host_fixture.ScientistRetainedHostIntegrationTests.setUp(self)
        self.events = []

        def synthetic_read(request, original):
            self.events.append('read')
            self.assertEqual(request, self.fixture.request)
            self.assertEqual(original, self.fixture.original)
            return deepcopy(self.socket.witness)

        self.reader = Mock(side_effect=synthetic_read)
        self.host.verifier.budget_verifier = ScientistBudgetWitnessVerifier(self.fixture.history,
            schema_sha256=digest(budget_witness_schema()),
            verify_source=self.fixture.source, read_source=self.reader)
        retained_physical = self.socket.physical.side_effect

        def synthetic_physical(*arguments):
            self.events.append('physical')
            return retained_physical(*arguments)

        self.socket.physical.side_effect = synthetic_physical

    def test_owned_uds_ack_resolution_independently_reads_before_and_after_physical(self):
        self.exchange_pair()
        self.reader.assert_not_called()
        self.assertEqual(self.socket.counts(), (2, 2))
        self.assert_original_pending()
        resolution = self.resolve()
        self.assertEqual(resolution['request_id'], self.fixture.request.request_id)
        self.assertEqual(self.events, ['read', 'read', 'physical', 'read', 'read'] * 2)
        self.assertEqual(self.reader.call_count, 8)
        self.assertEqual(self.fixture.source.call_count, 16)
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 1)
        self.assertEqual(self.host.inspect_resolution(
            self.fixture.request.request_id, self.capability_id), resolution)
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assert_original_pending()

    def test_owned_uds_second_postphysical_read_mismatch_leaves_no_resolution(self):
        self.exchange_pair()
        witness = deepcopy(self.socket.witness)
        self.reader.side_effect = [deepcopy(witness)] * 3 + [
            witness | {'allocation_binding_sha256': 'f' * 64}]
        before = tuple(self.fixture.store.connection.iterdump())
        changes = self.fixture.store.connection.total_changes
        with self.assertRaises(ScientistAdmissionError):
            self.resolve()
        self.assertEqual(self.reader.call_count, 4)
        self.socket.physical.assert_called_once()
        self.assertEqual(self.fixture.store.connection.execute(
            'SELECT count(*) FROM scientist_turn_resolutions').fetchone()[0], 0)
        self.assertEqual(tuple(self.fixture.store.connection.iterdump()), before)
        self.assertEqual(self.fixture.store.connection.total_changes, changes)
        self.assertEqual(self.socket.counts(), (2, 2))
        self.assertEqual(len(self.socket.server.requests), 2)
        self.assert_original_pending()
