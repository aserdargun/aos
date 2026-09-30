import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time


MODEL_FILES = {
    'README.md': '59e2d2cf0203fb309d31ca6f51c4c6ab758fa32cecd82cfcd3ce20425edea7b6',
    'encoder/config.json': '5268d24ad3b77c8151de5dcb0762ba4391619aad9ab0bda33e36fb083cfeae6d',
    'model.safetensors': '4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e',
    'rl_agent_config.json': 'ebf0cd524d92342a6be5e48e9fca3d7c2babfb5a56ccd79d2171ef5d8c7f7be8',
    'tokenizer/tokenizer.json': '6c8aaa9a542084f2457eab775d4eeb51f92a70c0fd9de28d5edb0ddec3c08d30',
    'tokenizer/tokenizer_config.json': '08d4cf3ac4dca381759441b85b91a6d40e688471dcd33d15d6649eb0a9a854d1',
}
DEPENDENCIES = ('torch', 'transformers', 'safetensors', 'huggingface-hub', 'numpy', 'laya')
REVISION = 'f9ab0b228f0fc0f14d873dbc99038f135c2da1b2'
WHEEL_SHA256 = 'e3b3aa7bb65c1a154db1b2718e3a7f5ecb1a506800933b519d640f438c5d8ad5'


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_files(root, expected, *, ignored=()):
    root = Path(root).absolute()
    if root.is_symlink() or any(part.is_symlink() for part in root.parents):
        raise ValueError('unsafe_model_root')
    root = root.resolve(strict=True)
    actual = set()
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('model_symlink_rejected')
        if path.is_file():
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError('model_file_not_private_regular')
            actual.add(str(relative))
    if actual != set(expected):
        raise ValueError('model_file_set_differs')
    for relative, checksum in expected.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or sha256(root / path) != checksum:
            raise ValueError('model_file_hash_differs')
    return root


def verify_manifest(manifest):
    pins = json.loads(Path(manifest).read_text())
    if (set(pins) != {'source', 'checkpoint_revision', 'tokenizer_revision', 'sdk_version',
                      'wheel_sha256', 'model_path', 'model_files', 'sdk_path', 'sdk_files', 'dependencies'}
            or pins['source'] != 'convaiinnovations/laya-typed-decisions'
            or pins['sdk_version'] != '0.3.6'
            or pins['checkpoint_revision'] != REVISION
            or pins['tokenizer_revision'] != pins['checkpoint_revision']
            or pins['wheel_sha256'] != WHEEL_SHA256
            or pins['model_files'] != MODEL_FILES
            or set(pins['dependencies']) != set(DEPENDENCIES)):
        raise ValueError('invalid_laya_candidate_manifest')
    model = verify_files(pins['model_path'], pins['model_files'], ignored={'.cache', 'manifest.json'})
    sdk = verify_files(pins['sdk_path'], pins['sdk_files'], ignored={'__pycache__'})
    sys.path.insert(0, str(sdk))
    if {name: importlib.metadata.version(name) for name in DEPENDENCIES} != pins['dependencies']:
        raise ValueError('laya_candidate_dependencies_differ')
    return pins, model


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != {'state', 'question', 'options'}
            or not isinstance(request['state'], str) or not isinstance(request['question'], str)
            or len(request['state']) > 4096 or len(request['question']) > 256
            or not isinstance(request['options'], list) or not 2 <= len(request['options']) <= 10):
        raise ValueError('invalid_laya_request')
    options = request['options']
    if any(not isinstance(option, dict) or set(option) != {'id', 'label'}
           or not isinstance(option['id'], str) or not isinstance(option['label'], str)
           or not option['id'] or not option['label']
           or len(option['id']) > 80 or len(option['label']) > 240 for option in options):
        raise ValueError('invalid_laya_option')
    if len({option['id'] for option in options}) != len(options) or len({option['label'] for option in options}) != len(options):
        raise ValueError('duplicate_laya_option')
    return options


def infer(agent, request, reused):
    import torch

    options = validate_request(request)
    state_tokens = len(agent.tok.encode(request['state'], add_special_tokens=False))
    question_tokens = len(agent.tok.encode(request['question'], add_special_tokens=False))
    option_label_tokens = sum(len(agent.tok.encode(option['label'], add_special_tokens=False)) for option in options)
    if state_tokens > 512 or question_tokens > 64 or option_label_tokens > 150:
        raise ValueError('laya_candidate_token_budget')
    if agent.device.type != 'cuda':
        raise ValueError('laya_candidate_cuda_required')
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    criteria = {option['id']: option['label'] for option in options}
    with contextlib.redirect_stdout(sys.stderr):
        answer = agent.predict(request['state'], {'next_action': {
            'type': 'choice', 'instructions': request['question'], 'criteria': criteria}})['answers']['next_action']
    torch.cuda.synchronize()
    if agent.device.type != 'cuda':
        raise ValueError('laya_candidate_cpu_fallback')
    probabilities = answer['probabilities']
    if set(probabilities) != set(criteria) or answer['choice'] not in criteria:
        raise ValueError('invalid_laya_candidate_choice')
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in probabilities.values()):
        raise ValueError('invalid_laya_candidate_probability')
    total = sum(probabilities.values())
    if not 0.999 <= total <= 1.001:
        raise ValueError('invalid_laya_candidate_probability_total')
    normalized = {key: value / total for key, value in probabilities.items()}
    return {'prediction': {'selected_option': answer['choice'], 'probabilities': normalized},
            'metrics': {'inference_ms': (time.perf_counter() - started) * 1000,
                        'state_tokens': state_tokens, 'question_tokens': question_tokens,
                        'option_label_tokens': option_label_tokens,
                        'peak_vram_bytes': torch.cuda.max_memory_allocated(), 'reused': reused, 'device': 'cuda'}}


def main():
    if len(sys.argv) != 2:
        raise ValueError('laya_candidate_manifest_required')
    pins, model = verify_manifest(sys.argv[1])
    import laya

    with contextlib.redirect_stdout(sys.stderr):
        agent = laya.load(str(model), device='cuda')
    if agent.device.type != 'cuda':
        raise ValueError('laya_candidate_cuda_required')
    deployment_digest = hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    calls = 0
    while line := sys.stdin.buffer.readline(65537):
        if len(line) > 65536 or not line.endswith(b'\n'):
            raise ValueError('laya_candidate_request_exceeds_bound')
        envelope = json.loads(line)
        if (not isinstance(envelope, dict) or set(envelope) != {'request_id', 'request'}
                or not isinstance(envelope['request_id'], str)
                or re.fullmatch(r'[a-f0-9]{32}', envelope['request_id']) is None):
            raise ValueError('invalid_laya_candidate_envelope')
        response = infer(agent, envelope['request'], calls > 0)
        calls += 1
        response.update(request_id=envelope['request_id'], deployment_digest=deployment_digest)
        payload = json.dumps(response, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(payload) > 65535:
            raise ValueError('laya_candidate_response_exceeds_bound')
        sys.stdout.buffer.write(payload + b'\n')
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
