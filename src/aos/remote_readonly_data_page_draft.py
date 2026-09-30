"""Seed and register an unreviewed page draft from stable JSON-bundle runs."""

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .remote_page_draft_seed import read_private_seed, seed_output_path
from .remote_readonly_data_change import compare_remote_readonly_data_change
from .site_knowledge import SiteKnowledgeStore, SitePageDraft
from .web_application import Checksum, Key, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskAdmissionDraft
from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_readonly_data_bundle


class RemoteReadonlyDataPageDraftSeed(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['audited_remote_json_page_draft_seed'] = 'audited_remote_json_page_draft_seed'
    status: Literal['unreviewed_private_draft'] = 'unreviewed_private_draft'
    snapshot_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    before_run_ref: Checksum
    after_run_ref: Checksum
    page_fingerprint_sha256: Checksum
    page_key: Key
    draft_sha256: Checksum
    asset_count: int = Field(ge=1, le=8)
    data_count: int = Field(ge=1, le=4)
    profile_bound: Literal[True] = True
    transport_readback_bound: Literal[True] = True
    semantic_page_key_verified: Literal[False] = False
    origin_verified: Literal[False] = False
    account_verified: Literal[False] = False
    reviewed: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class RemoteReadonlyDataPageDraftRegistration(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['audited_remote_json_page_draft_registration'] = 'audited_remote_json_page_draft_registration'
    status: Literal['registered_unreviewed_draft'] = 'registered_unreviewed_draft'
    snapshot_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    before_run_ref: Checksum
    after_run_ref: Checksum
    page_key: Key
    knowledge_sha256: Checksum
    source_bound: Literal[True] = True
    transport_readback_bound: Literal[True] = True
    semantic_page_key_verified: Literal[False] = False
    reviewed: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def seed_remote_readonly_data_page_draft(
        database: Path, before_run_id: str, after_run_id: str, *, profiles: Path,
        store: Path, selected_profile_sha256: str, selected_plan_sha256: str,
        page_key: str) -> tuple[SitePageDraft, dict]:
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    for report in profile_store.list():
        if report['profile_sha256'] != selected_profile_sha256:
            successor = profile_store.get(report['profile_sha256'])
            if successor.previous_sha256 == selected_profile_sha256:
                raise ValueError('remote_json_page_draft_profile_superseded')
    page_store = SiteKnowledgeStore(store, profile_store)
    if page_store.list(profile_sha256=selected_profile_sha256, page_key=page_key):
        raise ValueError('remote_json_page_draft_existing_page_key')
    with audit_snapshot(database) as (snapshot, identity):
        change = compare_remote_readonly_data_change(
            database, before_run_id, after_run_id, profiles=profiles,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            _source=nullcontext((snapshot, identity)))
        if (change['status'] != 'unchanged_profile_bound'
                or change['before_fingerprint_sha256'] != change['after_fingerprint_sha256']
                or change['snapshot_sha256'] != identity['sha256']):
            raise ValueError('remote_json_page_draft_unstable_source')
        row = snapshot.execute('''SELECT draft_json,plan_json
            FROM desktop_remote_static_asset_bindings WHERE run_id=?''',
                               (after_run_id,)).fetchone()
        if row is None:
            raise ValueError('remote_json_page_draft_binding_missing')
        draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
        plan = WebReadOnlyDataBundlePlan.model_validate_json(row['plan_json'])
        if (canonical(draft.model_dump(mode='json')) != row['draft_json']
                or canonical(plan.model_dump(mode='json')) != row['plan_json']
                or draft.profile_sha256 != selected_profile_sha256
                or plan.profile_sha256 != selected_profile_sha256
                or digest(plan.model_dump()) != selected_plan_sha256
                or len(plan.assets) != change['asset_count']
                or len(plan.data_resources) != change['data_count']):
            raise ValueError('remote_json_page_draft_binding_changed')
        verify_web_readonly_data_bundle(profile_store, draft.task, plan)
    origin, _local = canonical_origin(draft.task.entry_url)
    if origin not in profile.allowed_origins:
        raise ValueError('remote_json_page_draft_origin_mismatch')
    page = SitePageDraft(
        schema_version='1.0', profile_sha256=selected_profile_sha256,
        application_key=profile.application_key, tenant_key=profile.tenant_key,
        account_role=profile.account_role, page_key=page_key, revision=1,
        previous_sha256=None, origin=origin,
        route_template=urlsplit(draft.task.entry_url).path,
        page_fingerprint_sha256=change['after_fingerprint_sha256'],
        recorded_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        landmark_keys=[], outgoing_page_keys=[], source_kind='manual_draft',
        status='draft', execution_authorized=False,
        collection_authorized=False, training_ready=False)
    report = RemoteReadonlyDataPageDraftSeed(
        snapshot_sha256=identity['sha256'], profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256,
        before_run_ref=change['before_run_ref'], after_run_ref=change['after_run_ref'],
        page_fingerprint_sha256=page.page_fingerprint_sha256,
        page_key=page_key, draft_sha256=digest(page.model_dump()),
        asset_count=change['asset_count'], data_count=change['data_count']).model_dump()
    validator('remote_readonly_data_page_draft_seed').validate(report)
    return page, report


def register_remote_readonly_data_page_draft(
        database: Path, before_run_id: str, after_run_id: str, *, profiles: Path,
        store: Path, seed_root: Path, selected_profile_sha256: str,
        selected_plan_sha256: str, page_key: str, confirm_sha256: str) -> dict:
    expected, seed_report = seed_remote_readonly_data_page_draft(
        database, before_run_id, after_run_id, profiles=profiles, store=store,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, page_key=page_key)
    page = read_private_seed(seed_output_path(seed_root, selected_profile_sha256, page_key),
                             confirm_sha256)
    if (page.model_dump(exclude={'recorded_at', 'landmark_keys'})
            != expected.model_dump(exclude={'recorded_at', 'landmark_keys'})):
        raise ValueError('remote_json_page_draft_source_changed')
    page_store = SiteKnowledgeStore(store, WebApplicationProfiles(profiles))
    if page_store.list(profile_sha256=selected_profile_sha256, page_key=page_key):
        raise ValueError('remote_json_page_draft_existing_page_key')
    checksum = page_store.register(page, confirm_sha256=confirm_sha256)
    report = RemoteReadonlyDataPageDraftRegistration(
        snapshot_sha256=seed_report['snapshot_sha256'],
        profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256,
        before_run_ref=seed_report['before_run_ref'],
        after_run_ref=seed_report['after_run_ref'],
        page_key=page_key, knowledge_sha256=checksum).model_dump()
    validator('remote_readonly_data_page_draft_registration').validate(report)
    return report
