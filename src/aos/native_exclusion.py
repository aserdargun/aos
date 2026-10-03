import math
import os
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from . import local_app
from .contracts import REPO_ROOT, TypedModel, canonical, digest, now
from .lifecycle import ProcessIdentity
from .native_handover import NativeControlSnapshot, NativeHandoverPreview, _json
from .native_maintenance import (NativeAbsenceObservation, NativeMaintenanceRequest,
                                 NativeMaintenanceStore, observe_native_absence,
                                 read_native_maintenance_receipt)
from .shared_desktop_host import SharedServiceBinding, SystemdSharedDesktopTransport, run_bounded
from .shared_desktop_plan import SharedDesktopPlan, SharedSHA, read_pinned_file


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


class _NativeExclusionChecks:
    def __init__(self, store, request, receipt_sha256, *,
                 legacy_state_path, candidate_manifest_path, candidate_patch_path,
                 shared_plan_path):
        self.store = NativeMaintenanceStore.model_validate_json(canonical(store.model_dump(mode='json')))
        self.request = NativeMaintenanceRequest.model_validate_json(canonical(request.model_dump(mode='json')))
        self.request_sha256 = digest(self.request.model_dump(mode='json'))
        if self.store != self.request.store:
            raise ValueError('Native exclusion maintenance store differs from the original request')
        self.receipt_sha256 = self._sha(receipt_sha256)
        self.legacy_state_path = self._path(legacy_state_path)
        self.candidate_manifest_path = self._path(candidate_manifest_path)
        self.candidate_patch_path = self._path(candidate_patch_path)
        self.shared_plan_path = self._path(shared_plan_path)
        if (self.legacy_state_path != Path(self.store.record.directory).parent / 'current.json'
                or self.candidate_manifest_path != REPO_ROOT / 'scripts/shared-only-runtime-v1/manifest.json'
                or self.candidate_patch_path != REPO_ROOT / 'scripts/shared-only-runtime-v1/source.patch.txt'):
            raise ValueError('Explicit legacy manager and recipe paths differ from the reviewed maintenance scope')

    def _bound_deadline(self, expected_handover_sha256, expected_shared_plan_sha256, deadline):
        _fresh(self.request, deadline)
        if (self._sha(expected_handover_sha256) != self.request.handover_sha256
                or self._sha(expected_shared_plan_sha256) != self.request.shared_plan_sha256):
            raise ValueError('Exclusion verification cannot replay maintenance for another handover or shared plan')
        return min(float(deadline), self.request.expires_boottime)

    @staticmethod
    def _path(value):
        name = str(value)
        _file_map({name: '0' * 64}, digest({name: '0' * 64}))
        return Path(name)

    @staticmethod
    def _sha(value):
        if type(value) is not str or len(value) != 64 or any(character not in '0123456789abcdef' for character in value):
            raise ValueError('Exact reviewed raw SHA-256 is required')
        return value

    def _read_receipt(self, deadline):
        _fresh(self.request, deadline)
        receipt, raw_sha = read_native_maintenance_receipt(self.store, self.request_sha256)
        if (raw_sha != self.receipt_sha256 or receipt.request != self.request
                or receipt.request_sha256 != self.request_sha256):
            raise ValueError('Exact succeeded maintenance request and raw receipt are required')
        _fresh(self.request, deadline)
        return receipt

    def _read_pin(self, path, checksum, deadline, *, private=False):
        _fresh(self.request, deadline)
        content = read_pinned_file(path, checksum, limit=64 * 1024 * 1024, private=private)
        _fresh(self.request, deadline)
        return content

    def _promoted_files(self, receipt, deadline):
        request = self.request
        manifest = _json(self._read_pin(self.candidate_manifest_path, request.candidate_manifest_sha256, deadline))
        self._read_pin(self.candidate_patch_path, request.candidate_patch_sha256, deadline)
        entries = manifest.get('files')
        preserved = manifest.get('preserved_files')
        if (manifest.get('profile') != 'shared-only-runtime-v1'
                or manifest.get('patch_sha256') != request.candidate_patch_sha256
                or type(entries) is not list or len(entries) != 41
                or type(preserved) is not dict or len(preserved) != 13):
            raise ValueError('Complete reviewed shared-only source promotion required')
        expected = dict(request.source_files)
        selected = set()
        for entry in entries:
            relative = entry['path']
            path = self._path(REPO_ROOT / relative)
            if (not path.is_relative_to(REPO_ROOT) or relative in selected
                    or str(path.relative_to(REPO_ROOT)) != relative
                    or request.source_files.get(str(path)) != self._sha(entry['before_sha256'])):
                raise ValueError('Original source candidate before-map is incomplete or ambiguous')
            selected.add(relative)
            expected[str(path)] = self._sha(entry['after_sha256'])
        for relative, checksum in preserved.items():
            path = self._path(REPO_ROOT / relative)
            if (not path.is_relative_to(REPO_ROOT) or str(path.relative_to(REPO_ROOT)) != relative
                    or relative in selected or request.source_files.get(str(path)) != self._sha(checksum)):
                raise ValueError('Preserved broker and CPU source closure differs')
        helpers = ('src/aos/native_maintenance.py', 'src/aos/native_exclusion.py', 'scripts/native_maintenance.py',
                   'schemas/native_maintenance_store.schema.json', 'schemas/native_maintenance_request.schema.json',
                   'schemas/native_maintenance_receipt.schema.json', 'schemas/native_exclusion_evidence.schema.json',
                   'src/aos/native_handover.py', 'src/aos/lifecycle.py', 'src/aos/workspace_identity.py',
                   'src/aos/shared_desktop_plan.py', 'src/aos/contracts.py', 'src/aos/shared_desktop_host.py',
                   'src/aos/bounded_process.py', 'src/aos/native_inhibit.py', 'src/aos/shared_desktop_provision.py')
        required = {str(REPO_ROOT / relative) for relative in [*selected, *preserved, *helpers]}
        required.update((str(self.candidate_manifest_path), str(self.candidate_patch_path)))
        if (not required.issubset(expected)
                or expected[str(self.candidate_manifest_path)] != request.candidate_manifest_sha256
                or expected[str(self.candidate_patch_path)] != request.candidate_patch_sha256
                or receipt.source_files != expected or receipt.source_sha256 != digest(expected)
                or receipt.config_files != request.config_files or receipt.config_sha256 != request.config_sha256):
            raise ValueError('Promoted receipt source/config closure is incomplete or differs')
        return expected, required

    def _read_scope(self, receipt, files, required, deadline, plan_sha):
        for name, checksum in files.items():
            self._read_pin(Path(name), checksum, deadline)
        for name, checksum in self.request.config_files.items():
            self._read_pin(Path(name), checksum, deadline)
        preview = NativeHandoverPreview.model_validate(_json(self._read_pin(
            Path(self.request.handover_path), self.request.handover_sha256, deadline, private=True)))
        if (preview.expected_session != self.request.expected_session
                or preview.manager_base != str(self.legacy_state_path.parent)
                or preview.current_state_sha256 != receipt.original_state_sha256
                or preview.supervisor != receipt.supervisor or preview.backend != receipt.backend
                or preview.control != receipt.controller or not preview.snapshot_continuity_verified
                or not preview.local_idle_observed):
            raise ValueError('Original reviewed handover snapshot differs from exact maintenance evidence')
        state = local_app.LocalAppState.model_validate(_json(self._read_pin(
            self.legacy_state_path, receipt.stopped_state_sha256, deadline, private=True)))
        if (state.phase != 'stopped' or state.mode != 'real' or state.project is not None
                or state.session != self.request.expected_session or state.supervisor != receipt.supervisor
                or state.backend != receipt.backend or state.token_name is not None):
            raise ValueError('Exact stopped original legacy manager state required')
        raw_plan_sha = self.request.config_files.get(str(self.shared_plan_path))
        if raw_plan_sha is None:
            raise ValueError('Shared plan raw file must be in the original reviewed configuration closure')
        plan = SharedDesktopPlan.model_validate(_json(self._read_pin(self.shared_plan_path, raw_plan_sha, deadline, private=True)))
        if (plan.plan_sha256() != plan_sha or plan.app_session == state.session
                or plan.template.manager_base == str(self.legacy_state_path.parent)
                or not required.issubset(plan.template.source_files)):
            raise ValueError('Separate shared plan semantic identity, manager scope or promoted closure differs')
        for selected, original in ((plan.template.source_files, files),
                                   (plan.template.config_files, self.request.config_files)):
            for name, checksum in selected.items():
                if original.get(name) != checksum:
                    raise ValueError('Shared template source/config closure is absent or historical')
                self._read_pin(Path(name), checksum, deadline)
        return state

    def _observe_absence(self, receipt, deadline):
        _fresh(self.request, deadline)
        observed = observe_native_absence(self.request, receipt.retired_processes, deadline=deadline)
        observation = NativeAbsenceObservation.model_validate_json(canonical(observed.model_dump(mode='json')))
        if observation.boot_id != self.request.boot_id or observation.retired_processes != receipt.retired_processes:
            raise ValueError('Fresh native absence observation binding differs')
        _fresh(self.request, deadline)
        return observation


