"""Exact same-origin static asset planning and host HTTPS fetch."""

import hashlib
import ssl
from typing import Literal, get_args

from pydantic import Field, field_validator

from .contracts import TypedModel, digest
from .web_application import Checksum, WebApplicationProfiles, canonical_origin, readonly_resource_url_parts
from .web_application_binding import WebTaskContract
from .web_https_preflight import _fetch_response


MAX_STATIC_ASSET_BYTES = 1048576
StaticContentType = Literal['text/css', 'text/javascript', 'application/javascript',
                            'image/png', 'image/jpeg', 'image/webp', 'image/gif']
STATIC_CONTENT_TYPES = get_args(StaticContentType)


class WebStaticAsset(TypedModel):
    url: str = Field(min_length=1, max_length=2048)
    content_type: StaticContentType

    @field_validator('url')
    @classmethod
    def canonical_url(cls, value):
        readonly_resource_url_parts(value)
        return value


class WebStaticAssetPlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    assets: list[WebStaticAsset] = Field(min_length=1, max_length=8)
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('assets')
    @classmethod
    def unique_asset_urls(cls, values):
        if len({asset.url for asset in values}) != len(values):
            raise ValueError('duplicate_static_asset_url')
        return values


class WebStaticAssetFetchReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['explicit_bound_static_asset_fetch'] = 'explicit_bound_static_asset_fetch'
    status: Literal['https_asset_reached'] = 'https_asset_reached'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    asset_index: int = Field(ge=0, le=7)
    content_type: StaticContentType
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=MAX_STATIC_ASSET_BYTES)
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def plan_web_static_assets(profiles: WebApplicationProfiles, task: WebTaskContract,
                           assets: list[WebStaticAsset | dict]) -> WebStaticAssetPlan:
    checked_task = WebTaskContract.model_validate(task.model_dump())
    profile = profiles.get(checked_task.profile_sha256)
    plan = WebStaticAssetPlan(profile_sha256=checked_task.profile_sha256,
                              task_sha256=digest(checked_task.model_dump()), assets=assets)
    origin, local = canonical_origin(profile.entry_url)
    if (profile.environment not in {'staging', 'production'} or local
            or checked_task.task_key not in profile.task_keys
            or checked_task.entry_url != profile.entry_url
            or origin not in checked_task.allowed_origins
            or not set(checked_task.allowed_origins).issubset(profile.allowed_origins)
            or 'browser.navigate' not in checked_task.tools
            or any(readonly_resource_url_parts(asset.url)[0] == profile.entry_url
                   or canonical_origin(readonly_resource_url_parts(asset.url)[0])[0] != origin
                   for asset in plan.assets)):
        raise ValueError('static_assets_outside_confirmed_task')
    return plan


def verify_web_static_assets(profiles: WebApplicationProfiles, task: WebTaskContract,
                             plan: WebStaticAssetPlan) -> WebStaticAssetPlan:
    checked = WebStaticAssetPlan.model_validate(plan.model_dump())
    if plan_web_static_assets(profiles, task, checked.assets) != checked:
        raise ValueError('static_asset_plan_binding_changed')
    return checked


def fetch_web_static_asset(profiles: WebApplicationProfiles, task: WebTaskContract,
                           plan: WebStaticAssetPlan, confirm_plan_sha256: str,
                           asset_index: int, *, tls_context: ssl.SSLContext | None = None
                           ) -> tuple[WebStaticAssetFetchReport, bytes]:
    checked = verify_web_static_assets(profiles, task, plan)
    plan_sha256 = digest(checked.model_dump())
    if (not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256
            or type(asset_index) is not int or not 0 <= asset_index < len(checked.assets)):
        raise ValueError('static_asset_requires_exact_plan_and_index')
    asset = checked.assets[asset_index]
    _origin, request_url, body = _fetch_response(
        profiles, checked.profile_sha256, checked.profile_sha256,
        tls_context=tls_context, target_url=asset.url, cookie_header=None,
        content_type=asset.content_type, max_response_bytes=MAX_STATIC_ASSET_BYTES,
        allow_query=True)
    report = WebStaticAssetFetchReport(
        profile_sha256=checked.profile_sha256, task_sha256=checked.task_sha256,
        plan_sha256=plan_sha256, asset_index=asset_index,
        content_type=asset.content_type,
        request_sha256=digest({'method': 'GET', 'url': request_url}),
        response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body))
    return report, body
