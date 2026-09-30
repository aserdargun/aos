"""Exact same-origin JSON GET resources for a bounded dynamic web bundle."""

import hashlib
import ssl
from typing import Literal

from pydantic import Field, field_validator

from .contracts import TypedModel, digest
from .web_application import (Checksum, WebApplicationProfiles, canonical_origin,
                              readonly_resource_url_parts)
from .web_application_binding import WebTaskContract
from .web_https_preflight import _fetch_response
from .web_static_assets import (MAX_STATIC_ASSET_BYTES, WebStaticAsset,
                                WebStaticAssetFetchReport, WebStaticAssetPlan,
                                plan_web_static_assets, verify_web_static_assets)


MAX_READONLY_DATA_BYTES = 262144


class WebReadOnlyDataResource(TypedModel):
    url: str = Field(min_length=1, max_length=2048)
    content_type: Literal['application/json'] = 'application/json'

    @field_validator('url')
    @classmethod
    def canonical_url(cls, value):
        readonly_resource_url_parts(value)
        return value


class WebReadOnlyDataBundlePlan(TypedModel):
    schema_version: Literal['2.0'] = '2.0'
    profile_sha256: Checksum
    task_sha256: Checksum
    assets: list[WebStaticAsset] = Field(min_length=1, max_length=8)
    data_resources: list[WebReadOnlyDataResource] = Field(min_length=1, max_length=4)
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('data_resources')
    @classmethod
    def unique_data_urls(cls, values):
        if len({resource.url for resource in values}) != len(values):
            raise ValueError('duplicate_data_resource_url')
        return values


class WebReadOnlyDataFetchReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['explicit_bound_readonly_json_fetch'] = 'explicit_bound_readonly_json_fetch'
    status: Literal['https_json_reached'] = 'https_json_reached'
    profile_sha256: Checksum
    task_sha256: Checksum
    plan_sha256: Checksum
    data_index: int = Field(ge=0, le=3)
    content_type: Literal['application/json'] = 'application/json'
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=0, le=MAX_READONLY_DATA_BYTES)
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def plan_web_readonly_data_bundle(profiles: WebApplicationProfiles, task: WebTaskContract,
                                  assets: list[WebStaticAsset | dict],
                                  data_resources: list[WebReadOnlyDataResource | dict]
                                  ) -> WebReadOnlyDataBundlePlan:
    static_plan = plan_web_static_assets(profiles, task, assets)
    plan = WebReadOnlyDataBundlePlan(
        profile_sha256=static_plan.profile_sha256,
        task_sha256=static_plan.task_sha256,
        assets=static_plan.assets,
        data_resources=data_resources)
    profile = profiles.get(plan.profile_sha256)
    origin, _local = canonical_origin(profile.entry_url)
    if (any(resource.url == profile.entry_url
            or canonical_origin(readonly_resource_url_parts(resource.url)[0])[0] != origin
            for resource in plan.data_resources)
            or {resource.url for resource in plan.data_resources}
            & {asset.url for asset in plan.assets}):
        raise ValueError('data_resources_outside_confirmed_task')
    return plan


def verify_web_readonly_data_bundle(profiles: WebApplicationProfiles, task: WebTaskContract,
                                    plan: WebReadOnlyDataBundlePlan) -> WebReadOnlyDataBundlePlan:
    checked = WebReadOnlyDataBundlePlan.model_validate(plan.model_dump())
    if plan_web_readonly_data_bundle(profiles, task, checked.assets, checked.data_resources) != checked:
        raise ValueError('readonly_data_bundle_binding_changed')
    return checked


def verify_web_bundle_plan(profiles: WebApplicationProfiles, task: WebTaskContract,
                           plan: WebStaticAssetPlan | WebReadOnlyDataBundlePlan
                           ) -> WebStaticAssetPlan | WebReadOnlyDataBundlePlan:
    if isinstance(plan, WebReadOnlyDataBundlePlan):
        return verify_web_readonly_data_bundle(profiles, task, plan)
    if isinstance(plan, WebStaticAssetPlan):
        return verify_web_static_assets(profiles, task, plan)
    raise ValueError('web_bundle_plan_type')


def fetch_web_readonly_data(profiles: WebApplicationProfiles, task: WebTaskContract,
                            plan: WebReadOnlyDataBundlePlan, confirm_plan_sha256: str,
                            data_index: int, *, tls_context: ssl.SSLContext | None = None
                            ) -> tuple[WebReadOnlyDataFetchReport, bytes]:
    checked = verify_web_readonly_data_bundle(profiles, task, plan)
    plan_sha256 = digest(checked.model_dump())
    if (not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256
            or type(data_index) is not int or not 0 <= data_index < len(checked.data_resources)):
        raise ValueError('readonly_data_requires_exact_plan_and_index')
    resource = checked.data_resources[data_index]
    _origin, request_url, body = _fetch_response(
        profiles, checked.profile_sha256, checked.profile_sha256,
        tls_context=tls_context, target_url=resource.url, cookie_header=None,
        content_type=resource.content_type, max_response_bytes=MAX_READONLY_DATA_BYTES,
        allow_query=True)
    report = WebReadOnlyDataFetchReport(
        profile_sha256=checked.profile_sha256, task_sha256=checked.task_sha256,
        plan_sha256=plan_sha256, data_index=data_index,
        request_sha256=digest({'method': 'GET', 'url': request_url}),
        response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body))
    return report, body


def fetch_web_readonly_bundle_asset(profiles: WebApplicationProfiles, task: WebTaskContract,
                                    plan: WebReadOnlyDataBundlePlan, confirm_plan_sha256: str,
                                    asset_index: int, *, tls_context: ssl.SSLContext | None = None
                                    ) -> tuple[WebStaticAssetFetchReport, bytes]:
    checked = verify_web_readonly_data_bundle(profiles, task, plan)
    plan_sha256 = digest(checked.model_dump())
    if (not isinstance(confirm_plan_sha256, str) or confirm_plan_sha256 != plan_sha256
            or type(asset_index) is not int or not 0 <= asset_index < len(checked.assets)):
        raise ValueError('readonly_bundle_asset_requires_exact_plan_and_index')
    asset = checked.assets[asset_index]
    _origin, request_url, body = _fetch_response(
        profiles, checked.profile_sha256, checked.profile_sha256,
        tls_context=tls_context, target_url=asset.url, cookie_header=None,
        content_type=asset.content_type, max_response_bytes=MAX_STATIC_ASSET_BYTES,
        allow_query=True)
    report = WebStaticAssetFetchReport(
        profile_sha256=checked.profile_sha256, task_sha256=checked.task_sha256,
        plan_sha256=plan_sha256, asset_index=asset_index, content_type=asset.content_type,
        request_sha256=digest({'method': 'GET', 'url': request_url}),
        response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body))
    return report, body
