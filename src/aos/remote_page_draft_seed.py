"""Seed a private, unreviewed page draft from two audited HTTPS route runs."""

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal
from urllib.parse import urlsplit

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .lifecycle import private_directory
from .remote_route_change import _compare_remote_route_change_snapshot
from .site_knowledge import MAX_KNOWLEDGE_BYTES, SiteKnowledgeStore, SitePageDraft
from .web_application import Checksum, Key, WebApplicationProfiles, canonical_origin
from .web_application_binding import WebReadOnlyRoutePlan
from .workspace_identity import open_existing_workspace


class RemotePageDraftSeedReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['audited_remote_page_draft_seed'] = 'audited_remote_page_draft_seed'
    status: Literal['unreviewed_private_draft'] = 'unreviewed_private_draft'
    snapshot_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    before_run_ref: Checksum
    after_run_ref: Checksum
    route_index: int
    url_sha256: Checksum
    page_fingerprint_sha256: Checksum
    page_key: Key
    draft_sha256: Checksum
    profile_bound: Literal[True] = True
    readback_verified: Literal[True] = True
    semantic_page_key_verified: Literal[False] = False
    origin_verified: Literal[False] = False
    account_verified: Literal[False] = False
    reviewed: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class RemotePageDraftRegistrationReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['audited_remote_page_draft_registration'] = 'audited_remote_page_draft_registration'
    status: Literal['registered_unreviewed_draft'] = 'registered_unreviewed_draft'
    snapshot_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    before_run_ref: Checksum
    after_run_ref: Checksum
    route_index: int
    page_key: Key
    knowledge_sha256: Checksum
    source_bound: Literal[True] = True
    readback_verified: Literal[True] = True
    semantic_page_key_verified: Literal[False] = False
    reviewed: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def seed_remote_page_draft(database: Path, before_run_id: str, after_run_id: str, *,
                           profiles: Path, store: Path, selected_profile_sha256: str,
                           selected_plan_sha256: str, route_index: int,
                           page_key: str) -> tuple[SitePageDraft, dict]:
    if type(route_index) is not int or not 0 <= route_index <= 7:
        raise ValueError('remote_page_draft_seed_invalid_index')
    profile_store = WebApplicationProfiles(profiles)
    profile = profile_store.get(selected_profile_sha256)
    for report in profile_store.list():
        if report['profile_sha256'] != selected_profile_sha256:
            successor = profile_store.get(report['profile_sha256'])
            if successor.previous_sha256 == selected_profile_sha256:
                raise ValueError('remote_page_draft_seed_profile_superseded')
    page_store = SiteKnowledgeStore(store, profile_store)
    if page_store.list(profile_sha256=selected_profile_sha256, page_key=page_key):
        raise ValueError('remote_page_draft_seed_existing_page_key')
    with audit_snapshot(database) as (snapshot, identity):
        change = _compare_remote_route_change_snapshot(
            database, snapshot, identity, before_run_id, after_run_id,
            profiles=profiles, selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256)
        if route_index >= change['route_count'] or change['routes'][route_index]['changed']:
            raise ValueError('remote_page_draft_seed_unstable_route')
        row = snapshot.execute('''SELECT plan_json FROM desktop_remote_route_bindings
            WHERE run_id=?''', (after_run_id,)).fetchone()
        if row is None:
            raise ValueError('remote_page_draft_seed_missing_plan')
        plan = WebReadOnlyRoutePlan.model_validate_json(row['plan_json'])
        if (canonical(plan.model_dump(mode='json')) != row['plan_json']
                or digest(plan.model_dump()) != selected_plan_sha256
                or plan.profile_sha256 != selected_profile_sha256
                or len(plan.routes) != change['route_count']):
            raise ValueError('remote_page_draft_seed_plan_changed')
        route = change['routes'][route_index]
        url = plan.routes[route_index]
        if digest({'url': url}) != route['url_sha256']:
            raise ValueError('remote_page_draft_seed_url_changed')
        origin, _local = canonical_origin(url)
        path = urlsplit(url).path
        if origin not in profile.allowed_origins:
            raise ValueError('remote_page_draft_seed_origin_mismatch')
        page = SitePageDraft(
            schema_version='1.0', profile_sha256=selected_profile_sha256,
            application_key=profile.application_key, tenant_key=profile.tenant_key,
            account_role=profile.account_role, page_key=page_key, revision=1,
            previous_sha256=None, origin=origin, route_template=path,
            page_fingerprint_sha256=route['after_fingerprint_sha256'],
            recorded_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            landmark_keys=[], outgoing_page_keys=[], source_kind='manual_draft',
            status='draft', execution_authorized=False,
            collection_authorized=False, training_ready=False)
    report = RemotePageDraftSeedReport(
        snapshot_sha256=change['snapshot_sha256'],
        profile_sha256=selected_profile_sha256, plan_sha256=selected_plan_sha256,
        before_run_ref=change['before_run_ref'], after_run_ref=change['after_run_ref'],
        route_index=route_index, url_sha256=route['url_sha256'],
        page_fingerprint_sha256=page.page_fingerprint_sha256,
        page_key=page_key, draft_sha256=digest(page.model_dump())).model_dump()
    validator('remote_page_draft_seed').validate(report)
    return page, report


