import hashlib
import http.client
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker_runtime import TurnGate, clock, encoded, object_json, verify_tree
from bonsai_projection import project_bonsai_response, verify_projection_pin


def verify_manifest(pins):
    verify_tree(pins['model_path'], pins['model_files'])
    verify_tree(pins['runtime_path'], pins['runtime_files'])
    libraries = pins.get('native_libraries')
    if not isinstance(libraries, dict) or not libraries:
        raise ValueError('Pinned Bonsai native dependencies are required')
    for name, checksum in libraries.items():
        path = Path(name)
        if path.is_symlink() or not path.is_file():
            raise ValueError('Pinned Bonsai native dependency type differs')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                raise ValueError('Pinned Bonsai native dependency digest differs')
    server = Path(pins['server_path'])
    runtime = Path(pins['runtime_path']).resolve(strict=True)
    if server.is_symlink() or not server.resolve(strict=True).is_relative_to(runtime):
        raise ValueError('Bonsai server is outside its pinned runtime')
    for name in ('weights_file', 'projector_file'):
        relative = Path(pins[name])
        if relative.is_absolute() or '..' in relative.parts or relative.as_posix() not in pins['model_files']:
            raise ValueError('Bonsai weight/projector is outside its pinned artifact set')
    if (type(pins.get('context_tokens')) is not int or not 256 <= pins['context_tokens'] <= 16384
            or type(pins.get('max_output_tokens')) is not int or not 1 <= pins['max_output_tokens'] <= 512
            or type(pins.get('gpu_layers')) is not int or not 0 <= pins['gpu_layers'] <= 999
            or type(pins.get('parallel')) is not int or pins['parallel'] != 1):
        raise ValueError('Bonsai runtime exceeds its bounded broker profile')


def validate_payload(request, pins, gate, role):
    fields = {'model', 'temperature', 'max_tokens', 'stream', 'chat_template_kwargs', 'messages', 'response_format'}
    if (set(request) != fields or request['model'] != 'bonsai-' + gate.value['deployment_digest']
            or request['stream'] is not False or request['chat_template_kwargs'] != {'enable_thinking': False}
            or type(request['max_tokens']) is not int or not 1 <= request['max_tokens'] <= pins['max_output_tokens']
            or type(request['temperature']) not in (int, float) or request['temperature'] != pins['temperature']):
        raise ValueError('Bonsai request differs from its fixed deployment policy')
    messages = request['messages']
    if (not isinstance(messages, list) or len(messages) != 2
            or not all(isinstance(message, dict) and set(message) == {'role', 'content'} for message in messages)
            or [message['role'] for message in messages] != ['system', 'user']):
        raise ValueError('Bonsai input message roles differ')
    if role == 'recovery' and not all(type(message['content']) is str for message in messages):
        raise ValueError('Bonsai recovery input must be text')
    if role == 'vision':
        content = messages[1]['content']
        if (type(messages[0]['content']) is not str or not isinstance(content, list) or len(content) != 2
                or not isinstance(content[0], dict) or set(content[0]) != {'type', 'text'}
                or content[0]['type'] != 'text' or type(content[0]['text']) is not str
                or not isinstance(content[1], dict) or set(content[1]) != {'type', 'image_url'}
                or content[1]['type'] != 'image_url' or not isinstance(content[1]['image_url'], dict)
                or set(content[1]['image_url']) != {'url'}):
            raise ValueError('Bonsai vision input must contain one text and image')
        url = content[1]['image_url']['url']
        if type(url) is not str or not url.startswith('data:image/png;base64,') or len(url) > 80100:
            raise ValueError('Bonsai image is not a bounded inline PNG')
    response_format = request['response_format']
    if (not isinstance(response_format, dict) or set(response_format) != {'type', 'json_schema'}
            or response_format['type'] != 'json_schema'):
        raise ValueError('Bonsai output format differs')
    schema = response_format['json_schema']
    if (not isinstance(schema, dict) or set(schema) != {'name', 'strict', 'schema'}
            or schema['strict'] is not True or not isinstance(schema['schema'], dict)
            or schema['name'] != ('aos_recovery_plan' if role == 'recovery' else 'aos_visual_scene')):
        raise ValueError('Bonsai output schema identity differs')


