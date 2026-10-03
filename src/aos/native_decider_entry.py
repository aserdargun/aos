import hashlib
import errno
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import Field, model_validator

from .contracts import REPO_ROOT, TypedModel, digest
from .native_inhibit import InhibitFiles, InhibitPath, InhibitSHA, NativeInhibitStore, acquire_native_lease
from .shared_desktop_plan import _python_link_chain, read_pinned_file
from .workspace_identity import open_existing_workspace


ENTRY_CONFIG_PATH = REPO_ROOT / 'data/native-decider-entry-v1/entry-config.private.json'
ORIGINAL_WORKER_PATH = REPO_ROOT / 'services/decider/worker.py'
ORIGINAL_MANIFEST_PATH = REPO_ROOT / 'models/decider-manifest.json'
AOS_PYTHON_PATH = REPO_ROOT / '.venv/bin/python'
EXECUTABLE_LIMIT = 64 * 1024 * 1024
ENTRY_ENVIRONMENT = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONNOUSERSITE': '1',
                     'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'HF_HUB_OFFLINE': '1',
                     'TRANSFORMERS_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false'}


class NativeDeciderEntryConfig(TypedModel):
    version: Literal['1'] = '1'
    profile: Literal['native-decider-lifetime-entry-v1'] = 'native-decider-lifetime-entry-v1'
    enabled: Literal[True] = True
    store: NativeInhibitStore
    python_path: InhibitPath
    python_real_path: InhibitPath
    python_sha256: InhibitSHA
    python_links: dict[InhibitPath, str] = Field(max_length=8)
    worker_path: InhibitPath
    worker_sha256: InhibitSHA
    manifest_path: InhibitPath
    manifest_sha256: InhibitSHA
    source_files: InhibitFiles
    source_sha256: InhibitSHA
    config_files: InhibitFiles
    config_sha256: InhibitSHA
    shared_launch_authorized: Literal[False] = False
    gpu_release_verified: Literal[False] = False

    @model_validator(mode='after')
    def fixed_entry(self):
        _links, resolved = _python_link_chain(self.python_path, self.python_links)
        if (resolved != self.python_real_path or Path(self.python_path).name != 'python'
                or Path(self.python_path).parent.name != 'bin'
                or self.worker_path != str(ORIGINAL_WORKER_PATH)
                or self.manifest_path != str(ORIGINAL_MANIFEST_PATH)
                or self.store.record.directory != str(ENTRY_CONFIG_PATH.parent / 'store')
                or self.store.record.scope_id != 'native-default-v1'):
            raise ValueError('Staged Decider entry differs from fixed reviewed paths/scope')
        for files, expected in ((self.source_files, self.source_sha256), (self.config_files, self.config_sha256)):
            for path in files:
                _python_link_chain(path, {})
            if digest(files) != expected:
                raise ValueError('Staged entry file-map hash differs')
        required = {self.python_real_path: self.python_sha256, self.worker_path: self.worker_sha256,
                    self.manifest_path: self.manifest_sha256}
        for path in (Path(self.python_path).parent.parent / 'pyvenv.cfg',
                     REPO_ROOT / 'src/aos/native_decider_entry.py', REPO_ROOT / 'src/aos/native_inhibit.py',
                     REPO_ROOT / 'src/aos/shared_desktop_plan.py', REPO_ROOT / 'src/aos/workspace_identity.py',
                     REPO_ROOT / 'src/aos/contracts.py', REPO_ROOT / 'scripts/native_decider_entry.py'):
            if str(path) not in self.source_files:
                raise ValueError('Staged entry source/venv closure lacks required file')
        if (any(self.source_files.get(path) != expected for path, expected in required.items())
                or self.config_files.get(str(Path(self.store.record.directory) / 'store.json')) != self.store.content_sha256):
            raise ValueError('Staged entry executable/worker/manifest/store pins differ')
        return self


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate staged entry fields forbidden')
        result[key] = value
    return result


def load_native_decider_entry(expected_sha256: str) -> NativeDeciderEntryConfig:
    if type(expected_sha256) is not str or re.fullmatch(r'[a-f0-9]{64}', expected_sha256) is None:
        raise ValueError('Exact external staged entry configuration SHA required')
    parent = open_existing_workspace(ENTRY_CONFIG_PATH.parent)
    try:
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('Fixed staged entry configuration parent must be owned 0700')
    finally:
        os.close(parent)
    content = read_pinned_file(ENTRY_CONFIG_PATH, expected_sha256, private=True, limit=262144)
    value = json.loads(content, object_pairs_hook=_unique_pairs,
                       parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('Nonfinite staged entry JSON')))
    return NativeDeciderEntryConfig.model_validate(value)


