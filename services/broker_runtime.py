import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate broker JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite broker JSON value')


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite broker JSON number')
    return number


def object_json(raw):
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant, parse_float=_finite)
    if not isinstance(value, dict):
        raise ValueError('Broker input must be one JSON object')
    return value


def encoded(value, bound=65536):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    if not raw or len(raw) > bound:
        raise ValueError('Broker JSON exceeds its bound')
    return raw


def clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def verify_tree(root, expected):
    root = Path(root).resolve(strict=True)
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*')
              if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    if not isinstance(expected, dict) or not expected or actual != set(expected):
        raise ValueError('Pinned broker artifact file set differs')
    for name, checksum in expected.items():
        relative = Path(name)
        target = (root / relative).resolve(strict=True)
        if (relative.is_absolute() or '..' in relative.parts or not target.is_relative_to(root)
                or re.fullmatch(r'[a-f0-9]{64}', checksum) is None):
            raise ValueError('Pinned broker artifact escapes its root or digest')
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                raise ValueError('Pinned broker artifact digest differs')


def private_read(path, bound, *, private=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode)
                or (private and (before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1))
                or not 1 <= before.st_size <= bound):
            raise ValueError('Broker gate file is not private regular input')
        raw = os.read(descriptor, bound + 1)
        after = os.fstat(descriptor)
        linked = path.lstat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_nlink')
        if (len(raw) != before.st_size or any(getattr(before, field) != getattr(after, field) for field in fields)
                or linked.st_dev != before.st_dev or linked.st_ino != before.st_ino
                or not stat.S_ISREG(linked.st_mode)):
            raise ValueError('Broker gate file changed during read')
        return raw
    finally:
        os.close(descriptor)


class TurnGate:
    def __init__(self, ready_path, profile_id):
        environment = os.environ
        for name, pattern in [('SWAPP_GPU_REQUEST_ID', r'[a-f0-9]{32}'),
                              ('SWAPP_GPU_DEPLOYMENT_DIGEST', r'[a-f0-9]{64}'),
                              ('SWAPP_GPU_TURN_NONCE', r'[a-f0-9]{64}')]:
            if re.fullmatch(pattern, environment.get(name, '')) is None:
                raise ValueError('Broker turn identity is missing or malformed')
        if (environment.get('SWAPP_GPU_PROFILE_ID') != profile_id
                or environment.get('HF_HUB_OFFLINE') != '1'
                or environment.get('TRANSFORMERS_OFFLINE') != '1'):
            raise ValueError('Broker profile or offline model environment differs')
        self.activation_seconds = self._budget('SWAPP_GPU_ACTIVATION_SECONDS')
        self.inference_seconds = self._budget('SWAPP_GPU_INFERENCE_SECONDS')
        self.activation_deadline = clock() + self.activation_seconds
        self.ready_path = Path(ready_path)
        self.go_path = Path(environment['SWAPP_GPU_GO_FILE'])
        if (not self.ready_path.is_absolute() or not self.go_path.is_absolute()
                or str(self.ready_path) != environment.get('SWAPP_GPU_READY_FILE')
                or self.ready_path.parent != self.go_path.parent or self.ready_path == self.go_path
                or self.ready_path.parent.resolve(strict=True) != self.ready_path.parent):
            raise ValueError('Broker gate paths differ from their fixed work directory')
        parent = self.ready_path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError('Broker gate work directory is not private')
        group = Path('/proc/self/cgroup').read_text()
        if not any(re.fullmatch(r'0::/[^\n]*/swapp-aos-gpu-turn-[a-f0-9]{32}\.service', line)
                   for line in group.splitlines()):
            raise ValueError('Model worker is not inside a broker turn cgroup')
        host_namespace = int(environment['SWAPP_GPU_HOST_NETNS_INODE'])
        if host_namespace <= 0 or Path('/proc/self/ns/net').stat().st_ino == host_namespace:
            raise ValueError('Model worker must use the broker-isolated network namespace')
        self.value = {'request_id': environment['SWAPP_GPU_REQUEST_ID'], 'profile_id': profile_id,
                      'deployment_digest': environment['SWAPP_GPU_DEPLOYMENT_DIGEST'],
                      'nonce': environment['SWAPP_GPU_TURN_NONCE']}
        self.manifest_digest = None

    @staticmethod
    def _budget(name):
        value = os.environ.get(name, '')
        if re.fullmatch(r'[1-9][0-9]{0,2}', value) is None or not 1 <= int(value) <= 720:
            raise ValueError('Broker worker deadline is not bounded')
        return int(value)

    def manifest(self, path):
        path = Path(path)
        raw = private_read(path, 256 * 1024, private=False)
        pins = object_json(raw)
        self.manifest_digest = hashlib.sha256(raw).hexdigest()
        return pins

    def check_manifest(self, path):
        if hashlib.sha256(private_read(Path(path), 256 * 1024, private=False)).hexdigest() != self.manifest_digest:
            raise ValueError('Pinned broker manifest changed during activation')

    def admit_inference(self):
        if clock() >= self.activation_deadline:
            raise TimeoutError('Broker activation expired before readiness')
        descriptor = os.open(self.ready_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                stream.write(encoded(self.value, 1024))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        parent = os.open(self.ready_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        while clock() < self.activation_deadline:
            try:
                if object_json(private_read(self.go_path, 1024)) != self.value:
                    raise ValueError('Broker inference gate has a stale owner, request or nonce')
                if clock() >= self.activation_deadline:
                    raise TimeoutError('Broker activation expired during gate read')
                return clock() + self.inference_seconds
            except FileNotFoundError:
                time.sleep(0.025)
        raise TimeoutError('Broker inference gate was never admitted')
