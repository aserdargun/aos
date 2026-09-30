"""Private bounded helpers for owned episode adapter train and replay workers."""

import hashlib
import json
import os
from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_MAGIC = b'AOSLORA1'
ARTIFACT_BYTES = 8 + 3 * 32 + 3 * 4 + 32768 * 4
MAX_REQUEST_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 1_048_576
TOKEN_BUDGET = 1536


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def checksum(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _hash(value):
    return type(value) is str and len(value) == 64 and all(
        character in '0123456789abcdef' for character in value)


def validate_examples(examples):
    if not isinstance(examples, list) or not 1 <= len(examples) <= 32:
        raise ValueError('owned_episode_examples_invalid')
    for record in examples:
        if not isinstance(record, dict) or set(record) != {'context', 'qs', 'task'}:
            raise ValueError('owned_episode_example_invalid')
        if (type(record['context']) is not str or not 1 <= len(record['context']) <= 262144
                or record['task'] != 'aos-owned-synthetic-form'
                or not isinstance(record['qs'], list) or len(record['qs']) != 1):
            raise ValueError('owned_episode_example_invalid')
        question = record['qs'][0]
        if (not isinstance(question, dict) or set(question) != {'text', 'options', 'gold'}
                or type(question['text']) is not str or not 1 <= len(question['text']) <= 16384
                or not isinstance(question['options'], list)
                or not 2 <= len(question['options']) <= 10
                or any(type(option) is not str or not 1 <= len(option) <= 1024
                       for option in question['options'])
                or len(set(question['options'])) != len(question['options'])
                or type(question['gold']) is not int
                or not 0 <= question['gold'] < len(question['options'])):
            raise ValueError('owned_episode_example_invalid')
    return examples


def validate_request(request, *, replay):
    common = {'mode', 'examples', 'authorization_sha256', 'conversion_sha256', 'max_tokens'}
    expected = common | ({'artifact_sha256', 'deployment_manifest_sha256'} if replay else set())
    mode = 'owned_episode_adapter_replay_v1' if replay else 'owned_episode_adapter_train_v1'
    if not isinstance(request, dict) or set(request) != expected or request.get('mode') != mode:
        raise ValueError('owned_episode_adapter_request_invalid')
    validate_examples(request['examples'])
    if (type(request['max_tokens']) is not int or request['max_tokens'] != TOKEN_BUDGET
            or not _hash(request['authorization_sha256'])
            or not _hash(request['conversion_sha256'])
            or replay and (not _hash(request['artifact_sha256'])
                           or not _hash(request['deployment_manifest_sha256']))):
        raise ValueError('owned_episode_adapter_request_invalid')
    return request


def _directory_child(parent_fd, name):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                or opened.st_uid != os.getuid() or linked.st_uid != os.getuid()):
            raise ValueError('owned_episode_adapter_path_changed')
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def open_private_data_parent(path):
    path = Path(path)
    if '..' in path.parts:
        raise ValueError('owned_episode_adapter_artifact_path_invalid')
    root = ROOT.absolute()
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(root / 'data')
    except ValueError:
        raise ValueError('owned_episode_adapter_artifact_outside_data') from None
    if not relative.parts:
        raise ValueError('owned_episode_adapter_artifact_parent_invalid')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current, _identity = _directory_child(descriptor, 'data')
        os.close(descriptor)
        descriptor = current
        for component in relative.parts[:-1]:
            current, _identity = _directory_child(descriptor, component)
            os.close(descriptor)
            descriptor = current
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('owned_episode_adapter_parent_not_private')
        return descriptor, relative.parts[-1], info
    except BaseException:
        os.close(descriptor)
        raise


def assert_private_data_parent(path, expected):
    descriptor, _name, actual = open_private_data_parent(path)
    try:
        if ((actual.st_dev, actual.st_ino, actual.st_uid, actual.st_mode)
                != (expected.st_dev, expected.st_ino, expected.st_uid, expected.st_mode)):
            raise ValueError('owned_episode_adapter_parent_changed')
    finally:
        os.close(descriptor)


def _same_file(first, second):
    return (first.st_dev, first.st_ino, first.st_uid, first.st_mode, first.st_nlink,
            first.st_size, first.st_mtime_ns, first.st_ctime_ns) == (
            second.st_dev, second.st_ino, second.st_uid, second.st_mode, second.st_nlink,
            second.st_size, second.st_mtime_ns, second.st_ctime_ns)


