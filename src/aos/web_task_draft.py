"""Prepare an owner-only, non-authorizing remote-entry task from a stored profile."""

import hashlib
import os
from pathlib import Path
import re

from .contracts import REPO_ROOT, canonical, digest
from .lifecycle import private_directory
from .web_application import WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract, WebReadOnlyRoutePlan, plan_web_readonly_routes
from .web_static_assets import WebStaticAssetPlan, plan_web_static_assets
from .web_readonly_data import WebReadOnlyDataBundlePlan, plan_web_readonly_data_bundle
from .workspace_identity import open_existing_workspace


def preview_task(profiles: WebApplicationProfiles, profile_sha256: str,
                 task_key: str, verification_ref: str,
                 route_count: int = 1) -> tuple[WebTaskContract, str]:
    if type(route_count) is not int or not 1 <= route_count <= 8:
        raise ValueError('remote_task_invalid_route_count')
    profile = profiles.get(profile_sha256)
    origin, local = canonical_origin(profile.entry_url)
    if profile.environment not in {'staging', 'production'} or local or task_key not in profile.task_keys:
        raise ValueError('remote_task_outside_profile')
    task = WebTaskContract(profile_sha256=profile_sha256, task_key=task_key,
                           entry_url=profile.entry_url, allowed_origins=[origin],
                           tools=['browser.navigate', 'browser.snapshot', 'browser.verify'],
                           max_pages=route_count, max_navigations=route_count,
                           max_actions=route_count, max_seconds=min(120 * route_count, 900),
                           verification_ref=verification_ref)
    content = canonical(task.model_dump(mode='json')).encode()
    return task, hashlib.sha256(content).hexdigest()


def register_task(profiles: WebApplicationProfiles, root: Path, profile_sha256: str,
                  task_key: str, verification_ref: str, confirm_sha256: str,
                  route_count: int = 1) -> tuple[str, Path]:
    task, checksum = preview_task(profiles, profile_sha256, task_key, verification_ref,
                                  route_count)
    if confirm_sha256 != checksum:
        raise ValueError('remote_task_confirmation_mismatch')
    content = canonical(task.model_dump(mode='json')).encode()
    return checksum, write_private_content(root, checksum, content)


def write_private_content(root: Path, checksum: str, content: bytes,
                          suffix: str = '.json') -> Path:
    if (not root.is_absolute() or '..' in root.parts
            or not root.parent.resolve().is_relative_to((REPO_ROOT / 'data').resolve())
            or root == REPO_ROOT / 'data' or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', root.name)
            or suffix not in {'.json', '.txt'}
            or re.fullmatch('[a-f0-9]{64}', checksum) is None
            or hashlib.sha256(content).hexdigest() != checksum):
        raise ValueError('remote_task_output_outside_data')
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
    filename = checksum + suffix
    path = root / filename
    try:
        try:
            descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
        except FileExistsError:
            from .local_app import private_read
            if private_read(path) != content:
                raise ValueError('remote_task_existing_file_mismatch') from None
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        from .local_app import private_read
        if private_read(path) != content:
            raise ValueError('remote_task_file_changed')
        return path
    finally:
        os.close(directory)


def preview_routes(profiles: WebApplicationProfiles, task_root: Path,
                   profile_sha256: str, task_sha256: str,
                   routes: list[str]) -> tuple[WebReadOnlyRoutePlan, str]:
    if re.fullmatch('[a-f0-9]{64}', task_sha256) is None:
        raise ValueError('remote_routes_invalid_task_hash')
    from .local_app import private_read
    content = private_read(task_root / (task_sha256 + '.json'))
    task = WebTaskContract.model_validate_json(content)
    if (content != canonical(task.model_dump(mode='json')).encode()
            or hashlib.sha256(content).hexdigest() != task_sha256
            or task.profile_sha256 != profile_sha256):
        raise ValueError('remote_routes_task_changed')
    plan = plan_web_readonly_routes(profiles, task, routes)
    canonical_content = canonical(plan.model_dump(mode='json')).encode()
    return plan, hashlib.sha256(canonical_content).hexdigest()


