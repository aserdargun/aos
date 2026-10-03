import math

from .contracts import canonical
from .scientist_protocol import ScientistTurnReceipt, ScientistTurnRequest


METRIC_FIELDS = frozenset({'latency_ms', 'load_ms', 'inference_ms', 'reused',
                          'prepared_cpu', 'input_tokens', 'peak_vram_bytes',
                          'broker_activation_load_ms'})


def _object(value, fields):
    if type(value) is not dict or set(value) != fields:
        raise ValueError('Decider receipt object fields differ from the bounded contract')
    return value


def _number(value, minimum, maximum):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValueError('Decider receipt requires a bounded finite number')


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Decider receipt requires a bounded integer')


def _metrics(value):
    value = _object(value, METRIC_FIELDS)
    for field in ('latency_ms', 'load_ms', 'inference_ms'):
        _number(value[field], 0, 720000)
    _number(value['broker_activation_load_ms'], 0, 600000)
    _integer(value['input_tokens'], 1, 1536)
    _integer(value['peak_vram_bytes'], 0, 2**53 - 1)
    if value['reused'] is not False or value['prepared_cpu'] is not False:
        raise ValueError('Decider broker receipt requires a fresh GPU model session')


def validate_decider_receipt(request: ScientistTurnRequest, receipt: ScientistTurnReceipt) -> None:
    if type(request.version) is not int or type(receipt.version) is not int:
        raise ValueError('Decider protocol versions require integer tokens')
    request = ScientistTurnRequest.model_validate(request.model_dump(), strict=True)
    receipt = ScientistTurnReceipt.model_validate(receipt.model_dump(), strict=True)
    if (type(request.version) is not int or type(receipt.version) is not int
            or request.profile_id != 'aos.decider.turn.v1' or receipt.profile_id != request.profile_id
            or receipt.request_id != request.request_id
            or receipt.deployment_digest != request.deployment_digest):
        raise ValueError('Decider receipt belongs to another profile or request')
    payload = _object(request.payload, {'request'})
    decision = _object(payload['request'], {'state', 'question', 'options'})
    for field, limit in (('state', 16384), ('question', 1024)):
        value = decision[field]
        if type(value) is not str or not value.strip() or len(value.encode('utf-8')) > limit:
            raise ValueError('Decider request text exceeds its bounded contract')
    options = decision['options']
    if type(options) is not list or not 2 <= len(options) <= 10:
        raise ValueError('Decider receipt requires two to ten original options')
    identifiers, labels = [], []
    for option in options:
        option = _object(option, {'id', 'label'})
        if any(type(option[field]) is not str or not option[field].strip() for field in ('id', 'label')):
            raise ValueError('Decider request options require nonempty IDs and labels')
        identifiers.append(option['id'])
        labels.append(option['label'])
    if len(set(identifiers)) != len(identifiers) or len(set(labels)) != len(labels):
        raise ValueError('Decider request options must be unique')
    response = _object(receipt.response, {'deployment_digest', 'prediction', 'metrics'})
    if response['deployment_digest'] != request.deployment_digest:
        raise ValueError('Decider response deployment differs from the immutable request')
    prediction = _object(response['prediction'], {'selected_option', 'probabilities'})
    probabilities = _object(prediction['probabilities'], set(identifiers))
    if type(prediction['selected_option']) is not str or prediction['selected_option'] not in identifiers:
        raise ValueError('Decider prediction selects an unknown original option')
    for probability in probabilities.values():
        _number(probability, 0, 1)
    if abs(sum(probabilities.values()) - 1) > 1e-6:
        raise ValueError('Decider probabilities do not form a distribution')
    _metrics(response['metrics'])
    _metrics(receipt.usage)
    if canonical(response['metrics']) != canonical(receipt.usage):
        raise ValueError('Decider receipt usage differs from its measured response metrics')
