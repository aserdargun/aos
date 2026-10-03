import hashlib
import json
import math
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, digest
from .workspace_identity import open_existing_workspace


SHARED_DESKTOP_UNIT = 'swapp-aos-gpu-shared-desktop-default.service'
SHARED_DESKTOP_PROFILE = 'scientist-managed-shared-desktop-v1'
SharedSHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
SharedPath = Annotated[str, Field(min_length=1, max_length=4096)]
SharedSession = Annotated[str, Field(pattern=r'^app-[a-f0-9]{32}$')]
SharedFileMap = Annotated[dict[SharedPath, SharedSHA], Field(min_length=1, max_length=256)]


def _absolute_path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != value
            or any(ord(character) < 32 for character in value)):
        raise ValueError('Canonical absolute paths without traversal are required')
    return path


def _file_map(files, expected):
    for path in files:
        _absolute_path(path)
    if digest(files) != expected:
        raise ValueError('Reviewed file-map hash differs')


class SharedDesktopLimits(TypedModel):
    cpu_quota_percent: int = Field(ge=1, le=800)
    memory_max_bytes: int = Field(ge=268435456, le=17179869184)
    tasks_max: int = Field(ge=16, le=512)
    stop_timeout_seconds: int = Field(ge=1, le=30)


class SharedDesktopTemplate(TypedModel):
    version: Literal['1'] = '1'
    launcher_profile: Literal['scientist-managed-shared-desktop-v1'] = SHARED_DESKTOP_PROFILE
    caller_unit: Literal['swapp-aos-gpu-shared-desktop-default.service'] = SHARED_DESKTOP_UNIT
    origin: str = Field(pattern=r'^http://127\.0\.0\.1:[0-9]{4,5}$')
    manager_base: SharedPath
    project: str | None = Field(default=None, pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')
    session_root: SharedPath
    python_path: SharedPath
    python_sha256: SharedSHA
    python_real_path: SharedPath | None = None
    python_links: dict[SharedPath, str] = Field(default_factory=dict, max_length=8)
    launcher_path: SharedPath
    launcher_sha256: SharedSHA
    source_files: SharedFileMap
    source_sha256: SharedSHA
    config_files: SharedFileMap
    config_sha256: SharedSHA
    broker_socket: SharedPath
    broker_identity_sha256: SharedSHA | None = Field(description=(
        'Observed canonical ScientistServerGeneration digest, or explicit null when unobserved; '
        'never a configuration hash or an authorization wildcard. Activation always requires fresh authenticated proof.'))
    limits: SharedDesktopLimits
    native_fallback: Literal[False] = False

    @model_validator(mode='after')
    def reviewed_scope(self):
        for value in (self.manager_base, self.session_root, self.python_path, self.launcher_path, self.broker_socket):
            _absolute_path(value)
        port = int(self.origin.rsplit(':', 1)[1])
        if not 1024 <= port <= 65535 or self.origin != f'http://127.0.0.1:{port}':
            raise ValueError('Exact canonical unprivileged loopback origin is required')
        if self.session_root != self.manager_base:
            raise ValueError('Shared session root must equal the selected manager base')
        _file_map(self.source_files, self.source_sha256)
        _file_map(self.config_files, self.config_sha256)
        binary = self.python_real_path or self.python_path
        _absolute_path(binary)
        links, resolved = _python_link_chain(self.python_path, self.python_links)
        if resolved != binary or links and self.python_real_path is None:
            raise ValueError('Python invocation must match the complete explicit pinned symlink chain')
        if (self.source_files.get(binary) != self.python_sha256
                or self.source_files.get(self.launcher_path) != self.launcher_sha256
                or self.python_path == self.launcher_path):
            raise ValueError('Reviewed source closure must include exact Python and fixed launcher pins')
        environment = Path(self.python_path).parent.parent
        if environment.name in {'.venv', 'venv'} and str(environment / 'pyvenv.cfg') not in self.source_files:
            raise ValueError('Reviewed virtualenv source closure must include pyvenv.cfg')
        return self

    @property
    def port(self):
        return int(self.origin.rsplit(':', 1)[1])

    @property
    def url(self):
        return self.origin + '/ui/'


class SharedDesktopPlan(TypedModel):
    version: Literal['1'] = '1'
    template: SharedDesktopTemplate
    template_sha256: SharedSHA
    predecessor_session: SharedSession | None = None
    predecessor_identity_sha256: SharedSHA | None = None
    predecessor_snapshot_sha256: SharedSHA | None = Field(
        default=None, description='Audit-only raw observation hash, not an unchanged-state activation gate')
    app_session: SharedSession
    session_directory: SharedPath
    workspace: SharedPath
    database: SharedPath
    execution_authorized: Literal[False] = False

    @model_validator(mode='after')
    def inert_exact_scope(self):
        if digest(self.template.model_dump(mode='json')) != self.template_sha256:
            raise ValueError('Inert plan template hash differs')
        if (self.predecessor_session is None) != (self.predecessor_identity_sha256 is None):
            raise ValueError('Predecessor session and stable identity must be supplied together')
        if self.predecessor_session is None and self.predecessor_snapshot_sha256 is not None:
            raise ValueError('Absent predecessor cannot carry an audit snapshot')
        if self.app_session == self.predecessor_session:
            raise ValueError('New session cannot reuse predecessor identity')
        directory = Path(self.template.session_root) / self.app_session
        if (self.session_directory != str(directory) or self.workspace != str(directory / 'workspace')
                or self.database != str(directory / 'trajectory.sqlite3')):
            raise ValueError('Future private workspace and database must be scoped to the fresh app session')
        return self

    def plan_sha256(self):
        return digest(self.model_dump(mode='json'))


class SharedDesktopActivation(TypedModel):
    version: Literal['1'] = '1'
    plan_sha256: SharedSHA
    reviewed_launch_input_path: SharedPath
    reviewed_launch_input_sha256: SharedSHA
    source_files: SharedFileMap
    source_sha256: SharedSHA
    config_files: SharedFileMap
    config_sha256: SharedSHA
    boot_id: str = Field(pattern=r'^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    clock: Literal['CLOCK_BOOTTIME'] = 'CLOCK_BOOTTIME'
    issued_monotonic: float = Field(ge=0, description='CLOCK_BOOTTIME seconds, including suspend')
    expires_monotonic: float = Field(gt=0, description='CLOCK_BOOTTIME expiry; never renewed from an inert plan')
    capability_proof_sha256: SharedSHA
    source_proof_sha256: SharedSHA

    @model_validator(mode='after')
    def finite_reviewed_selection(self):
        _absolute_path(self.reviewed_launch_input_path)
        _file_map(self.source_files, self.source_sha256)
        _file_map(self.config_files, self.config_sha256)
        if self.config_files.get(self.reviewed_launch_input_path) != self.reviewed_launch_input_sha256:
            raise ValueError('Activation config closure must include its exact reviewed launch input')
        if not 0 < self.expires_monotonic - self.issued_monotonic <= 900:
            raise ValueError('Activation lifetime must be positive and at most 900 seconds')
        return self


def predecessor_identity_sha256(state: dict) -> str:
    if type(state) is not dict or state.get('version') not in {'1', '2'}:
        raise ValueError('Supported predecessor state version is required')
    common = ('version', 'session', 'mode', 'project', 'url', 'started_at')
    if any(field not in state for field in ('session', 'mode', 'url', 'started_at')):
        raise ValueError('Predecessor identity observation is incomplete')
    from .lifecycle import ProcessIdentity
    if state['version'] == '1':
        ProcessIdentity.model_validate(state.get('supervisor'), strict=True)
        if state.get('backend') is not None:
            ProcessIdentity.model_validate(state['backend'], strict=True)
        fields = (*common, 'supervisor', 'backend')
    else:
        binding = state.get('service_binding')
        if (type(binding) is not dict or binding.get('unit') != SHARED_DESKTOP_UNIT
                or state.get('mode') != 'shared'):
            raise ValueError('Shared predecessor requires its actual fixed service binding')
        ProcessIdentity.model_validate(binding.get('process'), strict=True)
        if any(state.get(field) is None for field in ('plan_sha256', 'activation_sha256')):
            raise ValueError('Shared predecessor requires exact plan and activation identities')
        fields = (*common, 'service_binding', 'plan_sha256', 'activation_sha256')
    if (type(state['session']) is not str or len(state['session']) != 36
            or not state['session'].startswith('app-')
            or any(character not in 'abcdef0123456789' for character in state['session'][4:])):
        raise ValueError('Predecessor app session is invalid')
    return digest({field: state.get(field) for field in fields})


def prepare_plan(template: SharedDesktopTemplate, *, predecessor: dict | None, new_session: str,
                 predecessor_snapshot_sha256: str | None = None) -> SharedDesktopPlan:
    """Record intended paths and reviewed selections without allocating runtime resources or authority."""
    template = SharedDesktopTemplate.model_validate(template.model_dump(), strict=True)
    directory = Path(template.session_root) / new_session
    return SharedDesktopPlan(template=template, template_sha256=digest(template.model_dump(mode='json')),
        predecessor_session=None if predecessor is None else predecessor.get('session'),
        predecessor_identity_sha256=None if predecessor is None else predecessor_identity_sha256(predecessor),
        predecessor_snapshot_sha256=predecessor_snapshot_sha256, app_session=new_session,
        session_directory=str(directory), workspace=str(directory / 'workspace'),
        database=str(directory / 'trajectory.sqlite3'))


def _read_file(path: Path, *, limit: int, private: bool) -> bytes:
    path = _absolute_path(str(path))
    if type(limit) is not int or not 1 <= limit <= 134217728:
        raise ValueError('File read limit must be between one byte and 128 MiB')
    parent = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit
                    or before.st_uid not in {0, os.getuid()} or stat.S_IMODE(before.st_mode) & 0o022):
                raise ValueError('Bounded non-writable reviewed regular file is required')
            if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
                raise ValueError('Private input must be owner-only 0600')
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b''.join(chunks)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(content) != before.st_size or len(content) > limit
                    or any(getattr(before, field) != getattr(current, field)
                           for current in (after, linked) for field in fields)):
                raise ValueError('Reviewed file changed during bounded read')
            return content
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def read_private_file(path: Path, limit: int = 262144) -> bytes:
    return _read_file(Path(path), limit=limit, private=True)


