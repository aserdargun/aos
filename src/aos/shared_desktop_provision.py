import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .shared_desktop_plan import SharedDesktopPlan, read_pinned_file, read_private_file
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


PROVISION_NAME = 'shared-provision.json'
LAUNCH_INTENT_NAME = 'shared-launch-intent.json'
ProvisionSHA = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
ProvisionSession = Annotated[str, Field(pattern=r'^app-[a-f0-9]{32}$')]
ProvisionPath = Annotated[str, Field(min_length=1, max_length=4096)]
ProvisionBoot = Annotated[str, Field(pattern=r'^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$')]


class SharedDesktopProvision(TypedModel):
    """Initial filesystem-only observations; these flags never describe a later running runtime."""

    version: Literal['1'] = '1'
    preparation_only: Literal[True] = True
    execution_authorized: Literal[False] = False
    runtime_started: Literal[False] = False
    runtime_authority: Literal[False] = False
    plan_sha256: ProvisionSHA
    template_sha256: ProvisionSHA
    app_session: ProvisionSession
    boot_id: ProvisionBoot
    manager_base: ProvisionPath
    session_directory: ProvisionPath
    workspace: ProvisionPath
    database: ProvisionPath
    directory_mode: Literal['0700'] = '0700'
    manager_identity: WorkspaceIdentity
    session_identity: WorkspaceIdentity
    workspace_identity: WorkspaceIdentity
    database_absent: Literal[True] = True
    token_absent: Literal[True] = True
    socket_absent: Literal[True] = True
    lifecycle_absent: Literal[True] = True
    workspace_empty: Literal[True] = True

    @model_validator(mode='after')
    def consistent_scope(self):
        for value in (self.manager_base, self.session_directory, self.workspace, self.database):
            _path(value)
        directory = Path(self.manager_base) / self.app_session
        if (self.session_directory != str(directory) or self.workspace != str(directory / 'workspace')
                or self.database != str(directory / 'trajectory.sqlite3')):
            raise ValueError('Provision receipt paths differ from its exact app session scope')
        for path, identity in ((self.manager_base, self.manager_identity),
                               (self.session_directory, self.session_identity),
                               (self.workspace, self.workspace_identity)):
            if identity.path_sha256 != digest({'workspace': path}):
                raise ValueError('Provision directory identity differs from its canonical path')
        if len({identity.owner_uid for identity in (self.manager_identity, self.session_identity, self.workspace_identity)}) != 1:
            raise ValueError('Provision directories require one exact owner')
        return self

    def provision_sha256(self):
        return digest(self.model_dump(mode='json'))


class SharedDesktopLaunchIntent(TypedModel):
    version: Literal['1'] = '1'
    preparation_only: Literal[True] = True
    execution_authorized: Literal[False] = False
    runtime_started: Literal[False] = False
    runtime_authority: Literal[False] = False
    plan_sha256: ProvisionSHA
    app_session: ProvisionSession
    boot_id: ProvisionBoot
    provision_sha256: ProvisionSHA
    activation_sha256: ProvisionSHA


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != value
            or any(ord(character) < 32 for character in value)):
        raise ValueError('Provision requires canonical absolute paths without traversal')
    return path


def _boot(value):
    if type(value) is not str or re.fullmatch(r'[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}', value) is None:
        raise ValueError('Provision requires an explicit valid current boot identity')


def _plan(plan):
    return SharedDesktopPlan.model_validate(plan.model_dump(), strict=True)


