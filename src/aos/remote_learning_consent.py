"""Explicit, run-bound local consent for content-free remote learning metadata."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import ssl
import stat
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .lifecycle import private_directory
from .remote_route_evidence import CHECKSUM, RUN_ID
from .web_application import Checksum, Key, WebApplicationProfiles
from .web_application_binding import (WebReadOnlyRoutePlan, WebTaskAdmissionDraft,
                                      verify_web_readonly_routes, verify_web_task_binding)
from .web_static_assets import WebStaticAssetPlan, verify_web_static_assets
from .web_readonly_data import WebReadOnlyDataBundlePlan, verify_web_readonly_data_bundle
from .web_https_form_transport import WebHTTPSFormPlan, verify_web_https_form_plan
from .web_https_form_state_probe import WebHTTPSFormStatePlan, verify_web_https_form_state_plan
from .workspace_identity import open_existing_workspace


MAX_CONSENTS = 1000
MAX_CONSENT_BYTES = 4096
MAX_REVOCATION_BYTES = 1024


class RemoteLearningConsent(TypedModel):
    schema_version: Literal['1.0']
    mode: Literal['local_operator_remote_metadata_consent']
    run_id: str = Field(pattern='^[A-Za-z0-9_-]{1,100}$')
    profile_sha256: Checksum
    plan_sha256: Checksum
    binding_sha256: Checksum
    task_key: Key
    data_rights_ref: Key
    roles: list[Literal['system1', 'system2']] = Field(min_length=1, max_length=2)
    retention_days: int = Field(ge=1, le=365)
    expires_at: str
    scope: Literal['remote_route_model_metadata_only', 'remote_static_model_metadata_only',
                   'remote_json_model_metadata_only', 'remote_form_model_metadata_only',
                   'remote_form_state_model_metadata_only']
    state_plan_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    local_operator_attested: bool
    external_rights_verified: bool
    raw_content_allowed: bool
    training_authorized: bool
    promotion_authorized: bool

    @model_validator(mode='after')
    def state_scope_requires_exact_plan(self):
        if ((self.scope == 'remote_form_state_model_metadata_only')
                != (self.state_plan_sha256 is not None)):
            raise ValueError('state_consent_plan_scope_mismatch')
        return self

    @field_validator('roles')
    @classmethod
    def canonical_roles(cls, roles):
        if roles != sorted(set(roles)) or not set(roles) <= {'system1', 'system2'}:
            raise ValueError('invalid_consent_roles')
        return roles

    @field_validator('local_operator_attested', mode='before')
    @classmethod
    def require_local_attestation(cls, value):
        if value is not True:
            raise ValueError('local_attestation_required')
        return value

    @field_validator('external_rights_verified', 'raw_content_allowed',
                     'training_authorized', 'promotion_authorized', mode='before')
    @classmethod
    def no_expanded_authority(cls, value):
        if value is not False:
            raise ValueError('consent_scope_cannot_expand')
        return value

    @field_validator('expires_at')
    @classmethod
    def canonical_expiry(cls, value):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError('invalid_consent_expiry') from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or value != parsed.isoformat():
            raise ValueError('invalid_consent_expiry')
        return value


class RemoteLearningRevocation(TypedModel):
    schema_version: Literal['1.0']
    mode: Literal['local_operator_remote_metadata_revocation',
                  'automatic_remote_metadata_retention_expiry']
    consent_sha256: Checksum
    revoked_at: str
    local_operator_attested: bool
    collection_authorized: Literal[False]
    training_authorized: Literal[False]

    @model_validator(mode='after')
    def attest_only_operator_revocation(self):
        if self.local_operator_attested != (self.mode == 'local_operator_remote_metadata_revocation'):
            raise ValueError('revocation_attestation_mismatch')
        return self

    @field_validator('revoked_at')
    @classmethod
    def canonical_revocation_time(cls, value):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError('invalid_revocation_time') from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or value != parsed.isoformat():
            raise ValueError('invalid_revocation_time')
        return value


def _bound_run_snapshot(snapshot: sqlite3.Connection, run_id: str,
                        profiles: WebApplicationProfiles, profile_sha256: str,
                        plan_sha256: str) -> tuple[sqlite3.Row, WebTaskAdmissionDraft, WebReadOnlyRoutePlan]:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(plan_sha256, str) or CHECKSUM.fullmatch(plan_sha256) is None):
        raise ValueError('invalid_consent_selection')
    row = snapshot.execute('''SELECT binding.*,job.kind,job.runtime_id AS job_runtime_id,
        run.policy_version FROM desktop_remote_route_bindings AS binding
        JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
        JOIN runs AS run ON run.run_id=binding.run_id
        WHERE binding.run_id=?''', (run_id,)).fetchone()
    if (row is None or row['kind'] != 'browser_remote_routes'
            or row['policy_version'] != 'browser-remote-routes-policy-v1'
            or row['profile_sha256'] != profile_sha256
            or row['plan_sha256'] != plan_sha256
            or row['job_runtime_id'] != row['browser_runtime_id']):
        raise ValueError('consent_run_binding_missing')
    draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
    plan = WebReadOnlyRoutePlan.model_validate_json(row['plan_json'])
    if (canonical(draft.model_dump(mode='json')) != row['draft_json']
            or canonical(plan.model_dump(mode='json')) != row['plan_json']
            or draft.profile_sha256 != profile_sha256
            or draft.binding_sha256 != row['binding_sha256']
            or draft.runtime_sha256 != row['runtime_sha256']
            or draft.runtime.runtime_id != row['browser_runtime_id']
            or plan.profile_sha256 != profile_sha256
            or digest(plan.model_dump()) != plan_sha256):
        raise ValueError('consent_run_binding_changed')
    verify_web_task_binding(profiles, draft)
    verify_web_readonly_routes(profiles, draft.task, plan)
    return row, draft, plan


def _bound_static_run_snapshot(snapshot: sqlite3.Connection, run_id: str,
                               profiles: WebApplicationProfiles, profile_sha256: str,
                               plan_sha256: str) -> tuple[sqlite3.Row, WebTaskAdmissionDraft, WebStaticAssetPlan]:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(plan_sha256, str) or CHECKSUM.fullmatch(plan_sha256) is None):
        raise ValueError('invalid_static_consent_selection')
    row = snapshot.execute('''SELECT binding.*,job.kind,job.runtime_id AS job_runtime_id,
        run.policy_version FROM desktop_remote_static_asset_bindings AS binding
        JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
        JOIN runs AS run ON run.run_id=binding.run_id
        WHERE binding.run_id=?''', (run_id,)).fetchone()
    if (row is None or row['kind'] != 'browser_remote_static_assets'
            or row['policy_version'] != 'browser-remote-static-assets-policy-v1'
            or row['profile_sha256'] != profile_sha256
            or row['plan_sha256'] != plan_sha256
            or row['job_runtime_id'] != row['browser_runtime_id']):
        raise ValueError('static_consent_run_binding_missing')
    draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
    plan = WebStaticAssetPlan.model_validate_json(row['plan_json'])
    if (canonical(draft.model_dump(mode='json')) != row['draft_json']
            or canonical(plan.model_dump(mode='json')) != row['plan_json']
            or draft.profile_sha256 != profile_sha256
            or draft.binding_sha256 != row['binding_sha256']
            or draft.runtime_sha256 != row['runtime_sha256']
            or draft.runtime.runtime_id != row['browser_runtime_id']
            or plan.profile_sha256 != profile_sha256
            or digest(plan.model_dump()) != plan_sha256):
        raise ValueError('static_consent_run_binding_changed')
    verify_web_task_binding(profiles, draft)
    verify_web_static_assets(profiles, draft.task, plan)
    return row, draft, plan


def _bound_readonly_data_run_snapshot(
        snapshot: sqlite3.Connection, run_id: str,
        profiles: WebApplicationProfiles, profile_sha256: str,
        plan_sha256: str) -> tuple[sqlite3.Row, WebTaskAdmissionDraft, WebReadOnlyDataBundlePlan]:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(plan_sha256, str) or CHECKSUM.fullmatch(plan_sha256) is None):
        raise ValueError('invalid_json_consent_selection')
    row = snapshot.execute('''SELECT binding.*,job.kind,job.runtime_id AS job_runtime_id,
        run.policy_version FROM desktop_remote_static_asset_bindings AS binding
        JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
        JOIN runs AS run ON run.run_id=binding.run_id
        WHERE binding.run_id=?''', (run_id,)).fetchone()
    if (row is None or row['kind'] != 'browser_remote_static_assets'
            or row['policy_version'] != 'browser-remote-static-assets-policy-v1'
            or row['profile_sha256'] != profile_sha256
            or row['plan_sha256'] != plan_sha256
            or row['job_runtime_id'] != row['browser_runtime_id']):
        raise ValueError('json_consent_run_binding_missing')
    draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
    plan = WebReadOnlyDataBundlePlan.model_validate_json(row['plan_json'])
    if (canonical(draft.model_dump(mode='json')) != row['draft_json']
            or canonical(plan.model_dump(mode='json')) != row['plan_json']
            or draft.profile_sha256 != profile_sha256
            or draft.binding_sha256 != row['binding_sha256']
            or draft.runtime_sha256 != row['runtime_sha256']
            or draft.runtime.runtime_id != row['browser_runtime_id']
            or plan.profile_sha256 != profile_sha256
            or digest(plan.model_dump()) != plan_sha256):
        raise ValueError('json_consent_run_binding_changed')
    verify_web_task_binding(profiles, draft)
    verify_web_readonly_data_bundle(profiles, draft.task, plan)
    return row, draft, plan


def _bound_form_run_snapshot(snapshot: sqlite3.Connection, run_id: str,
                             profiles: WebApplicationProfiles, profile_sha256: str,
                             plan_sha256: str, state_plan_sha256: str | None = None
                             ) -> tuple[sqlite3.Row, WebTaskAdmissionDraft, WebHTTPSFormPlan]:
    if (not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None
            or not isinstance(plan_sha256, str) or CHECKSUM.fullmatch(plan_sha256) is None):
        raise ValueError('invalid_form_consent_selection')
    row = snapshot.execute('''SELECT binding.*,job.kind,job.runtime_id AS job_runtime_id,
        run.policy_version FROM desktop_remote_form_bindings AS binding
        JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
        JOIN runs AS run ON run.run_id=binding.run_id
        WHERE binding.run_id=?''', (run_id,)).fetchone()
    if (row is None or row['kind'] != 'browser_remote_form'
            or row['policy_version'] != 'browser-remote-form-policy-v1'
            or row['profile_sha256'] != profile_sha256
            or row['plan_sha256'] != plan_sha256
            or row['job_runtime_id'] != row['browser_runtime_id']):
        raise ValueError('form_consent_run_binding_missing')
    draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
    plan = WebHTTPSFormPlan.model_validate_json(row['plan_json'])
    if (canonical(draft.model_dump(mode='json')) != row['draft_json']
            or canonical(plan.model_dump(mode='json')) != row['plan_json']
            or draft.profile_sha256 != profile_sha256
            or draft.binding_sha256 != row['binding_sha256']
            or draft.runtime_sha256 != row['runtime_sha256']
            or draft.runtime.runtime_id != row['browser_runtime_id']
            or plan.profile_sha256 != profile_sha256
            or digest(plan.model_dump()) != plan_sha256):
        raise ValueError('form_consent_run_binding_changed')
    verify_web_task_binding(profiles, draft)
    verify_web_https_form_plan(profiles, draft.task, plan)
    state_table = snapshot.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_remote_form_state_bindings'").fetchone()
    state_binding = (snapshot.execute('''SELECT * FROM desktop_remote_form_state_bindings
        WHERE job_id=? AND run_id=?''', (row['job_id'], run_id)).fetchone()
        if state_table else None)
    cookie_table = snapshot.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_remote_form_cookie_bindings'").fetchone()
    cookie_binding = (snapshot.execute('''SELECT 1 FROM desktop_remote_form_cookie_bindings
        WHERE job_id=? AND run_id=?''', (row['job_id'], run_id)).fetchone()
        if cookie_table else None)
    if cookie_binding is not None or (state_binding is None) != (state_plan_sha256 is None):
        raise ValueError('form_consent_state_or_cookie_scope_mismatch')
    if state_binding is not None:
        state_plan = WebHTTPSFormStatePlan.model_validate_json(state_binding['state_plan_json'])
        if (canonical(state_plan.model_dump(mode='json')) != state_binding['state_plan_json']
                or state_binding['form_plan_sha256'] != plan_sha256
                or state_binding['state_plan_sha256'] != state_plan_sha256
                or digest(state_plan.model_dump()) != state_plan_sha256
                or state_binding['browser_runtime_id'] != row['browser_runtime_id']):
            raise ValueError('form_consent_state_binding_changed')
        public = not urlsplit(plan.entry_url).hostname.endswith('.invalid')
        verify_web_https_form_state_plan(
            profiles, draft.task, plan, state_plan, ssl.create_default_context(),
            confirm_public_form_plan_sha256=plan_sha256 if public else None,
            confirm_public_state_plan_sha256=state_plan_sha256 if public else None)
    return row, draft, plan


def _bound_run(database: Path, run_id: str, profiles: WebApplicationProfiles,
               profile_sha256: str, plan_sha256: str, scope: str,
               state_plan_sha256: str | None = None) -> tuple[str, str]:
    with audit_snapshot(database) as (snapshot, _identity):
        if scope in {'remote_form_model_metadata_only', 'remote_form_state_model_metadata_only'}:
            row, draft, _plan = _bound_form_run_snapshot(
                snapshot, run_id, profiles, profile_sha256, plan_sha256,
                state_plan_sha256=state_plan_sha256)
            return row['binding_sha256'], draft.task.task_key
        if state_plan_sha256 is not None:
            raise ValueError('state_consent_requires_form_scope')
        binding = {'remote_route_model_metadata_only': _bound_run_snapshot,
                   'remote_static_model_metadata_only': _bound_static_run_snapshot,
                   'remote_json_model_metadata_only': _bound_readonly_data_run_snapshot}[scope]
        row, draft, _plan = binding(
            snapshot, run_id, profiles, profile_sha256, plan_sha256)
        return row['binding_sha256'], draft.task.task_key


def preview_remote_learning_consent(database: Path, run_id: str, *, profiles: Path,
                                    selected_profile_sha256: str, selected_plan_sha256: str,
                                    roles: list[str], expires_at: str,
                                    attest_data_rights: bool,
                                    selected_state_plan_sha256: str | None = None,
                                    scope: Literal['remote_route_model_metadata_only',
                                                   'remote_static_model_metadata_only',
                                                   'remote_json_model_metadata_only',
                                                   'remote_form_model_metadata_only',
                                                   'remote_form_state_model_metadata_only'] = 'remote_route_model_metadata_only') -> RemoteLearningConsent:
    if attest_data_rights is not True:
        raise ValueError('local_data_rights_attestation_required')
    if scope not in {'remote_route_model_metadata_only', 'remote_static_model_metadata_only',
                      'remote_json_model_metadata_only', 'remote_form_model_metadata_only',
                      'remote_form_state_model_metadata_only'}:
        raise ValueError('invalid_remote_learning_scope')
    if ((scope == 'remote_form_state_model_metadata_only')
            != (selected_state_plan_sha256 is not None)):
        raise ValueError('state_consent_plan_scope_mismatch')
    store = WebApplicationProfiles(profiles)
    profile = store.get(selected_profile_sha256)
    binding_sha256, task_key = _bound_run(
        database, run_id, store, selected_profile_sha256, selected_plan_sha256, scope,
        selected_state_plan_sha256)
    if (not roles or roles != sorted(set(roles))
            or any(getattr(profile.learning, role, None) != 'requested' for role in roles)
            or profile.learning.data_rights_ref is None):
        raise ValueError('consent_roles_not_requested')
    consent = RemoteLearningConsent(
        schema_version='1.0', mode='local_operator_remote_metadata_consent',
        run_id=run_id, profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256, binding_sha256=binding_sha256,
        task_key=task_key, data_rights_ref=profile.learning.data_rights_ref,
        roles=roles, retention_days=profile.learning.retention_days,
        expires_at=expires_at, scope=scope,
        state_plan_sha256=selected_state_plan_sha256,
        local_operator_attested=True, external_rights_verified=False,
        raw_content_allowed=False, training_authorized=False, promotion_authorized=False)
    deadline = datetime.fromisoformat(consent.expires_at)
    remaining = deadline - datetime.now(timezone.utc)
    if not timedelta(0) < remaining <= timedelta(hours=24):
        raise ValueError('consent_expiry_outside_one_day')
    validator('remote_learning_consent').validate(consent.model_dump())
    return consent


class RemoteLearningConsents:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _directory(self, *, create: bool) -> int:
        if create:
            parent = open_existing_workspace(self.root.parent)
            try:
                try:
                    os.mkdir(self.root.name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            finally:
                os.close(parent)
        return private_directory(self.root)

    @staticmethod
    def _read(directory: int, checksum: str) -> RemoteLearningConsent:
        if not isinstance(checksum, str) or CHECKSUM.fullmatch(checksum) is None:
            raise ValueError('invalid_consent_checksum')
        name = checksum + '.json'
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size == 0 or before.st_size > MAX_CONSENT_BYTES):
                raise ValueError('invalid_private_consent_file')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_CONSENT_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) != before.st_size or len(payload) > MAX_CONSENT_BYTES
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('consent_file_changed')
            record = RemoteLearningConsent.model_validate_json(payload)
            if (payload != canonical(record.model_dump()).encode()
                    or hashlib.sha256(payload).hexdigest() != checksum
                    or not validator('remote_learning_consent').is_valid(record.model_dump())):
                raise ValueError('consent_content_changed')
            return record
        finally:
            os.close(descriptor)

    @staticmethod
    def _revocation(directory: int, checksum: str) -> RemoteLearningRevocation | None:
        if not isinstance(checksum, str) or CHECKSUM.fullmatch(checksum) is None:
            raise ValueError('invalid_consent_checksum')
        name = checksum + '.revoked.json'
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size == 0 or before.st_size > MAX_REVOCATION_BYTES):
                raise ValueError('invalid_private_revocation_file')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_REVOCATION_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                      'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) != before.st_size or len(payload) > MAX_REVOCATION_BYTES
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('revocation_file_changed')
            record = RemoteLearningRevocation.model_validate_json(payload)
            if (payload != canonical(record.model_dump()).encode()
                    or record.consent_sha256 != checksum
                    or not validator('remote_learning_revocation').is_valid(record.model_dump())):
                raise ValueError('revocation_content_changed')
            return record
        finally:
            os.close(descriptor)

    def register(self, consent: RemoteLearningConsent, *, confirm_sha256: str,
                 database: Path, profiles: Path) -> str:
        record = RemoteLearningConsent.model_validate(consent.model_dump())
        checksum = digest(record.model_dump())
        remaining = datetime.fromisoformat(record.expires_at) - datetime.now(timezone.utc)
        if confirm_sha256 != checksum or not timedelta(0) < remaining <= timedelta(hours=24):
            raise ValueError('exact_live_consent_confirmation_required')
        current = preview_remote_learning_consent(
            database, record.run_id, profiles=profiles,
            selected_profile_sha256=record.profile_sha256,
            selected_plan_sha256=record.plan_sha256, roles=record.roles,
            expires_at=record.expires_at, attest_data_rights=True, scope=record.scope,
            selected_state_plan_sha256=record.state_plan_sha256)
        if current != record:
            raise ValueError('consent_source_changed')
        payload = canonical(record.model_dump()).encode()
        if len(payload) > MAX_CONSENT_BYTES:
            raise ValueError('consent_size_limit')
        directory = self._directory(create=True)
        temporary = '.consent-' + uuid4().hex
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            if self._revocation(directory, checksum) is not None:
                raise ValueError('consent_already_revoked')
            if sum(bool(CHECKSUM.fullmatch(name.removesuffix('.json')))
                   for name in os.listdir(directory) if name.endswith('.json')) >= MAX_CONSENTS:
                raise ValueError('consent_store_limit')
            try:
                existing = self._read(directory, checksum)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                return checksum
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, checksum + '.json', src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if self._read(directory, checksum) != record:
                    raise ValueError('consent_conflict')
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            self._read(directory, checksum)
            return checksum
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def revoke(self, checksum: str, *, confirm_sha256: str) -> RemoteLearningRevocation:
        if (not isinstance(checksum, str) or CHECKSUM.fullmatch(checksum) is None
                or confirm_sha256 != checksum):
            raise ValueError('exact_consent_revocation_confirmation_required')
        directory = self._directory(create=False)
        temporary = '.revocation-' + uuid4().hex
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            self._read(directory, checksum)
            return self._publish_revocation(directory, checksum, temporary,
                                            mode='local_operator_remote_metadata_revocation',
                                            attested=True, recorded_at=datetime.now(timezone.utc))
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def expire_for_retention(self, checksum: str, *, now: datetime | None = None) -> RemoteLearningRevocation:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError('retention_requires_utc_time')
        directory = self._directory(create=False)
        temporary = '.revocation-' + uuid4().hex
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            consent = self._read(directory, checksum)
            deadline = datetime.fromisoformat(consent.expires_at) + timedelta(days=consent.retention_days)
            if current < deadline:
                raise ValueError('remote_learning_retention_not_due')
            return self._publish_revocation(directory, checksum, temporary,
                                            mode='automatic_remote_metadata_retention_expiry',
                                            attested=False, recorded_at=current)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def retention_candidates(self, *, now: datetime | None = None) -> list[tuple[str, bool]]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError('retention_requires_utc_time')
        try:
            directory = self._directory(create=False)
        except FileNotFoundError:
            return []
        try:
            fcntl.flock(directory, fcntl.LOCK_SH)
            names = os.listdir(directory)
            if len(names) > MAX_CONSENTS * 3:
                raise ValueError('remote_learning_retention_store_limit')
            checksums = sorted(name.removesuffix('.json') for name in names
                               if name.endswith('.json') and CHECKSUM.fullmatch(name.removesuffix('.json')))
            if len(checksums) > MAX_CONSENTS:
                raise ValueError('remote_learning_retention_store_limit')
            candidates = []
            for checksum in checksums:
                consent = self._read(directory, checksum)
                due = current >= (datetime.fromisoformat(consent.expires_at)
                                  + timedelta(days=consent.retention_days))
                if due or self._revocation(directory, checksum) is not None:
                    candidates.append((checksum, due))
            return candidates
        finally:
            os.close(directory)

    def _publish_revocation(self, directory: int, checksum: str, temporary: str, *,
                            mode: Literal['local_operator_remote_metadata_revocation',
                                          'automatic_remote_metadata_retention_expiry'],
                            attested: bool, recorded_at: datetime) -> RemoteLearningRevocation:
        existing = self._revocation(directory, checksum)
        if existing is not None:
            return existing
        record = RemoteLearningRevocation(
            schema_version='1.0', mode=mode, consent_sha256=checksum,
            revoked_at=recorded_at.isoformat(), local_operator_attested=attested,
            collection_authorized=False, training_authorized=False)
        validator('remote_learning_revocation').validate(record.model_dump())
        payload = canonical(record.model_dump()).encode()
        if len(payload) > MAX_REVOCATION_BYTES:
            raise ValueError('revocation_size_limit')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, checksum + '.revoked.json', src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if self._revocation(directory, checksum) != record:
                raise ValueError('revocation_conflict')
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
        return self._revocation(directory, checksum)

    @contextmanager
    def active(self, checksum: str):
        directory = self._directory(create=False)
        try:
            fcntl.flock(directory, fcntl.LOCK_SH)
            record = self._read(directory, checksum)
            if self._revocation(directory, checksum) is not None:
                raise ValueError('consent_revoked')
            if datetime.fromisoformat(record.expires_at) <= datetime.now(timezone.utc):
                raise ValueError('consent_expired')
            yield record
        finally:
            os.close(directory)

    def get(self, checksum: str) -> RemoteLearningConsent:
        with self.active(checksum) as record:
            return record

    def revocation(self, checksum: str) -> RemoteLearningRevocation:
        directory = self._directory(create=False)
        try:
            self._read(directory, checksum)
            record = self._revocation(directory, checksum)
            if record is None:
                raise ValueError('consent_not_revoked')
            return record
        finally:
            os.close(directory)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Confirm local, run-bound metadata scope; no collection or training')
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--selected-state-plan-sha256')
    parser.add_argument('--scope', choices=('remote_route_model_metadata_only',
                                            'remote_static_model_metadata_only',
                                            'remote_json_model_metadata_only',
                                            'remote_form_model_metadata_only',
                                            'remote_form_state_model_metadata_only'),
                        default='remote_route_model_metadata_only')
    parser.add_argument('--roles', nargs='+', choices=('system1', 'system2'), required=True)
    parser.add_argument('--expires-at', required=True)
    parser.add_argument('--attest-data-rights', action='store_true')
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--confirm-sha256')
    arguments = parser.parse_args(argv)
    try:
        consent = preview_remote_learning_consent(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            selected_state_plan_sha256=arguments.selected_state_plan_sha256,
            roles=arguments.roles, expires_at=arguments.expires_at,
            attest_data_rights=arguments.attest_data_rights, scope=arguments.scope)
        checksum = digest(consent.model_dump())
        if arguments.confirm_sha256 is not None:
            RemoteLearningConsents(arguments.store).register(
                consent, confirm_sha256=arguments.confirm_sha256,
                database=arguments.database, profiles=arguments.profiles)
        print(canonical({'consent_sha256': checksum, 'registered': arguments.confirm_sha256 is not None,
                         'run_ref': digest({'run_id': consent.run_id}), 'roles': consent.roles,
                         'expires_at': consent.expires_at, 'scope': consent.scope, 'metadata_only': True,
                         'external_rights_verified': False, 'training_authorized': False}))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote learning consent rejected: scope, binding, rights or private store invalid.\n')


if __name__ == '__main__':
    main()