def read_pinned_file(path: Path, expected_sha256: str, *, limit: int = 16777216,
                     private: bool = False) -> bytes:
    content = _read_file(Path(path), limit=limit, private=private)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError('Reviewed raw file hash differs')
    return content


def _python_link_chain(path, links):
    current = _absolute_path(path)
    used = []
    while True:
        matched = next((parent for parent in reversed((current, *current.parents)) if str(parent) in links), None)
        if matched is None:
            break
        name = str(matched)
        if name in used or len(used) == 8:
            raise ValueError('Pinned Python symlink chain loops or exceeds eight links')
        _absolute_path(name)
        target = links[name]
        if (type(target) is not str or not 1 <= len(target) <= 4096
                or any(ord(character) < 32 for character in target)):
            raise ValueError('Pinned Python link requires its exact bounded raw target')
        used.append(name)
        suffix = current.relative_to(matched)
        current = Path(os.path.normpath(str(matched.parent / target / suffix)))
        _absolute_path(str(current))
    if set(used) != set(links):
        raise ValueError('Pinned Python link map has extra or missing links')
    return used, str(current)


def verify_python(template: SharedDesktopTemplate) -> None:
    links, resolved = _python_link_chain(template.python_path, template.python_links)
    for name in links:
        path = Path(name)
        parent = open_existing_workspace(path.parent)
        try:
            before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISLNK(before.st_mode) or before.st_uid not in {0, os.getuid()} or before.st_nlink != 1:
                raise ValueError('Pinned Python symlink identity differs')
            observed = os.readlink(path.name, dir_fd=parent)
            after = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_uid')
            if observed != template.python_links[name] or any(getattr(before, field) != getattr(after, field) for field in fields):
                raise ValueError('Pinned Python symlink target or identity changed')
        finally:
            os.close(parent)
    read_pinned_file(Path(resolved), template.python_sha256)


