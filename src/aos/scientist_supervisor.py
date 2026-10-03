from copy import deepcopy
import json
import time
from uuid import uuid4

from .contracts import AOSFault, ErrorCode, canonical, digest
from .scientist_protocol import ScientistTurnRequest, _reject_constant, _unique_object
from .scientist_transport import ScientistAdmissionError
from .supervisor import BonsaiSupervisor
from .vision import BonsaiVisionSupervisor, Capture, VisionScene


def _deny_context(problem, evidence, request_context):
    raise ScientistAdmissionError('Current Supervisor task/control authority is not configured')


class ScientistBonsaiSupervisor(BonsaiSupervisor):
    profile_id = 'aos.bonsai.recovery.v1'

    def __init__(self, manifest, client, *, verify_context=_deny_context):
        BonsaiSupervisor.__init__(self, manifest)
        self.client = client
        self.verify_context = verify_context
        self._freeze_identity()

    def _freeze_identity(self):
        self._deployment_digest = digest(self.pins)
        self.identity = {'deployment_id': 'bonsai-' + self._deployment_digest,
                         'kind': 'scientist_bonsai_broker', 'real_model': True,
                         'pins': deepcopy(self.pins)}

    def _verify_context(self, problem, evidence, request_context):
        if (digest(self.pins) != self._deployment_digest
                or self.identity['deployment_id'] != 'bonsai-' + self._deployment_digest):
            raise ScientistAdmissionError('Pinned Supervisor deployment changed')
        if self.verify_context(problem, deepcopy(evidence), deepcopy(request_context)) is not None:
            raise ScientistAdmissionError('Supervisor authority verifier must complete or raise')

    async def plan(self, problem, evidence, *, request_context=None):
        self.last_metrics, self.last_request, self.last_response = {}, None, None
        started = time.perf_counter()
        problem, evidence, request_context = deepcopy((problem, evidence, request_context))
        if type(problem) is not str or not problem.strip() or type(evidence) is not list:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid Supervisor problem or evidence')
        self._verify_context(problem, evidence, request_context)
        payload = (self.request_body(problem, evidence) if request_context is None else
                   self.request_body(problem, evidence, request_context=request_context))
        request = ScientistTurnRequest(request_id=uuid4().hex, profile_id=self.profile_id,
            deployment_digest=self._deployment_digest, payload=deepcopy(payload))
        before_model_call = getattr(self, 'before_model_call', None)
        if before_model_call is not None:
            before_model_call()
        self._verify_context(problem, evidence, request_context)
        self.last_request = deepcopy(payload)
        receipt = await self.client.infer(request)
        self._verify_context(problem, evidence, request_context)
        try:
            if (receipt.request_id != request.request_id or receipt.profile_id != request.profile_id
                    or receipt.deployment_digest != request.deployment_digest):
                raise ValueError('Supervisor receipt correlation differs')
            response = receipt.response
            if 'model' in response and response['model'] != self.identity['deployment_id']:
                raise ValueError('Supervisor response model differs')
            choices = response['choices']
            if (type(choices) is not list or len(choices) != 1
                    or choices[0]['finish_reason'] != 'stop'
                    or choices[0]['message'].get('role', 'assistant') != 'assistant'):
                raise ValueError('Supervisor completion is incomplete')
            content = choices[0]['message']['content']
            if type(content) is not str or len(content.encode('utf-8')) > 65536:
                raise ValueError('Supervisor content exceeds its bound')
            decoded = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            parsed = self.parse_response(canonical(decoded), evidence)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Invalid broker-backed Supervisor response') from error
        self.last_response = parsed.model_dump(mode='json')
        self.last_metrics = {'latency_ms': (time.perf_counter() - started) * 1000,
                             'broker_usage': deepcopy(receipt.usage)}
        return parsed


class ScientistBonsaiVisionSupervisor(ScientistBonsaiSupervisor):
    profile_id = 'aos.bonsai.vision.v1'
    request_body = BonsaiVisionSupervisor.request_body
    parse_response = BonsaiVisionSupervisor.parse_response

    def __init__(self, manifest, client, *, verify_context=_deny_context):
        super().__init__(manifest, client, verify_context=verify_context)
        self.pins['vision_schema_sha256'] = digest(VisionScene.model_json_schema())
        self.pins['vision_protocol'] = 'aos-bonsai-vision-v1'
        self._freeze_identity()

    def _verify_context(self, problem, evidence, request_context):
        if len(evidence) != 1 or set(evidence[0]) != {'capture', 'state_version'}:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Vision requires one bounded capture evidence')
        capture = Capture.model_validate(evidence[0]['capture'], strict=True)
        capture.image_bytes()
        if type(evidence[0]['state_version']) is not int or evidence[0]['state_version'] < 0:
            raise AOSFault(ErrorCode.INVALID_OUTPUT, 'Vision state version is invalid')
        super()._verify_context(problem, evidence, request_context)

    async def describe(self, capture, state_version):
        capture = Capture.model_validate(capture.model_dump(), strict=True)
        return await self.plan('Describe the visible controls in this synthetic screenshot.',
                               [{'capture': capture.model_dump(), 'state_version': state_version}])