def _identity(path, descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('Provision directories must be exact owned 0700 directories')
    return workspace_identity(Path(path), descriptor)


@contextmanager
def _scope(plan):
    descriptors = []
    try:
        manager = open_existing_workspace(Path(plan.template.manager_base))
        descriptors.append(manager)
        _identity(plan.template.manager_base, manager)
        session = os.open(plan.app_session, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=manager)
        descriptors.append(session)
        _identity(plan.session_directory, session)
        workspace = os.open('workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=session)
        descriptors.append(workspace)
        _identity(plan.workspace, workspace)
        yield manager, session, workspace
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_identities(plan, provision, descriptors):
    for path, expected, descriptor in (
        (plan.template.manager_base, provision.manager_identity, descriptors[0]),
        (plan.session_directory, provision.session_identity, descriptors[1]),
        (plan.workspace, provision.workspace_identity, descriptors[2]),
    ):
        _verify_directory(path, expected, descriptor)


def _verify_directory(path, expected, descriptor):
    if _identity(path, descriptor) != expected:
        raise ValueError('Provision directory device, inode, UID, mode or path changed')
    current = open_existing_workspace(Path(path))
    try:
        if _identity(path, current) != expected:
            raise ValueError('Provision directory path was replaced')
    finally:
        os.close(current)


def _directory_entries(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    return os.listdir(descriptor)


def _pristine(session, workspace):
    if _directory_entries(workspace) or set(_directory_entries(session)) != {'workspace', PROVISION_NAME}:
        raise ValueError('Provision must contain only its receipt and empty workspace; no runtime artifacts or launch retry')


def _write_new(parent, name, value):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
    with os.fdopen(descriptor, 'wb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(canonical(value.model_dump(mode='json')).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(parent)


def _decoded(content, model):
    def unique_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError('Duplicate provision document fields are forbidden')
            result[name] = value
        return result
    value = model.model_validate(json.loads(content, object_pairs_hook=unique_pairs), strict=True)
    if content != canonical(value.model_dump(mode='json')).encode():
        raise ValueError('Provision and launch documents require exact canonical bytes')
    return value


def provision_scope(plan: SharedDesktopPlan, *, current_boot_id: str) -> SharedDesktopProvision:
    """Allocate a fresh empty filesystem scope, not a runtime, capability or deadline."""
    plan = _plan(plan)
    _boot(current_boot_id)
    descriptors = []
    try:
        manager = open_existing_workspace(Path(plan.template.manager_base))
        descriptors.append(manager)
        manager_identity = _identity(plan.template.manager_base, manager)
        os.mkdir(plan.app_session, 0o700, dir_fd=manager)
        session = os.open(plan.app_session, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=manager)
        descriptors.append(session)
        session_identity = _identity(plan.session_directory, session)
        _verify_directory(plan.template.manager_base, manager_identity, manager)
        _verify_directory(plan.session_directory, session_identity, session)
        os.fsync(manager)
        os.mkdir('workspace', 0o700, dir_fd=session)
        workspace = os.open('workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=session)
        descriptors.append(workspace)
        captured_workspace = _identity(plan.workspace, workspace)
        os.fsync(workspace)
        os.fsync(session)
        if _directory_entries(workspace) or set(_directory_entries(session)) != {'workspace'}:
            raise ValueError('New provision scope is not empty; never adopt existing runtime artifacts')
        provision = SharedDesktopProvision(plan_sha256=plan.plan_sha256(), template_sha256=plan.template_sha256,
            app_session=plan.app_session, boot_id=current_boot_id, manager_base=plan.template.manager_base,
            session_directory=plan.session_directory, workspace=plan.workspace, database=plan.database,
            manager_identity=manager_identity, session_identity=session_identity,
            workspace_identity=captured_workspace)
        _verify_identities(plan, provision, descriptors)
        _write_new(session, PROVISION_NAME, provision)
        _pristine(session, workspace)
        _verify_identities(plan, provision, descriptors)
        return provision
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_provision(plan: SharedDesktopPlan, path: Path, expected_sha256: str, *,
                   current_boot_id: str, require_pristine: bool = True) -> SharedDesktopProvision:
    plan = _plan(plan)
    _boot(current_boot_id)
    if type(require_pristine) is not bool:
        raise ValueError('Pristine selection must be an explicit boolean')
    path = _path(str(path))
    if path != Path(plan.session_directory) / PROVISION_NAME:
        raise ValueError('Provision receipt must use its fixed selected session path')
    with _scope(plan) as descriptors:
        content = read_pinned_file(path, expected_sha256, limit=16384, private=True)
        provision = _decoded(content, SharedDesktopProvision)
        if (provision.plan_sha256 != plan.plan_sha256() or provision.template_sha256 != plan.template_sha256
                or provision.app_session != plan.app_session or provision.boot_id != current_boot_id
                or provision.manager_base != plan.template.manager_base
                or provision.session_directory != plan.session_directory or provision.workspace != plan.workspace
                or provision.database != plan.database):
            raise ValueError('Provision receipt differs from its exact plan, boot or filesystem scope')
        _verify_identities(plan, provision, descriptors)
        if require_pristine:
            _pristine(descriptors[1], descriptors[2])
        return provision


def claim_launch(plan: SharedDesktopPlan, provision: SharedDesktopProvision,
                 provision_sha256: str, activation_sha256: str, *,
                 current_boot_id: str) -> SharedDesktopLaunchIntent:
    plan = _plan(plan)
    _boot(current_boot_id)
    intent = SharedDesktopLaunchIntent(plan_sha256=plan.plan_sha256(), app_session=plan.app_session,
        boot_id=current_boot_id, provision_sha256=provision_sha256, activation_sha256=activation_sha256)
    selected = load_provision(plan, Path(plan.session_directory) / PROVISION_NAME, provision_sha256,
                              current_boot_id=current_boot_id)
    if selected != provision:
        raise ValueError('Launch claim differs from its exact private provision receipt')
    with _scope(plan) as descriptors:
        _verify_identities(plan, selected, descriptors)
        _pristine(descriptors[1], descriptors[2])
        _write_new(descriptors[1], LAUNCH_INTENT_NAME, intent)
        _verify_identities(plan, selected, descriptors)
    return intent


def verify_launch_intent(plan: SharedDesktopPlan, provision_sha256: str, activation_sha256: str, *,
                         current_boot_id: str) -> SharedDesktopLaunchIntent:
    plan = _plan(plan)
    _boot(current_boot_id)
    expected = SharedDesktopLaunchIntent(plan_sha256=plan.plan_sha256(), app_session=plan.app_session,
        boot_id=current_boot_id, provision_sha256=provision_sha256, activation_sha256=activation_sha256)
    load_provision(plan, Path(plan.session_directory) / PROVISION_NAME, provision_sha256,
                   current_boot_id=current_boot_id, require_pristine=False)
    observed = _decoded(read_private_file(Path(plan.session_directory) / LAUNCH_INTENT_NAME, 16384),
                        SharedDesktopLaunchIntent)
    if observed != expected:
        raise ValueError('Durable launch intent differs from the exact provision, activation or boot')
    return observed
