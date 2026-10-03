"""CPU-only original admission identity transactions with synthetic generations."""

from copy import deepcopy
from contextlib import closing
from dataclasses import asdict, replace
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

import jsonschema

from aos.contracts import REPO_ROOT, canonical, digest
from aos.scientist_admission_history import (
    ScientistAdmissionBinding, ScientistAdmissionCapture, ScientistAdmissionHistory,
    ScientistAdmissionRecord,
    ScientistOutputContractPin, ScientistProfilePin, ScientistProfilePinV2,
    ScientistAdmissionBindingV2, ScientistAdmissionCaptureV2, ScientistAdmissionRecordV2,
)
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_protocol import scientist_request_sha256, ScientistTurnRequest
from aos.scientist_transport import BROKER_UNIT, ScientistAdmissionError

import test_scientist_intents as intent_fixture
import test_scientist_desktop as desktop_fixture


BOOT_ID = '11111111-1111-1111-1111-111111111111'


def admission_capture_fixture(request, peer):
    server = {**asdict(peer), 'unit': BROKER_UNIT}
    value = {
        'admission_binding': {
            'server_generation': server,
            'caller_generation': {**server, 'pid': peer.pid + 1, 'start_ticks': peer.start_ticks + 1,
                'unit': 'swapp-aos-gpu-caller.service', 'invocation_id': 'd' * 32,
                'control_group': '/synthetic/aos-caller', 'parent_pid': peer.pid + 2,
                'parent_start_ticks': peer.start_ticks + 2},
            'policy_sha256': '1' * 64,
            'source_fingerprints': {'scientist': '2' * 64, 'aos': '3' * 64},
            'profile_id': request.profile_id,
            'profile_pin': {'deployment_digest': request.deployment_digest,
                'manifest_sha256': '4' * 64, 'config_sha256': '5' * 64,
                'response_schema_sha256': '6' * 64},
            'infer_schema': {'name': 'aos-scientist-runtime.v1', 'version': 1, 'sha256': '7' * 64},
            'control_schema': {'name': 'aos-scientist-control.v1', 'version': 1, 'sha256': '8' * 64},
            'terminal_schema': {'name': 'aos-scientist-terminal.v1', 'version': 1, 'sha256': '9' * 64},
        },
        'capability_sha256': 'a' * 64,
        'capability_freshness': {'boot_id': peer.boot_id, 'issued_boottime': 90, 'expires_boottime': 160},
    }
    return ScientistAdmissionCapture.model_validate(value, strict=True).model_dump(mode='json')


