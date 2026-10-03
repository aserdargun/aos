import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from aos.contracts import AOSFault, REPO_ROOT, canonical
from aos.scientist_bonsai_receipt import validate_bonsai_receipt, validate_profile_receipt
from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
from aos.scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest, scientist_request_frame
from aos.scientist_transport import ScientistAdmissionError, ScientistTurnClient, ScientistUncertainTurn
from aos.storage import TrajectoryStore
from aos.supervisor import BonsaiSupervisor, RECOVERY_ACTIONS
from aos.vision import BonsaiVisionSupervisor, Capture

import test_scientist_decider_receipt as decider_fixture
from test_bonsai_projection import native_shape_fixture, project_bonsai_response


def request_fixture(manifest, vision=False):
    if not vision:
        supervisor = BonsaiSupervisor(manifest)
        evidence = [{'id': 'synthetic-missing-file', 'message': 'Synthetic authorized file missing'}]
        payload = supervisor.request_body('Synthetic bounded recovery; no inference', evidence)
        content = {'diagnosis': 'Synthetic evidence requires review', 'evidence_refs': ['synthetic-missing-file'],
                   'assumptions': [], 'revised_plan': [], 'verification_criteria': ['exact_file_content'], 'needs_human': True}
    else:
        image = (b'\x89PNG\r\n\x1a\n' + struct.pack('>I4sII', 13, b'IHDR', 640, 360)
                 + b'\x00\x00\x00\x00IEND\xaeB\x60\x82')
        capture = Capture(capture_id='a' * 32, width=640, height=360,
            sha256=hashlib.sha256(image).hexdigest(), image_base64=base64.b64encode(image).decode())
        supervisor = BonsaiVisionSupervisor(manifest)
        payload = supervisor.request_body('Synthetic visual receipt; no inference',
            [{'capture': capture.model_dump(), 'state_version': 2}])
        content = json.loads((REPO_ROOT / 'examples/vision_scene.json').read_text())['scene']
        content.update(capture_id=capture.capture_id, state_version=2)
    request = ScientistTurnRequest(request_id='a' * 32,
        profile_id='aos.bonsai.vision.v1' if vision else 'aos.bonsai.recovery.v1',
        deployment_digest=supervisor.identity['deployment_id'].removeprefix('bonsai-'), payload=payload)
    return request, content


def receipt_fixture(request, content):
    unit = 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service'
    usage = {'prompt_tokens': 80, 'completion_tokens': 32}
    return {'version': 1, 'request_id': request['request_id'], 'profile_id': request['profile_id'],
            'deployment_digest': request['deployment_digest'],
            'generation': {'unit': unit, 'invocation_id': 'd' * 32, 'main_pid': 1234, 'control_group': '/synthetic/' + unit},
            'response': {'model': request['payload']['model'], 'choices': [{'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': canonical(content)}}], 'usage': deepcopy(usage)},
            'usage': deepcopy(usage)}


class BonsaiReceiptValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-bonsai-receipt-')
        self.addCleanup(temporary.cleanup)
        self.manifest = Path(temporary.name) / 'synthetic-manifest.json'
        self.manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 128}))

    def fixtures(self, vision=False):
        request, content = request_fixture(self.manifest, vision)
        return request, receipt_fixture(request.model_dump(), content), content

    def validate(self, request, receipt, **options):
        return validate_bonsai_receipt(request, ScientistTurnReceipt.model_validate(receipt, strict=True), **options)

    def test_actual_request_builders_accept_optional_model_and_assistant_without_mutation(self):
        for vision in (False, True):
            for include_model in (False, True):
                for include_role in (False, True):
                    with self.subTest(vision=vision, model=include_model, role=include_role):
                        request, receipt, _content = self.fixtures(vision)
                        if not include_model:
                            receipt['response'].pop('model')
                        if not include_role:
                            receipt['response']['choices'][0]['message'].pop('role')
                        original = deepcopy(receipt)
                        self.assertIsNone(self.validate(request, receipt))
                        self.assertIsNone(validate_profile_receipt(request, ScientistTurnReceipt.model_validate(receipt, strict=True)))
                        self.assertEqual(receipt, original)

    def test_closed_response_usage_choices_message_reject_extras_and_missing_fields(self):
        for vision in (False, True):
            for path in [('response',), ('response', 'usage'), ('usage',), ('response', 'choices', 0),
                         ('response', 'choices', 0, 'message')]:
                for operation in ('extra', 'missing'):
                    with self.subTest(vision=vision, path=path, operation=operation):
                        request, receipt, _content = self.fixtures(vision)
                        target = receipt
                        for field in path:
                            target = target[field]
                        if operation == 'extra':
                            target['reasoning'] = 'Synthetic forbidden channel'
                        else:
                            required = ('content' if path[-1] == 'message' else 'choices' if path == ('response',)
                                        else 'finish_reason' if path[-1] == 0 else 'prompt_tokens')
                            target.pop(required)
                        with self.assertRaises((ValueError, AOSFault)):
                            self.validate(request, receipt)

    def test_usage_is_exactly_two_equal_integer_counts_with_output_and_context_budgets(self):
        for vision in (False, True):
            for key in ('prompt_tokens', 'completion_tokens'):
                for invalid in (True, 1.0, '1', -1, float('nan'), float('inf')):
                    with self.subTest(vision=vision, key=key, invalid=invalid):
                        request, receipt, _content = self.fixtures(vision)
                        for usage in (receipt['usage'], receipt['response']['usage']):
                            usage[key] = invalid
                        with self.assertRaises(ValueError):
                            self.validate(request, receipt)
            request, receipt, _content = self.fixtures(vision)
            receipt['usage']['completion_tokens'] += 1
            with self.assertRaises(ValueError):
                self.validate(request, receipt)
            for usage in (receipt['usage'], receipt['response']['usage']):
                usage.update(prompt_tokens=0, completion_tokens=128)
            self.assertIsNone(self.validate(request, receipt, context_tokens=256))
            for usage in (receipt['usage'], receipt['response']['usage']):
                usage.update(prompt_tokens=129, completion_tokens=128)
            with self.assertRaises(ValueError):
                self.validate(request, receipt, context_tokens=256)
            for usage in (receipt['usage'], receipt['response']['usage']):
                usage.update(prompt_tokens=0, completion_tokens=129)
            with self.assertRaises(ValueError):
                self.validate(request, receipt)
            for limit in (True, 256.0, 255, 16385):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    self.validate(request, receipt, context_tokens=limit)

    def test_incomplete_tool_refusal_reasoning_and_foreign_assistant_are_denied(self):
        for vision in (False, True):
            for finish in ('length', 'tool_calls', None):
                request, receipt, _content = self.fixtures(vision)
                receipt['response']['choices'][0]['finish_reason'] = finish
                with self.subTest(vision=vision, finish=finish), self.assertRaises(ValueError):
                    self.validate(request, receipt)
            for extra in ('tool_calls', 'function_call', 'refusal', 'reasoning_content', 'reasoning'):
                request, receipt, _content = self.fixtures(vision)
                receipt['response']['choices'][0]['message'][extra] = []
                with self.subTest(vision=vision, extra=extra), self.assertRaises(ValueError):
                    self.validate(request, receipt)
            for choices in ([], [{}, {}]):
                request, receipt, _content = self.fixtures(vision)
                receipt['response']['choices'] = choices
                with self.assertRaises(ValueError):
                    self.validate(request, receipt)
            request, receipt, _content = self.fixtures(vision)
            receipt['response']['choices'][0]['message']['role'] = 'tool'
            with self.assertRaises(ValueError):
                self.validate(request, receipt)

    def test_duplicate_nonfinite_oversized_nonstring_and_unknown_inner_content_are_denied(self):
        for vision in (False, True):
            for content in ('', None, {}, '[' + '0,' * 33000 + '0]', '{"needs_human":true,"needs_human":false}',
                            '{"unknown":NaN}', '{"unknown":Infinity}', '{"unknown":-Infinity}'):
                request, receipt, _original = self.fixtures(vision)
                receipt['response']['choices'][0]['message']['content'] = content
                with self.subTest(vision=vision, type=type(content).__name__), self.assertRaises((ValueError, AOSFault)):
                    self.validate(request, receipt)
            request, receipt, content = self.fixtures(vision)
            content['tools'] = []
            receipt['response']['choices'][0]['message']['content'] = canonical(content)
            with self.assertRaises(ValueError):
                self.validate(request, receipt)

    def test_recovery_evidence_exact_ids_and_finite_authorized_plan(self):
        for refs in (['foreign'], [], ['synthetic-missing-file', 'foreign']):
            request, receipt, content = self.fixtures()
            content['evidence_refs'] = refs
            receipt['response']['choices'][0]['message']['content'] = canonical(content)
            with self.subTest(refs=refs), self.assertRaises((ValueError, AOSFault)):
                self.validate(request, receipt)
        request, receipt, content = self.fixtures()
        content.update(needs_human=False, revised_plan=[{'order': index, 'action': action, 'expected_result': 'Synthetic bounded result'}
            for index, action in enumerate(RECOVERY_ACTIONS, 1)])
        receipt['response']['choices'][0]['message']['content'] = canonical(content)
        self.assertIsNone(self.validate(request, receipt))
        for field, invalid in [('order', 1.0), ('order', True), ('action', 'run_shell'), ('expected_result', '')]:
            changed = deepcopy(content)
            changed['revised_plan'][0][field] = invalid
            receipt['response']['choices'][0]['message']['content'] = canonical(changed)
            with self.subTest(field=field, invalid=invalid), self.assertRaises((ValueError, AOSFault)):
                self.validate(request, receipt)
        content['needs_human'] = True
        receipt['response']['choices'][0]['message']['content'] = canonical(content)
        with self.assertRaises(AOSFault):
            self.validate(request, receipt)
        request, receipt, _content = self.fixtures()
        original = json.loads(request.payload['messages'][1]['content'])
        original['evidence'] *= 2
        request.payload['messages'][1]['content'] = canonical(original)
        with self.assertRaises(ValueError):
            self.validate(request, receipt)

    def test_nested_plan_scene_and_original_request_extras_are_denied(self):
        for vision in (False, True):
            request, receipt, content = self.fixtures(vision)
            if vision:
                content['elements'][0]['tool_calls'] = []
            else:
                content.update(needs_human=False, revised_plan=[
                    {'order': index, 'action': action, 'expected_result': 'Synthetic bounded result'}
                    for index, action in enumerate(RECOVERY_ACTIONS, 1)])
                content['revised_plan'][0]['tool_calls'] = []
            receipt['response']['choices'][0]['message']['content'] = canonical(content)
            with self.subTest(vision=vision, location='result'), self.assertRaises(ValueError):
                self.validate(request, receipt)
            request, receipt, _content = self.fixtures(vision)
            user = request.payload['messages'][1]
            original = json.loads(user['content'][0]['text'] if vision else user['content'])
            original['tools'] = []
            if vision:
                user['content'][0]['text'] = canonical(original)
            else:
                user['content'] = canonical(original)
            with self.subTest(vision=vision, location='request'), self.assertRaises(ValueError):
                self.validate(request, receipt)

    def test_vision_capture_state_dimensions_image_and_nested_boxes_are_bound(self):
        for field, invalid in [('capture_id', 'b' * 32), ('state_version', 3), ('state_version', True),
                               ('state_version', 2.0), ('width', 640.0), ('height', True), ('needs_human', 0)]:
            request, receipt, content = self.fixtures(True)
            content[field] = invalid
            receipt['response']['choices'][0]['message']['content'] = canonical(content)
            with self.subTest(field=field, invalid=invalid), self.assertRaises((ValueError, AOSFault)):
                self.validate(request, receipt)
        for field, invalid in [('x', 60.0), ('x', True), ('width', 1), ('width', 640)]:
            request, receipt, content = self.fixtures(True)
            content['elements'][0]['bbox'][field] = invalid
            receipt['response']['choices'][0]['message']['content'] = canonical(content)
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                self.validate(request, receipt)
        for change in ('digest', 'image', 'extras'):
            request, receipt, _content = self.fixtures(True)
            text = request.payload['messages'][1]['content'][0]
            if change == 'digest':
                original = json.loads(text['text'])
                original['sha256'] = 'b' * 64
                text['text'] = canonical(original)
            elif change == 'image':
                request.payload['messages'][1]['content'][1]['image_url']['url'] = 'data:image/png;base64,AAAA'
            else:
                request.payload['messages'][1]['content'][1]['image_url']['detail'] = 'high'
            with self.subTest(change=change), self.assertRaises((ValueError, AOSFault)):
                self.validate(request, receipt)

    def test_profile_deployment_request_protocol_and_original_schema_are_exact(self):
        for vision in (False, True):
            for field, invalid in [('profile_id', 'aos.decider.turn.v1'), ('request_id', 'b' * 32),
                                   ('deployment_digest', 'b' * 64)]:
                request, receipt, _content = self.fixtures(vision)
                receipt[field] = invalid
                with self.subTest(vision=vision, field=field), self.assertRaises(ValueError):
                    self.validate(request, receipt)
            request, receipt, _content = self.fixtures(vision)
            receipt['response']['model'] = 'foreign'
            with self.assertRaises(ValueError):
                self.validate(request, receipt)
            for field, invalid in [('stream', True), ('max_tokens', True), ('max_tokens', 513), ('model', 'foreign'),
                                   ('temperature', True), ('temperature', 0.1), ('temperature', float('nan')),
                                   ('chat_template_kwargs', {'enable_thinking': 0}),
                                   ('chat_template_kwargs', {'enable_thinking': False, 'tools': []})]:
                request, receipt, _content = self.fixtures(vision)
                request.payload[field] = invalid
                with self.subTest(vision=vision, field=field), self.assertRaises(ValueError):
                    self.validate(request, receipt)
            for field, invalid in [('name', 'other_schema'), ('strict', 1), ('schema', {})]:
                request, receipt, _content = self.fixtures(vision)
                request.payload['response_format']['json_schema'][field] = invalid
                with self.subTest(vision=vision, schema_field=field), self.assertRaises(ValueError):
                    self.validate(request, receipt)

    def test_protocol_versions_remain_lexical_integers_before_model_normalization(self):
        for vision in (False, True):
            request, raw_receipt, _content = self.fixtures(vision)
            receipt = ScientistTurnReceipt.model_validate(raw_receipt, strict=True)
            for invalid in (True, 1.0):
                with self.subTest(vision=vision, side='request', invalid=invalid), self.assertRaises(ValueError):
                    validate_bonsai_receipt(request.model_copy(update={'version': invalid}), receipt)
                with self.subTest(vision=vision, side='receipt', invalid=invalid), self.assertRaises(ValueError):
                    validate_bonsai_receipt(request, receipt.model_copy(update={'version': invalid}))

    def test_dispatcher_rejects_foreign_profiles_and_preserves_decider_guard(self):
        request, raw_receipt, _content = self.fixtures()
        receipt = ScientistTurnReceipt.model_validate(raw_receipt, strict=True)
        with self.assertRaises(ValueError):
            validate_profile_receipt(request.model_copy(update={'profile_id': 'foreign'}), receipt)
        request = decider_fixture.request_fixture()
        receipt = ScientistTurnReceipt.model_validate(decider_fixture.receipt_fixture(request.model_dump()), strict=True)
        self.assertIsNone(validate_profile_receipt(request, receipt))
        for invalid in (True, 1.0):
            with self.subTest(side='request', invalid=invalid), self.assertRaises(ValueError):
                validate_profile_receipt(request.model_copy(update={'version': invalid}), receipt)
            with self.subTest(side='receipt', invalid=invalid), self.assertRaises(ValueError):
                validate_profile_receipt(request, receipt.model_copy(update={'version': invalid}))


class BonsaiReceiptTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-bonsai-receipt-socket-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.manifest = self.root / 'synthetic-manifest.json'
        self.manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 128}))

    def exercise(self, vision, invalid=False, producer=False):
        root = self.root / ('vision' if vision else 'recovery')
        root.mkdir(mode=0o700)
        request, content = request_fixture(self.manifest, vision)
        store = TrajectoryStore(root / 'synthetic.sqlite3')
        with closing(store):
            with store.connection:
                store.connection.execute('INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                    ('session', 'runtime', 'synthetic', 'AGENT', 'lease', 0, 'running', 'synthetic', 'synthetic'))
            journal = ScientistIntentJournal(store, ScientistIntentBinding(session_id='session', runtime_id='runtime',
                owner='AGENT', lease_id='lease', generation=0, authorization_context_sha256='a' * 64))

            def factory(raw_request):
                self.assertEqual(raw_request, request.model_dump())
                value = receipt_fixture(raw_request, content)
                if producer:
                    native = native_shape_fixture()
                    native['model'] = request.payload['model']
                    native['choices'][0]['message'] = deepcopy(value['response']['choices'][0]['message'])
                    native['usage'] = deepcopy(value['usage']) | {
                        'total_tokens': sum(value['usage'].values()), 'prompt_tokens_details': {'cached_tokens': 0}}
                    native['timings'].update(prompt_n=value['usage']['prompt_tokens'],
                                             predicted_n=value['usage']['completion_tokens'])
                    value['response'] = project_bonsai_response(native,
                        deployment_digest=request.deployment_digest, context_tokens=16384,
                        max_output_tokens=request.payload['max_tokens'])
                if invalid:
                    value['usage']['completion_tokens'] += 1
                return value

            observed = []

            def validate(original_request, receipt):
                observed.append(tuple(store.connection.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()))
                validate_profile_receipt(original_request, receipt)

            with patch.object(decider_fixture, 'receipt_fixture', side_effect=factory):
                broker = decider_fixture.ReceiptBroker(root)
                try:
                    client = ScientistTurnClient(broker.path, timeout_seconds=3,
                        authenticator=decider_fixture.authenticator_fixture(),
                        verify_admission=journal.verify_admission, persist_intent=journal.persist_intent,
                        record_receipt=journal.record_receipt, validate_receipt=validate)
                    if invalid:
                        with self.assertRaises(ScientistUncertainTurn):
                            client.infer(request)
                        self.assertEqual(client.uncertain_request_id, request.request_id)
                        for request_id in (request.request_id, 'f' * 32):
                            with self.assertRaises(ScientistAdmissionError):
                                client.infer(request.model_copy(update={'request_id': request_id}))
                        expected = ('pending', None)
                    else:
                        receipt = client.infer(request)
                        expected = ('receipt_recorded', receipt.model_dump_json())
                        self.assertEqual(receipt.model_dump(), receipt_fixture(request.model_dump(), content))
                        self.assertIsNone(client.uncertain_request_id)
                    with closing(sqlite3.connect(root / 'synthetic.sqlite3')) as reader:
                        row = reader.execute('SELECT state,receipt_json FROM scientist_turn_intents').fetchone()
                    self.assertEqual(row, expected)
                    self.assertEqual(observed, [('pending', None)])
                    self.assertEqual(broker.requests, [scientist_request_frame(request)])
                finally:
                    broker.close()

    def test_actual_socket_guards_both_profiles_before_journal_commit_and_preserves_receipt(self):
        for vision in (False, True):
            with self.subTest(vision=vision):
                self.exercise(vision)

    def test_actual_socket_invalid_usage_stays_pending_uncertain_and_blocks_old_and_new_replay(self):
        for vision in (False, True):
            with self.subTest(vision=vision):
                self.exercise(vision, invalid=True)

    def test_source_shaped_producer_projection_passes_both_profile_journal_guards(self):
        for vision in (False, True):
            with self.subTest(vision=vision):
                self.exercise(vision, producer=True)
