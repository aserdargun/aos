"""CPU-only terminal evidence validation, never physical GPU release or resolution."""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock

import jsonschema

from aos.contracts import REPO_ROOT, canonical as local_canonical, digest as local_digest
from aos.scientist_admission_history import ScientistAdmissionHistory, ScientistAdmissionRecordV2
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_profile_output import admission_profile_validator
from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame, scientist_request_sha256
from aos.scientist_terminal import (
    TERMINAL_DESCRIPTOR_SHA256, ScientistTerminalBudget, ScientistTerminalReceipt,
    ScientistTerminalVerifier, canonical, digest, terminal_schema_sha256,
)
from aos.scientist_transport import ScientistAdmissionError
from aos.storage import TrajectoryStore

from test_scientist_admission_history import BOOT_ID, admission_capture_fixture
from test_scientist_decider_receipt import receipt_fixture, request_fixture
from test_scientist_profile_output import OUTPUT_PIN
import test_scientist_intents as intent_fixture


def terminal_budget_fixture():
    return {'activation_seconds': 20, 'inference_seconds': 10, 'total_seconds': 30,
            'queue_seconds': 60, 'max_output_tokens': 512, 'context_tokens': 16384,
            'admitted_boottime': 101, 'envelope_deadline': 180, 'queue_deadline': 160,
            'activation_deadline': 121, 'inference_deadline': 125, 'total_deadline': 131,
            'boot_id': BOOT_ID}


def terminal_receipt_fixture(record, request):
    admission = record.admission_binding.model_dump(mode='json')
    infer = receipt_fixture(request.model_dump(mode='json'))
    options = request.payload['request']['options']
    selected = options[0]['id']
    infer['response']['prediction'] = {'selected_option': selected,
        'probabilities': {option['id']: float(option['id'] == selected) for option in options}}
    result = {key: infer[key] for key in ('response', 'usage', 'generation')}
    generation = result['generation']
    terminal = {
        'schema': 'aos-scientist-terminal.v1', 'request_id': request.request_id,
        'request_sha256': scientist_request_sha256(request),
        'original_principal': deepcopy(admission['caller_generation']),
        'profile_id': request.profile_id, 'deployment_digest': request.deployment_digest,
        'profile_config_sha256': admission['profile_pin']['config_sha256'],
        'response_schema_sha256': admission['profile_pin']['response_schema_sha256'],
        'admission_binding': admission, 'admission_binding_sha256': record.admission_binding_sha256,
        'original_budget': terminal_budget_fixture(), 'allocation_binding_sha256': 'a' * 64,
        'child_generation': {'unit': generation['unit'], 'invocation_id': generation['invocation_id'],
            'pid': generation['main_pid'], 'control_group': generation['control_group'],
            'start_ticks': 80, 'boot_id': BOOT_ID},
        'drain_evidence_sha256': 'b' * 64, 'no_admission_evidence_sha256': None,
        'release_outcome': 'released', 'terminal_state': 'completed', 'reason_code': 'success',
        'result_sha256': digest(result), 'recorded_boot_id': BOOT_ID, 'recorded_boottime': 120,
    }
    terminal['receipt_sha256'] = digest(terminal)
    return terminal, result


