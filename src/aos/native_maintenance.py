import fcntl
import hashlib
import json
import os
import re
import select
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import Field, model_validator

from . import local_app
from .contracts import REPO_ROOT, TypedModel, canonical, digest, now
from .lifecycle import ProcessIdentity, observe_process, process_identity, read_journal
from .native_handover import (ENDPOINTS, NativeControlSnapshot, NativeHandoverPreview,
                              _json, _read_current, _response, _summarize, observe_descendants)
from .native_inhibit import NativeInhibitFileIdentity
from .shared_desktop_plan import read_pinned_file, write_new_private_file
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


STORE_DIRECTORY = local_app.BASE / 'native-maintenance-v1'
RECIPE = REPO_ROOT / 'scripts/shared-only-runtime-v1'
SHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Files = Annotated[dict[str, SHA], Field(min_length=1, max_length=512)]
Clock = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _path(value):
    path = Path(value)
    if not path.is_absolute() or str(path) != value or '..' in path.parts or any(ord(char) < 32 for char in value):
        raise ValueError('Canonical absolute maintenance path required')
    return path


class NativeMaintenanceStoreRecord(TypedModel):
    version: Literal['1'] = '1'
    directory: str
    directory_identity: WorkspaceIdentity
    lock_identity: NativeInhibitFileIdentity
    metadata_identity: NativeInhibitFileIdentity


class NativeMaintenanceStore(TypedModel):
    record: NativeMaintenanceStoreRecord
    content_sha256: SHA


class NativeMaintenanceRequest(TypedModel):
    version: Literal['1'] = '1'
    request_id: str = Field(pattern=r'^native-maintenance-[a-f0-9]{32}$')
    store: NativeMaintenanceStore
    principal: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$')
    owner_uid: int = Field(ge=1)
    expected_session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    handover_path: str
    handover_sha256: SHA
    original_state_sha256: SHA
    shared_plan_sha256: SHA
    candidate_manifest_sha256: SHA
    candidate_patch_sha256: SHA
    source_files: Files
    source_sha256: SHA
    config_files: Files
    config_sha256: SHA
    native_python_real_paths: list[str] = Field(min_length=1, max_length=8)
    native_server_paths: list[str] = Field(min_length=1, max_length=8)
    gpu_uuid: str = Field(pattern=r'^GPU-[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$')
    boot_id: str = Field(pattern=r'^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    clock: Literal['CLOCK_BOOTTIME'] = 'CLOCK_BOOTTIME'
    issued_boottime: Clock
    expires_boottime: Clock

    @model_validator(mode='after')
    def exact_request(self):
        _path(self.handover_path)
        if not 0 < self.expires_boottime - self.issued_boottime <= 900:
            raise ValueError('Maintenance validity must be finite and at most 900 seconds')
        for files, checksum in ((self.source_files, self.source_sha256), (self.config_files, self.config_sha256)):
            for name in files:
                _path(name)
            if digest(files) != checksum:
                raise ValueError('Maintenance file-map hash differs')
        for name in [*self.native_python_real_paths, *self.native_server_paths]:
            _path(name)
            if name not in self.source_files:
                raise ValueError('Native inventory executables must be source pinned')
        for name in ('/usr/bin/nvidia-smi', '/usr/bin/docker'):
            if name not in self.source_files:
                raise ValueError('Physical observer tools must be source pinned')
        if not {'models/decider-manifest.json', 'models/bonsai-manifest.json'}.issubset(
                {str(Path(name).relative_to(REPO_ROOT)) for name in self.config_files if Path(name).is_relative_to(REPO_ROOT)}):
            raise ValueError('Both original model manifests must be configuration pinned')
        return self


class NativeAbsenceObservation(TypedModel):
    version: Literal['1'] = '1'
    recorded_at: str
    boot_id: str
    retired_processes: list[ProcessIdentity] = Field(min_length=2, max_length=64)
    scan_sha256: list[SHA] = Field(min_length=2, max_length=2)
    native_processes_absent: Literal[True] = True
    scope: Literal['repository-managed-entrypoints'] = 'repository-managed-entrypoints'
    arbitrary_owner_code_excluded: Literal[True] = True
    active_model_interpreter_coexistence_supported: Literal[False] = False
    gpu_release_verified: Literal[False] = False


class NativeMaintenanceReceipt(TypedModel):
    version: Literal['1'] = '1'
    store: NativeMaintenanceStore
    request: NativeMaintenanceRequest
    request_sha256: SHA
    handover_sha256: SHA
    shared_plan_sha256: SHA
    original_state_sha256: SHA
    stopped_state_sha256: SHA
    controller: NativeControlSnapshot
    supervisor: ProcessIdentity
    backend: ProcessIdentity
    observed_workers: list[ProcessIdentity] = Field(max_length=62)
    retired_processes: list[ProcessIdentity] = Field(min_length=2, max_length=64)
    source_files: Files
    source_sha256: SHA
    config_files: Files
    config_sha256: SHA
    cleanup: NativeAbsenceObservation
    lifecycle_sha256: dict[str, SHA] = Field(min_length=1, max_length=1000)
    boot_id: str
    issued_boottime: Clock
    expires_boottime: Clock
    recorded_at: str
    shared_only_promoted: Literal[True] = True
    gpu_quiet_observed_during_maintenance: Literal[True] = True
    gpu_release_verified: Literal[False] = False
    shared_launch_authorized: Literal[False] = False
    automatic_native_restart_allowed: Literal[False] = False

    @model_validator(mode='after')
    def exact_receipt(self):
        request = self.request
        if (self.request_sha256 != digest(request.model_dump(mode='json')) or self.store != request.store
                or self.handover_sha256 != request.handover_sha256 or self.shared_plan_sha256 != request.shared_plan_sha256
                or self.original_state_sha256 != request.original_state_sha256
                or self.config_files != request.config_files or self.config_sha256 != request.config_sha256
                or digest(self.source_files) != self.source_sha256
                or (self.boot_id, self.issued_boottime, self.expires_boottime)
                != (request.boot_id, request.issued_boottime, request.expires_boottime)
                or self.retired_processes != [self.supervisor, self.backend, *self.observed_workers]
                or self.cleanup.retired_processes != self.retired_processes
                or self.cleanup.boot_id != self.boot_id
                or len({process.pid for process in self.retired_processes}) != len(self.retired_processes)
                or any(process.boot_id != self.boot_id or process.uid != request.owner_uid for process in self.retired_processes)):
            raise ValueError('Maintenance receipt binding differs')
        return self


def _clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _current(request, deadline=None):
    moment = _clock()
    if (os.getuid() != request.owner_uid or process_identity(os.getpid()).boot_id != request.boot_id
            or not request.issued_boottime <= moment < request.expires_boottime
            or deadline is not None and moment >= deadline):
        raise ValueError('Original maintenance owner, boot or deadline is no longer current')


def _identity(metadata):
    return NativeInhibitFileIdentity(device=metadata.st_dev, inode=metadata.st_ino, owner_uid=metadata.st_uid)


def _private_metadata(metadata):
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid() or metadata.st_nlink != 1):
        raise ValueError('Private owned single-link maintenance file required')


