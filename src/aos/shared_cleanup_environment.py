import json
import math
import os
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from .bounded_process import run_bounded
from .contracts import TypedModel, canonical, digest
from .desktop import DOCKER
from .lifecycle import ProcessIdentity, process_identity
from .scientist_shared_launch import ScientistSharedLaunchAdapter
from .shared_desktop_cleanup import ReviewedDockerDaemon, SharedDesktopCleanupObserver, _boottime
from .shared_desktop_plan import (
    SharedSHA, load_activation, load_plan, read_pinned_file, write_new_private_file,
)
from .shared_desktop_provision import PROVISION_NAME, load_provision, verify_launch_intent
from .workspace_identity import WorkspaceIdentity


class SharedCleanupEnvironment(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    preparation_only: Literal[True] = True
    execution_authorized: Literal[False] = False
    cleanup_authorized: Literal[False] = False
    plan_sha256: SharedSHA
    provision_sha256: SharedSHA
    app_session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    workspace: str = Field(min_length=1, max_length=4096)
    workspace_identity: WorkspaceIdentity
    manager: ProcessIdentity
    mount_namespace: int = Field(gt=0)
    cgroup_namespace: int = Field(gt=0)
    docker_endpoint: Literal['unix:///var/run/docker.sock'] = 'unix:///var/run/docker.sock'
    daemon_id: str = Field(pattern=r'^[A-Za-z0-9:_-]{1,128}$')
    socket_device: int = Field(ge=0)
    socket_inode: int = Field(gt=0)
    captured_boottime: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def original_scope(self):
        path = Path(self.workspace)
        if (not path.is_absolute() or str(path) != self.workspace or '..' in path.parts
                or path.name != 'workspace' or path.parent.name != self.app_session
                or self.workspace_identity.path_sha256 != digest({'workspace': self.workspace})
                or self.workspace_identity.owner_uid != self.manager.uid):
            raise ValueError('Cleanup environment requires the exact same-owner provision workspace')
        return self

    def daemon(self):
        return ReviewedDockerDaemon(self.daemon_id, self.socket_device, self.socket_inode)


def _remaining(deadline, clock):
    current = clock()
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(current) not in (int, float) or not math.isfinite(current)
            or not 0 < deadline - current <= 10):
        raise ValueError('Cleanup environment requires the original finite BOOTTIME deadline')
    return min(2.0, deadline - current)


def _environment(deadline, *, runner, clock):
    _remaining(deadline, clock)
    manager = process_identity(os.getpid())
    mount_namespace = os.stat('/proc/self/ns/mnt').st_ino
    cgroup_namespace = os.stat('/proc/self/ns/cgroup').st_ino
    socket = os.stat('/var/run/docker.sock', follow_symlinks=False)
    if not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != 0:
        raise ValueError('Cleanup environment requires the original root-owned fixed Docker socket')
    result = runner([*DOCKER, 'info', '--format', '{{.ID}}'], input=b'',
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
        timeout=_remaining(deadline, clock), max_output=1024)
    _remaining(deadline, clock)
    if result.returncode != 0 or result.stderr:
        raise ValueError('Original Docker daemon observation failed')
    daemon = ReviewedDockerDaemon(result.stdout.decode('ascii').strip(), socket.st_dev, socket.st_ino)
    return {'manager': manager, 'mount_namespace': mount_namespace, 'cgroup_namespace': cgroup_namespace,
            'daemon_id': daemon.daemon_id, 'socket_device': daemon.socket_device, 'socket_inode': daemon.socket_inode}


def capture_cleanup_environment(plan, provision_sha256, *, deadline, runner=run_bounded, clock=_boottime):
    first = _environment(deadline, runner=runner, clock=clock)
    provision = load_provision(plan, Path(plan.session_directory) / PROVISION_NAME, provision_sha256,
                              current_boot_id=first['manager'].boot_id, require_pristine=True)
    second = _environment(deadline, runner=runner, clock=clock)
    if first != second or first['manager'].uid != os.getuid():
        raise ValueError('Original cleanup environment changed during preparation')
    repeated = load_provision(plan, Path(plan.session_directory) / PROVISION_NAME, provision_sha256,
                             current_boot_id=first['manager'].boot_id, require_pristine=True)
    _remaining(deadline, clock)
    if repeated != provision:
        raise ValueError('Provision changed during cleanup environment preparation')
    return SharedCleanupEnvironment(plan_sha256=plan.plan_sha256(), provision_sha256=provision_sha256,
        app_session=plan.app_session, workspace=plan.workspace, workspace_identity=provision.workspace_identity,
        captured_boottime=clock(), **first)


def write_cleanup_environment(path, candidate):
    candidate = SharedCleanupEnvironment.model_validate(candidate.model_dump(), strict=True)
    path = Path(path)
    manager_base = Path(candidate.workspace).parent.parent
    if path == manager_base or manager_base in path.parents:
        raise ValueError('Cleanup review must remain outside the pristine manager/session scope')
    write_new_private_file(path, canonical(candidate.model_dump(mode='json')).encode())


