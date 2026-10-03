import fcntl
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest, now
from .shared_desktop_plan import read_pinned_file
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


LOCK_NAME = 'native-inhibit.lock'
STORE_NAME = 'store.json'
RECEIPT_NAME = 'inhibit.json'
SOURCE_FILE_LIMIT = 64 * 1024 * 1024
InhibitSHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
InhibitPath = Annotated[str, Field(min_length=1, max_length=4096)]
InhibitScope = Annotated[str, Field(pattern=r'^native-[a-z0-9][a-z0-9-]{0,63}$')]
InhibitFiles = Annotated[dict[InhibitPath, InhibitSHA], Field(min_length=1, max_length=256)]


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or any(ord(character) < 32 for character in value)):
        raise ValueError('Canonical absolute inhibit path required')
    return path


class NativeInhibitFileIdentity(TypedModel):
    device: int = Field(ge=0)
    inode: int = Field(ge=1)
    owner_uid: int = Field(ge=0)


class NativeInhibitStoreRecord(TypedModel):
    version: Literal['1'] = '1'
    scope_id: InhibitScope
    directory: InhibitPath
    directory_identity: WorkspaceIdentity
    lock_identity: NativeInhibitFileIdentity
    metadata_identity: NativeInhibitFileIdentity
    initial_lock_mtime_ns: int = Field(ge=0)
    initial_lock_ctime_ns: int = Field(ge=0)

    @model_validator(mode='after')
    def exact_scope(self):
        _path(self.directory)
        if (self.directory_identity.path_sha256 != digest({'workspace': self.directory})
                or len({self.directory_identity.owner_uid, self.lock_identity.owner_uid,
                        self.metadata_identity.owner_uid}) != 1):
            raise ValueError('Inhibit store scope identity differs')
        return self


class NativeInhibitStore(TypedModel):
    record: NativeInhibitStoreRecord
    content_sha256: InhibitSHA


