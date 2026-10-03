import math
import os
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest, now
from .lifecycle import ProcessIdentity
from .native_handover import NativeControlSnapshot
from .shared_desktop_host import SharedServiceBinding, SystemdSharedDesktopTransport
from .shared_desktop_plan import SharedSHA, read_pinned_file


ExclusionFiles = Annotated[dict[str, SharedSHA], Field(min_length=1, max_length=512)]
ExclusionClock = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _file_map(files, expected):
    for name in files:
        path = Path(name)
        if (not path.is_absolute() or str(path) != name or '..' in path.parts
                or any(ord(character) < 32 for character in name)):
            raise ValueError('Exclusion source/config paths must be canonical and absolute')
    if digest(files) != expected:
        raise ValueError('Exclusion file-map identity differs')


class NativeExclusionLegacy(TypedModel):
    manager_session: str = Field(pattern=r'^app-[a-f0-9]{32}$')
    original_state_sha256: SharedSHA
    stopped_state_sha256: SharedSHA
    controller: NativeControlSnapshot
    supervisor: ProcessIdentity
    backend: ProcessIdentity
    observed_workers: list[ProcessIdentity] = Field(max_length=62)

    @model_validator(mode='after')
    def distinct_processes(self):
        processes = [self.supervisor, self.backend, *self.observed_workers]
        if len({process.pid for process in processes}) != len(processes):
            raise ValueError('Retired native process identities must be distinct')
        if len({(process.boot_id, process.pid_namespace, process.uid) for process in processes}) != 1:
            raise ValueError('Retired native process identities must share boot, namespace and owner')
        if self.controller.owner != 'AGENT' or self.controller.status != 'running':
            raise ValueError('Legacy handover requires the original idle AGENT controller')
        return self


class NativeExclusionEvidence(TypedModel):
    schema_version: Literal['aos.native-exclusion.v1'] = 'aos.native-exclusion.v1'
    profile: Literal['shared-only-runtime-v1'] = 'shared-only-runtime-v1'
    scope: Literal['repository-managed-entrypoints'] = 'repository-managed-entrypoints'
    recorded_at: str = Field(min_length=1, max_length=64)
    request_id: str = Field(pattern=r'^native-maintenance-[a-f0-9]{32}$')
    principal: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    owner_uid: int = Field(ge=1)
    boot_id: str = Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    issued_boottime: ExclusionClock
    expires_boottime: ExclusionClock
    effective_deadline_boottime: ExclusionClock
    observed_boottime: ExclusionClock
    maintenance_request_sha256: SharedSHA
    maintenance_receipt_sha256: SharedSHA
    handover_sha256: SharedSHA
    shared_plan_sha256: SharedSHA
    candidate_manifest_sha256: SharedSHA
    candidate_patch_sha256: SharedSHA
    source_files: ExclusionFiles
    source_sha256: SharedSHA
    config_files: ExclusionFiles
    config_sha256: SharedSHA
    legacy: NativeExclusionLegacy
    shared_caller: SharedServiceBinding
    absence_observation_sha256: list[SharedSHA] = Field(min_length=2, max_length=2)
    native_admission_disabled: Literal[True] = True
    legacy_workers_absent: Literal[True] = True
    expiry_reopens_native: Literal[False] = False
    allocation_authority: Literal[False] = False
    shared_launch_authorized: Literal[False] = False
    gpu_release_verified: Literal[False] = False

    @model_validator(mode='after')
    def finite_bound_identity(self):
        _file_map(self.source_files, self.source_sha256)
        _file_map(self.config_files, self.config_sha256)
        if not (self.issued_boottime <= self.observed_boottime
                < self.effective_deadline_boottime <= self.expires_boottime
                <= self.issued_boottime + 900):
            raise ValueError('Exclusion evidence must retain and only shorten the original finite interval')
        retired = [self.legacy.supervisor, self.legacy.backend, *self.legacy.observed_workers]
        caller = self.shared_caller.process
        if (any(process.boot_id != self.boot_id or process.uid != self.owner_uid for process in [*retired, caller])
                or caller.pid in {process.pid for process in retired}
                or caller.pid_namespace != self.legacy.supervisor.pid_namespace):
            raise ValueError('Legacy and shared caller identities must be separate on the same owned boot')
        return self


def _clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _fresh(request, deadline):
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or Path('/proc/sys/kernel/random/boot_id').read_text().strip() != request.boot_id
            or request.owner_uid != os.getuid()
            or not request.issued_boottime <= _clock() < min(request.expires_boottime, deadline)):
        raise ValueError('Native exclusion boot, principal owner or original deadline is unavailable')
