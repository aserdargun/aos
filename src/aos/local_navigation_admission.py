"""Opt-in extra identity gate for the fixed networkless MCP navigation fixture."""

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .contracts import REPO_ROOT, TypedModel, digest
from .desktop_mcp import DesktopMCPBrowserRuntime
from .desktop_mcp_bundle import PLAYWRIGHT_VERSION, read_bundle
from .web_application import Checksum, WebApplicationProfiles


class SyntheticNavigationPin(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_key: Literal['synthetic-local-navigation']
    fixture_port: int = Field(strict=True, ge=1024, le=65535)
    parent_runtime_id: str = Field(pattern='^desktop-[a-f0-9]{32}$')
    container_id: str = Field(pattern='^[a-f0-9]{64}$')
    image_id: str = Field(pattern='^sha256:[a-f0-9]{64}$')
    mcp_bundle_sha256: Checksum
    worker_sha256: Checksum
    fixture_sha256: Checksum
    runtime_digest: Checksum
    network_mode: Literal['none'] = 'none'
    collection_authorized: Literal[False] = False

    @field_validator('collection_authorized', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('synthetic_navigation_cannot_collect')
        return value


class SyntheticStagingPin(SyntheticNavigationPin):
    task_key: Literal['synthetic-staging-workflow']


def _check_synthetic_profile(profiles: WebApplicationProfiles, pin, entry_path: str):
    profile = profiles.get(pin.profile_sha256)
    origin = f'http://127.0.0.1:{pin.fixture_port}'
    if (profile.environment != 'local_test' or pin.task_key not in profile.task_keys
            or profile.entry_url != origin + entry_path
            or profile.allowed_origins != [origin]):
        raise ValueError('synthetic_fixture_profile_scope_mismatch')
    return profile


def check_synthetic_navigation_profile(profiles: WebApplicationProfiles, pin: SyntheticNavigationPin):
    pin = SyntheticNavigationPin.model_validate(pin.model_dump())
    return _check_synthetic_profile(profiles, pin, '/start')


def check_synthetic_staging_profile(profiles: WebApplicationProfiles, pin: SyntheticStagingPin):
    pin = SyntheticStagingPin.model_validate(pin.model_dump())
    return _check_synthetic_profile(profiles, pin, '/app')


def check_synthetic_navigation_runtime(profiles: WebApplicationProfiles, pin: SyntheticNavigationPin,
                                       runtime: DesktopMCPBrowserRuntime) -> dict:
    check_synthetic_navigation_profile(profiles, pin)
    return _check_synthetic_runtime(pin, runtime, staging_workflow=False)


def check_synthetic_staging_runtime(profiles: WebApplicationProfiles, pin: SyntheticStagingPin,
                                    runtime: DesktopMCPBrowserRuntime) -> dict:
    check_synthetic_staging_profile(profiles, pin)
    return _check_synthetic_runtime(pin, runtime, staging_workflow=True)


def _check_synthetic_runtime(pin, runtime: DesktopMCPBrowserRuntime, *, staging_workflow: bool) -> dict:
    if not isinstance(runtime, DesktopMCPBrowserRuntime):
        raise ValueError('synthetic_fixture_requires_mcp_runtime')
    runtime.owned_desktop()
    browser = runtime.status()
    desktop = runtime.desktop.status()
    bundle, _payload = read_bundle(runtime.mcp_manifest)
    isolation = browser.get('isolation')
    if (browser.get('kind') != 'docker_chromium_mcp'
            or browser.get('browser_transport') != 'playwright_mcp'
            or browser.get('staging_workflow') is not staging_workflow
            or browser.get('fixture_request_guard') is not True
            or not runtime.valid_fixture_request_guard()
            or browser.get('parent_runtime_id') != pin.parent_runtime_id
            or browser.get('container_id') != pin.container_id
            or browser.get('image_id') != pin.image_id
            or browser.get('worker_sha256') != pin.worker_sha256
            or browser.get('fixture_sha256') != pin.fixture_sha256
            or browser.get('runtime_digest') != pin.runtime_digest
            or browser.get('network') is not False or browser.get('running') is not True
            or browser.get('real_execution') is not True or browser.get('desktop') is not True
            or browser.get('browser_display') != 'desktop'
            or desktop.get('kind') != 'docker_xfce' or desktop.get('running') is not True
            or desktop.get('runtime_id') != pin.parent_runtime_id
            or desktop.get('container_id') != pin.container_id
            or desktop.get('image_id') != pin.image_id
            or desktop.get('network') is not False
            or desktop.get('workspace_identity') is None or desktop.get('lifecycle_ref') is None
            or not isinstance(isolation, dict) or isolation.get('ready') is not True
            or isolation.get('headed') is not True or isolation.get('display') != ':99'
            or isolation.get('transport') != 'playwright_mcp'
            or isolation.get('staging_workflow') is not staging_workflow
            or isolation.get('bundle_sha256') != pin.mcp_bundle_sha256
            or isolation.get('server_version') != PLAYWRIGHT_VERSION
            or isolation.get('fixture_request_guard', {}).get('kind') != 'landlock_tcp_connect_v1'
            or isolation.get('fixture_request_guard', {}).get('verified') is not True
            or isolation.get('fixture_request_guard', {}).get('fixture_port') != pin.fixture_port
            or isolation.get('fixture_proxy_mode') is not True
            or isolation.get('page_request_gate') is not True
            or not isinstance(isolation.get('network_namespace'), str)
            or isolation.get('network_namespace') == os.readlink('/proc/self/ns/net')
            or isolation.get('chromium_sha256') != runtime.pins.get('chromium_sha256')
            or isolation.get('home_visible') is not False
            or isolation.get('docker_socket_visible') is not False
            or bundle['bundle_sha256'] != pin.mcp_bundle_sha256
            or runtime.pins.get('mcp', {}).get('bundle_sha256') != pin.mcp_bundle_sha256
            or runtime.process is None or runtime.process.poll() is not None):
        raise ValueError('synthetic_fixture_runtime_identity_mismatch')
    runtime.owned_desktop()
    return {'schema_version': '1.0', 'status': 'synthetic_runtime_match_only',
            'profile_sha256': pin.profile_sha256, 'task_key': pin.task_key,
            'parent_runtime_id': pin.parent_runtime_id, 'browser_runtime_id': browser['runtime_id'],
            'container_id': pin.container_id, 'runtime_digest': pin.runtime_digest,
            'profile_to_origin_bound': True, 'account_role_verified': False,
            'execution_authorized': False, 'collection_authorized': False}


def check_synthetic_navigation_preflight(profiles: WebApplicationProfiles, pin: SyntheticNavigationPin,
                                         desktop, mcp_manifest: Path) -> None:
    check_synthetic_navigation_profile(profiles, pin)
    candidate = DesktopMCPBrowserRuntime(desktop, mcp_manifest, fixture_request_guard=True,
                                         fixture_port=pin.fixture_port)
    _check_synthetic_preflight(pin, desktop, mcp_manifest, candidate)


def check_synthetic_staging_preflight(profiles: WebApplicationProfiles, pin: SyntheticStagingPin,
                                      desktop, mcp_manifest: Path) -> None:
    check_synthetic_staging_profile(profiles, pin)
    candidate = DesktopMCPBrowserRuntime(desktop, mcp_manifest, fixture_request_guard=True,
                                         staging_workflow=True, fixture_port=pin.fixture_port)
    _check_synthetic_preflight(pin, desktop, mcp_manifest, candidate)


def _check_synthetic_preflight(pin, desktop, mcp_manifest: Path, candidate) -> None:
    candidate.owned_desktop()
    observed = desktop.status()
    bundle, _payload = read_bundle(mcp_manifest)
    fixture_sha256 = hashlib.sha256((REPO_ROOT / 'examples' / candidate.fixture_file).read_bytes()).hexdigest()
    worker_sha256 = hashlib.sha256(candidate.worker_source().encode()).hexdigest()
    if (observed.get('kind') != 'docker_xfce' or observed.get('running') is not True
            or observed.get('network') is not False
            or observed.get('workspace_identity') is None or observed.get('lifecycle_ref') is None
            or observed.get('runtime_id') != pin.parent_runtime_id
            or observed.get('container_id') != pin.container_id
            or observed.get('image_id') != pin.image_id
            or bundle['bundle_sha256'] != pin.mcp_bundle_sha256
            or worker_sha256 != pin.worker_sha256 or fixture_sha256 != pin.fixture_sha256
            or digest({**desktop.pins, 'mcp': bundle}) != pin.runtime_digest):
        raise ValueError('synthetic_fixture_preflight_mismatch')
    candidate.owned_desktop()
