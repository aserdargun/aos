import hashlib
import math
import os
from pathlib import Path
import re
import stat

from broker_runtime import encoded, object_json


PROJECTION_SCHEMA_SHA256 = 'c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d'
PRODUCER_REVISION = '9a9394a895b96003ca842a6041cb28ac49a108f7'
PRODUCER_SOURCE_URLS = tuple(
    'https://raw.githubusercontent.com/PrismML-Eng/llama.cpp/' + PRODUCER_REVISION + '/' + path
    for path in ('tools/server/server-task.cpp', 'tools/server/server-common.cpp',
                 'tools/server/server-common.h', 'common/chat.cpp'))
SCHEMA_PATH = Path(__file__).resolve().parents[1] / 'schemas/scientist_bonsai_wire_projection.schema.json'


def _source_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 65536:
            raise ValueError('Bonsai projection source is not a bounded regular file')
        raw = os.read(descriptor, 65537)
        after = os.fstat(descriptor)
        linked = path.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (len(raw) != before.st_size or not stat.S_ISREG(linked.st_mode)
                or any(getattr(before, field) != getattr(after, field) for field in fields)
                or linked.st_dev != before.st_dev or linked.st_ino != before.st_ino):
            raise ValueError('Bonsai projection source changed while reading')
        return raw
    finally:
        os.close(descriptor)


def projection_pin():
    schema_sha256 = hashlib.sha256(encoded(object_json(_source_bytes(SCHEMA_PATH)))).hexdigest()
    if schema_sha256 != PROJECTION_SCHEMA_SHA256:
        raise ValueError('Bonsai projection schema differs from the supported source contract')
    return {'version': 1, 'schema_sha256': schema_sha256,
            'adapter_sha256': hashlib.sha256(_source_bytes(Path(__file__))).hexdigest()}


def verify_projection_pin(pins):
    if 'bonsai_output_projection' not in pins:
        return False
    pin = pins['bonsai_output_projection']
    if (pins.get('code_revision') != PRODUCER_REVISION
            or type(pin) is not dict or set(pin) != {'version', 'schema_sha256', 'adapter_sha256'}
            or type(pin['version']) is not int or pin['version'] != 1
            or any(type(pin[field]) is not str or re.fullmatch('[a-f0-9]{64}', pin[field]) is None
                   for field in ('schema_sha256', 'adapter_sha256'))
            or pin != projection_pin()):
        raise ValueError('Bonsai output projection requires its exact trusted manifest pin')
    return True


def _object(value, required, optional=()):
    if type(value) is not dict or not required <= set(value) or set(value) - required - set(optional):
        raise ValueError('Bonsai producer fields differ from the narrow output projection')
    return value


def _integer(value, maximum=2**53 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Bonsai producer metadata requires a bounded integer')


def _metadata(response, context_tokens, max_output_tokens):
    if 'object' in response and response['object'] != 'chat.completion':
        raise ValueError('Bonsai producer object is not a nonstreaming chat completion')
    if 'id' in response and (type(response['id']) is not str
            or re.fullmatch('chatcmpl-[0-9A-Za-z]{32}', response['id']) is None):
        raise ValueError('Bonsai producer completion ID differs from the pinned source')
    if 'created' in response:
        _integer(response['created'])
    if 'system_fingerprint' in response and (type(response['system_fingerprint']) is not str
            or not 1 <= len(response['system_fingerprint'].encode('utf-8')) <= 512):
        raise ValueError('Bonsai producer fingerprint is not bounded metadata')
    if 'timings' not in response:
        return
    counts = {'cache_n', 'prompt_n', 'predicted_n'}
    durations = {'prompt_ms', 'prompt_per_token_ms', 'prompt_per_second',
                 'predicted_ms', 'predicted_per_token_ms', 'predicted_per_second'}
    timings = _object(response['timings'], counts | durations, {'draft_n', 'draft_n_accepted'})
    for field in counts:
        _integer(timings[field], max_output_tokens if field == 'predicted_n' else context_tokens)
    for field in durations:
        value = timings[field]
        if (type(value) not in (int, float) or not 0 <= value <= 2**53 - 1
                or not math.isfinite(value)):
            raise ValueError('Bonsai producer timing must be finite nonnegative bounded metadata')
    if ('draft_n' in timings) != ('draft_n_accepted' in timings):
        raise ValueError('Bonsai producer draft counters must be paired')
    if 'draft_n' in timings:
        _integer(timings['draft_n'])
        _integer(timings['draft_n_accepted'], timings['draft_n'])
        if timings['draft_n'] == 0:
            raise ValueError('Bonsai producer emits draft counters only for positive draft count')


def project_bonsai_response(response, *, deployment_digest, context_tokens, max_output_tokens):
    if (type(deployment_digest) is not str or re.fullmatch('[a-f0-9]{64}', deployment_digest) is None
            or type(context_tokens) is not int or not 256 <= context_tokens <= 16384
            or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 512):
        raise ValueError('Bonsai projection needs an exact deployment and bounded profile limits')
    encoded(response)
    response = _object(response, {'choices', 'usage'},
                       {'model', 'id', 'object', 'created', 'system_fingerprint', 'timings'})
    _metadata(response, context_tokens, max_output_tokens)
    if 'model' in response and response['model'] != 'bonsai-' + deployment_digest:
        raise ValueError('Bonsai response model differs from its pinned deployment')
    choices = response['choices']
    if type(choices) is not list or len(choices) != 1:
        raise ValueError('Bonsai projection requires exactly one completion')
    choice = _object(choices[0], {'finish_reason', 'message'}, {'index'})
    if 'index' in choice:
        _integer(choice['index'], 0)
    if choice['finish_reason'] != 'stop':
        raise ValueError('Bonsai completion is not a complete answer')
    message = _object(choice['message'], {'content'}, {'role', 'reasoning_content'})
    if 'role' in message and message['role'] != 'assistant':
        raise ValueError('Bonsai completion role differs')
    if 'reasoning_content' in message and message['reasoning_content'] not in (None, ''):
        raise ValueError('Bonsai completion contains a forbidden reasoning channel')
    content = message['content']
    if type(content) is not str or len(content.encode('utf-8')) > 65536:
        raise ValueError('Bonsai completion content exceeds its byte bound')
    object_json(content)
    usage = _object(response['usage'], {'prompt_tokens', 'completion_tokens'},
                    {'total_tokens', 'prompt_tokens_details'})
    if (any(type(usage[field]) is not int or usage[field] < 0
            for field in ('prompt_tokens', 'completion_tokens'))
            or usage['completion_tokens'] > max_output_tokens
            or usage['prompt_tokens'] + usage['completion_tokens'] > context_tokens):
        raise ValueError('Bonsai usage exceeds the original profile or request budget')
    if 'total_tokens' in usage:
        _integer(usage['total_tokens'], context_tokens)
        if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
            raise ValueError('Bonsai producer total token count differs')
    if 'prompt_tokens_details' in usage:
        details = _object(usage['prompt_tokens_details'], {'cached_tokens'})
        _integer(details['cached_tokens'], usage['prompt_tokens'])
    projected_message = {'content': content}
    if 'role' in message:
        projected_message['role'] = message['role']
    projected = {'choices': [{'finish_reason': 'stop', 'message': projected_message}],
                 'usage': {field: usage[field] for field in ('prompt_tokens', 'completion_tokens')}}
    if 'model' in response:
        projected['model'] = response['model']
    return projected
