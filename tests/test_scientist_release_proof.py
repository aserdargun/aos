"""Synthetic closed preimages and trusted callback seams; no native physical proof."""

from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import Mock

from aos.contracts import REPO_ROOT
from aos.scientist_release_proof import ScientistReleaseProofVerifier
from aos.scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, canonical, digest, terminal_schema_sha256
from aos.scientist_transport import ScientistAdmissionError

import test_scientist_terminal as terminal_cases


EVIDENCE_PIN = 'aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c'


def release_preimages_fixture(terminal):
    principal, budget = terminal['original_principal'], terminal['original_budget']
    allocation = {'lease': {'owner': 'aos', 'request_id': terminal['request_id'], 'fencing_token': 42,
        'phase': 'activating', 'activation_deadline': budget['activation_deadline'],
        'inference_deadline': budget['total_deadline'], 'total_deadline': budget['total_deadline'],
        'heartbeat_deadline': 110.0, 'slice_seconds': budget['inference_seconds'],
        'owner_identity': {key: principal[key] for key in ('pid', 'start_ticks', 'boot_id')},
        'owner_unit': principal['unit'], 'owner_invocation_id': principal['invocation_id']},
        'admission_binding_sha256': terminal['admission_binding_sha256'], 'original_principal': deepcopy(principal),
        'request_sha256': terminal['request_sha256'], 'original_deadline': budget['envelope_deadline'],
        'original_budget': {key: budget[key] for key in ('activation_seconds', 'inference_seconds',
            'total_seconds', 'queue_seconds', 'max_output_tokens', 'context_tokens')}}
    drain = {'kind': 'physical_drain', 'boot_id': terminal['recorded_boot_id'], 'observed_boottime': 119.0,
        'child_generation': deepcopy(terminal['child_generation']), 'never_started': False,
        'child_intent': {'unit': terminal['child_generation']['unit'], 'nonce': '6' * 64,
                         'created_boottime': 102.0, 'total_seconds': budget['total_seconds']},
        'late_start_fenced': True, 'cgroup_empty': True, 'gpu_absent': True,
        'observed_gpu_pids': [terminal['child_generation']['pid']],
        'remaining_owned_gpu_pids': [], 'remaining_foreign_gpu_pids': [],
        'allocation_binding_sha256': digest(allocation), 'handoff_stage': 'go',
        'late_start_fence': 162.0, 'final_unit_state': 'inactive', 'final_main_pid': 0}
    return allocation, drain


def release_envelope_fixture(terminal, result, allocation, drain, no_admission=None):
    terminal = deepcopy(terminal)
    drain = deepcopy(drain)
    if drain is not None:
        drain['allocation_binding_sha256'] = digest(allocation)
    for part, key in ((allocation, 'allocation_binding_sha256'), (drain, 'drain_evidence_sha256'),
                      (no_admission, 'no_admission_evidence_sha256')):
        terminal[key] = None if part is None else digest(part)
    terminal['receipt_sha256'] = digest({key: value for key, value in terminal.items() if key != 'receipt_sha256'})
    return {'schema': 'aos-scientist-terminal-evidence.v2', 'version': 2,
        'target': {'request_id': terminal['request_id'], 'request_sha256': terminal['request_sha256'],
                   'original_peer_generation_sha256': digest(terminal['original_principal'])},
        'terminal_canonical': canonical(terminal),
        'allocation_canonical': None if allocation is None else canonical(allocation),
        'drain_canonical': None if drain is None else canonical(drain),
        'no_admission_canonical': None if no_admission is None else canonical(no_admission),
        'result_canonical': None if result is None else canonical(result)}