class NativeInhibitRequest(TypedModel):
    version: Literal['1'] = '1'
    request_id: str = Field(pattern=r'^native-inhibit-[a-f0-9]{32}$')
    scope_id: InhibitScope
    principal_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$')
    expected_session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    controller_session_id: str = Field(pattern=r'^desktop-session-[a-f0-9]{32}$')
    generation: int = Field(ge=0)
    handover_sha256: InhibitSHA
    shared_plan_sha256: InhibitSHA
    boot_id: str = Field(pattern=r'^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    source_files: InhibitFiles
    source_sha256: InhibitSHA
    config_files: InhibitFiles
    config_sha256: InhibitSHA
    clock: Literal['CLOCK_BOOTTIME'] = 'CLOCK_BOOTTIME'
    issued_boottime: float = Field(ge=0)
    expires_boottime: float = Field(gt=0)

    @model_validator(mode='after')
    def bounded_request(self):
        for files, expected in ((self.source_files, self.source_sha256), (self.config_files, self.config_sha256)):
            for path in files:
                _path(path)
            if digest(files) != expected:
                raise ValueError('Inhibit source/config map differs')
        if not 0 < self.expires_boottime - self.issued_boottime <= 900:
            raise ValueError('Inhibit publication request validity must be finite and at most 900 seconds')
        return self


class NativeInhibitReceipt(TypedModel):
    version: Literal['1'] = '1'
    store: NativeInhibitStore
    request: NativeInhibitRequest
    request_sha256: InhibitSHA
    recorded_at: str
    native_admission_inhibited: Literal[True] = True
    expiry_reopens_native: Literal[False] = False
    shared_launch_authorized: Literal[False] = False
    gpu_release_verified: Literal[False] = False
    worker_absence_verified: Literal[False] = False
    source_coverage_verified: Literal[False] = False
    restore_implemented: Literal[False] = False

    @model_validator(mode='after')
    def exact_request(self):
        if (self.request.scope_id != self.store.record.scope_id
                or digest(self.request.model_dump(mode='json')) != self.request_sha256):
            raise ValueError('Inhibit receipt request binding differs')
        return self


class NativeInhibitIntent(TypedModel):
    version: Literal['1'] = '1'
    store_content_sha256: InhibitSHA
    request_sha256: InhibitSHA
    receipt_sha256: InhibitSHA


def _file_identity(descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid() or metadata.st_nlink != 1):
        raise ValueError('Inhibit files require owned single-link regular 0600 identity')
    return NativeInhibitFileIdentity(device=metadata.st_dev, inode=metadata.st_ino, owner_uid=metadata.st_uid)


def _directory_identity(path, descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()):
        raise ValueError('Inhibit directory must be owned 0700')
    return workspace_identity(path, descriptor)


def _read(directory, name, expected_identity=None):
    descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        identity = _file_identity(descriptor)
        before = os.fstat(descriptor)
        if expected_identity is not None and identity != expected_identity or before.st_size > 262144:
            raise ValueError('Inhibit file identity or bound differs')
        content = bytearray()
        while len(content) <= 262144:
            chunk = os.read(descriptor, min(65536, 262145 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
        after = os.fstat(descriptor)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (len(content) != before.st_size or len(content) > 262144
                or any(getattr(before, field) != getattr(current, field) for current in (after, linked) for field in fields)):
            raise ValueError('Inhibit file changed during bounded read')
        return bytes(content)
    finally:
        os.close(descriptor)


def _json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate inhibit fields are forbidden')
            result[key] = value
        return result
    return json.loads(content, object_pairs_hook=pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('Nonfinite inhibit JSON')))


def _write(descriptor, content):
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError('Inhibit durable write failed')
        remaining = remaining[written:]
    os.fsync(descriptor)


def _verify(store, directory, lock):
    path = _path(store.record.directory)
    if (_directory_identity(path, directory) != store.record.directory_identity
            or _file_identity(lock) != store.record.lock_identity or os.fstat(lock).st_size > 4096):
        raise ValueError('Inhibit retained identity differs')
    linked_directory = open_existing_workspace(path)
    try:
        if _directory_identity(path, linked_directory) != store.record.directory_identity:
            raise ValueError('Inhibit directory was replaced')
    finally:
        os.close(linked_directory)
    linked = os.stat(LOCK_NAME, dir_fd=directory, follow_symlinks=False)
    retained = os.fstat(lock)
    if any(getattr(linked, field) != getattr(retained, field) for field in ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size')):
        raise ValueError('Inhibit fixed lock was replaced')
    content = _read(directory, STORE_NAME, store.record.metadata_identity)
    if (hashlib.sha256(content).hexdigest() != store.content_sha256
            or NativeInhibitStoreRecord.model_validate(_json(content)) != store.record):
        raise ValueError('Inhibit store metadata differs from external binding')
    os.lseek(directory, 0, os.SEEK_SET)
    if not set(os.listdir(directory)) <= {LOCK_NAME, STORE_NAME, RECEIPT_NAME}:
        raise ValueError('Unknown or partial inhibit store contents')


def _open(store, *, writable=False):
    if type(store) is not NativeInhibitStore:
        raise ValueError('Exact external inhibit store binding required')
    directory = open_existing_workspace(_path(store.record.directory))
    lock = None
    try:
        access = os.O_RDWR if writable else os.O_RDONLY
        lock = os.open(LOCK_NAME, access | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        _verify(store, directory, lock)
        return directory, lock
    except BaseException:
        if lock is not None:
            os.close(lock)
        os.close(directory)
        raise


def _absent(store, directory, lock):
    metadata = os.fstat(lock)
    if (metadata.st_size != 0 or metadata.st_mtime_ns != store.record.initial_lock_mtime_ns
            or metadata.st_ctime_ns != store.record.initial_lock_ctime_ns):
        raise ValueError('Native inhibited or lock publication altered; no native admission')
    try:
        os.stat(RECEIPT_NAME, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError('Native inhibited or publication partial; no native admission')


def _lock(descriptor, mode):
    try:
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('Native inhibit lock contention; no wait or process effect') from None


def provision_native_inhibit(directory: Path, *, scope_id: str) -> NativeInhibitStore:
    path = _path(str(directory))
    if type(scope_id) is not str or re.fullmatch(r'native-[a-z0-9][a-z0-9-]{0,63}', scope_id) is None:
        raise ValueError('Explicit native inhibit scope required')
    parent = open_existing_workspace(path.parent)
    descriptors = []
    try:
        parent_identity = _directory_identity(path.parent, parent)
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        root = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        descriptors.append(root)
        directory_identity = _directory_identity(path, root)
        lock = os.open(LOCK_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root)
        descriptors.append(lock)
        metadata = os.open(STORE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root)
        descriptors.append(metadata)
        record = NativeInhibitStoreRecord(scope_id=scope_id, directory=str(path), directory_identity=directory_identity,
                                          lock_identity=_file_identity(lock), metadata_identity=_file_identity(metadata),
                                          initial_lock_mtime_ns=os.fstat(lock).st_mtime_ns,
                                          initial_lock_ctime_ns=os.fstat(lock).st_ctime_ns)
        content = (canonical(record.model_dump(mode='json')) + '\n').encode()
        _write(metadata, content)
        os.fsync(lock)
        os.fsync(root)
        os.fsync(parent)
        store = NativeInhibitStore(record=record, content_sha256=hashlib.sha256(content).hexdigest())
        if _directory_identity(path.parent, parent) != parent_identity:
            raise ValueError('Inhibit provision parent changed')
        check_parent = open_existing_workspace(path.parent)
        try:
            if _directory_identity(path.parent, check_parent) != parent_identity:
                raise ValueError('Inhibit provision parent was replaced')
        finally:
            os.close(check_parent)
        _verify(store, root, lock)
        _absent(store, root, lock)
        return store
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(parent)


class NativeInhibitLease:
    def __init__(self, store, directory, descriptor):
        self.store = store
        self._directory = directory
        self.fd = descriptor
        self._closed = False

    def assert_native_allowed(self):
        if self._closed:
            raise ValueError('Native lifetime lease is closed')
        _verify(self.store, self._directory, self.fd)
        _absent(self.store, self._directory, self.fd)

    def close(self):
        if not self._closed:
            self._closed = True
            os.close(self.fd)
            os.close(self._directory)

    def __enter__(self):
        self.assert_native_allowed()
        return self

    def __exit__(self, *_arguments):
        self.close()


def acquire_native_lease(store: NativeInhibitStore) -> NativeInhibitLease:
    directory, lock = _open(store)
    try:
        _lock(lock, fcntl.LOCK_SH)
        _verify(store, directory, lock)
        _absent(store, directory, lock)
        os.set_inheritable(lock, True)
        return NativeInhibitLease(store, directory, lock)
    except BaseException:
        os.close(lock)
        os.close(directory)
        raise


def _request_current(request):
    current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    current_time = time.clock_gettime(time.CLOCK_BOOTTIME)
    if request.boot_id != current_boot or not request.issued_boottime <= current_time < request.expires_boottime:
        raise ValueError('Inhibit publication request boot/time is not current')


def publish_native_inhibit(store: NativeInhibitStore, request: NativeInhibitRequest) -> NativeInhibitReceipt:
    if type(request) is not NativeInhibitRequest or request.scope_id != store.record.scope_id:
        raise ValueError('Exact scoped inhibit request required')
    request = NativeInhibitRequest.model_validate(request.model_dump())
    directory, lock = _open(store, writable=True)
    try:
        _lock(lock, fcntl.LOCK_EX)
        _verify(store, directory, lock)
        _absent(store, directory, lock)
        _request_current(request)
        for path, expected in request.source_files.items():
            _request_current(request)
            read_pinned_file(Path(path), expected, limit=SOURCE_FILE_LIMIT)
        for path, expected in request.config_files.items():
            _request_current(request)
            read_pinned_file(Path(path), expected, private=True)
        receipt = NativeInhibitReceipt(store=store, request=request,
                                       request_sha256=digest(request.model_dump(mode='json')), recorded_at=now())
        content = (canonical(receipt.model_dump(mode='json')) + '\n').encode()
        if len(content) > 262144:
            raise ValueError('Inhibit receipt exceeds bounded durable readback')
        intent = NativeInhibitIntent(store_content_sha256=store.content_sha256,
                                     request_sha256=receipt.request_sha256,
                                     receipt_sha256=hashlib.sha256(content).hexdigest())
        _request_current(request)
        _verify(store, directory, lock)
        _absent(store, directory, lock)
        _request_current(request)
        _write(lock, (canonical(intent.model_dump(mode='json')) + '\n').encode())
        descriptor = os.open(RECEIPT_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            _write(descriptor, content)
        finally:
            os.close(descriptor)
        os.fsync(directory)
        _verify(store, directory, lock)
        if NativeInhibitReceipt.model_validate(_json(_read(directory, RECEIPT_NAME))) != receipt:
            raise ValueError('Inhibit publication readback differs')
        if NativeInhibitIntent.model_validate(_json(_read(directory, LOCK_NAME))) != intent:
            raise ValueError('Inhibit publication intent readback differs')
        _request_current(request)
        return receipt
    finally:
        os.close(lock)
        os.close(directory)


def read_native_inhibit(store: NativeInhibitStore) -> NativeInhibitReceipt | None:
    """None means only no receipt observed; it is never native admission or worker/GPU absence proof."""
    directory, lock = _open(store)
    try:
        _lock(lock, fcntl.LOCK_SH)
        _verify(store, directory, lock)
        marker = _read(directory, LOCK_NAME, store.record.lock_identity)
        if not marker:
            _absent(store, directory, lock)
            _verify(store, directory, lock)
            return None
        intent = NativeInhibitIntent.model_validate(_json(marker))
        content = _read(directory, RECEIPT_NAME)
        receipt = NativeInhibitReceipt.model_validate(_json(content))
        if (receipt.store != store or intent.store_content_sha256 != store.content_sha256
                or intent.request_sha256 != receipt.request_sha256
                or intent.receipt_sha256 != hashlib.sha256(content).hexdigest()):
            raise ValueError('Inhibit receipt external store binding differs')
        _verify(store, directory, lock)
        return receipt
    finally:
        os.close(lock)
        os.close(directory)