def write_new_private_file(path: Path, content: bytes) -> None:
    path = _absolute_path(str(path))
    if type(content) is not bytes or not 1 <= len(content) <= 262144:
        raise ValueError('Private serialized document must be bounded nonempty bytes')
    parent = open_existing_workspace(path.parent)
    try:
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('Existing owner-only 0700 output directory is required')
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def _load(path, expected_sha256, model):
    content = read_pinned_file(Path(path), expected_sha256, limit=262144, private=True)
    def unique_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError('Duplicate JSON object fields are forbidden')
            result[name] = value
        return result
    return model.model_validate(json.loads(content, object_pairs_hook=unique_pairs), strict=True)


def load_template(path: Path, expected_sha256: str) -> SharedDesktopTemplate:
    return _load(path, expected_sha256, SharedDesktopTemplate)


def load_plan(path: Path, expected_sha256: str) -> SharedDesktopPlan:
    return _load(path, expected_sha256, SharedDesktopPlan)


def load_activation(path: Path, expected_sha256: str) -> SharedDesktopActivation:
    return _load(path, expected_sha256, SharedDesktopActivation)


def verify_template_files(template: SharedDesktopTemplate) -> None:
    verify_python(template)
    for path, expected in template.source_files.items():
        read_pinned_file(Path(path), expected)
    for path, expected in template.config_files.items():
        read_pinned_file(Path(path), expected, private=True)


def _deny_activation(plan, activation):
    raise ValueError('Trusted shared-unit capability and original finite API authority verifier is unavailable')


def verify_activation(plan: SharedDesktopPlan, activation: SharedDesktopActivation, *,
                      current_boot_id: str, now_monotonic: float,
                      activation_verifier: Callable | None = None) -> None:
    """Integrity and finite freshness are necessary; only the trusted host hook can verify runtime authority."""
    plan = SharedDesktopPlan.model_validate(plan.model_dump(), strict=True)
    activation = SharedDesktopActivation.model_validate(activation.model_dump(), strict=True)
    if (type(now_monotonic) not in {float, int} or not math.isfinite(now_monotonic)
            or activation.boot_id != current_boot_id
            or not activation.issued_monotonic <= now_monotonic < activation.expires_monotonic):
        raise ValueError('Activation boot or finite monotonic interval is stale')
    if (activation.plan_sha256 != plan.plan_sha256()
            or activation.source_files != plan.template.source_files
            or activation.source_sha256 != plan.template.source_sha256):
        raise ValueError('Activation differs from the exact inert plan or reviewed source closure')
    verify_python(plan.template)
    for path, expected in activation.source_files.items():
        read_pinned_file(Path(path), expected)
    for path, expected in activation.config_files.items():
        read_pinned_file(Path(path), expected, private=True)
    verifier = activation_verifier or _deny_activation
    if verifier(plan.model_copy(deep=True), activation.model_copy(deep=True)) is not None:
        raise ValueError('Trusted activation verifier must complete or raise, never return an approval flag')