class ScientistReleaseProofTests(unittest.TestCase):
    def setUp(self):
        self.fixture = terminal_cases.ScientistTerminalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.schema = (REPO_ROOT / 'schemas/scientist_release_proof.schema.json').read_bytes()
        self.allocation, self.drain = release_preimages_fixture(self.fixture.terminal)
        self.physical = Mock(side_effect=self.trusted_synthetic_physical)

    def trusted_synthetic_physical(self, original, terminal, allocation, drain, no_admission):
        self.assertEqual(original, self.fixture.original)
        if allocation is not None:
            if allocation['lease']['fencing_token'] != 42 or drain['child_intent']['nonce'] != '6' * 64:
                raise ScientistAdmissionError('Synthetic independently retained allocation/child identity differs')
        else:
            self.assertIsNotNone(no_admission)
        self.fixture.events.append('physical')

    def verifier(self, **changes):
        options = {'evidence_schema_bytes': self.schema, 'evidence_schema_sha256': EVIDENCE_PIN,
            'descriptor_sha256': TERMINAL_DESCRIPTOR_SHA256, 'terminal_schema_sha256': terminal_schema_sha256(),
            'expected_budget': terminal_cases.terminal_budget_fixture(),
            'validate_result': self.fixture.output, 'verify_resolver': self.fixture.resolver,
            'verify_physical': self.physical}
        return ScientistReleaseProofVerifier(self.fixture.history, **(options | changes))

    def envelope(self, **changes):
        options = {'terminal': self.fixture.terminal, 'result': self.fixture.result,
                   'allocation': self.allocation, 'drain': self.drain}
        return release_envelope_fixture(**(options | changes))

    def verify(self, evidence=None, **options):
        return self.verifier(**options).verify(self.fixture.request,
                                              canonical(evidence or self.envelope()).encode())

    def no_callbacks(self):
        self.fixture.output.assert_not_called()
        self.fixture.resolver.assert_not_called()
        self.physical.assert_not_called()

    def test_public_full_schema_hash_and_successful_original_chain_leave_database_unchanged(self):
        self.assertEqual(hashlib.sha256(self.schema).hexdigest(),
                         'd1d1f8f2c7a4e4bd7611a83eb36758f956f4fcbf2283f26f6cefc46f4e8af18f')
        self.assertEqual(digest(json.loads(self.schema)), EVIDENCE_PIN)
        before = self.fixture.rows()
        result = self.verify()
        self.assertEqual(result.terminal_state, 'completed')
        self.assertEqual(self.fixture.events, ['output', 'resolver', 'physical', 'resolver'])
        self.assertEqual(self.fixture.rows(), before)
        self.assertEqual(self.fixture.history.read(self.fixture.request.request_id)[1], self.fixture.original_sha)

    def test_true_flags_and_matching_hashes_do_not_supply_default_physical_or_resolver_authority(self):
        arguments = {'evidence_schema_bytes': self.schema, 'evidence_schema_sha256': EVIDENCE_PIN,
                     'descriptor_sha256': TERMINAL_DESCRIPTOR_SHA256,
                     'terminal_schema_sha256': terminal_schema_sha256(),
                     'expected_budget': terminal_cases.terminal_budget_fixture(), 'validate_result': self.fixture.output}
        for additions in ({}, {'verify_resolver': self.fixture.resolver}, {'verify_physical': self.physical}):
            verifier = ScientistReleaseProofVerifier(self.fixture.history, **(arguments | additions))
            with self.assertRaises(ScientistAdmissionError):
                verifier.verify(self.fixture.request, canonical(self.envelope()).encode())
        self.physical.assert_not_called()

    def test_missing_or_changed_hash_and_wrong_target_deny_before_any_callback(self):
        for field, value in (('allocation_canonical', None), ('drain_canonical', '{}'), ('target', {
                'request_id': 'f' * 32, 'request_sha256': 'f' * 64, 'original_peer_generation_sha256': 'f' * 64})):
            envelope = self.envelope()
            envelope[field] = value
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(envelope)
        self.no_callbacks()

    def test_rehashed_foreign_owner_principal_request_admission_budget_and_phase_deny(self):
        for field in ('owner_identity', 'owner_invocation_id', 'request_sha256', 'admission_binding_sha256',
                      'original_deadline', 'max_output_tokens', 'activation_deadline', 'inference_deadline', 'slice_seconds'):
            allocation = deepcopy(self.allocation)
            if field == 'owner_identity':
                allocation['lease'][field]['pid'] += 1
            elif field == 'owner_invocation_id':
                allocation['lease'][field] = 'a' * 32
            elif field in ('request_sha256', 'admission_binding_sha256'):
                allocation[field] = 'f' * 64
            elif field == 'original_deadline':
                allocation[field] += 1
            elif field == 'max_output_tokens':
                allocation['original_budget'][field] -= 1
            else:
                allocation['lease'][field] += 1
            with self.subTest(field=field), self.assertRaises(ScientistAdmissionError):
                self.verify(self.envelope(allocation=allocation))
        self.no_callbacks()

    def test_original_initial_inference_deadline_may_exceed_shortened_ready_deadline(self):
        self.assertGreater(self.allocation['lease']['inference_deadline'], self.fixture.terminal['original_budget']['inference_deadline'])
        self.assertEqual(self.verify().terminal_state, 'completed')

    def test_unknown_nested_fields_boolean_or_float_fences_and_nonempty_remaining_pids_deny(self):
        for kind in ('unknown', 'float', 'boolean', 'remaining_owned', 'remaining_foreign', 'false_flag', 'duplicate_pid'):
            allocation, drain = deepcopy(self.allocation), deepcopy(self.drain)
            if kind == 'unknown':
                allocation['lease']['owner_identity']['extra'] = True
            elif kind in ('float', 'boolean'):
                allocation['lease']['fencing_token'] = 42.0 if kind == 'float' else True
            elif kind.startswith('remaining'):
                drain[kind + '_gpu_pids'] = [999]
            elif kind == 'false_flag':
                drain['late_start_fenced'] = False
            else:
                drain['observed_gpu_pids'] *= 2
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                self.verify(self.envelope(allocation=allocation, drain=drain))
        self.no_callbacks()

    def test_rehashed_child_clock_handoff_and_late_start_fence_deny(self):
        for kind in ('child', 'boot', 'future', 'old', 'late_fence', 'intent_total', 'handoff', 'unit', 'running'):
            drain = deepcopy(self.drain)
            if kind == 'child':
                drain['child_generation']['pid'] += 1
            elif kind == 'boot':
                drain['boot_id'] = '00000000-0000-0000-0000-000000000002'
            elif kind in ('future', 'old'):
                drain['observed_boottime'] = 121 if kind == 'future' else 99
            elif kind == 'late_fence':
                drain['late_start_fence'] += 1
            elif kind == 'intent_total':
                drain['child_intent']['total_seconds'] += 1
            elif kind == 'handoff':
                drain['handoff_stage'] = None
            elif kind == 'unit':
                drain['child_intent']['unit'] = 'swapp-aos-gpu-turn-' + 'f' * 32 + '.service'
            else:
                drain['final_unit_state'] = 'active'
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                self.verify(self.envelope(drain=drain))
        self.no_callbacks()

    def test_independent_expected_budget_is_not_derived_from_terminal(self):
        budget = terminal_cases.terminal_budget_fixture()
        budget['queue_deadline'] += 1
        with self.assertRaises(ScientistAdmissionError):
            self.verify(expected_budget=budget)
        self.no_callbacks()

    def test_independently_retained_fencing_and_child_nonce_still_require_physical_provider(self):
        for kind in ('fence', 'nonce'):
            allocation, drain = deepcopy(self.allocation), deepcopy(self.drain)
            if kind == 'fence':
                allocation['lease']['fencing_token'] += 1
            else:
                drain['child_intent']['nonce'] = '7' * 64
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                self.verify(self.envelope(allocation=allocation, drain=drain))
        self.assertEqual(self.physical.call_count, 2)

    def test_no_admission_preimage_stays_bound_to_original_intent_and_never_runs_output(self):
        terminal = deepcopy(self.fixture.terminal)
        terminal.update(terminal_state='canceled', release_outcome='never_admitted', reason_code='caller_cancel',
                        child_generation=None, result_sha256=None)
        for field in ('activation_deadline', 'inference_deadline', 'total_deadline'):
            terminal['original_budget'][field] = None
        proof = {'request_id': terminal['request_id'], 'request_sha256': terminal['request_sha256'],
                 'principal_sha256': digest(terminal['original_principal']), 'cancel_before_intent': False,
                 'never_allocated': True, 'reason': 'caller_cancel'}
        envelope = self.envelope(terminal=terminal, allocation=None, drain=None, result=None, no_admission=proof)
        self.assertEqual(self.verify(envelope, expected_budget=terminal['original_budget']).terminal_state, 'canceled')
        self.fixture.output.assert_not_called()
        self.assertEqual(self.fixture.events, ['resolver', 'physical', 'resolver'])
        proof['cancel_before_intent'] = True
        with self.assertRaises(ScientistAdmissionError):
            self.verify(self.envelope(terminal=terminal, allocation=None, drain=None, result=None, no_admission=proof),
                        expected_budget=terminal['original_budget'])

    def test_noncanonical_duplicate_unknown_and_oversized_preimages_are_not_normalized(self):
        for encoded in (self.envelope()['allocation_canonical'] + '\n', '{"x":1,"x":1}',
                        '{"clock":NaN}', '{"x":"\\u015f"}', ' ' * (128 * 1024)):
            envelope = self.envelope()
            envelope['allocation_canonical'] = encoded
            with self.subTest(encoded=encoded[:20]), self.assertRaises(ScientistAdmissionError):
                self.verify(envelope)
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(evidence_schema_sha256='f' * 64)
        self.no_callbacks()

    def test_resolver_revocation_after_physical_callback_does_not_resolve_original_intent(self):
        before = self.fixture.rows()
        self.fixture.resolver.side_effect = [None, ScientistAdmissionError('Synthetic resolver revoked')]
        with self.assertRaises(ScientistAdmissionError):
            self.verify()
        self.physical.assert_called_once()
        self.assertEqual(self.fixture.rows(), before)

    def test_callback_copies_and_truthy_return_cannot_mutate_or_replace_proof(self):
        envelope = self.envelope()
        before = canonical(envelope)

        def mutate(original, terminal, allocation, drain, no_admission):
            self.trusted_synthetic_physical(original, terminal, allocation, drain, no_admission)
            allocation['lease']['fencing_token'] = 999
            drain['child_intent']['nonce'] = 'f' * 64

        self.verify(envelope, verify_physical=mutate)
        self.assertEqual(canonical(envelope), before)
        with self.assertRaises(ScientistAdmissionError):
            self.verify(verify_physical=lambda *_arguments: True)

    def test_unbound_allocated_child_and_cross_boot_are_not_adopted(self):
        for kind in ('no_child', 'cross_boot'):
            terminal, drain = deepcopy(self.fixture.terminal), deepcopy(self.drain)
            if kind == 'no_child':
                terminal['child_generation'] = drain['child_generation'] = None
                drain['never_started'] = True
                drain['handoff_stage'] = 'plan'
                terminal.update(terminal_state='failed', reason_code='execution_failed', result_sha256=None)
                result = None
            else:
                terminal['recorded_boot_id'] = drain['boot_id'] = '00000000-0000-0000-0000-000000000002'
                drain['child_generation']['boot_id'] = terminal['recorded_boot_id']
                terminal['child_generation']['boot_id'] = terminal['recorded_boot_id']
                result = self.fixture.result
            with self.subTest(kind=kind), self.assertRaises(ScientistAdmissionError):
                self.verify(self.envelope(terminal=terminal, drain=drain, result=result))
        self.no_callbacks()