def provision_native_maintenance_store():
    parent = local_app.private_directory(STORE_DIRECTORY.parent)
    parent_identity = workspace_identity(STORE_DIRECTORY.parent, parent)
    directory = None
    descriptors = []
    try:
        os.mkdir(STORE_DIRECTORY.name, 0o700, dir_fd=parent)
        directory = os.open(STORE_DIRECTORY.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        allocated = workspace_identity(STORE_DIRECTORY, directory)
        metadata = os.fstat(directory)
        if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
            raise ValueError('Private newly allocated maintenance directory required')
        os.fsync(parent)
        for name in ('maintenance.lock', 'store.json'):
            descriptors.append(os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory))
        record = NativeMaintenanceStoreRecord(directory=str(STORE_DIRECTORY),
            directory_identity=allocated,
            lock_identity=_identity(os.fstat(descriptors[0])), metadata_identity=_identity(os.fstat(descriptors[1])))
        content = (canonical(record.model_dump(mode='json')) + '\n').encode()
        _write_all(descriptors[1], content)
        os.fsync(descriptors[0])
        os.fsync(directory)
        store = NativeMaintenanceStore(record=record, content_sha256=hashlib.sha256(content).hexdigest())
        _verify_store(store, directory)
        linked_parent = local_app.private_directory(STORE_DIRECTORY.parent)
        try:
            if workspace_identity(STORE_DIRECTORY.parent, linked_parent) != parent_identity:
                raise ValueError('Manager directory replaced during maintenance provision')
        finally:
            os.close(linked_parent)
        return store
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
        os.close(parent)


