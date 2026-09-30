"""Bounded per-run Decider inference with an exact private adapter binding."""

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import struct
import sys
import time

from owned_episode_adapter_core import (
    ARTIFACT_BYTES,
    ARTIFACT_MAGIC,
    MAX_MANIFEST_BYTES,
    private_manifest_pins,
    read_manifest,
    read_private_artifact,
)
from worker import ModelSession, validate_request, verify_environment, verify_files


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = 'owned-adapter-runtime-v1'
BINDING_KEYS = {'protocol', 'authorization_sha256', 'adaptation_report_sha256', 'artifact_sha256',
                'input_sha256', 'deployment_manifest_sha256', 'base_deployment_id',
                'runtime_worker_sha256'}
HASH = re.compile(r'^[a-f0-9]{64}$')
MAX_REQUESTS = 6
MAX_LINE_BYTES = 65536
IDLE_SECONDS = 75


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def runtime_worker_pin():
    names = ('src/aos/owned_adapter_engine.py', 'services/decider/owned_adapter_worker.py',
             'services/decider/owned_episode_adapter_core.py', 'services/decider/worker.py',
             'src/aos/reusable_decider.py', 'src/aos/decision.py')
    return hashlib.sha256(canonical({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                     for name in names}).encode()).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('owned_adapter_duplicate_json_key')
        value[key] = item
    return value


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))


def validate_binding(binding, pins, manifest_sha256):
    if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
        raise ValueError('owned_adapter_binding_invalid')
    if (binding['protocol'] != PROTOCOL
            or any(type(binding[key]) is not str or HASH.fullmatch(binding[key]) is None
                   for key in ('authorization_sha256', 'adaptation_report_sha256', 'artifact_sha256',
                               'input_sha256', 'deployment_manifest_sha256', 'runtime_worker_sha256'))
            or binding['deployment_manifest_sha256'] != manifest_sha256
            or binding['base_deployment_id'] != 'decider-' + hashlib.sha256(
                canonical(pins).encode()).hexdigest()
            or binding['runtime_worker_sha256'] != runtime_worker_pin()):
        raise ValueError('owned_adapter_binding_invalid')
    return binding


def validate_artifact_payload(payload, binding):
    if (type(payload) is not bytes or len(payload) != ARTIFACT_BYTES
            or payload[:8] != ARTIFACT_MAGIC
            or payload[8:40] != bytes.fromhex(binding['authorization_sha256'])
            or payload[40:72] != bytes.fromhex(binding['input_sha256'])
            or payload[72:104] != bytes.fromhex(binding['deployment_manifest_sha256'])
            or struct.unpack_from('<III', payload, 104) != (4, 6144, 2048)
            or hashlib.sha256(payload).hexdigest() != binding['artifact_sha256']):
        raise ValueError('owned_adapter_artifact_binding_invalid')
    weights = struct.unpack_from('<32768f', payload, 116)
    if not all(math.isfinite(weight) for weight in weights):
        raise ValueError('owned_adapter_artifact_values_invalid')
    return weights


def validate_envelope(envelope, request_ids):
    if (not isinstance(envelope, dict) or set(envelope) != {'request_id', 'request'}
            or type(envelope['request_id']) is not str
            or re.fullmatch(r'[a-f0-9]{32}', envelope['request_id']) is None
            or envelope['request_id'] in request_ids):
        raise ValueError('owned_adapter_request_envelope_invalid')
    validate_request(envelope['request'])
    return envelope


def _read_line(descriptor, buffer, deadline):
    while b'\n' not in buffer:
        if len(buffer) >= MAX_LINE_BYTES:
            raise ValueError('owned_adapter_request_too_large')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise TimeoutError('owned_adapter_idle_timeout')
        chunk = os.read(descriptor, min(8192, MAX_LINE_BYTES + 1 - len(buffer)))
        if not chunk:
            if buffer:
                raise ValueError('owned_adapter_incomplete_request')
            return None, b''
        buffer += chunk
    line, buffer = buffer.split(b'\n', 1)
    if len(line) + 1 > MAX_LINE_BYTES:
        raise ValueError('owned_adapter_request_too_large')
    return line, buffer


def _emit(response):
    encoded = json.dumps(response, allow_nan=False, separators=(',', ':'))
    raw = encoded.encode() + b'\n'
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError('owned_adapter_response_too_large')
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _source_bytes(manifest, artifact, binding, pins):
    manifest_bytes = read_manifest(manifest)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ValueError('owned_adapter_manifest_too_large')
    if hashlib.sha256(manifest_bytes).hexdigest() != binding['deployment_manifest_sha256']:
        raise ValueError('owned_adapter_manifest_changed')
    current_pins, _expected = private_manifest_pins(manifest_bytes)
    if current_pins != pins:
        raise ValueError('owned_adapter_manifest_changed')
    payload = read_private_artifact(artifact)
    validate_artifact_payload(payload, binding)
    return manifest_bytes, payload


