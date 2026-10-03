import json

from .contracts import canonical, digest
from .scientist_decider_receipt import validate_decider_receipt
from .scientist_protocol import (
    ScientistTurnReceipt, ScientistTurnRequest, _reject_constant, _unique_object,
)
from .supervisor import RecoveryPlan
from .vision import Capture, VisionScene


def _object(value, required, optional=()):
    if (type(value) is not dict or not set(required) <= set(value)
            or not set(value) <= set(required) | set(optional)):
        raise ValueError('Bonsai receipt fields differ from the closed profile')
    return value


def _json(value):
    if type(value) is not str or not value or len(value.encode('utf-8')) > 65536:
        raise ValueError('Bonsai content exceeds the bounded profile')
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _usage(value, max_output_tokens, context_tokens):
    value = _object(value, {'prompt_tokens', 'completion_tokens'})
    if (any(type(count) is not int or count < 0 for count in value.values())
            or value['completion_tokens'] > max_output_tokens
            or sum(value.values()) > context_tokens):
        raise ValueError('Bonsai usage exceeds its requested output or trusted context')
    return value


def validate_bonsai_receipt(request: ScientistTurnRequest, receipt: ScientistTurnReceipt,
                            *, context_tokens=16384) -> None:
    if type(context_tokens) is not int or not 256 <= context_tokens <= 16384:
        raise ValueError('Bonsai trusted context limit is invalid')
    if type(request.version) is not int or type(receipt.version) is not int:
        raise ValueError('Bonsai protocol versions require integer tokens')
    request = ScientistTurnRequest.model_validate(request.model_dump(), strict=True)
    receipt = ScientistTurnReceipt.model_validate(receipt.model_dump(), strict=True)
    if (type(request.version) is not int or type(receipt.version) is not int
            or request.profile_id not in {'aos.bonsai.recovery.v1', 'aos.bonsai.vision.v1'}
            or receipt.profile_id != request.profile_id or receipt.request_id != request.request_id
            or receipt.deployment_digest != request.deployment_digest):
        raise ValueError('Bonsai receipt belongs to another profile or request')
    payload = _object(request.payload, {'model', 'temperature', 'max_tokens', 'stream',
        'chat_template_kwargs', 'messages', 'response_format'})
    alias = 'bonsai-' + request.deployment_digest
    if (payload['model'] != alias or payload['stream'] is not False
            or type(payload['temperature']) not in (int, float) or payload['temperature'] != 0
            or type(payload['chat_template_kwargs']) is not dict
            or set(payload['chat_template_kwargs']) != {'enable_thinking'}
            or payload['chat_template_kwargs']['enable_thinking'] is not False
            or type(payload['max_tokens']) is not int or not 1 <= payload['max_tokens'] <= 512):
        raise ValueError('Bonsai request is outside the fixed broker profile')
    messages = payload['messages']
    if (type(messages) is not list or len(messages) != 2
            or any(type(message) is not dict or set(message) != {'role', 'content'} for message in messages)
            or [message['role'] for message in messages] != ['system', 'user']
            or type(messages[0]['content']) is not str):
        raise ValueError('Bonsai request roles differ from the original profile')
    vision = request.profile_id == 'aos.bonsai.vision.v1'
    content_model = VisionScene if vision else RecoveryPlan
    output_format = _object(payload['response_format'], {'type', 'json_schema'})
    output_schema = _object(output_format['json_schema'], {'name', 'strict', 'schema'})
    if (output_format['type'] != 'json_schema' or output_schema['strict'] is not True
            or output_schema['name'] != ('aos_visual_scene' if vision else 'aos_recovery_plan')
            or digest(output_schema['schema']) != digest(content_model.model_json_schema())):
        raise ValueError('Bonsai original content schema differs from its profile')
    response = _object(receipt.response, {'choices', 'usage'}, {'model'})
    if 'model' in response and response['model'] != alias:
        raise ValueError('Bonsai response model differs from the original deployment')
    choices = response['choices']
    if type(choices) is not list or len(choices) != 1:
        raise ValueError('Bonsai response must contain exactly one completion')
    choice = _object(choices[0], {'finish_reason', 'message'})
    message = _object(choice['message'], {'content'}, {'role'})
    if choice['finish_reason'] != 'stop' or 'role' in message and message['role'] != 'assistant':
        raise ValueError('Bonsai response is incomplete or has another role')
    usage = _usage(response['usage'], payload['max_tokens'], context_tokens)
    _usage(receipt.usage, payload['max_tokens'], context_tokens)
    if canonical(usage) != canonical(receipt.usage):
        raise ValueError('Bonsai receipt usage differs from the projected response')
    decoded = _json(message['content'])
    parsed = content_model.model_validate(decoded, strict=True)
    if not vision:
        original = _object(_json(messages[1]['content']), {'problem', 'evidence'})
        evidence = original['evidence']
        if (type(evidence) is not list or not 1 <= len(evidence) <= 4
                or any(type(item) is not dict or type(item.get('id')) is not str
                       or not item['id'].strip() for item in evidence)
                or len({item['id'] for item in evidence}) != len(evidence)):
            raise ValueError('Bonsai original evidence identity is invalid')
        parsed.validate_evidence(evidence)
        return
    content = messages[1]['content']
    if type(content) is not list or len(content) != 2:
        raise ValueError('Bonsai vision request needs one text and one image')
    text = _object(content[0], {'type', 'text'})
    image = _object(content[1], {'type', 'image_url'})
    image_url = _object(image['image_url'], {'url'})['url']
    if (text['type'] != 'text' or image['type'] != 'image_url'
            or type(image_url) is not str or not image_url.startswith('data:image/png;base64,')):
        raise ValueError('Bonsai vision image binding is invalid')
    original = _object(_json(text['text']), {'problem', 'capture_id', 'width', 'height', 'sha256', 'state_version'})
    if (type(original['state_version']) is not int or original['state_version'] < 0
            or any(type(original[field]) is not int for field in ('width', 'height'))
            or any(type(decoded[field]) is not int for field in ('width', 'height', 'state_version'))):
        raise ValueError('Bonsai vision dimensions and state require integer tokens')
    capture = Capture.model_validate({key: original[key] for key in ('capture_id', 'width', 'height', 'sha256')}
                                    | {'image_base64': image_url.removeprefix('data:image/png;base64,')}, strict=True)
    capture.image_bytes()
    parsed.validate_capture(capture, original['state_version'])


def validate_profile_receipt(request: ScientistTurnRequest, receipt: ScientistTurnReceipt) -> None:
    if request.profile_id == 'aos.decider.turn.v1':
        validate_decider_receipt(request, receipt)
    else:
        validate_bonsai_receipt(request, receipt)