def _write_all(descriptor, content):
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise ValueError('Incomplete durable maintenance write')
        written += count
    os.fsync(descriptor)


def _verify_store(store, directory):
    path = _path(store.record.directory)
    if path != STORE_DIRECTORY or workspace_identity(path, directory) != store.record.directory_identity:
        raise ValueError('Maintenance store directory binding differs')
    linked = local_app.private_directory(path)
    try:
        if workspace_identity(path, linked) != store.record.directory_identity:
            raise ValueError('Maintenance directory replaced')
    finally:
        os.close(linked)
    for name, expected in (('maintenance.lock', store.record.lock_identity), ('store.json', store.record.metadata_identity)):
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        _private_metadata(metadata)
        if _identity(metadata) != expected:
            raise ValueError('Maintenance store file replaced')
    content = read_pinned_file(path / 'store.json', store.content_sha256, private=True)
    if NativeMaintenanceStoreRecord.model_validate(_json(content)) != store.record:
        raise ValueError('Maintenance store metadata differs')


def _publish(store, directory, name, value):
    _verify_store(store, directory)
    content = (canonical(value) + '\n').encode()
    if len(content) > 262144:
        raise ValueError('Maintenance record exceeds bound')
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        _write_all(descriptor, content)
        os.fsync(directory)
        _verify_store(store, directory)
    finally:
        os.close(descriptor)
    return hashlib.sha256(content).hexdigest()


