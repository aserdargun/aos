import os
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, digest, now
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .web_application import (Checksum, Key, WebApplicationProfile,
                              WebApplicationProfiles, canonical_origin,
                              readonly_resource_url_parts)


BrowserTool = Literal['browser.navigate', 'browser.snapshot', 'browser.fill', 'browser.click', 'browser.verify']


class WebTaskContract(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_key: Key
    entry_url: str = Field(min_length=1, max_length=2048)
    allowed_origins: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=16)
    tools: list[BrowserTool] = Field(min_length=2, max_length=5)
    max_pages: int = Field(ge=1, le=8)
    max_navigations: int = Field(ge=1, le=16)
    max_actions: int = Field(ge=1, le=16)
    max_seconds: int = Field(ge=1, le=900)
    verification_ref: Key
    approval: Literal['fresh_per_action'] = 'fresh_per_action'
    uncertain_write: Literal['stop_without_retry'] = 'stop_without_retry'
    network_access: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('allowed_origins')
    @classmethod
    def canonical_origins(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate_task_origin')
        for value in values:
            if canonical_origin(value)[0] != value:
                raise ValueError('noncanonical_task_origin')
        return sorted(values)

    @field_validator('tools')
    @classmethod
    def canonical_tools(cls, values):
        if (len(values) != len(set(values)) or 'browser.snapshot' not in values
                or 'browser.verify' not in values
                or not set(values).intersection({'browser.navigate', 'browser.fill', 'browser.click'})):
            raise ValueError('task_requires_observation_and_verification')
        return sorted(values)

    @field_validator('network_access', 'collection_authorized', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('task_cannot_authorize_network_or_collection')
        return value


class WebReadOnlyRoutePlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    routes: list[str] = Field(min_length=2, max_length=8)
    approval: Literal['fresh_per_action'] = 'fresh_per_action'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('routes')
    @classmethod
    def exact_canonical_routes(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate_readonly_route')
        for index, value in enumerate(values):
            if index == 0:
                WebApplicationProfile.entry_is_canonical(value)
            else:
                readonly_resource_url_parts(value)
        return values

    @field_validator('execution_authorized', 'collection_authorized', mode='before')
    @classmethod
    def no_authority(cls, value):
        if value is not False:
            raise ValueError('readonly_route_plan_cannot_grant_authority')
        return value


def plan_web_readonly_routes(profiles: WebApplicationProfiles, task: WebTaskContract,
                            routes: list[str]) -> WebReadOnlyRoutePlan:
    task = WebTaskContract.model_validate(task.model_dump())
    profile = profiles.get(task.profile_sha256)
    plan = WebReadOnlyRoutePlan(profile_sha256=task.profile_sha256,
                                task_sha256=digest(task.model_dump()), routes=routes)
    origin, local = canonical_origin(task.entry_url)
    if (profile.environment not in {'staging', 'production'} or local
            or task.task_key not in profile.task_keys or task.entry_url != profile.entry_url
            or origin not in task.allowed_origins
            or not set(task.allowed_origins).issubset(profile.allowed_origins)
            or 'browser.navigate' not in task.tools
            or len(plan.routes) > min(task.max_pages, task.max_navigations, task.max_actions)
            or plan.routes[0] != task.entry_url
            or any(canonical_origin(readonly_resource_url_parts(route)[0])[0] != origin
                   for route in plan.routes)):
        raise ValueError('readonly_routes_outside_confirmed_task')
    return plan


def verify_web_readonly_routes(profiles: WebApplicationProfiles, task: WebTaskContract,
                               plan: WebReadOnlyRoutePlan) -> WebReadOnlyRoutePlan:
    checked = WebReadOnlyRoutePlan.model_validate(plan.model_dump())
    if plan_web_readonly_routes(profiles, task, checked.routes) != checked:
        raise ValueError('readonly_route_plan_binding_changed')
    return checked


class WebRuntimePin(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    kind: Literal['docker_chromium_mcp'] = 'docker_chromium_mcp'
    transport: Literal['playwright_mcp'] = 'playwright_mcp'
    runtime_id: str = Field(pattern='^browser-[a-f0-9]{32}$')
    parent_runtime_id: str = Field(pattern='^desktop-[a-f0-9]{32}$')
    container_id: str = Field(pattern='^[a-f0-9]{64}$')
    image_id: str = Field(pattern='^sha256:[a-f0-9]{64}$')
    mcp_bundle_sha256: Checksum
    worker_sha256: Checksum
    runtime_digest: Checksum
    network_mode: Literal['none'] = 'none'
    display: Literal[':99'] = ':99'


class WebTaskAdmissionDraft(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    runtime_sha256: Checksum
    binding_sha256: Checksum
    task: WebTaskContract
    runtime: WebRuntimePin
    status: Literal['draft'] = 'draft'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    runtime_identity_verified: Literal[False] = False
    blockers: list[Literal['runtime_not_connected', 'network_policy_not_enforced',
                           'account_role_not_verified', 'task_verifier_not_registered']] = Field(min_length=4, max_length=4)

    @field_validator('execution_authorized', 'collection_authorized', 'runtime_identity_verified', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('draft_cannot_authorize_execution')
        return value

    @model_validator(mode='after')
    def exact_binding(self):
        task_sha256 = digest(self.task.model_dump())
        runtime_sha256 = digest(self.runtime.model_dump())
        binding_sha256 = digest({'profile_sha256': self.profile_sha256,
                                 'task_sha256': task_sha256, 'runtime_sha256': runtime_sha256})
        if (self.profile_sha256 != self.task.profile_sha256 or self.task_sha256 != task_sha256
                or self.runtime_sha256 != runtime_sha256 or self.binding_sha256 != binding_sha256
                or self.blockers != ['runtime_not_connected', 'network_policy_not_enforced',
                                     'account_role_not_verified', 'task_verifier_not_registered']):
            raise ValueError('web_binding_mismatch')
        return self


class WebRuntimeAttestation(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    binding_sha256: Checksum
    runtime_sha256: Checksum
    runtime_id: str = Field(pattern='^browser-[a-f0-9]{32}$')
    parent_runtime_id: str = Field(pattern='^desktop-[a-f0-9]{32}$')
    container_id: str = Field(pattern='^[a-f0-9]{64}$')
    observed_at: str
    status: Literal['observed_match'] = 'observed_match'
    runtime_identity_observed: Literal[True] = True
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    network_access_authorized: Literal[False] = False

    @field_validator('observed_at')
    @classmethod
    def utc_observation(cls, value):
        try:
            observed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError('invalid_attestation_time') from None
        if observed.tzinfo is None or observed.utcoffset() != timezone.utc.utcoffset(observed):
            raise ValueError('attestation_time_must_be_utc')
        return value

    @field_validator('execution_authorized', 'collection_authorized', 'network_access_authorized', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('attestation_cannot_grant_authority')
        return value

    @field_validator('runtime_identity_observed', mode='before')
    @classmethod
    def true_is_boolean(cls, value):
        if value is not True:
            raise ValueError('attestation_must_record_observation')
        return value


def bind_web_task(profiles: WebApplicationProfiles, task: WebTaskContract,
                  runtime: WebRuntimePin) -> WebTaskAdmissionDraft:
    task = WebTaskContract.model_validate_json(task.model_dump_json())
    runtime = WebRuntimePin.model_validate_json(runtime.model_dump_json())
    profile = profiles.get(task.profile_sha256)
    if (task.task_key not in profile.task_keys or task.entry_url != profile.entry_url
            or not set(task.allowed_origins).issubset(profile.allowed_origins)
            or canonical_origin(task.entry_url)[0] not in task.allowed_origins):
        raise ValueError('web_task_outside_profile')
    task_sha256 = digest(task.model_dump())
    runtime_sha256 = digest(runtime.model_dump())
    return WebTaskAdmissionDraft(profile_sha256=task.profile_sha256, task_sha256=task_sha256,
                                 runtime_sha256=runtime_sha256,
                                 binding_sha256=digest({'profile_sha256': task.profile_sha256,
                                                        'task_sha256': task_sha256,
                                                        'runtime_sha256': runtime_sha256}),
                                 task=task, runtime=runtime,
                                 blockers=['runtime_not_connected', 'network_policy_not_enforced',
                                           'account_role_not_verified', 'task_verifier_not_registered'])


def verify_web_task_binding(profiles: WebApplicationProfiles,
                            draft: WebTaskAdmissionDraft) -> WebTaskAdmissionDraft:
    checked = WebTaskAdmissionDraft.model_validate(draft.model_dump())
    if bind_web_task(profiles, checked.task, checked.runtime) != checked:
        raise ValueError('web_binding_does_not_match_profile')
    return checked


def attest_web_task_runtime(profiles: WebApplicationProfiles, draft: WebTaskAdmissionDraft,
                            runtime: DesktopMCPBrowserRuntime) -> WebRuntimeAttestation:
    checked = verify_web_task_binding(profiles, draft)
    if not isinstance(runtime, DesktopMCPBrowserRuntime):
        raise ValueError('mcp_runtime_required')
    runtime.owned_desktop()
    browser = runtime.status()
    desktop = runtime.desktop.status()
    bundle_pins, _payload = read_bundle(runtime.mcp_manifest)
    pinned = checked.runtime
    evidence = browser.get('isolation')
    if (browser.get('kind') != pinned.kind or browser.get('browser_transport') != pinned.transport
            or browser.get('runtime_id') != pinned.runtime_id
            or browser.get('parent_runtime_id') != pinned.parent_runtime_id
            or browser.get('container_id') != pinned.container_id
            or browser.get('image_id') != pinned.image_id
            or browser.get('runtime_digest') != pinned.runtime_digest
            or browser.get('worker_sha256') != pinned.worker_sha256
            or browser.get('browser_display') != 'desktop'
            or browser.get('running') is not True or browser.get('real_execution') is not True
            or browser.get('desktop') is not True or browser.get('network') is not False
            or desktop.get('kind') != 'docker_xfce' or desktop.get('running') is not True
            or desktop.get('runtime_id') != pinned.parent_runtime_id
            or desktop.get('container_id') != pinned.container_id
            or desktop.get('image_id') != pinned.image_id
            or desktop.get('network') is not False or desktop.get('workspace_identity') is None
            or desktop.get('lifecycle_ref') is None
            or not isinstance(evidence, dict) or evidence.get('ready') is not True
            or evidence.get('headed') is not True or evidence.get('display') != pinned.display
            or evidence.get('transport') != pinned.transport
            or evidence.get('bundle_sha256') != pinned.mcp_bundle_sha256
            or evidence.get('server_version') != PLAYWRIGHT_VERSION
            or evidence.get('chromium_sha256') != runtime.pins.get('chromium_sha256')
            or evidence.get('network_namespace') == os.readlink('/proc/self/ns/net')
            or evidence.get('home_visible') is not False
            or evidence.get('docker_socket_visible') is not False
            or runtime.pins.get('mcp', {}).get('bundle_sha256') != pinned.mcp_bundle_sha256
            or bundle_pins['bundle_sha256'] != pinned.mcp_bundle_sha256):
        raise ValueError('owned_mcp_runtime_identity_changed')
    runtime.owned_desktop()
    if runtime.process is None or runtime.process.poll() is not None:
        raise ValueError('owned_mcp_runtime_stopped')
    return WebRuntimeAttestation(binding_sha256=checked.binding_sha256,
                                 runtime_sha256=checked.runtime_sha256,
                                 runtime_id=pinned.runtime_id,
                                 parent_runtime_id=pinned.parent_runtime_id,
                                 container_id=pinned.container_id, observed_at=now())