def register_routes(profiles: WebApplicationProfiles, task_root: Path, plan_root: Path,
                    profile_sha256: str, task_sha256: str, routes: list[str],
                    confirm_sha256: str) -> tuple[str, Path]:
    plan, checksum = preview_routes(profiles, task_root, profile_sha256, task_sha256,
                                     routes)
    if checksum != confirm_sha256:
        raise ValueError('remote_routes_confirmation_mismatch')
    content = canonical(plan.model_dump(mode='json')).encode()
    return checksum, write_private_content(plan_root, checksum, content)


def preview_static_assets(profiles: WebApplicationProfiles, task_root: Path,
                          profile_sha256: str, task_sha256: str,
                          assets: list[dict]) -> tuple[WebStaticAssetPlan, str]:
    if re.fullmatch('[a-f0-9]{64}', task_sha256) is None:
        raise ValueError('remote_static_assets_invalid_task_hash')
    from .local_app import private_read
    content = private_read(task_root / (task_sha256 + '.json'))
    task = WebTaskContract.model_validate_json(content)
    if (content != canonical(task.model_dump(mode='json')).encode()
            or hashlib.sha256(content).hexdigest() != task_sha256
            or task.profile_sha256 != profile_sha256 or task.max_pages != 1
            or task.max_navigations != 1):
        raise ValueError('remote_static_assets_task_changed')
    plan = plan_web_static_assets(profiles, task, assets)
    return plan, digest(plan.model_dump(mode='json'))


def register_static_assets(profiles: WebApplicationProfiles, task_root: Path,
                           plan_root: Path, profile_sha256: str, task_sha256: str,
                           assets: list[dict], confirm_sha256: str) -> tuple[str, Path]:
    plan, checksum = preview_static_assets(profiles, task_root, profile_sha256,
                                            task_sha256, assets)
    if checksum != confirm_sha256:
        raise ValueError('remote_static_assets_confirmation_mismatch')
    content = canonical(plan.model_dump(mode='json')).encode()
    return checksum, write_private_content(plan_root, checksum, content)


def preview_readonly_data(profiles: WebApplicationProfiles, task_root: Path,
                          profile_sha256: str, task_sha256: str,
                          assets: list[dict], data_resources: list[dict]
                          ) -> tuple[WebReadOnlyDataBundlePlan, str]:
    if re.fullmatch('[a-f0-9]{64}', task_sha256) is None:
        raise ValueError('remote_readonly_data_invalid_task_hash')
    from .local_app import private_read
    content = private_read(task_root / (task_sha256 + '.json'))
    task = WebTaskContract.model_validate_json(content)
    if (content != canonical(task.model_dump(mode='json')).encode()
            or hashlib.sha256(content).hexdigest() != task_sha256
            or task.profile_sha256 != profile_sha256 or task.max_pages != 1
            or task.max_navigations != 1):
        raise ValueError('remote_readonly_data_task_changed')
    plan = plan_web_readonly_data_bundle(profiles, task, assets, data_resources)
    return plan, digest(plan.model_dump(mode='json'))


def register_readonly_data(profiles: WebApplicationProfiles, task_root: Path,
                           plan_root: Path, profile_sha256: str, task_sha256: str,
                           assets: list[dict], data_resources: list[dict],
                           confirm_sha256: str) -> tuple[str, Path]:
    plan, checksum = preview_readonly_data(profiles, task_root, profile_sha256,
                                            task_sha256, assets, data_resources)
    if checksum != confirm_sha256:
        raise ValueError('remote_readonly_data_confirmation_mismatch')
    content = canonical(plan.model_dump(mode='json')).encode()
    return checksum, write_private_content(plan_root, checksum, content)