def write_private_draft(path: Path, page: SitePageDraft) -> None:
    if not path.resolve().is_relative_to((REPO_ROOT / 'data').resolve()):
        raise ValueError('remote_page_draft_seed_output_outside_data')
    directory = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(canonical(page.model_dump()).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def seed_output_path(root: Path, profile_sha256: str, page_key: str) -> Path:
    root = Path(root)
    data_root = REPO_ROOT / 'data'
    if (not root.is_absolute() or '..' in root.parts or root.resolve() != root
            or data_root.resolve() != data_root
            or re.fullmatch(r'[a-f0-9]{64}', profile_sha256) is None
            or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', page_key) is None):
        raise ValueError('remote_page_draft_seed_invalid_private_output')
    try:
        relative = root.relative_to(data_root).parts
    except ValueError:
        raise ValueError('remote_page_draft_seed_invalid_private_output') from None
    if len(relative) != 1:
        if (len(relative) != 3
                or re.fullmatch(r'local-app-(?:v1|project-[a-z0-9][a-z0-9-]{0,47})', relative[0]) is None
                or re.fullmatch(r'app-[a-f0-9]{32}', relative[1]) is None
                or relative[2] not in {'site-page-seeds', 'json-page-seeds'}):
            raise ValueError('remote_page_draft_seed_invalid_private_output')
        for directory in (root.parent.parent, root.parent):
            descriptor = private_directory(directory)
            os.close(descriptor)
    return root / profile_sha256 / (page_key + '.json')


def private_seed_output(root: Path, profile_sha256: str, page_key: str) -> Path:
    output = seed_output_path(root, profile_sha256, page_key)
    parent = open_existing_workspace(root.parent)
    try:
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    directory = private_directory(root)
    try:
        try:
            os.mkdir(profile_sha256, 0o700, dir_fd=directory)
            os.fsync(directory)
        except FileExistsError:
            pass
    finally:
        os.close(directory)
    profile_directory = private_directory(root / profile_sha256)
    os.close(profile_directory)
    return output


def read_private_seed(path: Path, confirm_sha256: str) -> SitePageDraft:
    if not isinstance(confirm_sha256, str) or re.fullmatch(r'[a-f0-9]{64}', confirm_sha256) is None:
        raise ValueError('remote_page_draft_seed_confirmation_invalid')
    directory = private_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_KNOWLEDGE_BYTES):
                raise ValueError('remote_page_draft_seed_private_file_required')
            payload = os.read(descriptor, MAX_KNOWLEDGE_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink',
                      'st_mode', 'st_uid')
            if (len(payload) != before.st_size or len(payload) > MAX_KNOWLEDGE_BYTES
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('remote_page_draft_seed_file_changed')
            page = SitePageDraft.model_validate_json(payload)
            if (payload != canonical(page.model_dump()).encode()
                    or hashlib.sha256(payload).hexdigest() != confirm_sha256):
                raise ValueError('remote_page_draft_seed_confirmation_mismatch')
            return page
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def register_remote_page_draft(database: Path, before_run_id: str, after_run_id: str, *,
                               profiles: Path, store: Path, seed_root: Path,
                               selected_profile_sha256: str, selected_plan_sha256: str,
                               route_index: int, page_key: str,
                               confirm_sha256: str) -> dict:
    expected, seed_report = seed_remote_page_draft(
        database, before_run_id, after_run_id, profiles=profiles, store=store,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, route_index=route_index,
        page_key=page_key)
    page = read_private_seed(seed_output_path(seed_root, selected_profile_sha256, page_key),
                             confirm_sha256)
    compared = {'recorded_at', 'landmark_keys', 'outgoing_page_keys'}
    if page.model_dump(exclude=compared) != expected.model_dump(exclude=compared):
        raise ValueError('remote_page_draft_seed_source_mismatch')
    page_store = SiteKnowledgeStore(store, WebApplicationProfiles(profiles))
    if page_store.list(profile_sha256=selected_profile_sha256, page_key=page_key):
        raise ValueError('remote_page_draft_seed_existing_page_key')
    checksum = page_store.register(page, confirm_sha256=confirm_sha256)
    report = RemotePageDraftRegistrationReport(
        snapshot_sha256=seed_report['snapshot_sha256'],
        profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256,
        before_run_ref=seed_report['before_run_ref'],
        after_run_ref=seed_report['after_run_ref'],
        route_index=route_index, page_key=page_key,
        knowledge_sha256=checksum).model_dump()
    validator('remote_page_draft_registration').validate(report)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Seed an unreviewed private page draft from audited routes',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--before-run-id', required=True)
    parser.add_argument('--after-run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--route-index', type=int, required=True)
    parser.add_argument('--page-key', required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        page, report = seed_remote_page_draft(
            arguments.database, arguments.before_run_id, arguments.after_run_id,
            profiles=arguments.profiles, store=arguments.store,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            route_index=arguments.route_index, page_key=arguments.page_key)
        write_private_draft(arguments.output, page)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote page draft seed unavailable: missing, changed or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