@contextmanager
def _maintenance_lock(store):
    directory = local_app.private_directory(_path(store.record.directory))
    descriptor = None
    try:
        _verify_store(store, directory)
        descriptor = os.open('maintenance.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
        if _identity(os.fstat(descriptor)) != store.record.lock_identity:
            raise ValueError('Maintenance lock replaced')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _verify_store(store, directory)
        yield directory
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _verify_files(request, files):
    for path, checksum in files.items():
        _current(request)
        read_pinned_file(Path(path), checksum, limit=64 * 1024 * 1024)


def _reviewed_entry_paths(request):
    manifest = _json(read_pinned_file(RECIPE / 'manifest.json', request.candidate_manifest_sha256))
    paths = {}
    for entry in manifest['files']:
        relative = Path(entry['path'])
        if (relative.is_absolute() or '..' in relative.parts or str(relative) != entry['path']
                or request.source_files.get(str(REPO_ROOT / relative)) != entry['before_sha256']):
            raise ValueError('Native inventory candidate entry selection differs')
        if relative.suffix == '.py':
            paths[str(REPO_ROOT / relative)] = entry['after_sha256']
    if len(manifest['files']) != 41:
        raise ValueError('Native inventory candidate is incomplete')
    return paths


def _entry_matches(arguments, cwd, entries):
    modules = {'aos.' + Path(name).stem for name in entries if Path(name).parent == REPO_ROOT / 'src/aos'}
    module_indices = [index for index, value in enumerate(arguments) if value == '-m']
    compact_modules = [value[2:] for value in arguments if value.startswith('-m') and value[2:] in modules]
    if any(index + 1 >= len(arguments) for index in module_indices):
        raise ValueError('Ambiguous process module arguments')
    if len(module_indices) + len(compact_modules) > 1 and (
            compact_modules or any(arguments[index + 1] in modules for index in module_indices)):
        raise ValueError('Ambiguous reviewed native module invocation')
    if compact_modules or any(arguments[index + 1] in modules for index in module_indices):
        return True
    for argument in arguments:
        if not argument.endswith('.py'):
            continue
        if len(argument) > 4096 or any(ord(character) < 32 for character in argument):
            raise ValueError('Native script argument unavailable or ambiguous')
        path = Path(os.path.normpath(str(cwd / argument)))
        if str(path) not in entries:
            continue
        if path == REPO_ROOT / 'scripts/serve_desktop.py':
            indices = [index for index, value in enumerate(arguments) if value == '--engine']
            if len(indices) == 1 and indices[0] + 1 < len(arguments) and arguments[indices[0] + 1] == 'scientist':
                try:
                    read_pinned_file(path, entries[str(path)])
                except ValueError:
                    return True
                continue
        return True
    return False


def _native_inventory(request, deadline):
    _current(request, deadline)
    paths = set(request.native_python_real_paths + request.native_server_paths)
    entry_paths = _reviewed_entry_paths(request)
    inventory = []
    matches = []
    root = Path('/proc')
    entries = sorted((entry for entry in root.iterdir() if entry.name.isdecimal()), key=lambda entry: int(entry.name))
    if len(entries) > 8192:
        raise ValueError('Native process inventory truncated')
    for entry in entries:
        _current(request, deadline)
        try:
            if entry.stat().st_uid != request.owner_uid:
                continue
            identity = process_identity(int(entry.name))
            executable = os.readlink(entry / 'exe')
            with (entry / 'cmdline').open('rb') as stream:
                content = stream.read(65537)
            if len(content) > 65536 or not content or not content.endswith(b'\0') or executable.endswith(' (deleted)'):
                raise ValueError('Native process identity or arguments unavailable')
            arguments = content[:-1].decode('utf-8', errors='strict').split('\0')
            cwd = Path(os.readlink(entry / 'cwd'))
            _path(str(cwd))
            entry_match = _entry_matches(arguments, cwd, entry_paths)
            if os.readlink(entry / 'cwd') != str(cwd):
                raise ValueError('Native process working directory changed during scan')
            if process_identity(identity.pid) != identity:
                raise ValueError('Native process changed during scan')
            inventory.append({'identity': identity.model_dump(mode='json'), 'executable': executable,
                              'arguments_sha256': digest(arguments)})
            if executable in paths or entry_match:
                matches.append(identity)
        except FileNotFoundError:
            try:
                process_identity(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            raise ValueError('Live process inventory is unavailable')
    return inventory, matches


def observe_native_absence(request, retired_processes, *, deadline):
    if (not 2 <= len(retired_processes) <= 64 or len({item.pid for item in retired_processes}) != len(retired_processes)
            or any(item.boot_id != request.boot_id or item.uid != request.owner_uid for item in retired_processes)):
        raise ValueError('Exact retired process inventory required')
    scans = []
    for _iteration in range(2):
        _current(request, deadline)
        if any(observe_process(item) != 'not_observed' for item in retired_processes):
            raise ValueError('Original native process remains or its absence is unknown')
        inventory, matches = _native_inventory(request, deadline)
        if matches:
            raise ValueError('Native or unsupported active model interpreter remains')
        scans.append(digest(inventory))
        if any(observe_process(item) != 'not_observed' for item in retired_processes):
            raise ValueError('Retired process observation changed')
    _current(request, deadline)
    return NativeAbsenceObservation(recorded_at=now(), boot_id=request.boot_id,
                                    retired_processes=retired_processes, scan_sha256=scans)


def _command(request, arguments):
    _current(request)
    read_pinned_file(Path(arguments[0]), request.source_files[arguments[0]], limit=64 * 1024 * 1024)
    result = subprocess.run(arguments, capture_output=True, timeout=min(10, request.expires_boottime - _clock()),
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(Path.home())})
    if len(result.stdout) > 262144 or len(result.stderr) > 262144:
        raise ValueError('Physical observer output exceeds bound')
    _current(request)
    return result


def _gpu_quiet(request):
    result = _command(request, ['/usr/bin/nvidia-smi', '--id=' + request.gpu_uuid,
                      '--query-compute-apps=pid,process_name,used_memory', '--format=csv,noheader,nounits'])
    if result.returncode != 0 or result.stderr or result.stdout.strip():
        raise ValueError('Maintenance requires an independently observed quiet GPU')
    identity = _command(request, ['/usr/bin/nvidia-smi', '--id=' + request.gpu_uuid,
                        '--query-gpu=uuid', '--format=csv,noheader,nounits'])
    if identity.returncode != 0 or identity.stderr or identity.stdout.decode().strip() != request.gpu_uuid:
        raise ValueError('Exact GPU identity observation unavailable')


def _physical_cleanup(request, original, retired):
    observation = observe_native_absence(request, retired, deadline=request.expires_boottime)
    _gpu_quiet(request)
    if original.token_name is None or os.path.lexists(REPO_ROOT / 'runs' / original.token_name):
        raise ValueError('Original console token remains or is unknown')
    directory = local_app.BASE / original.session
    workspace = local_app.private_directory(directory / 'workspace')
    try:
        expected_workspace = workspace_identity(directory / 'workspace', workspace)
    finally:
        os.close(workspace)
    journals = directory / '.aos-lifecycle'
    descriptor = local_app.private_directory(journals)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        names = sorted(os.listdir(descriptor))
        if not 1 <= len(names) <= 1000 or any(not re.fullmatch(r'desktop-[a-f0-9]{32}\.jsonl', name) for name in names):
            raise ValueError('Exact lifecycle inventory unavailable')
        checksums = {}
        containers = set()
        for name in names:
            _current(request)
            events, checksum = read_journal(journals / name)
            birth, terminal = events[0].birth, events[-1]
            container = terminal.container_id
            if (birth.process != original.backend or birth.workspace != expected_workspace
                    or name != birth.runtime_id + '.jsonl' or terminal.stage != 'removed'
                    or container is None or container in containers):
                raise ValueError('Lifecycle cleanup identity is unresolved')
            result = _command(request, ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock',
                                       'inspect', '--type', 'container', '--format', '{{.Id}}', container])
            if (result.returncode != 1 or result.stdout.strip()
                    or result.stderr.strip() != ('Error response from daemon: No such container: ' + container).encode()):
                raise ValueError('Actual original container absence is unverified')
            containers.add(container)
            checksums[name] = checksum
        os.lseek(descriptor, 0, os.SEEK_SET)
        if sorted(os.listdir(descriptor)) != names or any(read_journal(journals / name)[1] != checksum for name, checksum in checksums.items()):
            raise ValueError('Lifecycle inventory changed')
    finally:
        os.close(descriptor)
    if os.path.lexists(REPO_ROOT / 'runs' / original.token_name):
        raise ValueError('Original token reappeared')
    observe_native_absence(request, retired, deadline=request.expires_boottime)
    _gpu_quiet(request)
    return observation, checksums