def worker_argv(config: NativeDeciderEntryConfig, arguments: list[str]) -> list[str]:
    if type(arguments) is not list or any(type(argument) is not str for argument in arguments):
        raise ValueError('Explicit bounded Decider worker arguments required')
    if not arguments or arguments[0] != config.manifest_path:
        raise ValueError('Decider manifest argument differs from reviewed configuration')
    if arguments != [config.manifest_path] and arguments != [config.manifest_path, '--serve']:
        if (len(arguments) != 3 or arguments[-1] != '--serve'
                or re.fullmatch(r'--idle-seconds=([1-9][0-9]{0,2})', arguments[1]) is None
                or int(arguments[1].split('=')[1]) > 630):
            raise ValueError('Unsupported Decider worker argument shape')
    return [config.python_path, config.worker_path, *arguments]


def _verify_files(config):
    for path, expected in config.source_files.items():
        read_pinned_file(Path(path), expected, limit=EXECUTABLE_LIMIT)
    for path, expected in config.config_files.items():
        read_pinned_file(Path(path), expected, private=True, limit=262144)


def _verify_python_links(config):
    links, resolved = _python_link_chain(config.python_path, config.python_links)
    if resolved != config.python_real_path:
        raise ValueError('Reviewed venv interpreter target differs')
    for name in links:
        path = Path(name)
        parent = open_existing_workspace(path.parent)
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            observed = os.readlink(path.name, dir_fd=parent)
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_uid', 'st_nlink', 'st_mode')
            if (not stat.S_ISLNK(before.st_mode) or before.st_uid not in {0, os.getuid()} or before.st_nlink != 1
                    or observed != config.python_links[name]
                    or any(getattr(before, field) != getattr(after, field) for field in fields)):
                raise ValueError('Reviewed venv symlink identity/target differs')
        finally:
            os.close(parent)


def _verify_executable(descriptor, config):
    before = os.fstat(descriptor)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid not in {0, os.getuid()}
            or before.st_nlink != 1 or before.st_mode & 0o022 or not before.st_mode & 0o111
            or not 1 <= before.st_size <= EXECUTABLE_LIMIT):
        raise ValueError('Reviewed executable descriptor exceeds bound or identity policy')
    os.lseek(descriptor, 0, os.SEEK_SET)
    checksum = hashlib.sha256()
    observed_size = 0
    while True:
        content = os.read(descriptor, min(65536, EXECUTABLE_LIMIT + 1 - observed_size))
        if not content:
            break
        observed_size += len(content)
        if observed_size > EXECUTABLE_LIMIT:
            raise ValueError('Reviewed executable grew beyond bound')
        checksum.update(content)
    after = os.fstat(descriptor)
    parent = open_existing_workspace(Path(config.python_real_path).parent)
    try:
        linked = os.stat(Path(config.python_real_path).name, dir_fd=parent, follow_symlinks=False)
    finally:
        os.close(parent)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_nlink')
    if (observed_size != before.st_size or checksum.hexdigest() != config.python_sha256
            or any(getattr(before, field) != getattr(current, field) for current in (after, linked) for field in fields)):
        raise ValueError('Retained executable descriptor differs from reviewed pin')


def _isolate_descriptors(allowed):
    descriptors = []
    for index, name in enumerate(os.listdir('/proc/self/fd')):
        if index >= 4096:
            raise ValueError('Entry descriptor inventory exceeds bound')
        if name.isdecimal():
            descriptors.append(int(name))
    for descriptor in descriptors:
        if descriptor not in allowed:
            try:
                os.set_inheritable(descriptor, False)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def exec_native_decider_entry(expected_sha256: str, arguments: list[str]) -> NoReturn:
    if sys.executable != str(AOS_PYTHON_PATH) or os.execve not in os.supports_fd:
        raise ValueError('Staged entry requires fixed AOS Python and Linux descriptor exec support')
    config = load_native_decider_entry(expected_sha256)
    argv = worker_argv(config, arguments)
    _verify_files(config)
    with acquire_native_lease(config.store) as lease:
        if load_native_decider_entry(expected_sha256) != config:
            raise ValueError('Staged entry configuration changed under native lifetime lease')
        _verify_files(config)
        _verify_python_links(config)
        parent = open_existing_workspace(Path(config.python_real_path).parent)
        try:
            executable = os.open(Path(config.python_real_path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            _verify_executable(executable, config)
            lease.assert_native_allowed()
            _verify_python_links(config)
            _isolate_descriptors({0, 1, 2, lease.fd})
            os.set_inheritable(executable, False)
            os.set_inheritable(lease.fd, True)
            os.execve(executable, argv, dict(ENTRY_ENVIRONMENT))
            raise RuntimeError('Descriptor exec unexpectedly returned')
        finally:
            os.close(executable)