class ScientistAdmissionHistoryTests(unittest.TestCase):
    def setUp(self):
        intent_fixture.ScientistIntentTests.setUp(self)
        self.peer = replace(self.peer, boot_id=BOOT_ID)
        self.capture = Mock(side_effect=lambda request, binding, peer: admission_capture_fixture(request, peer))
        self.verify = Mock(return_value=None)
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
                                                 verify_current=self.verify, clock=lambda: 100)
        self.journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)

    def persist(self):
        self.journal.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)

    def counts(self, connection=None):
        connection = connection or self.store.connection
        return tuple(connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('scientist_turn_intents', 'scientist_admission_history'))

    def independent_counts(self):
        with closing(sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)) as connection:
            return self.counts(connection)

    def test_atomic_original_pair_is_visible_only_after_commit_and_exact_on_readback(self):
        observations = []

        def verify(record, request, binding, peer):
            self.assertTrue(self.store.connection.in_transaction)
            observations.append((self.counts(), self.independent_counts()))
            self.assertEqual(record.request_sha256, self.digest)
            self.assertEqual(record.admission_binding.server_generation.model_dump(mode='json'),
                             {**asdict(peer), 'unit': BROKER_UNIT})

        self.verify.side_effect = verify
        self.persist()
        self.assertEqual(observations, [((1, 0), (0, 0)), ((1, 1), (0, 0))])
        self.assertEqual(self.independent_counts(), (1, 1))
        record, checksum = self.history.read(self.request.request_id)
        self.assertEqual(checksum, digest(record.model_dump(mode='json')))
        self.assertEqual(record.intent_binding_sha256, digest(self.binding.model_dump(mode='json')))
        self.assertEqual(record.admission_binding_sha256,
                         digest(record.admission_binding.model_dump(mode='json')))
        self.assertEqual(record.captured_boottime, 100)
        with closing(sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)) as connection:
            row = connection.execute('SELECT record_json,record_sha256 FROM scientist_admission_history').fetchone()
        self.assertEqual(row, (canonical(record.model_dump(mode='json')), checksum))

    def test_default_denied_capture_or_current_verifier_rolls_back_both_rows(self):
        for options in ({}, {'capture': self.capture}):
            with self.subTest(options=tuple(options)):
                history = ScientistAdmissionHistory(self.store, clock=lambda: 100, **options)
                journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
                with self.assertRaises(ScientistAdmissionError):
                    journal.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)
                self.assertEqual(self.independent_counts(), (0, 0))
                self.assertFalse(self.store.connection.in_transaction)

    def test_current_callback_owner_or_generation_change_rolls_back_entire_capture(self):
        for assignment in ("owner='HUMAN'", 'generation=generation+1'):
            with self.subTest(assignment=assignment):
                self.verify.side_effect = lambda *_arguments: self.store.connection.execute(
                    'UPDATE desktop_sessions SET ' + assignment) and None
                with self.assertRaises(ScientistAdmissionError):
                    self.persist()
                self.assertEqual(self.independent_counts(), (0, 0))
                row = self.store.connection.execute('SELECT owner,generation FROM desktop_sessions').fetchone()
                self.assertEqual(tuple(row), ('AGENT', 0))

    def test_postinsert_failure_and_keyboard_interrupt_roll_back_both_rows(self):
        for error in (ScientistAdmissionError('synthetic post-insert denial'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                calls = []

                def verify(*_arguments):
                    calls.append(self.counts())
                    if calls[-1] == (1, 1):
                        raise error

                self.verify.side_effect = verify
                with self.assertRaises(type(error)):
                    self.persist()
                self.assertEqual(calls, [(1, 0), (1, 1)])
                self.assertEqual(self.independent_counts(), (0, 0))
                self.assertFalse(self.store.connection.in_transaction)

    def test_duplicate_intent_cannot_recapture_original_identity(self):
        self.persist()
        original = self.history.read(self.request.request_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.persist()
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(self.history.read(self.request.request_id), original)
        self.assertEqual(self.independent_counts(), (1, 1))

    def test_legacy_missing_identity_cannot_be_adopted(self):
        legacy = ScientistIntentJournal(self.store, self.binding)
        legacy.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_admission(self.request)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.record_receipt(self.receipt, self.peer)
        self.capture.assert_not_called()
        self.assertEqual(self.independent_counts(), (1, 0))

    def test_captured_identity_requires_verifier_even_if_hook_is_removed(self):
        self.persist()
        downgraded = ScientistIntentJournal(self.store, self.binding)
        with self.assertRaises(ScientistAdmissionError):
            downgraded.verify_admission(self.request)
        with self.assertRaises(ScientistAdmissionError):
            downgraded.record_receipt(self.receipt, self.peer)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')

    def test_sql_immutable_history_and_receipt_do_not_unfence_or_admit_new_generation(self):
        self.persist()
        for sql in ('DELETE FROM scientist_admission_history',
                    "UPDATE scientist_admission_history SET created_at='replacement'",
                    "UPDATE scientist_admission_history SET record_sha256='" + 'f' * 64 + "'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection:
                    self.store.connection.execute(sql)
        original = self.history.read(self.request.request_id)
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=1,lease_id='new'")
        successor = ScientistIntentJournal(self.store,
            self.binding.model_copy(update={'generation': 1, 'lease_id': 'new'}), admission_history=self.history)
        with self.assertRaises(ScientistAdmissionError):
            successor.verify_admission(self.request)
        with self.assertRaises(ScientistAdmissionError):
            successor.record_receipt(self.receipt, self.peer)
        self.assertEqual(self.history.read(self.request.request_id), original)
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')
        with self.store.connection:
            self.store.connection.execute("UPDATE desktop_sessions SET generation=0,lease_id='lease'")
        self.journal.record_receipt(self.receipt, self.peer)
        self.assertEqual(self.history.read(self.request.request_id), original)
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_admission(self.request.model_copy(update={'request_id': 'f' * 32}))

    def test_historical_read_never_calls_capture_current_verifier_or_clock(self):
        self.persist()
        expected = self.history.read(self.request.request_id)
        denied = Mock(side_effect=AssertionError('Historical read must not authorize or refresh'))
        historical = ScientistAdmissionHistory(self.store, capture=denied, verify_current=denied, clock=denied)
        self.assertEqual(historical.read(self.request.request_id), expected)
        denied.assert_not_called()
        before = self.store.connection.total_changes
        self.assertEqual(historical.read(self.request.request_id), expected)
        self.assertEqual(self.store.connection.total_changes, before)

    def test_capability_expiring_during_capture_rolls_back_original_pair(self):
        self.history.clock = Mock(side_effect=[100, 160])
        with self.assertRaises(ScientistAdmissionError):
            self.persist()
        self.assertEqual(self.verify.call_count, 2)
        self.assertEqual(self.independent_counts(), (0, 0))

    def test_slow_current_verifier_cannot_extend_original_intent_deadline(self):
        self.persist()
        expected = self.history.read(self.request.request_id)
        deadline = self.store.connection.execute('SELECT deadline FROM scientist_turn_intents').fetchone()[0]
        for operation in (lambda: self.journal.verify_admission(self.request),
                          lambda: self.journal.record_receipt(self.receipt, self.peer)):
            observed = deadline - 1

            def slow_verifier(*_arguments):
                nonlocal observed
                observed = deadline + 1

            self.verify.side_effect = slow_verifier
            with patch('aos.scientist_intents.time.monotonic', side_effect=lambda: observed):
                with self.assertRaises(ScientistAdmissionError):
                    operation()
                self.assertEqual(self.history.read(self.request.request_id), expected)
            row = self.store.connection.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
            self.assertEqual(tuple(row), ('pending', None))
            self.assertFalse(self.store.connection.in_transaction)

    def test_wrong_peer_profile_boot_and_open_nested_schema_are_not_captured(self):
        base = admission_capture_fixture(self.request, self.peer)
        for scope, field, value in (
                ('server_generation', 'start_ticks', self.peer.start_ticks + 1),
                ('caller_generation', 'boot_id', '22222222-2222-2222-2222-222222222222'),
                ('profile_pin', 'deployment_digest', 'f' * 64),
                ('profile_pin', 'output_contract', {'name': 'unconfirmed'}),
                ('infer_schema', 'version', True)):
            with self.subTest(scope=scope, field=field):
                invalid = deepcopy(base)
                invalid['admission_binding'][scope][field] = value
                self.capture.side_effect = None
                self.capture.return_value = invalid
                with self.assertRaises((ScientistAdmissionError, ValueError)):
                    self.persist()
                self.assertEqual(self.independent_counts(), (0, 0))

    def test_canonical_schemas_match_models_and_valid_capture(self):
        for name, model in (('scientist_admission_binding', ScientistAdmissionBinding),
                            ('scientist_admission_capture', ScientistAdmissionCapture),
                            ('scientist_admission_record', ScientistAdmissionRecord),
                            ('scientist_admission_binding_v2', ScientistAdmissionBindingV2),
                            ('scientist_admission_capture_v2', ScientistAdmissionCaptureV2),
                            ('scientist_admission_record_v2', ScientistAdmissionRecordV2)):
            with self.subTest(name=name):
                schema = json.loads((REPO_ROOT / 'schemas' / (name + '.schema.json')).read_text())
                jsonschema.Draft202012Validator.check_schema(schema)
                schema.pop('$schema')
                self.assertEqual(schema, model.model_json_schema())
        self.persist()
        record, _checksum = self.history.read(self.request.request_id)
        jsonschema.validate(record.model_dump(mode='json'), ScientistAdmissionRecord.model_json_schema())

    def test_public_example_is_explicitly_synthetic_and_exactly_hash_bound(self):
        example = json.loads((REPO_ROOT / 'examples/scientist_admission_history.json').read_text())
        self.assertIs(example['synthetic_cpu_fixture'], True)
        self.assertIs(example['runtime_admission'], False)
        self.assertIs(example['native_gpu_verified'], False)
        record = ScientistAdmissionRecord.model_validate(example['record'], strict=True)
        schema = json.loads((REPO_ROOT / 'schemas/scientist_admission_record.schema.json').read_text())
        jsonschema.validate(example['record'], schema)
        self.assertEqual(example['record_sha256'], digest(record.model_dump(mode='json')))
        request = ScientistTurnRequest.model_validate(example['request'], strict=True)
        self.assertEqual(record.request_sha256, scientist_request_sha256(request))
        self.assertEqual(record.intent_binding_sha256, digest(example['intent_binding']))

    def test_legacy_profile_serialization_and_original_public_hashes_are_unchanged(self):
        example = json.loads((REPO_ROOT / 'examples/scientist_admission_history.json').read_text())
        record = ScientistAdmissionRecord.model_validate(example['record'], strict=True)
        self.assertIs(type(record.admission_binding.profile_pin), ScientistProfilePin)
        self.assertEqual(set(record.admission_binding.profile_pin.model_dump()),
                         {'deployment_digest', 'manifest_sha256', 'config_sha256', 'response_schema_sha256'})
        self.assertEqual(record.model_dump(mode='json'), example['record'])
        self.assertEqual(digest(record.model_dump(mode='json')),
                         '2004221a96866d446177b256102a8ebe74b3d85df46885f6b549a1f3f4e4423c')
        self.assertEqual(record.admission_binding_sha256,
                         '155f7e20f8e675dbd2f8b63d976c4d539b514845d4028901479640e8ad29b045')

    def test_explicit_v2_pin_is_closed_and_rejects_null_or_coerced_version(self):
        output = {'name': 'aos-scientist-profile-output.v2', 'version': 2, 'bundle_sha256': 'b' * 64}
        self.assertEqual(ScientistOutputContractPin.model_validate(output).model_dump(), output)
        invalid = [None, {}, {**output, 'name': 'aos-scientist-profile-output.v1'},
                   {**output, 'bundle_sha256': 'B' * 64}, {**output, 'extra': False}]
        invalid.extend({**output, 'version': version} for version in (True, False, 1, 2.0, '2', None))
        for value in invalid:
            with self.subTest(value=value):
                captured = admission_capture_fixture(self.request, self.peer)
                captured['admission_binding']['profile_pin']['output_contract'] = value
                self.capture.side_effect = None
                self.capture.return_value = captured
                with self.assertRaises(ValueError):
                    self.persist()
                self.assertEqual(self.independent_counts(), (0, 0))

    def test_v2_union_preserves_typed_pin_and_exact_immutable_original_journal(self):
        captured = admission_capture_fixture(self.request, self.peer)
        legacy_hash = digest(captured['admission_binding'])
        output = {'name': 'aos-scientist-profile-output.v2', 'version': 2,
                  'bundle_sha256': 'ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e'}
        captured['admission_binding']['profile_pin']['output_contract'] = output
        binding = ScientistAdmissionBinding.model_validate(captured['admission_binding'], strict=True)
        self.assertIs(type(binding.profile_pin), ScientistProfilePinV2)
        self.assertEqual(binding.profile_pin.output_contract.model_dump(), output)
        typed = {**captured['admission_binding'], 'profile_pin': binding.profile_pin}
        self.assertEqual(ScientistAdmissionBinding.model_validate(typed).model_dump(mode='json'),
                         captured['admission_binding'])
        self.capture.side_effect = None
        self.capture.return_value = captured
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
            verify_current=self.verify, clock=lambda: 100, record_version='2.0')
        self.journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)
        self.persist()
        record, checksum = self.history.read(self.request.request_id)
        self.assertIs(type(record), ScientistAdmissionRecordV2)
        self.assertEqual(record.schema_version, '2.0')
        self.assertNotEqual(record.admission_binding_sha256, legacy_hash)
        self.assertEqual(record.admission_binding.profile_pin.output_contract.model_dump(), output)
        jsonschema.validate(record.model_dump(mode='json'), ScientistAdmissionRecordV2.model_json_schema())
        with closing(sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)) as connection:
            stored = connection.execute('SELECT record_json FROM scientist_admission_history').fetchone()[0]
        self.assertEqual(stored, canonical(record.model_dump(mode='json')))
        self.journal.verify_admission(self.request)
        self.journal.record_receipt(self.receipt, self.peer)
        self.assertEqual(self.history.read(self.request.request_id), (record, checksum))
        with self.assertRaises(ScientistAdmissionError):
            self.journal.verify_admission(self.request.model_copy(update={'request_id': 'f' * 32}))

    def test_v2_bundle_mutation_or_removal_cannot_reuse_original_binding_hash(self):
        example = json.loads((REPO_ROOT / 'examples/scientist_admission_history.json').read_text())
        value = example['record']
        value['schema_version'] = '2.0'
        output = {'name': 'aos-scientist-profile-output.v2', 'version': 2, 'bundle_sha256': 'b' * 64}
        value['admission_binding']['profile_pin']['output_contract'] = output
        with self.assertRaises(ValueError):
            ScientistAdmissionRecordV2.model_validate(value, strict=True)
        value['admission_binding_sha256'] = digest(value['admission_binding'])
        original = ScientistAdmissionRecordV2.model_validate(value, strict=True)
        for replacement in ({**output, 'bundle_sha256': 'c' * 64}, None):
            changed = deepcopy(value)
            pin = changed['admission_binding']['profile_pin']
            if replacement is None:
                del pin['output_contract']
            else:
                pin['output_contract'] = replacement
            with self.assertRaises(ValueError):
                ScientistAdmissionRecordV2.model_validate(changed, strict=True)
        self.assertNotEqual(digest(original.model_dump(mode='json')), example['record_sha256'])

    def test_versioned_capture_never_falls_back_or_creates_interim_v1_v2_records(self):
        captured = admission_capture_fixture(self.request, self.peer)
        output = {'name': 'aos-scientist-profile-output.v2', 'version': 2, 'bundle_sha256': 'b' * 64}
        for version, include_pin in (('1.0', True), ('2.0', False)):
            value = deepcopy(captured)
            if include_pin:
                value['admission_binding']['profile_pin']['output_contract'] = output
            history = ScientistAdmissionHistory(self.store, capture=lambda *_args: value,
                verify_current=self.verify, clock=lambda: 100, record_version=version)
            journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
            with self.subTest(version=version), self.assertRaises((ValueError, ScientistAdmissionError)):
                journal.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)
            self.assertEqual(self.independent_counts(), (0, 0))
        for version in (None, 1, 2, 2.0, True, '3.0', '2'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ScientistAdmissionHistory(self.store, record_version=version)

    def test_interim_v1_v2_record_is_historical_only_without_rehash_or_promotion(self):
        legacy = ScientistIntentJournal(self.store, self.binding)
        legacy.persist_intent(self.frame, self.digest, time.monotonic() + 60, self.peer)
        captured = admission_capture_fixture(self.request, self.peer)
        captured['admission_binding']['profile_pin']['output_contract'] = {
            'name': 'aos-scientist-profile-output.v2', 'version': 2, 'bundle_sha256': 'b' * 64}
        record = ScientistAdmissionRecord.model_validate({**captured, 'schema_version': '1.0',
            'request_id': self.request.request_id, 'request_sha256': self.digest,
            'session_id': self.binding.session_id,
            'intent_binding_sha256': digest(self.binding.model_dump(mode='json')),
            'admission_binding_sha256': digest(captured['admission_binding']), 'captured_boottime': 100})
        checksum = digest(record.model_dump(mode='json'))
        with self.store.connection:
            self.store.connection.execute('INSERT INTO scientist_admission_history VALUES(?,?,?,?,?,?,?,?)',
                (record.request_id, record.request_sha256, record.session_id, record.intent_binding_sha256,
                 record.admission_binding_sha256, checksum, canonical(record.model_dump(mode='json')), 'synthetic'))
        for version in ('1.0', '2.0'):
            history = ScientistAdmissionHistory(self.store, capture=self.capture,
                verify_current=self.verify, clock=lambda: 100, record_version=version)
            self.assertEqual(history.read(self.request.request_id), (record, checksum))
            journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
            with self.assertRaises(ScientistAdmissionError):
                journal.verify_admission(self.request)
            with self.assertRaises(ScientistAdmissionError):
                journal.record_receipt(self.receipt, self.peer)
        self.verify.assert_not_called()
        self.capture.assert_not_called()
        self.assertEqual(self.independent_counts(), (1, 1))

    def test_v2_reader_preserves_legacy_history_but_cannot_adopt_pending_work(self):
        self.persist()
        original = self.history.read(self.request.request_id)
        history = ScientistAdmissionHistory(self.store, capture=self.capture,
            verify_current=self.verify, clock=lambda: 100, record_version='2.0')
        self.assertEqual(history.read(self.request.request_id), original)
        journal = ScientistIntentJournal(self.store, self.binding, admission_history=history)
        with self.assertRaises(ScientistAdmissionError):
            journal.verify_admission(self.request)
        with self.assertRaises(ScientistAdmissionError):
            journal.record_receipt(self.receipt, self.peer)
        self.assertEqual(self.history.read(self.request.request_id), original)

    def test_read_dispatch_rejects_unknown_versions_and_v2_without_required_pin(self):
        self.persist()
        original, _checksum = self.history.read(self.request.request_id)
        with self.store.connection:
            self.store.connection.execute('DROP TRIGGER scientist_admission_history_no_update')
        for version in ('2.0', '3.0', 2, None, ['1.0']):
            value = original.model_dump(mode='json')
            value['schema_version'] = version
            with self.store.connection:
                self.store.connection.execute('UPDATE scientist_admission_history SET record_json=?,record_sha256=?',
                    (canonical(value), digest(value)))
            with self.subTest(version=version), self.assertRaises(ScientistAdmissionError):
                self.history.read(self.request.request_id)

    def test_public_v2_example_has_complete_versioned_schema_and_distinct_identity(self):
        example = json.loads((REPO_ROOT / 'examples/scientist_admission_history_v2.json').read_text())
        self.assertIs(example['synthetic_cpu_fixture'], True)
        self.assertIs(example['runtime_admission'], False)
        self.assertIs(example['native_gpu_verified'], False)
        record = ScientistAdmissionRecordV2.model_validate(example['record'], strict=True)
        self.assertEqual(record.schema_version, '2.0')
        for suffix, value in (('record', example['record']),
                              ('binding', example['record']['admission_binding'])):
            schema = json.loads((REPO_ROOT / f'schemas/scientist_admission_{suffix}_v2.schema.json').read_text())
            jsonschema.validate(value, schema)
        self.assertEqual(example['record_sha256'], digest(record.model_dump(mode='json')))
        request = ScientistTurnRequest.model_validate(example['request'], strict=True)
        self.assertEqual(record.request_sha256, scientist_request_sha256(request))
        self.assertEqual(record.intent_binding_sha256, digest(example['intent_binding']))


class ScientistAdmissionDesktopTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = desktop_fixture.ScientistDesktopTests.asyncSetUp
    asyncTearDown = desktop_fixture.ScientistDesktopTests.asyncTearDown
    start = desktop_fixture.ScientistDesktopTests.start
    approval = desktop_fixture.ScientistDesktopTests.approval

    def build(self):
        factory = desktop_fixture.create_scientist_desktop_scheduler
        peer_type = desktop_fixture.BrokerPeer
        self.capture = Mock(side_effect=lambda request, binding, peer: admission_capture_fixture(request, peer))
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
                                                 verify_current=lambda *_arguments: None, clock=lambda: 100)

        def create(*arguments, **keywords):
            return factory(*arguments, **keywords, admission_history=self.history)

        with patch.object(desktop_fixture, 'create_scientist_desktop_scheduler', side_effect=create), \
                patch.object(desktop_fixture, 'BrokerPeer',
                             side_effect=lambda *arguments: replace(peer_type(*arguments), boot_id=BOOT_ID)):
            return desktop_fixture.ScientistDesktopTests.build(self)

    async def test_factory_opt_in_actual_cpu_socket_hello_approval_and_original_identity(self):
        await desktop_fixture.ScientistDesktopTests.test_actual_typed_task_socket_intent_human_approval_and_independent_file_readback(self)
        with closing(sqlite3.connect(f'file:{self.settings.database}?mode=ro', uri=True)) as connection:
            rows = connection.execute('SELECT admission.request_id,intent.state,admission.record_json '
                'FROM scientist_admission_history admission JOIN scientist_turn_intents intent '
                'ON admission.request_id=intent.request_id').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 'receipt_recorded')
        record, _checksum = self.history.read(rows[0][0])
        self.assertEqual(json.loads(rows[0][2]), record.model_dump(mode='json'))
        self.assertEqual(record.admission_binding.profile_pin.deployment_digest,
                         self.scheduler.scientist_binding.profiles['aos.decider.turn.v1'])
        self.assertEqual(self.capture.call_count, 1)
        self.assertEqual(len(self.broker.requests), 1)
