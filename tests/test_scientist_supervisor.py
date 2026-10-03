import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aos.contracts import AOSFault, REPO_ROOT, digest
from aos.scientist_protocol import ScientistTurnReceipt
from aos.registries import ModelRegistry
from aos.scientist_supervisor import ScientistBonsaiSupervisor, ScientistBonsaiVisionSupervisor
from aos.scientist_transport import ScientistAdmissionError, ScientistUncertainTurn
from aos.supervisor import BonsaiSupervisor
from aos.vision import BonsaiVisionSupervisor, Capture
from test_scientist_broker_workers import BONSAI


class ScientistSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manifest = Path(self.temporary.name) / 'synthetic.json'
        self.manifest.write_text(json.dumps({'temperature': 0.0, 'max_output_tokens': 512}))
        self.evidence = [{'id': 'synthetic-missing-file', 'message': 'Authorized hello file is missing'}]
        self.plan = {'diagnosis': 'Insufficient synthetic evidence',
                     'evidence_refs': ['synthetic-missing-file'], 'assumptions': [],
                     'revised_plan': [], 'verification_criteria': ['exact_file_content'], 'needs_human': True}
        payload = (b'\x89PNG\r\n\x1a\n' + struct.pack('>I4sII', 13, b'IHDR', 640, 360)
                   + b'\x00\x00\x00\x00IEND\xaeB\x60\x82')
        self.capture = Capture(capture_id='a' * 32, width=640, height=360,
            sha256=hashlib.sha256(payload).hexdigest(), image_base64=base64.b64encode(payload).decode())
        self.scene = json.loads((REPO_ROOT / 'examples/vision_scene.json').read_text())['scene']
        self.scene.update(capture_id=self.capture.capture_id, state_version=2)

    def supervisor(self, *, vision=False, verifier=lambda *arguments: None, mutate=None):
        async def infer(request):
            content = deepcopy(self.scene if vision else self.plan)
            response = {'choices': [{'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': json.dumps(content)}}]}
            unit = 'swapp-aos-gpu-turn-' + 'c' * 32 + '.service'
            receipt = ScientistTurnReceipt(version=1, request_id=request.request_id,
                profile_id=request.profile_id, deployment_digest=request.deployment_digest,
                generation={'unit': unit, 'invocation_id': 'd' * 32, 'main_pid': 1234,
                            'control_group': '/synthetic/' + unit}, response=response, usage={})
            if mutate:
                mutate(receipt)
            return receipt
        client = Mock(infer=AsyncMock(side_effect=infer))
        constructor = ScientistBonsaiVisionSupervisor if vision else ScientistBonsaiSupervisor
        return constructor(self.manifest, client, verify_context=verifier), client

    async def test_recovery_uses_existing_native_schema_identity_and_only_broker(self):
        supervisor, client = self.supervisor()
        native = BonsaiSupervisor(self.manifest)
        self.assertEqual(supervisor.identity['deployment_id'], native.identity['deployment_id'])
        with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('No native fallback')):
            result = await supervisor.plan('Recover authorized file', self.evidence)
        request = client.infer.call_args.args[0]
        self.assertEqual(request.profile_id, 'aos.bonsai.recovery.v1')
        self.assertEqual(request.payload, native.request_body('Recover authorized file', self.evidence))
        BONSAI['validate_payload'](request.payload, supervisor.pins,
            Mock(value={'deployment_digest': request.deployment_digest}), 'recovery')
        self.assertEqual(result.model_dump(), self.plan)
        self.assertEqual(supervisor.last_response, self.plan)

    async def test_vision_preserves_capture_schema_and_native_deployment_identity(self):
        supervisor, client = self.supervisor(vision=True)
        native = BonsaiVisionSupervisor(self.manifest)
        self.assertEqual(supervisor.identity['deployment_id'], native.identity['deployment_id'])
        result = await supervisor.describe(self.capture, 2)
        request = client.infer.call_args.args[0]
        self.assertEqual(request.profile_id, 'aos.bonsai.vision.v1')
        self.assertEqual(request.deployment_digest, digest(native.pins))
        self.assertEqual(result.capture_id, self.capture.capture_id)
        self.assertEqual(request.payload['response_format']['json_schema']['name'], 'aos_visual_scene')
        self.assertEqual(request.payload['messages'][1]['content'][1]['image_url']['url'],
                         'data:image/png;base64,' + self.capture.image_base64)
        BONSAI['validate_payload'](request.payload, supervisor.pins,
            Mock(value={'deployment_digest': request.deployment_digest}), 'vision')

    async def test_default_and_boolean_authority_denied_before_infer(self):
        client = Mock(infer=AsyncMock())
        supervisor = ScientistBonsaiSupervisor(self.manifest, client)
        with self.assertRaises(ScientistAdmissionError):
            await supervisor.plan('Recover', self.evidence)
        supervisor.verify_context = lambda *arguments: True
        with self.assertRaises(ScientistAdmissionError):
            await supervisor.plan('Recover', self.evidence)
        client.infer.assert_not_called()

    async def test_revoked_authority_after_receipt_never_publishes_plan(self):
        current = {'valid': True}
        def verify(*arguments):
            if not current['valid']:
                raise ScientistAdmissionError('Synthetic stale generation')
        supervisor, client = self.supervisor(verifier=verify, mutate=lambda receipt: current.update(valid=False))
        with self.assertRaises(ScientistAdmissionError):
            await supervisor.plan('Recover', self.evidence)
        self.assertIsNone(supervisor.last_response)
        self.assertEqual(client.infer.await_count, 1)

    async def test_changed_pins_in_hook_rejected_before_infer(self):
        supervisor, client = self.supervisor()
        supervisor.before_model_call = lambda: supervisor.pins.update(temperature=0.5)
        with self.assertRaises(ScientistAdmissionError):
            await supervisor.plan('Recover', self.evidence)
        client.infer.assert_not_called()

    async def test_receipt_correlation_truncation_foreign_evidence_and_duplicate_json_rejected(self):
        def content(receipt, value):
            receipt.response['choices'][0]['message']['content'] = value
        mutations = [lambda receipt: setattr(receipt, 'request_id', 'e' * 32),
                     lambda receipt: receipt.response['choices'][0].update(finish_reason='length'),
                     lambda receipt: content(receipt, json.dumps({**self.plan, 'evidence_refs': ['foreign']})),
                     lambda receipt: content(receipt, '{"needs_human":true,"needs_human":false}'),
                     lambda receipt: content(receipt, '{"value":1e999}'),
                     lambda receipt: receipt.response.update(model='foreign')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                supervisor, client = self.supervisor(mutate=mutate)
                with self.assertRaises((AOSFault, ValueError)):
                    await supervisor.plan('Recover', self.evidence)
                self.assertIsNone(supervisor.last_response)

    async def test_vision_stale_scene_bad_capture_and_wrong_state_denied(self):
        supervisor, client = self.supervisor(vision=True,
            mutate=lambda receipt: receipt.response['choices'][0]['message'].update(
                content=json.dumps({**self.scene, 'state_version': 1})))
        with self.assertRaises(AOSFault):
            await supervisor.describe(self.capture, 2)
        self.assertIsNone(supervisor.last_response)
        supervisor, client = self.supervisor(vision=True)
        for capture, version in [(self.capture.model_copy(update={'sha256': 'b' * 64}), 2),
                                 (self.capture, True), (self.capture, -1)]:
            with self.assertRaises(AOSFault):
                await supervisor.describe(capture, version)
        client.infer.assert_not_called()

    async def test_uncertain_transport_is_not_retried_or_sent_to_native_backend(self):
        supervisor, client = self.supervisor()
        client.infer.side_effect = ScientistUncertainTurn('Synthetic lost ACK')
        with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('No fallback')):
            with self.assertRaises(ScientistUncertainTurn):
                await supervisor.plan('Recover', self.evidence)
        self.assertEqual(client.infer.await_count, 1)
        self.assertIsNone(supervisor.last_response)

    async def test_broker_bonsai_registration_preserves_bonsai_backend_and_weights(self):
        supervisor, client = self.supervisor()
        identity = deepcopy(supervisor.identity)
        identity['pins'].update(model_files={'synthetic.gguf': 'a' * 64}, weights_file='synthetic.gguf',
                                source='synthetic-cpu-fixture', checkpoint_revision='synthetic',
                                tokenizer_revision='synthetic')
        registered = ModelRegistry.experiment_values(identity)
        self.assertEqual(registered['model_id'], 'bonsai-base-' + 'a' * 64)
        self.assertEqual(registered['backend'], 'prism_llama_cpp_cuda')
        self.assertEqual(registered['enabled'], 0)