def read_private_artifact(path):
    directory, name, parent_identity = open_private_data_parent(path)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(linked.st_mode)
                    or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1 or before.st_size != ARTIFACT_BYTES
                    or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_episode_adapter_artifact_not_private')
            chunks = []
            remaining = ARTIFACT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b''.join(chunks)
            after = os.fstat(descriptor)
            linked_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (len(payload) != ARTIFACT_BYTES or not _same_file(before, after)
                    or not _same_file(before, linked_after)):
                raise ValueError('owned_episode_adapter_artifact_changed')
            return payload
        finally:
            os.close(descriptor)
    finally:
        try:
            assert_private_data_parent(path, parent_identity)
        finally:
            os.close(directory)


def validate_empty_candidate(path):
    directory, name, parent_identity = open_private_data_parent(path)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(linked.st_mode)
                    or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1 or before.st_size != 0
                    or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_episode_adapter_output_not_empty_private')
            after = os.fstat(descriptor)
            linked_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not _same_file(before, after) or not _same_file(before, linked_after):
                raise ValueError('owned_episode_adapter_output_changed')
            return before
        finally:
            os.close(descriptor)
    finally:
        try:
            assert_private_data_parent(path, parent_identity)
        finally:
            os.close(directory)


def write_private_candidate(path, payload):
    if type(payload) is not bytes or len(payload) != ARTIFACT_BYTES:
        raise ValueError('owned_episode_adapter_artifact_size')
    directory, name, parent_identity = open_private_data_parent(path)
    result = None
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(linked.st_mode)
                    or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_nlink != 1 or before.st_size != 0
                    or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_episode_adapter_output_not_empty_private')
            position = 0
            while position < len(payload):
                written = os.write(descriptor, payload[position:])
                if written <= 0:
                    raise ValueError('owned_episode_adapter_output_write_failed')
                position += written
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            linked_after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not _same_file(after, linked_after) or after.st_size != len(payload)
                    or after.st_nlink != 1 or stat.S_IMODE(after.st_mode) != 0o600):
                raise ValueError('owned_episode_adapter_output_changed')
            os.fsync(directory)
            result = after
        finally:
            os.close(descriptor)
    finally:
        try:
            assert_private_data_parent(path, parent_identity)
        finally:
            os.close(directory)
    if read_private_artifact(path) != payload:
        raise ValueError('owned_episode_adapter_output_changed')
    return result


def read_manifest(path):
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_MANIFEST_BYTES:
            raise ValueError('owned_episode_adapter_manifest_invalid')
        raw = bytearray()
        while len(raw) <= MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_MANIFEST_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
        if (len(raw) > MAX_MANIFEST_BYTES or not _same_file(before, after)
                or not _same_file(before, linked)):
            raise ValueError('owned_episode_adapter_manifest_changed')
        return bytes(raw)
    finally:
        os.close(descriptor)


def private_manifest_pins(manifest_bytes):
    try:
        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        pins = json.loads(manifest_bytes, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(pins, dict):
            raise ValueError
        expected = json.loads((ROOT / 'examples/dataset_converter_pin.json').read_text())
        if (pins['code_revision'] != expected['revision']
                or pins['checkpoint_revision'] != pins['tokenizer_revision']
                or pins['code_files']['prompt.py'] != expected['files']['prompt.py']
                or not isinstance(pins['dependencies'], dict)):
            raise ValueError
        return pins, expected
    except Exception:
        raise ValueError('owned_episode_adapter_manifest_pin_mismatch') from None


def validate_artifact_binding(payload, request, manifest_sha256):
    import math
    import struct

    if (len(payload) != ARTIFACT_BYTES or payload[:8] != ARTIFACT_MAGIC
            or payload[8:40] != bytes.fromhex(request['authorization_sha256'])
            or payload[40:72] != bytes.fromhex(checksum(request['examples']))
            or payload[72:104] != bytes.fromhex(manifest_sha256)
            or struct.unpack_from('<III', payload, 104) != (4, 6144, 2048)
            or hashlib.sha256(payload).hexdigest() != request['artifact_sha256']):
        raise ValueError('owned_episode_adapter_artifact_binding')
    weights = struct.unpack_from('<32768f', payload, 116)
    if not all(math.isfinite(value) for value in weights):
        raise ValueError('owned_episode_adapter_artifact_values')
    return weights
