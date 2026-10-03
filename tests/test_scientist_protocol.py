import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from aos.scientist_protocol import (
    ScientistTurnRequest, scientist_receipt_frame, scientist_request_frame,
    scientist_request_sha256,
    ScientistRunStatus, verify_scientist_report,
    ScientistTurnReceipt, ScientistReport,
)


class ScientistProtocolTests(unittest.TestCase):
    def test_canonical_schemas_match_typed_models(self):
        root = Path(__file__).resolve().parents[1] / 'schemas'
        for model, name in [(ScientistTurnRequest, 'scientist_turn_request'),
                            (ScientistTurnReceipt, 'scientist_turn_receipt'),
                            (ScientistRunStatus, 'scientist_run_status'),
                            (ScientistReport, 'scientist_report')]:
            schema = json.loads((root / (name + '.schema.json')).read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            schema.pop('$schema')
            self.assertEqual(schema, model.model_json_schema())

    def setUp(self):
        self.request = ScientistTurnRequest(
            request_id='a' * 32, profile_id='aos.decider.turn.v1',
            deployment_digest='b' * 64, payload={'text': 'Türkçe'},
        )
        self.receipt = {
            'version': 1, 'request_id': self.request.request_id,
            'profile_id': self.request.profile_id, 'deployment_digest': 'b' * 64,
            'generation': {'unit': 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service',
                           'invocation_id': 'd' * 32, 'main_pid': 1234,
                           'control_group': '/user.slice/swapp-aos-gpu-turn-' + 'c' * 32 + '.service'},
            'response': {}, 'usage': {},
        }

    def frame(self):
        return json.dumps(self.receipt).encode() + b'\n'

    def test_wire_matches_scientist_canonical_utf8_and_hash(self):
        frame = scientist_request_frame(self.request)
        self.assertIn('Türkçe'.encode(), frame)
        self.assertEqual(frame[:-1], json.dumps(self.request.model_dump(), ensure_ascii=False,
                         allow_nan=False, sort_keys=True, separators=(',', ':')).encode())
        self.assertEqual(scientist_request_sha256(self.request), hashlib.sha256(frame[:-1]).hexdigest())

    def test_correlated_receipt_is_metadata_not_a_drain_attestation(self):
        receipt = scientist_receipt_frame(self.frame(), self.request)
        self.assertEqual(receipt.generation.main_pid, 1234)
        self.assertNotIn('drained', receipt.model_dump())

    def test_wrong_request_profile_deployment_and_version_are_rejected(self):
        for field, value in [('request_id', 'e' * 32), ('deployment_digest', 'e' * 64),
                             ('profile_id', 'aos.bonsai.vision.v1'), ('version', 2), ('version', True)]:
            with self.subTest(field=field, value=value):
                original = self.receipt[field]
                self.receipt[field] = value
                with self.assertRaises(ValueError):
                    scientist_receipt_frame(self.frame(), self.request)
                self.receipt[field] = original

    def test_duplicate_fields_including_nested_fields_are_rejected(self):
        for frame in [self.frame().replace(b'"version": 1', b'"version": 1, "version": 1'),
                      self.frame().replace(b'"response": {}', b'"response": {"x":1,"x":2}')]:
            with self.assertRaises(ValueError):
                scientist_receipt_frame(frame, self.request)

    def test_nonfinite_nested_receipt_is_rejected_before_persistence(self):
        for value in [b'NaN', b'Infinity', b'-Infinity']:
            frame = self.frame().replace(b'"response": {}', b'"response": {"value":' + value + b'}')
            with self.assertRaises(ValueError):
                scientist_receipt_frame(frame, self.request)

    def test_malformed_oversized_and_multiple_frames_are_rejected(self):
        for frame in [b'{}', b'{}\n{}\n', b'x' * 131072 + b'\n', b'\xff\n']:
            with self.assertRaises(ValueError):
                scientist_receipt_frame(frame, self.request)

    def test_foreign_worker_unit_and_cgroup_are_rejected(self):
        for field, value in [('unit', 'other.service'), ('main_pid', True),
                             ('control_group', '/foreign.service'), ('invocation_id', 'old')]:
            with self.subTest(field=field):
                original = self.receipt['generation'][field]
                self.receipt['generation'][field] = value
                with self.assertRaises(ValueError):
                    scientist_receipt_frame(self.frame(), self.request)
                self.receipt['generation'][field] = original

    def test_requests_reject_nonfinite_and_oversized_payloads(self):
        for payload in [{'bad': float('nan')}, {'large': 'x' * 131072}]:
            with self.assertRaises(ValueError):
                scientist_request_frame(self.request.model_copy(update={'payload': payload}))


class ScientistReportTests(unittest.TestCase):
    def setUp(self):
        self.run_id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.body = {'run_id': self.run_id, 'status': 'stopped', 'summary': 'İptal'}
        self.digest = hashlib.sha256(json.dumps(self.body, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.report = {'run_id': self.run_id, 'report_sha256': self.digest,
                       'report': self.body, 'verified_at': 'synthetic'}
        self.status = ScientistRunStatus(run_id=self.run_id, origin='aos', purpose='research',
                                         state='stopped', created_at='synthetic', updated_at='synthetic',
                                         stop_requested=True, report_sha256=self.digest)

    def verify(self, status=None):
        return verify_scientist_report(json.dumps(self.report).encode(),
                                       status=status or self.status, expected_run_id=self.run_id)

    def test_terminal_report_requires_both_independent_hash_and_identity(self):
        self.assertEqual(self.verify().report_sha256, self.digest)
        self.report['report']['summary'] = 'changed'
        with self.assertRaises(ValueError):
            self.verify()

    def test_stop_ack_is_not_terminal_or_gpu_release(self):
        for state in ['queued', 'running', 'stop_requested']:
            with self.assertRaises(ValueError):
                self.verify(self.status.model_copy(update={'state': state}))

    def test_foreign_run_origin_and_status_hash_are_rejected(self):
        for field, value in [('run_id', 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'),
                             ('origin', 'local'), ('report_sha256', 'f' * 64)]:
            with self.assertRaises(ValueError):
                self.verify(self.status.model_copy(update={field: value}))

    def test_matching_baseline_terminal_report_cannot_be_adopted_as_research(self):
        with self.assertRaisesRegex(ValueError, 'purpose'):
            self.verify(self.status.model_copy(update={'purpose': 'baseline'}))

    def test_duplicate_json_and_report_status_mismatch_are_rejected(self):
        raw = json.dumps(self.report).replace('"verified_at": "synthetic"',
                                              '"verified_at":"a","verified_at":"b"').encode()
        with self.assertRaises(ValueError):
            verify_scientist_report(raw, status=self.status, expected_run_id=self.run_id)
        self.body['status'] = 'completed'
        with self.assertRaises(ValueError):
            self.verify()