class AdapterInference:
    def __init__(self, session, manifest, artifact, binding, pins, weights, torch, hook):
        self.session = session
        self.manifest = manifest
        self.artifact = artifact
        self.binding = binding
        self.pins = pins
        self.weights = weights
        self.torch = torch
        self.hook = hook
        self.base_digest = hashlib.sha256(canonical(pins).encode()).hexdigest()
        self.binding_digest = hashlib.sha256(canonical(binding).encode()).hexdigest()
        self.model = session.model.m
        self.versions = [parameter._version for parameter in self.model.parameters()]
        self.calls = 0

    def infer(self, request):
        before_manifest, before_artifact = _source_bytes(
            self.manifest, self.artifact, self.binding, self.pins)
        previous_calls = self.calls
        response = self.session.infer(request)
        self.calls += 1
        if self.calls > MAX_REQUESTS or self.calls <= previous_calls:
            raise ValueError('owned_adapter_request_budget_exceeded')
        hook_calls = self.hook['state']['count']
        if (response.get('deployment_digest') != self.base_digest
                or hook_calls <= 0
                or self.versions != [parameter._version for parameter in self.model.parameters()]
                or any(parameter.grad is not None for parameter in self.model.parameters())):
            raise ValueError('owned_adapter_base_model_changed')
        after_manifest, after_artifact = _source_bytes(
            self.manifest, self.artifact, self.binding, self.pins)
        if after_manifest != before_manifest or after_artifact != before_artifact:
            raise ValueError('owned_adapter_sources_changed')
        metrics = dict(response['metrics']) | {
            'adapter_loaded': True,
            'adapter_hook_calls': hook_calls,
            'adapter_sha256': self.binding['artifact_sha256'],
            'base_parameters_unchanged': True,
        }
        self.hook['state']['count'] = 0
        return {'deployment_digest': self.binding_digest,
                'prediction': response['prediction'], 'metrics': metrics}


def create_inference(manifest, artifact, binding):
    manifest_bytes = read_manifest(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    pins, _expected = private_manifest_pins(manifest_bytes)
    validate_binding(binding, pins, manifest_sha)
    model_root = verify_environment(pins)
    payload = read_private_artifact(artifact)
    weights = validate_artifact_payload(payload, binding)

    with contextlib.redirect_stdout(sys.stderr):
        import torch

        session = ModelSession(model_root, pins)
        session.manifest = manifest
        session.prepare_cpu()
        if torch.cuda.is_initialized():
            raise ValueError('owned_adapter_cpu_prepare_initialized_cuda')
        if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 8 * 1024 ** 3:
            raise ValueError('owned_adapter_requires_8gib_free_cuda')
        model = session.model.m
        projection = model.lm.model.layers[-1].mlp.down_proj
        if (not isinstance(projection, torch.nn.Linear)
                or projection.in_features != 6144 or projection.out_features != 2048):
            raise ValueError('owned_adapter_architecture_changed')
        model.eval().requires_grad_(False).to('cuda')
        session.model.dev = 'cuda'
        session.device = 'cuda'
        torch.cuda.synchronize()
        down = torch.tensor(weights[:24576], dtype=torch.float32, device='cuda').reshape(4, 6144)
        up = torch.tensor(weights[24576:], dtype=torch.float32, device='cuda').reshape(2048, 4)
        down.requires_grad_(False)
        up.requires_grad_(False)
        hook_state = {'count': 0}

        def inject(_module, inputs, output):
            hook_state['count'] += 1
            correction = torch.nn.functional.linear(
                torch.nn.functional.linear(inputs[0].float(), down), up)
            return output + correction.to(output.dtype)

        handle = projection.register_forward_hook(inject)
        return AdapterInference(session, manifest, artifact, binding, pins, weights, torch,
                                {'handle': handle, 'state': hook_state})


def serve(inference, descriptor):
    buffer = b''
    request_ids = set()
    try:
        for _ in range(MAX_REQUESTS):
            line, buffer = _read_line(descriptor, buffer, time.monotonic() + IDLE_SECONDS)
            if line is None:
                return
            envelope = validate_envelope(parse_json(line), request_ids)
            request_ids.add(envelope['request_id'])
            response = inference.infer(envelope['request'])
            _emit(response | {'request_id': envelope['request_id']})
        if buffer or select.select([descriptor], [], [], 0)[0]:
            raise ValueError('owned_adapter_request_budget_exceeded')
    finally:
        inference.hook['handle'].remove()


def main():
    try:
        if len(sys.argv) != 5 or sys.argv[4] != '--serve' or len(sys.argv[3]) > 4096:
            raise ValueError('owned_adapter_usage_invalid')
        manifest, artifact = Path(sys.argv[1]), Path(sys.argv[2])
        binding = parse_json(sys.argv[3])
        manifest_bytes = read_manifest(manifest)
        pins, _expected = private_manifest_pins(manifest_bytes)
        validate_binding(binding, pins, hashlib.sha256(manifest_bytes).hexdigest())
        inference = create_inference(manifest, artifact, binding)
        serve(inference, sys.stdin.fileno())
    except Exception:
        sys.stderr.write('Pinned owned adapter inference failed closed; base deployment unchanged.\n')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