def request_json(port, token, route, body, deadline):
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError('Bonsai broker HTTP deadline expired')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=remaining)
    try:
        connection.request('GET' if body is None else 'POST', route,
            body=None if body is None else encoded(body, 128 * 1024),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        channel = connection.sock
        if channel is None or clock() >= deadline:
            raise TimeoutError('Bonsai HTTP connection missed its deadline')
        channel.settimeout(deadline - clock())
        response = connection.getresponse()
        if response.status == 503:
            raise ConnectionError('Bonsai model is still loading')
        if response.status != 200:
            raise RuntimeError('Pinned Bonsai server rejected request')
        raw = bytearray()
        while response.fp is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('Bonsai HTTP read exceeded its original deadline')
            channel.settimeout(remaining)
            chunk = response.read1(min(8192, 65537 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 65536:
                raise ValueError('Bonsai response exceeds broker output bound')
        if clock() >= deadline:
            raise TimeoutError('Bonsai completion exceeded its original deadline')
        return object_json(bytes(raw))
    finally:
        connection.close()


def cleanup(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    if child.poll() is None:
        raise RuntimeError('Bonsai child cleanup is not proven')


def main():
    if len(sys.argv) != 4 or sys.argv[3] not in {'recovery', 'vision'}:
        raise ValueError('Usage: broker_worker.py MANIFEST READY_FILE recovery|vision')
    manifest, ready, role = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    gate = TurnGate(ready, 'aos.bonsai.' + role + '.v1')
    pins = gate.manifest(manifest)
    projection_enabled = verify_projection_pin(pins)
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if not raw or len(raw) > 128 * 1024:
        raise ValueError('Bonsai broker input exceeds its bound')
    payload = object_json(raw)
    validate_payload(payload, pins, gate, role)
    verify_manifest(pins)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    alias = 'bonsai-' + gate.value['deployment_digest']
    with tempfile.TemporaryDirectory(prefix='bonsai-turn-', dir=os.environ['TMPDIR']) as directory:
        key_path = Path(directory) / 'api-key'
        token = secrets.token_urlsafe(32)
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        command = [pins['server_path'], '--model', str(Path(pins['model_path']) / pins['weights_file']),
            '--mmproj', str(Path(pins['model_path']) / pins['projector_file']), '--host', '127.0.0.1',
            '--port', str(port), '--api-key-file', str(key_path), '--alias', alias,
            '--ctx-size', str(pins['context_tokens']), '--parallel', '1', '--n-gpu-layers', str(pins['gpu_layers']),
            '--flash-attn', 'on', '--jinja', '--reasoning-budget', '0', '--no-webui',
            '--no-warmup', '--no-context-shift', '--log-disable']
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        try:
            while clock() < gate.activation_deadline:
                if child.poll() is not None:
                    raise RuntimeError('Bonsai server exited before readiness')
                try:
                    models = request_json(port, token, '/v1/models', None, min(gate.activation_deadline, clock() + 1))
                    if models.get('data') != [{'id': alias}] and (
                            not isinstance(models.get('data'), list)
                            or [item.get('id') for item in models['data']] != [alias]):
                        raise ValueError('Bonsai ready model alias differs')
                    break
                except (OSError, ConnectionError, TimeoutError):
                    time.sleep(0.2)
            else:
                raise TimeoutError('Bonsai activation expired')
            gate.check_manifest(manifest)
            deadline = gate.admit_inference()
            gate.check_manifest(manifest)
            response = request_json(port, token, '/v1/chat/completions', payload, deadline)
            choices = response.get('choices')
            if (not isinstance(choices, list) or len(choices) != 1
                    or not isinstance(choices[0], dict) or choices[0].get('finish_reason') != 'stop'):
                raise ValueError('Bonsai completion is incomplete')
            if projection_enabled:
                verify_projection_pin(pins)
                response = project_bonsai_response(response,
                    deployment_digest=gate.value['deployment_digest'],
                    context_tokens=pins['context_tokens'],
                    max_output_tokens=min(payload['max_tokens'], pins['max_output_tokens']))
            result = encoded(response)
        finally:
            cleanup(child)
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
