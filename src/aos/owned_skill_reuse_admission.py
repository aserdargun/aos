"""Exact fresh-session admission for selected owned-skill reuse."""

import json
import os
import re
import stat
from pathlib import Path

from .contracts import canonical, digest
from .dataset import validator
from .workspace_identity import open_existing_workspace


_HASH = re.compile(r'[a-f0-9]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def validate_owned_skill_reuse_admission(value):
    if not isinstance(value, dict):
        raise ValueError('owned_skill_reuse_admission_invalid')
    try:
        validator('owned_skill_reuse_admission').validate(value)
    except Exception as error:
        raise ValueError('owned_skill_reuse_admission_invalid') from error
    preview = value['preview']
    try:
        validator('owned_skill_reuse_preview').validate(preview)
    except Exception as error:
        raise ValueError('owned_skill_reuse_preview_invalid') from error
    if (value['synthetic'] is not True
            or value['execution_authorized'] is not False
            or value['old_actions_replayed'] is not False
            or value['activation_authorized'] is not False
            or value['training_ready'] is not False
            or value['preview_sha256'] != digest(preview)
            or value['manager_session'] == preview['previous_manager_session']
            or any(_HASH.fullmatch(preview.get(key, '')) is None for key in (
                'source_manifest_sha256', 'source_run_ref', 'source_invocation_sha256',
                'family_sha256', 'release_sha256', 'selection_sha256', 'review_sha256',
                'candidate_sha256', 'recipe_sha256', 'source_fingerprint_sha256'))):
        raise ValueError('owned_skill_reuse_admission_invalid')
    return value


def make_owned_skill_reuse_admission(preview: dict, *, manager_session: str,
                                     controller_state: dict) -> dict:
    if not isinstance(preview, dict) or not isinstance(controller_state, dict):
        raise ValueError('owned_skill_reuse_admission_input_invalid')
    try:
        validator('owned_skill_reuse_preview').validate(preview)
    except Exception as error:
        raise ValueError('owned_skill_reuse_preview_invalid') from error
    if (type(manager_session) is not str or _ID.fullmatch(manager_session) is None
            or manager_session == preview['previous_manager_session']):
        raise ValueError('owned_skill_reuse_manager_session_invalid')
    identity = {}
    for name in ('session_id', 'runtime_id', 'lease_id', 'generation'):
        identity[name] = controller_state.get(name)
    if (any(type(identity[name]) is not str or _ID.fullmatch(identity[name]) is None
            for name in ('session_id', 'runtime_id', 'lease_id'))
            or type(identity['generation']) is not int or identity['generation'] < 0):
        raise ValueError('owned_skill_reuse_controller_identity_invalid')
    admission = {
        'schema_version': '1.0', 'synthetic': True,
        'purpose': 'owned_selected_skill_reuse_admission',
        'preview': preview, 'preview_sha256': digest(preview),
        'manager_session': manager_session,
        'desktop_session_id': identity['session_id'],
        'runtime_id': identity['runtime_id'], 'lease_id': identity['lease_id'],
        'generation': identity['generation'], 'execution_authorized': False,
        'old_actions_replayed': False, 'activation_authorized': False,
        'training_ready': False,
    }
    return validate_owned_skill_reuse_admission(admission)


def _private_file(path: Path, *, create: bool):
    path = Path(path).absolute()
    if path.name != 'owned-skill-reuse-admission.json':
        raise ValueError('owned_skill_reuse_admission_path_invalid')
    directory_fd = open_existing_workspace(path.parent)
    try:
        directory = os.fstat(directory_fd)
        if (directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700
                or not stat.S_ISDIR(directory.st_mode)):
            raise ValueError('owned_skill_reuse_admission_directory_invalid')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
            return path, directory_fd, fd, directory
        fd = os.open(path.name, flags, dir_fd=directory_fd)
        metadata = os.fstat(fd)
        linked = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino) != (linked.st_dev, linked.st_ino)):
            os.close(fd)
            raise ValueError('owned_skill_reuse_admission_file_invalid')
        return path, directory_fd, fd, directory
    except BaseException:
        os.close(directory_fd)
        raise


def persist_owned_skill_reuse_admission(path: Path, admission: dict) -> str:
    admission = validate_owned_skill_reuse_admission(admission)
    path, directory_fd, descriptor, directory_before = _private_file(Path(path), create=True)
    content = canonical(admission).encode('utf-8')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory_fd)
        linked = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        current_directory = os.fstat(directory_fd)
        path_directory_fd = open_existing_workspace(path.parent)
        try:
            path_directory = os.fstat(path_directory_fd)
        finally:
            os.close(path_directory_fd)
        if ((linked.st_uid != os.getuid() or stat.S_IMODE(linked.st_mode) != 0o600
             or not stat.S_ISREG(linked.st_mode) or linked.st_nlink != 1)
                or any((getattr(directory_before, field) != getattr(info, field))
                       for info in (current_directory, path_directory)
                       for field in ('st_dev', 'st_ino', 'st_uid', 'st_mode'))):
            raise ValueError('owned_skill_reuse_admission_changed')
        return digest(admission)
    except BaseException:
        try:
            os.unlink(path.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)


def load_owned_skill_reuse_admission(path: Path, expected_sha256: str | None = None):
    if expected_sha256 is not None and _HASH.fullmatch(expected_sha256 or '') is None:
        raise ValueError('owned_skill_reuse_admission_hash_invalid')
    path, directory_fd, descriptor, directory_before = _private_file(
        Path(path), create=False)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > 65536:
            raise ValueError('owned_skill_reuse_admission_file_invalid')
        chunks = []
        remaining = 65537
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > 65536:
            raise ValueError('owned_skill_reuse_admission_file_invalid')
        value = json.loads(raw)
        validate_owned_skill_reuse_admission(value)
        if canonical(value).encode('utf-8') != raw:
            raise ValueError('owned_skill_reuse_admission_not_canonical')
        checksum = digest(value)
        if expected_sha256 is not None and checksum != expected_sha256:
            raise ValueError('owned_skill_reuse_admission_changed')
        after = os.fstat(descriptor)
        linked = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size',
                  'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(metadata, field) != getattr(info, field)
               for info in (after, linked) for field in fields):
            raise ValueError('owned_skill_reuse_admission_changed')
        current_directory = os.fstat(directory_fd)
        path_directory_fd = open_existing_workspace(path.parent)
        try:
            path_directory = os.fstat(path_directory_fd)
        finally:
            os.close(path_directory_fd)
        if any(getattr(directory_before, field) != getattr(info, field)
               for info in (current_directory, path_directory)
               for field in ('st_dev', 'st_ino', 'st_uid', 'st_mode')):
            raise ValueError('owned_skill_reuse_admission_directory_changed')
        return value, checksum
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def assert_owned_skill_reuse_current(path: Path, expected_sha256: str,
                                     workspace_lock, controller_state: dict,
                                     manager_session: str):
    admission, checksum = load_owned_skill_reuse_admission(path, expected_sha256)
    if (admission['manager_session'] != manager_session
            or admission['desktop_session_id'] != controller_state.get('session_id')
            or admission['runtime_id'] != controller_state.get('runtime_id')
            or admission['lease_id'] != controller_state.get('lease_id')
            or admission['generation'] != controller_state.get('generation')):
        raise ValueError('owned_skill_reuse_controller_changed')
    if workspace_lock is None or not callable(getattr(workspace_lock, 'assert_current', None)):
        raise ValueError('owned_skill_reuse_workspace_lock_missing')
    workspace_lock.assert_current()
    return admission, checksum