def load_cleanup_environment(path, expected_sha256, *, plan, activation, provision_sha256):
    path = Path(path)
    if activation.config_files.get(str(path)) != expected_sha256:
        raise ValueError('Original cleanup environment must be pinned in the activation config closure')
    content = read_pinned_file(path, expected_sha256, limit=16384, private=True)
    candidate = SharedCleanupEnvironment.model_validate(json.loads(content), strict=True)
    if content != canonical(candidate.model_dump(mode='json')).encode():
        raise ValueError('Cleanup environment requires exact canonical bytes without duplicate fields')
    if (candidate.plan_sha256 != plan.plan_sha256() or activation.plan_sha256 != candidate.plan_sha256
            or candidate.provision_sha256 != provision_sha256 or candidate.app_session != plan.app_session
            or candidate.workspace != plan.workspace or candidate.manager.boot_id != activation.boot_id
            or candidate.captured_boottime > activation.issued_monotonic):
        raise ValueError('Cleanup environment differs from its original pre-activation scope')
    provision = load_provision(plan, Path(plan.session_directory) / PROVISION_NAME, provision_sha256,
                              current_boot_id=candidate.manager.boot_id, require_pristine=False)
    if candidate.workspace_identity != provision.workspace_identity:
        raise ValueError('Cleanup environment workspace differs from original provision')
    return candidate


def verify_cleanup_launch_environment(path, expected_sha256, *, plan, activation, provision_sha256,
                                      deadline, runner=run_bounded, clock=_boottime):
    _remaining(deadline, clock)
    if not activation.issued_monotonic <= clock() < deadline <= activation.expires_monotonic:
        raise ValueError('Launch environment verification must remain inside original activation')
    candidate = load_cleanup_environment(path, expected_sha256, plan=plan,
                                         activation=activation, provision_sha256=provision_sha256)
    expected = {name: getattr(candidate, name) for name in (
        'manager', 'mount_namespace', 'cgroup_namespace', 'daemon_id', 'socket_device', 'socket_inode')}
    for observation in range(2):
        if _environment(deadline, runner=runner, clock=clock) != expected:
            raise ValueError('Reviewed original launch environment no longer matches')
    if load_cleanup_environment(path, expected_sha256, plan=plan, activation=activation,
                                provision_sha256=provision_sha256) != candidate:
        raise ValueError('Reviewed launch environment changed during readback')
    _remaining(deadline, clock)
    return candidate


def _deny_cleanup_scope(state, environment, deadline):
    raise ValueError('Independent original request, spawn fence and cleanup authority are unavailable')


class EnvironmentBoundSharedLaunchAdapter:
    """Trusted opt-in composition; preserves the existing authenticated claim protocol."""

    def __init__(self, adapter, path, expected_sha256, *, runner=run_bounded):
        if not isinstance(adapter, ScientistSharedLaunchAdapter):
            raise TypeError('Environment binding requires the reviewed Scientist launch adapter')
        self.adapter = adapter
        self.path = Path(path)
        self.expected_sha256 = expected_sha256
        self.runner = runner

    def _verify_environment(self, plan, activation):
        review = self.adapter.review
        deadline = min(self.adapter.clock() + 3.0, review.expires_boottime, activation.expires_monotonic)
        candidate = verify_cleanup_launch_environment(self.path, self.expected_sha256,
            plan=plan, activation=activation, provision_sha256=review.provision_sha256,
            deadline=deadline, runner=self.runner, clock=self.adapter.clock)
        if candidate.manager != review.manager or candidate.plan_sha256 != review.plan_sha256:
            raise ValueError('Cleanup environment differs from the original authenticated launch manager')

    def verify(self, plan, activation):
        self._verify_environment(plan, activation)
        if self.adapter.verify(plan, activation) is not None:
            raise ValueError('Authenticated launch verification must complete or raise')
        self._verify_environment(plan, activation)

    def claim(self, plan, activation, state):
        self._verify_environment(plan, activation)
        if self.adapter.claim(plan, activation, state) is not None:
            raise ValueError('Authenticated launch must confirm a fresh claim or raise')
        self._verify_environment(plan, activation)


class PinnedSharedDesktopCleanupObserver:
    """Reconstruct original physical pins from reviewed config, never recapture them after failure."""

    def __init__(self, path, expected_sha256, *, verify_scope=_deny_cleanup_scope,
                 runner=run_bounded, clock=_boottime):
        self.path = Path(path)
        self.expected_sha256 = expected_sha256
        self.verify_scope = verify_scope
        self.runner = runner
        self.clock = clock

    def observe(self, state, *, deadline):
        frozen = state.model_copy(deep=True)
        path, expected_sha256 = self.path, self.expected_sha256

        def original():
            _remaining(deadline, self.clock)
            if self.path != path or self.expected_sha256 != expected_sha256:
                raise ValueError('Original cleanup environment selection changed')
            plan = load_plan(Path(frozen.plan_path), frozen.plan_sha256)
            activation = load_activation(Path(frozen.activation_path), frozen.activation_sha256)
            candidate = load_cleanup_environment(path, expected_sha256, plan=plan,
                activation=activation, provision_sha256=frozen.provision_sha256)
            if (candidate.app_session != frozen.session or candidate.workspace != frozen.workspace
                    or candidate.workspace_identity != frozen.workspace_identity):
                raise ValueError('Cleanup target differs from original reviewed environment')
            intent = verify_launch_intent(plan, frozen.provision_sha256, frozen.activation_sha256,
                                          current_boot_id=candidate.manager.boot_id)
            if digest(intent.model_dump(mode='json')) != frozen.launch_intent_sha256:
                raise ValueError('Cleanup target differs from its durable original launch intent')
            return candidate

        environment = original()

        def authority(selected, original_deadline):
            if original() != environment:
                raise ValueError('Original cleanup environment changed')
            if self.verify_scope(selected, environment.model_copy(deep=True), original_deadline) is not None:
                raise ValueError('Original cleanup authority must complete or raise')
            if original() != environment:
                raise ValueError('Original cleanup environment changed after authority readback')

        observer = SharedDesktopCleanupObserver(environment.daemon(), mount_namespace=environment.mount_namespace,
            cgroup_namespace=environment.cgroup_namespace, verify_scope=authority, runner=self.runner, clock=self.clock)
        return observer.observe(frozen, deadline=deadline)
