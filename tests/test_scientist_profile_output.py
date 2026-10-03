from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_desktop import create_scientist_desktop_scheduler
from aos.scientist_intents import ScientistIntentJournal
from aos.scientist_inventory import _admission_identity
from aos.scientist_profile_output import admission_profile_validator
from aos.scientist_protocol import ScientistTurnReceipt, scientist_request_frame, scientist_request_sha256
from aos.scientist_transport import ScientistAdmissionError

from test_scientist_admission_history import admission_capture_fixture, BOOT_ID
from test_scientist_decider_receipt import request_fixture, receipt_fixture
import test_scientist_bonsai_receipt as bonsai_fixture
import test_scientist_intents as intent_fixture


OUTPUT_PIN = {'name': 'aos-scientist-profile-output.v2', 'version': 2,
              'bundle_sha256': 'ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e'}


class AdmissionProfileOutputTests(unittest.TestCase):
    def setUp(self):
        intent_fixture.ScientistIntentTests.setUp(self)
        self.peer = replace(self.peer, boot_id=BOOT_ID)
        self.request = request_fixture()
        self.receipt = ScientistTurnReceipt.model_validate(receipt_fixture(self.request.model_dump()), strict=True)
        self.capture = Mock(side_effect=self.captured)
        self.verify = Mock(return_value=None)
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
                                                 verify_current=self.verify, clock=lambda: 100,
                                                 record_version='2.0')
        self.journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)

    def captured(self, request, binding, peer):
        value = admission_capture_fixture(request, peer)
        value['admission_binding']['profile_pin']['output_contract'] = deepcopy(OUTPUT_PIN)
        return value

    def persist(self):
        self.journal.persist_intent(scientist_request_frame(self.request)[:-1],
                                    scientist_request_sha256(self.request), time.monotonic() + 60, self.peer)

    def test_original_pin_validates_without_authorizing_or_recording_a_receipt(self):
        self.persist()
        original = self.history.read(self.request.request_id)
        self.verify.reset_mock()
        validate = admission_profile_validator(self.history, OUTPUT_PIN)
        self.assertIsNone(validate(self.request, self.receipt))
        self.assertEqual(self.history.read(self.request.request_id), original)
        record, checksum = original
        self.assertEqual(_admission_identity(self.store, self.request.request_id),
                         {'available': True, 'present': True, 'record_sha256': checksum,
                          'binding_sha256': record.admission_binding_sha256})
        self.verify.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT state FROM scientist_turn_intents').fetchone()[0],
                         'pending')

    def test_missing_legacy_or_different_output_pin_does_not_adopt_original_identity(self):
        self.capture.side_effect = lambda request, binding, peer: admission_capture_fixture(request, peer)
        self.history = ScientistAdmissionHistory(self.store, capture=self.capture,
                                                 verify_current=self.verify, clock=lambda: 100)
        self.journal = ScientistIntentJournal(self.store, self.binding, admission_history=self.history)
        self.persist()
        with self.assertRaises(ScientistAdmissionError):
            admission_profile_validator(self.history, OUTPUT_PIN)
        reader = ScientistAdmissionHistory(self.store, record_version='2.0')
        validate = admission_profile_validator(reader, OUTPUT_PIN)
        with self.assertRaises(ScientistAdmissionError):
            validate(self.request, self.receipt)

    def test_bundle_or_original_request_change_denied_before_content_guard(self):
        self.persist()
        changed_pin = {**OUTPUT_PIN, 'bundle_sha256': 'f' * 64}
        for pin, request in ((changed_pin, self.request), (OUTPUT_PIN,
                self.request.model_copy(update={'payload': {'request': {'different': True}}}))):
            with patch('aos.scientist_profile_output.validate_decider_receipt') as guard:
                with self.assertRaises(ScientistAdmissionError):
                    admission_profile_validator(self.history, pin)(request, self.receipt)
                guard.assert_not_called()

    def test_matching_pin_does_not_bypass_semantic_validation(self):
        self.persist()
        response = deepcopy(self.receipt.response)
        response['prediction']['selected_option'] = 'unapproved'
        with self.assertRaises(ValueError):
            admission_profile_validator(self.history, OUTPUT_PIN)(
                self.request, self.receipt.model_copy(update={'response': response}))

    def bonsai_original(self, vision):
        manifest = Path(self.temporary.name) / 'synthetic-bonsai-manifest.json'
        manifest.write_text('{"temperature":0.0,"max_output_tokens":128}')
        self.request, content = bonsai_fixture.request_fixture(manifest, vision)
        self.receipt = ScientistTurnReceipt.model_validate(
            bonsai_fixture.receipt_fixture(self.request.model_dump(), content), strict=True)
        self.persist()
        validate = admission_profile_validator(self.history, OUTPUT_PIN, context_tokens=256)
        self.assertIsNone(validate(self.request, self.receipt))
        response = deepcopy(self.receipt.response)
        usage = {'prompt_tokens': 256, 'completion_tokens': 32}
        response['usage'] = usage
        with self.assertRaises(ValueError):
            validate(self.request, self.receipt.model_copy(update={'response': response, 'usage': usage}))

    def test_v2_recovery_guard_preserves_original_evidence_and_trusted_context(self):
        self.bonsai_original(False)

    def test_v2_vision_guard_preserves_original_capture_and_trusted_context(self):
        self.bonsai_original(True)

    def test_trusted_pin_and_context_are_frozen_and_bounded(self):
        self.persist()
        pin = deepcopy(OUTPUT_PIN)
        validate = admission_profile_validator(self.history, pin)
        pin['bundle_sha256'] = 'f' * 64
        self.assertIsNone(validate(self.request, self.receipt))
        for version in (True, 2.0, '2', 1):
            with self.assertRaises(ValueError):
                admission_profile_validator(self.history, {**OUTPUT_PIN, 'version': version})
        for context in (True, 255, 16385, 512.0):
            with self.assertRaises(ValueError):
                admission_profile_validator(self.history, OUTPUT_PIN, context_tokens=context)

    def test_factory_rejects_missing_history_or_conflicting_callback_before_runtime(self):
        for options in ({'output_contract': OUTPUT_PIN},
                        {'output_contract': OUTPUT_PIN, 'admission_history': self.history,
                         'validate_receipt': lambda *_arguments: None}):
            with self.assertRaises((TypeError, ValueError)):
                create_scientist_desktop_scheduler(None, None, None, None, **options)


if __name__ == '__main__':
    unittest.main()