def _idle(client, expected_control=None, *, quiesced=False):
    payloads = {path: _response(client, 'GET', path)[0] for path in ENDPOINTS}
    control, tasks, scientist, binding, blockers = _summarize(payloads)
    if (blockers or control is None or tasks is None or scientist is None or binding is None
            or scientist.lab_configured or scientist.inference_configured
            or quiesced and not tasks.restart_quiesced or expected_control is not None and control != expected_control):
        raise ValueError('Exact idle native controller and unsupported/unknown Scientist inventory required')
    return control, binding


def _quiesce(client, control):
    with client.stream('POST', '/api/restart/quiesce', json={'session_id': control.session_id}) as response:
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > 65536:
                raise ValueError('Quiesce response exceeds bound')
            content.extend(chunk)
        if response.status_code != 200 or _json(bytes(content)) != {'quiesced': True, 'session_id': control.session_id}:
            raise ValueError('Exact restart quiesce acknowledgment required')


def _candidate(request):
    manifest = _json(read_pinned_file(RECIPE / 'manifest.json', request.candidate_manifest_sha256))
    patch = read_pinned_file(RECIPE / 'source.patch.txt', request.candidate_patch_sha256)
    if (manifest.get('profile') != 'shared-only-runtime-v1' or manifest.get('patch_sha256') != request.candidate_patch_sha256
            or len(manifest.get('files', [])) != 41 or len(manifest.get('preserved_files', {})) != 13):
        raise ValueError('Exact reviewed shared-only source candidate required')
    for relative in ('src/aos/native_maintenance.py', 'src/aos/native_exclusion.py', 'scripts/native_maintenance.py',
                     'schemas/native_maintenance_store.schema.json', 'schemas/native_maintenance_request.schema.json',
                     'schemas/native_maintenance_receipt.schema.json', 'schemas/native_exclusion_evidence.schema.json',
                     'src/aos/native_handover.py', 'src/aos/lifecycle.py', 'src/aos/workspace_identity.py',
                     'src/aos/shared_desktop_plan.py', 'src/aos/contracts.py'):
        if str(REPO_ROOT / relative) not in request.source_files:
            raise ValueError('Reviewed maintenance/producer/helper closure is incomplete')
    if (request.source_files.get(str(RECIPE / 'manifest.json')) != request.candidate_manifest_sha256
            or request.source_files.get(str(RECIPE / 'source.patch.txt')) != request.candidate_patch_sha256):
        raise ValueError('Candidate recipe must remain in the reviewed source closure')
    selected = {}
    for entry in manifest['files']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative) != entry['path'] or entry['path'] in selected:
            raise ValueError('Candidate source path is not canonical')
        name = str(REPO_ROOT / relative)
        if request.source_files.get(name) != entry['before_sha256']:
            raise ValueError('Original request does not cover candidate source')
        selected[entry['path']] = entry
    for relative, checksum in manifest['preserved_files'].items():
        name = str(REPO_ROOT / relative)
        if not Path(name).is_relative_to(REPO_ROOT) or '..' in Path(relative).parts or request.source_files.get(name) != checksum:
            raise ValueError('Original request does not cover preserved broker/CPU source')
    replacements = _apply_patch(patch, selected)
    return manifest, replacements