class NativeExclusionPrelaunchVerifier(_NativeExclusionChecks):
    def verify_prelaunch(self, expected_handover_sha256, expected_shared_plan_sha256, *, deadline):
        effective_deadline = self._bound_deadline(expected_handover_sha256, expected_shared_plan_sha256, deadline)
        receipt = self._read_receipt(effective_deadline)
        files, required = self._promoted_files(receipt, effective_deadline)
        self._read_scope(receipt, files, required, effective_deadline, expected_shared_plan_sha256)
        self._observe_absence(receipt, effective_deadline)
        self._read_scope(receipt, files, required, effective_deadline, expected_shared_plan_sha256)
        if self._read_receipt(effective_deadline) != receipt:
            raise ValueError('Immutable maintenance receipt changed during prelaunch verification')
        _fresh(self.request, effective_deadline)


class NativeExclusionReader(_NativeExclusionChecks):
    def __init__(self, store, request, receipt_sha256, expected_shared_caller, *,
                 legacy_state_path, candidate_manifest_path, candidate_patch_path,
                 shared_plan_path, transport=None):
        super().__init__(store, request, receipt_sha256, legacy_state_path=legacy_state_path,
            candidate_manifest_path=candidate_manifest_path, candidate_patch_path=candidate_patch_path,
            shared_plan_path=shared_plan_path)
        self.expected_shared_caller = SharedServiceBinding.model_validate_json(
            canonical(expected_shared_caller.model_dump(mode='json')))
        self.transport = transport

    def _shared_caller(self, deadline):
        _fresh(self.request, deadline)
        transport = self.transport
        if transport is None:
            def runner(command, **arguments):
                _fresh(self.request, deadline)
                remaining = min(deadline, self.request.expires_boottime) - _clock()
                arguments['timeout'] = min(arguments['timeout'], remaining)
                if arguments['timeout'] <= 0:
                    raise ValueError('Original native exclusion deadline elapsed before shared-unit observation')
                return run_bounded(command, **arguments)
            transport = SystemdSharedDesktopTransport(runner=runner)
        actual = transport.read()
        if actual != self.expected_shared_caller:
            raise ValueError('Actual dedicated shared service generation differs')
        _fresh(self.request, deadline)
        return actual

    def read_native_exclusion(self, expected_handover_sha256, expected_shared_plan_sha256, *, deadline):
        request = self.request
        effective_deadline = self._bound_deadline(expected_handover_sha256, expected_shared_plan_sha256, deadline)
        receipt = self._read_receipt(effective_deadline)
        caller = self._shared_caller(effective_deadline)
        files, required = self._promoted_files(receipt, effective_deadline)
        self._read_scope(receipt, files, required, effective_deadline, expected_shared_plan_sha256)
        observations = []
        for _iteration in range(2):
            observation = self._observe_absence(receipt, effective_deadline)
            observations.append(digest(observation.model_dump(mode='json')))
            if _iteration == 0:
                self._read_scope(receipt, files, required, effective_deadline, expected_shared_plan_sha256)
        self._read_scope(receipt, files, required, effective_deadline, expected_shared_plan_sha256)
        if self._read_receipt(effective_deadline) != receipt:
            raise ValueError('Immutable maintenance receipt changed during fresh exclusion observation')
        self._shared_caller(effective_deadline)
        _fresh(request, effective_deadline)
        evidence = NativeExclusionEvidence(recorded_at=now(), request_id=request.request_id,
            principal=request.principal, owner_uid=request.owner_uid, boot_id=request.boot_id,
            issued_boottime=request.issued_boottime, expires_boottime=request.expires_boottime,
            effective_deadline_boottime=effective_deadline, observed_boottime=_clock(),
            maintenance_request_sha256=self.request_sha256, maintenance_receipt_sha256=self.receipt_sha256,
            handover_sha256=request.handover_sha256, shared_plan_sha256=request.shared_plan_sha256,
            candidate_manifest_sha256=request.candidate_manifest_sha256, candidate_patch_sha256=request.candidate_patch_sha256,
            source_files=files, source_sha256=digest(files), config_files=request.config_files,
            config_sha256=digest(request.config_files),
            legacy=NativeExclusionLegacy(manager_session=request.expected_session,
                original_state_sha256=request.original_state_sha256, stopped_state_sha256=receipt.stopped_state_sha256,
                controller=receipt.controller, supervisor=receipt.supervisor, backend=receipt.backend,
                observed_workers=receipt.observed_workers), shared_caller=caller,
            absence_observation_sha256=observations)
        value = evidence.model_dump(mode='json')
        _fresh(request, effective_deadline)
        return value, digest(value)