class ScientistTerminalTests(unittest.TestCase):
    def setUp(self):
        intent_fixture.ScientistIntentTests.setUp(self)
        self.peer = replace(self.peer, boot_id=BOOT_ID)
        self.request = request_fixture()
        payload = deepcopy(self.request.payload)
        payload['request']['options'][0]['id'] = 'yaz_şimdi'
        self.request = self.request.model_copy(update={'payload': payload})
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
            verify_current=lambda *_arguments: None, clock=lambda: 100, record_version='2.0')
        self.journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)
        self.journal.persist_intent(scientist_request_frame(self.request)[:-1],
            scientist_request_sha256(self.request), time.monotonic() + 60, self.peer)
        self.original, self.original_sha = self.history.read(self.request.request_id)
        self.terminal, self.result = terminal_receipt_fixture(self.original, self.request)
        self.events = []
        self.output = Mock(side_effect=self.validate_output)
        self.proof = Mock(side_effect=lambda *_arguments: self.events.append('proof'))
        self.resolver = Mock(side_effect=lambda *_arguments: self.events.append('resolver'))

    def capture(self, request, binding, peer):
        value = admission_capture_fixture(request, peer)
        value['admission_binding']['profile_pin']['output_contract'] = deepcopy(OUTPUT_PIN)
        value['admission_binding']['terminal_schema']['sha256'] = TERMINAL_DESCRIPTOR_SHA256
        return value

    def validate_output(self, request, receipt):
        self.events.append('output')
        return admission_profile_validator(self.history, OUTPUT_PIN)(request, receipt)

    def verifier(self, **changes):
        options = {'descriptor_sha256': TERMINAL_DESCRIPTOR_SHA256,
                   'schema_sha256': terminal_schema_sha256(), 'expected_budget': terminal_budget_fixture(),
                   'validate_result': self.output, 'verify_proof': self.proof, 'verify_resolver': self.resolver}
        return ScientistTerminalVerifier(self.history, **(options | changes))

    def signed(self, terminal):
        value = deepcopy(terminal)
        value['receipt_sha256'] = digest({key: entry for key, entry in value.items() if key != 'receipt_sha256'})
        return canonical(value).encode()

    def rows(self):
        with closing(sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)) as connection:
            return {table: connection.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('desktop_sessions', 'scientist_turn_intents', 'scientist_admission_history')}

    def assert_no_callbacks(self):
        self.output.assert_not_called()
        self.proof.assert_not_called()
        self.resolver.assert_not_called()

    def test_completed_released_validates_original_result_and_proof_without_resolving_intent(self):
        before = self.rows()
        terminal = self.verifier().verify(self.request, self.signed(self.terminal),
                                          result_bytes=canonical(self.result).encode())
        self.assertEqual(terminal.model_dump(mode='json', by_alias=True), self.terminal)
        self.assertEqual(self.events, ['output', 'resolver', 'proof', 'resolver'])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.history.read(self.request.request_id), (self.original, self.original_sha))
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')

    def test_canceled_never_admitted_requires_original_intent_but_no_result_or_allocation(self):
        terminal = deepcopy(self.terminal)
        terminal.update(terminal_state='canceled', reason_code='caller_cancel', release_outcome='never_admitted',
            allocation_binding_sha256=None, child_generation=None, drain_evidence_sha256=None,
            no_admission_evidence_sha256='c' * 64, result_sha256=None)
        for field in ('activation_deadline', 'inference_deadline', 'total_deadline'):
            terminal['original_budget'][field] = None
        before = self.rows()
        result = self.verifier(expected_budget=terminal['original_budget']).verify(self.request, self.signed(terminal))
        self.assertEqual(result.terminal_state, 'canceled')
        self.assertEqual(self.events, ['resolver', 'proof', 'resolver'])
        self.output.assert_not_called()
        self.assertEqual(self.rows(), before)

    def test_noncanonical_unknown_duplicate_nonfinite_and_coerced_fields_deny_before_callbacks(self):
        raw = self.signed(self.terminal)
        invalid = [raw + b'\n', b' ' + raw, raw[:-1] + b',"schema":"aos-scientist-terminal.v1"}',
                   b'\xff', b'[]', b'{"clock":NaN}', b'{}' * (128 * 1024),
                   self.signed({**self.terminal, 'version': 1})]
        for field, value in (('pid', True), ('start_ticks', 80.0), ('pid', '1234')):
            changed = deepcopy(self.terminal)
            changed['child_generation'][field] = value
            invalid.append(self.signed(changed))
        for content in invalid:
            with self.subTest(content=content[:70]), self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, content, result_bytes=canonical(self.result).encode())
        self.assert_no_callbacks()

    def test_original_identity_schema_boot_and_budget_tampering_deny_before_callbacks(self):
        changes = [(['request_id'], 'f' * 32), (['request_sha256'], 'f' * 64),
                   (['profile_config_sha256'], 'f' * 64), (['response_schema_sha256'], 'f' * 64),
                   (['admission_binding_sha256'], 'f' * 64), (['admission_binding', 'policy_sha256'], 'f' * 64),
                   (['original_principal', 'parent_pid'], 42), (['original_budget', 'max_output_tokens'], 511),
                   (['recorded_boot_id'], '22222222-2222-2222-2222-222222222222'),
                   (['child_generation', 'boot_id'], '22222222-2222-2222-2222-222222222222'),
                   (['recorded_boottime'], 99), (['admission_binding'], None), (['original_budget'], None)]
        for path, value in changes:
            changed = deepcopy(self.terminal)
            target = changed
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, self.signed(changed), result_bytes=canonical(self.result).encode())
        changed = deepcopy(self.terminal)
        changed['receipt_sha256'] = 'f' * 64
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(self.request, canonical(changed).encode(), result_bytes=canonical(self.result).encode())
        self.assert_no_callbacks()

    def test_tombstone_null_admission_unbound_allocations_and_cleanup_failure_are_not_release(self):
        invalid = [dict(child_generation=None), dict(drain_evidence_sha256=None),
                   dict(allocation_binding_sha256=None), dict(no_admission_evidence_sha256='c' * 64),
                   dict(terminal_state='failed', reason_code='execution_failed'),
                   dict(terminal_state='completed', release_outcome='recovered_released'),
                   dict(reason_code='cleanup_failed'), dict(terminal_state='quarantined')]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, self.signed(self.terminal | changes),
                                       result_bytes=canonical(self.result).encode())
        budget = terminal_budget_fixture() | {'admitted_boottime': None, 'envelope_deadline': None}
        with self.assertRaises(ScientistAdmissionError):
            self.verifier(expected_budget=budget).verify(self.request,
                self.signed(self.terminal | {'original_budget': budget}), result_bytes=canonical(self.result).encode())
        self.assert_no_callbacks()

    def test_even_trusted_expected_budget_cannot_place_phases_before_admission_or_after_total(self):
        for field, value in (('activation_deadline', 100), ('queue_deadline', 101),
                             ('inference_deadline', 132), ('activation_deadline', 132)):
            budget = terminal_budget_fixture() | {field: value}
            terminal = self.terminal | {'original_budget': budget}
            with self.subTest(field=field, value=value), self.assertRaises(ScientistAdmissionError):
                self.verifier(expected_budget=budget).verify(self.request, self.signed(terminal),
                    result_bytes=canonical(self.result).encode())
        self.assert_no_callbacks()

    def test_result_hash_generation_missing_extra_and_noncanonical_bytes_deny_before_callbacks(self):
        invalid = [None, canonical(self.result).encode() + b'\n', b'{}']
        changed = deepcopy(self.result)
        changed['generation']['main_pid'] += 1
        invalid.append(canonical(changed).encode())
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, self.signed(self.terminal), result_bytes=result)
        for change in (dict(unexpected=True), {'generation': changed['generation']}):
            result = self.result | change
            terminal = self.terminal | {'result_sha256': digest(result)}
            with self.assertRaises(ScientistAdmissionError):
                self.verifier().verify(self.request, self.signed(terminal), result_bytes=canonical(result).encode())
        self.assert_no_callbacks()

    def test_utf8_wire_result_is_not_interchangeable_with_ascii_escaped_local_json(self):
        wire = canonical(self.result).encode()
        escaped = local_canonical(self.result).encode()
        self.assertIn('yaz_şimdi'.encode(), wire)
        self.assertNotEqual(wire, escaped)
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(self.request, self.signed(self.terminal), result_bytes=escaped)
        self.assert_no_callbacks()
        self.verifier().verify(self.request, self.signed(self.terminal), result_bytes=wire)
        self.assertEqual(self.events, ['output', 'resolver', 'proof', 'resolver'])

    def test_hash_matched_semantically_invalid_output_never_reaches_release_callbacks(self):
        result = deepcopy(self.result)
        result['response']['prediction']['selected_option'] = 'unapproved'
        terminal = self.terminal | {'result_sha256': digest(result)}
        before = self.rows()
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(self.request, self.signed(terminal), result_bytes=canonical(result).encode())
        self.output.assert_called_once()
        self.proof.assert_not_called()
        self.resolver.assert_not_called()
        self.assertEqual(self.rows(), before)

    def test_default_denied_and_failed_trusted_callbacks_cannot_resolve_or_mutate_history(self):
        base = {'descriptor_sha256': TERMINAL_DESCRIPTOR_SHA256, 'schema_sha256': terminal_schema_sha256(),
                'expected_budget': terminal_budget_fixture()}
        before = self.rows()
        for callbacks in ({}, {'validate_result': self.output},
                          {'validate_result': self.output, 'verify_resolver': self.resolver},
                          {'validate_result': self.output, 'verify_resolver': self.resolver,
                           'verify_proof': lambda *_arguments: True}):
            verifier = ScientistTerminalVerifier(self.history, **base, **callbacks)
            with self.assertRaises(ScientistAdmissionError):
                verifier.verify(self.request, self.signed(self.terminal), result_bytes=canonical(self.result).encode())
            self.assertEqual(self.rows(), before)
        self.proof.side_effect = ScientistAdmissionError('Synthetic physical drain not proven')
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(self.request, self.signed(self.terminal), result_bytes=canonical(self.result).encode())
        self.assertEqual(self.rows(), before)

    def test_owner_change_during_physical_proof_rechecks_resolver_but_preserves_original_read(self):
        def resolver(record, terminal):
            state = self.store.connection.execute('SELECT owner FROM desktop_sessions').fetchone()[0]
            if state != 'AGENT':
                raise ScientistAdmissionError('Synthetic current resolver lost authority')

        def changed_owner(record, terminal):
            with self.store.connection:
                self.store.connection.execute("UPDATE desktop_sessions SET owner='HUMAN'")

        self.resolver.side_effect = resolver
        self.proof.side_effect = changed_owner
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(self.request, self.signed(self.terminal), result_bytes=canonical(self.result).encode())
        self.assertEqual(self.resolver.call_count, 2)
        self.assertEqual(self.history.read(self.request.request_id), (self.original, self.original_sha))
        row = self.store.connection.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
        self.assertEqual(tuple(row), ('pending', None))

    def test_legacy_reader_mismatched_schema_or_descriptor_and_missing_original_are_denied(self):
        for changes in ({'descriptor_sha256': 'f' * 64}, {'schema_sha256': 'f' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ScientistAdmissionError):
                self.verifier(**changes)
        with self.assertRaises(ScientistAdmissionError):
            ScientistTerminalVerifier(ScientistAdmissionHistory(self.store),
                descriptor_sha256=TERMINAL_DESCRIPTOR_SHA256, schema_sha256=terminal_schema_sha256(),
                expected_budget=terminal_budget_fixture())
        request = self.request.model_copy(update={'request_id': 'f' * 32})
        terminal = self.terminal | {'request_id': request.request_id, 'request_sha256': scientist_request_sha256(request)}
        with self.assertRaises(ScientistAdmissionError):
            self.verifier().verify(request, self.signed(terminal), result_bytes=canonical(self.result).encode())
        self.assert_no_callbacks()

    def test_v2_reader_cannot_adopt_an_actual_immutable_v1_admission_row(self):
        store = TrajectoryStore(self.path.with_name('legacy.sqlite3'))
        self.addCleanup(store.close)
        session = tuple(self.store.connection.execute('SELECT * FROM desktop_sessions').fetchone())
        with store.connection:
            store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)', session)
        history = ScientistAdmissionHistory(store,
            capture=lambda request, binding, peer: admission_capture_fixture(request, peer),
            verify_current=lambda *_arguments: None, clock=lambda: 100)
        journal = ScientistIntentJournal(store, self.binding, admission_history=history)
        journal.persist_intent(scientist_request_frame(self.request)[:-1],
            scientist_request_sha256(self.request), time.monotonic() + 60, self.peer)
        original = history.read(self.request.request_id)
        reader = ScientistAdmissionHistory(store, record_version='2.0')
        verifier = ScientistTerminalVerifier(reader, descriptor_sha256=TERMINAL_DESCRIPTOR_SHA256,
            schema_sha256=terminal_schema_sha256(), expected_budget=terminal_budget_fixture(),
            validate_result=self.output, verify_proof=self.proof, verify_resolver=self.resolver)
        with self.assertRaises(ScientistAdmissionError):
            verifier.verify(self.request, self.signed(self.terminal), result_bytes=canonical(self.result).encode())
        self.assertEqual(history.read(self.request.request_id), original)
        self.assertEqual(original[0].schema_version, '1.0')
        self.assert_no_callbacks()

    def test_terminal_schema_and_budget_models_match_canonical_public_files(self):
        schema = json.loads((REPO_ROOT / 'schemas/scientist_terminal_evidence.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertEqual(digest(schema), terminal_schema_sha256())
        jsonschema.validate(self.terminal, schema)
        self.assertEqual(schema['$defs']['ScientistTerminalBudget'], ScientistTerminalBudget.model_json_schema())
        schema.pop('$schema')
        self.assertEqual(schema, ScientistTerminalReceipt.model_json_schema())

    def test_public_example_is_hash_consistent_synthetic_data_without_authority(self):
        example = json.loads((REPO_ROOT / 'examples/scientist_terminal_evidence.json').read_text())
        self.assertIs(example['synthetic_cpu_fixture'], True)
        for flag in ('runtime_admission', 'physical_proof_verified', 'resolution_authorized'):
            self.assertIs(example[flag], False)
        self.assertEqual(example['descriptor_sha256'], TERMINAL_DESCRIPTOR_SHA256)
        self.assertEqual(example['local_schema_sha256'], terminal_schema_sha256())
        record = ScientistAdmissionRecordV2.model_validate(example['original_admission'], strict=True)
        record_schema = json.loads((REPO_ROOT / 'schemas/scientist_admission_record_v2.schema.json').read_text())
        jsonschema.validate(example['original_admission'], record_schema)
        self.assertEqual(example['original_admission_sha256'], local_digest(record.model_dump(mode='json')))
        terminal = ScientistTerminalReceipt.model_validate(example['terminal'], strict=True)
        schema = json.loads((REPO_ROOT / 'schemas/scientist_terminal_evidence.schema.json').read_text())
        jsonschema.validate(example['terminal'], schema)
        self.assertEqual(terminal.receipt_sha256, digest({key: value for key, value in
                         example['terminal'].items() if key != 'receipt_sha256'}))
        self.assertEqual(terminal.result_sha256, digest(example['result']))
        self.assertEqual(terminal.admission_binding.model_dump(mode='json'),
                         record.admission_binding.model_dump(mode='json'))
        self.assertEqual(terminal.admission_binding_sha256, record.admission_binding_sha256)
        self.assertEqual(terminal.original_budget.model_dump(mode='json'), example['expected_budget'])
        request = ScientistTurnRequest.model_validate(example['request'], strict=True)
        self.assertEqual(terminal.request_sha256, scientist_request_sha256(request))
        self.assertEqual(record.request_sha256, terminal.request_sha256)