def _verify_inventory_selection(request, backend):
    path = Path('/proc') / str(backend.pid) / 'cmdline'
    with path.open('rb') as stream:
        content = stream.read(65537)
    if not content or len(content) > 65536 or not content.endswith(b'\0') or observe_process(backend) != 'same_process':
        raise ValueError('Original backend argument inventory unavailable')
    arguments = content[:-1].decode().split('\0')
    indices = [index for index, argument in enumerate(arguments) if argument == '--model-python']
    if len(indices) > 1 or indices and indices[0] + 1 >= len(arguments):
        raise ValueError('Original model interpreter selection is ambiguous')
    python = _path(arguments[indices[0] + 1]) if indices else Path.home() / '.venv/bin/python'
    if str(python.resolve(strict=True)) not in request.native_python_real_paths:
        raise ValueError('Native process inventory omits the original model interpreter')
    manifest_path = REPO_ROOT / 'models/bonsai-manifest.json'
    manifest = _json(read_pinned_file(manifest_path, request.config_files[str(manifest_path)]))
    server = _path(manifest['server_path'])
    if str(server.resolve(strict=True)) not in request.native_server_paths:
        raise ValueError('Native process inventory omits the original Bonsai server')


def _apply_patch(content, selected):
    lines = content.decode().splitlines(keepends=True)
    index = 0
    replacements = {}
    while index < len(lines):
        if not lines[index].startswith('--- a/'):
            raise ValueError('Unsupported source patch header')
        name = lines[index][6:].rstrip('\n')
        index += 1
        if name not in selected or name in replacements or lines[index] != '+++ b/' + name + '\n':
            raise ValueError('Exact source patch file pair required')
        index += 1
        original = read_pinned_file(REPO_ROOT / name, selected[name]['before_sha256']).decode().splitlines(keepends=True)
        output = []
        cursor = 0
        while index < len(lines) and lines[index].startswith('@@ '):
            header = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n', lines[index])
            if header is None:
                raise ValueError('Invalid source patch hunk')
            start = int(header[1]) - 1
            if start < cursor or start > len(original):
                raise ValueError('Overlapping source patch hunk')
            output.extend(original[cursor:start])
            cursor = start
            consumed = produced = 0
            index += 1
            while index < len(lines) and lines[index][:1] in {' ', '+', '-'} and not lines[index].startswith('--- a/'):
                line = lines[index]
                if line[0] in {' ', '-'}:
                    if cursor >= len(original) or original[cursor] != line[1:]:
                        raise ValueError('Source patch context differs')
                    cursor += 1
                    consumed += 1
                if line[0] in {' ', '+'}:
                    output.append(line[1:])
                    produced += 1
                index += 1
            if consumed != int(header[2] or 1) or produced != int(header[4] or 1):
                raise ValueError('Source patch hunk count differs')
        output.extend(original[cursor:])
        after = ''.join(output).encode()
        if hashlib.sha256(after).hexdigest() != selected[name]['after_sha256']:
            raise ValueError('Exact promoted source hash differs')
        replacements[name] = after
    if set(replacements) != set(selected):
        raise ValueError('Incomplete source patch inventory')
    return replacements


def _stopped(original):
    content = local_app.private_read(local_app.BASE / 'current.json')
    state = local_app.LocalAppState.model_validate(_json(content))
    expected = original.model_copy(update={'phase': 'stopped', 'token_name': None})
    if state != expected:
        raise ValueError('Original stopped state was replaced or changed')
    return hashlib.sha256(content).hexdigest()


@contextmanager
def _manager_lock(expected, expected_directory):
    directory = local_app.private_directory(local_app.BASE)
    descriptor = None
    try:
        if workspace_identity(local_app.BASE, directory) != expected_directory:
            raise ValueError('Original manager directory replaced')
        descriptor = os.open('manager.lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
        _private_metadata(os.fstat(descriptor))
        if _identity(os.fstat(descriptor)) != expected:
            raise ValueError('Original manager lifetime lock replaced')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _identity(os.stat('manager.lock', dir_fd=directory, follow_symlinks=False)) != expected:
            raise ValueError('Manager lock path replaced')
        yield
        if _identity(os.stat('manager.lock', dir_fd=directory, follow_symlinks=False)) != expected:
            raise ValueError('Manager lock path replaced during promotion')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _promote_file(request, directory, sequence, entry, content):
    _current(request)
    path = REPO_ROOT / entry['path']
    read_pinned_file(path, entry['before_sha256'])
    parent = open_existing_workspace(path.parent)
    staged = '.native-maintenance-' + request.request_id + '.tmp'
    descriptor = None
    try:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != request.owner_uid or before.st_nlink != 1:
            raise ValueError('Promotion requires owned single-link source')
        _publish(request.store, directory, f'{sequence:04d}-source-intent.json', entry)
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(before.st_mode), dir_fd=parent)
        os.fchmod(descriptor, stat.S_IMODE(before.st_mode))
        _write_all(descriptor, content)
        _current(request)
        read_pinned_file(path, entry['before_sha256'])
        linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if linked != before:
            raise ValueError('Original promotion source inode or metadata changed')
        os.replace(staged, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        read_pinned_file(path, entry['after_sha256'])
        _publish(request.store, directory, f'{sequence:04d}-source-complete.json', entry)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def execute_native_maintenance(request, *, confirm_request_sha256):
    request = NativeMaintenanceRequest.model_validate_json(canonical(request.model_dump(mode='json')))
    request_sha = digest(request.model_dump(mode='json'))
    if confirm_request_sha256 != request_sha:
        raise ValueError('Explicit exact maintenance request consent required')
    with local_app.instance_scope(local_app.LocalAppInstance(base=local_app.BASE, port=8765)):
        with _maintenance_lock(request.store) as directory:
            os.lseek(directory, 0, os.SEEK_SET)
            if set(os.listdir(directory)) != {'maintenance.lock', 'store.json'}:
                raise ValueError('Maintenance replay or partial prior execution requires human review')
            _current(request)
            _verify_files(request, request.source_files)
            _verify_files(request, request.config_files)
            preview = NativeHandoverPreview.model_validate(_json(read_pinned_file(Path(request.handover_path), request.handover_sha256, private=True)))
            original, state_sha = _read_current(request.expected_session)
            if (state_sha != request.original_state_sha256 or preview.current_state_sha256 != state_sha
                    or preview.expected_session != original.session or preview.supervisor != original.supervisor
                    or preview.backend != original.backend or not preview.local_idle_observed
                    or not preview.snapshot_continuity_verified or preview.control is None):
                raise ValueError('Reviewed preview and exact original session differ')
            manifest, replacements = _candidate(request)
            _verify_inventory_selection(request, original.backend)
            manager_metadata = os.stat(local_app.BASE / 'manager.lock', follow_symlinks=False)
            _private_metadata(manager_metadata)
            manager_identity = _identity(manager_metadata)
            manager_directory = local_app.private_directory(local_app.BASE)
            try:
                manager_directory_identity = workspace_identity(local_app.BASE, manager_directory)
            finally:
                os.close(manager_directory)
            descendants, blockers = observe_descendants(original.supervisor, original.backend)
            if blockers:
                raise ValueError('Original descendant inventory unavailable')
            retired = [original.supervisor, original.backend, *[item.identity for item in descendants
                       if item.identity not in (original.supervisor, original.backend)]]
            descriptors = []
            try:
                for identity in retired:
                    descriptor = os.pidfd_open(identity.pid)
                    descriptors.append(descriptor)
                    if select.select([descriptor], [], [], 0)[0] or observe_process(identity) != 'same_process':
                        raise ValueError('Original native process changed before maintenance')
                _publish(request.store, directory, '0000-request.json', request.model_dump(mode='json'))
                with httpx.Client(base_url='http://127.0.0.1:8765', headers={'Origin': 'http://127.0.0.1:8765'},
                                  timeout=5, trust_env=False, follow_redirects=False) as client:
                    login, _hash = _response(client, 'POST', '/api/login', json={'token': local_app.token_value(original)})
                    if login.get('authenticated') is not True:
                        raise ValueError('Native maintenance login failed')
                    control, binding = _idle(client, preview.control)
                    _publish(request.store, directory, '0001-quiesce-intent.json', {'controller': control.model_dump(mode='json')})
                    _quiesce(client, control)
                    _publish(request.store, directory, '0002-stop-intent.json', {'retired_processes': [item.model_dump(mode='json') for item in retired]})
                    _current(request)
                    if _read_current(request.expected_session)[1] != state_sha:
                        raise ValueError('Original manager state changed before UI interruption')
                    fresh_control, fresh_binding = _idle(client, control, quiesced=True)
                    if fresh_binding.binding_sha256 != binding.binding_sha256 or fresh_control != control:
                        raise ValueError('Controller binding changed before UI interruption')
                    if any(observe_process(identity) != 'same_process' for identity in retired):
                        raise ValueError('Original process generation changed before stop')
                    _inventory, native_matches = _native_inventory(request, request.expires_boottime)
                    if any(identity not in retired for identity in native_matches):
                        raise ValueError('Unknown detached native worker requires human review before stop')
                    _current(request)
                    _idle(client, control, quiesced=True)
                    local_app.stop(expected_session=request.expected_session, expected_state=original)
                if any(not select.select([descriptor], [], [], 0)[0] for descriptor in descriptors):
                    raise ValueError('An original native worker survived graceful supervisor stop')
                if any(observe_process(identity) != 'not_observed' for identity in retired):
                    raise ValueError('Original native process exit is unknown')
                with _manager_lock(manager_identity, manager_directory_identity):
                    stopped_sha = _stopped(original)
                    cleanup, lifecycle = _physical_cleanup(request, original, retired)
                    _publish(request.store, directory, '0003-cleanup.json', {'observation': cleanup.model_dump(mode='json'), 'lifecycle_sha256': lifecycle})
                    _verify_files(request, request.source_files)
                    _verify_files(request, request.config_files)
                    for sequence, entry in enumerate(manifest['files'], 10):
                        if _stopped(original) != stopped_sha:
                            raise ValueError('Stopped manager state changed during source promotion')
                        _promote_file(request, directory, sequence, entry, replacements[entry['path']])
                    after_files = dict(request.source_files)
                    for entry in manifest['files']:
                        after_files[str(REPO_ROOT / entry['path'])] = entry['after_sha256']
                    _verify_files(request, after_files)
                    _verify_files(request, request.config_files)
                    cleanup, lifecycle = _physical_cleanup(request, original, retired)
                    if _stopped(original) != stopped_sha:
                        raise ValueError('Stopped manager state changed before final maintenance receipt')
                    _current(request)
                    receipt = NativeMaintenanceReceipt(store=request.store, request=request, request_sha256=request_sha,
                        handover_sha256=request.handover_sha256, shared_plan_sha256=request.shared_plan_sha256,
                        original_state_sha256=state_sha, stopped_state_sha256=stopped_sha, controller=control,
                        supervisor=original.supervisor, backend=original.backend, observed_workers=retired[2:],
                        retired_processes=retired, source_files=after_files, source_sha256=digest(after_files),
                        config_files=request.config_files, config_sha256=request.config_sha256, cleanup=cleanup,
                        lifecycle_sha256=lifecycle, boot_id=request.boot_id, issued_boottime=request.issued_boottime,
                        expires_boottime=request.expires_boottime, recorded_at=now())
                    _publish(request.store, directory, 'receipt.json', receipt.model_dump(mode='json'))
                    _current(request)
                    return read_native_maintenance_receipt(request.store, request_sha)[0]
            except BaseException as error:
                try:
                    _publish(request.store, directory, 'failure.json', {'request_sha256': request_sha,
                        'recorded_at': now(), 'exception_type': type(error).__name__, 'automatic_restart': False,
                        'automatic_reverse_patch': False, 'requires_human_review': True})
                except (OSError, ValueError):
                    pass
                raise
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)


def read_native_maintenance_receipt(store, request_sha256):
    directory = local_app.private_directory(_path(store.record.directory))
    try:
        _verify_store(store, directory)
        receipt_content = local_app.private_read(Path(store.record.directory) / 'receipt.json', limit=262144)
        receipt = NativeMaintenanceReceipt.model_validate(_json(receipt_content))
        if receipt.store != store or receipt.request_sha256 != request_sha256:
            raise ValueError('Exact immutable maintenance receipt differs')
        request_content = local_app.private_read(Path(store.record.directory) / '0000-request.json', limit=262144)
        if NativeMaintenanceRequest.model_validate(_json(request_content)) != receipt.request:
            raise ValueError('Durable maintenance request differs')
        if os.path.lexists(Path(store.record.directory) / 'failure.json'):
            raise ValueError('Failed or expired maintenance publication requires human review')
        _verify_store(store, directory)
        return receipt, hashlib.sha256(receipt_content).hexdigest()
    finally:
        os.close(directory)
