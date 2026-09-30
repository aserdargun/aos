"""Explicit metadata review and fresh-run recheck for JSON-bundle page drafts."""

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .lifecycle import private_directory
from .remote_readonly_data_change import FINGERPRINT_VERSION
from .remote_readonly_data_knowledge import preview_remote_readonly_data_knowledge
from .remote_route_evidence import RUN_ID
from .remote_static_learning_source import _inspect_bundle_learning_source
from .site_knowledge import SiteKnowledgeStore
from .web_application import Checksum, Key, WebApplicationProfiles
from .workspace_identity import open_existing_workspace


MAX_REVIEW_BYTES = 8192


class RemoteReadonlyDataKnowledgeReview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['manual_remote_json_metadata_review'] = 'manual_remote_json_metadata_review'
    before_run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    after_run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    candidate_sha256: Checksum
    candidate_source_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    knowledge_sha256: Checksum
    page_fingerprint_sha256: Checksum
    recorded_at: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$')
    metadata_reviewed: Literal[True] = True
    origin_verified: Literal[False] = False
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class RemoteReadonlyDataKnowledgeCandidatePreview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['remote_json_metadata_candidate_preview'] = 'remote_json_metadata_candidate_preview'
    candidate_sha256: Checksum
    candidate: dict

    @model_validator(mode='after')
    def exact_candidate(self):
        validator('remote_readonly_data_knowledge').validate(self.candidate)
        if digest(self.candidate) != self.candidate_sha256:
            raise ValueError('remote_json_candidate_hash_mismatch')
        return self


class LiveRemoteReadonlyDataKnowledgePin(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['historical_remote_json_review_pin'] = 'historical_remote_json_review_pin'
    review_sha256: Checksum
    knowledge_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    source_snapshot_sha256: Checksum
    page_key: Key
    draft_revision: int = Field(ge=1, le=100)
    landmark_keys: list[Key] = Field(max_length=32)
    page_fingerprint_sha256: Checksum
    metadata_reviewed: Literal[True] = True
    current_readback_bound: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class LiveRemoteReadonlyDataKnowledgeEvent(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['remote_json_knowledge_live_readback'] = 'remote_json_knowledge_live_readback'
    review_sha256: Checksum
    status: Literal['matched', 'stale']
    expected_fingerprint_sha256: Checksum
    current_fingerprint_sha256: Checksum
    page_key: Key | None = None
    draft_revision: int | None = Field(default=None, ge=1, le=100)
    landmark_keys: list[Key] | None = Field(default=None, max_length=32)
    metadata_reviewed: Literal[True] = True
    current_readback_bound: Literal[True] = True
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def only_matching_page_keys(self):
        if (self.status == 'matched') != (
                self.expected_fingerprint_sha256 == self.current_fingerprint_sha256):
            raise ValueError('remote_json_live_event_fingerprint_status_mismatch')
        has_keys = (self.page_key is not None and self.draft_revision is not None
                    and self.landmark_keys is not None)
        if has_keys != (self.status == 'matched'):
            raise ValueError('remote_json_live_event_keys_require_match')
        if self.status == 'stale' and any(value is not None for value in (
                self.page_key, self.draft_revision, self.landmark_keys)):
            raise ValueError('remote_json_live_event_stale_keys')
        return self


def candidate_source_sha256(candidate: dict) -> str:
    return digest({key: value for key, value in candidate.items()
                   if key != 'snapshot_sha256'})


class RemoteReadonlyDataKnowledgeReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if not self.root.is_relative_to(REPO_ROOT / 'data'):
            raise ValueError('remote_json_review_requires_private_data_root')

    def _directory(self, *, create: bool = False) -> int:
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
    def _get(directory: int, checksum: str) -> RemoteReadonlyDataKnowledgeReview:
        if (not isinstance(checksum, str) or len(checksum) != 64
                or any(character not in '0123456789abcdef' for character in checksum)):
            raise ValueError('remote_json_review_invalid_checksum')
        name = checksum + '.json'
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_REVIEW_BYTES):
                raise ValueError('remote_json_review_invalid_private_file')
            payload = os.read(descriptor, MAX_REVIEW_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                      'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('remote_json_review_file_changed')
            record = RemoteReadonlyDataKnowledgeReview.model_validate_json(payload)
            if (payload != canonical(record.model_dump()).encode()
                    or hashlib.sha256(payload).hexdigest() != checksum
                    or not validator('remote_readonly_data_knowledge_review').is_valid(
                        record.model_dump())):
                raise ValueError('remote_json_review_content_changed')
            return record
        finally:
            os.close(descriptor)

    def get(self, checksum: str) -> RemoteReadonlyDataKnowledgeReview:
        directory = self._directory()
        try:
            return self._get(directory, checksum)
        finally:
            os.close(directory)

    def register(self, record: RemoteReadonlyDataKnowledgeReview) -> str:
        record = RemoteReadonlyDataKnowledgeReview.model_validate(record.model_dump())
        if not validator('remote_readonly_data_knowledge_review').is_valid(record.model_dump()):
            raise ValueError('remote_json_review_invalid_record')
        payload = canonical(record.model_dump()).encode()
        checksum = hashlib.sha256(payload).hexdigest()
        directory = self._directory(create=True)
        temporary = '.review-' + uuid4().hex
        try:
            try:
                self._get(directory, checksum)
                return checksum
            except FileNotFoundError:
                pass
            if len(os.listdir(directory)) >= 1000:
                raise ValueError('remote_json_review_store_limit')
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
                pass
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            self._get(directory, checksum)
            return checksum
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


def register_remote_readonly_data_knowledge_review(
        database: Path, before_run_id: str, after_run_id: str, *, profiles: Path,
        site_store: Path, review_store: Path, knowledge_sha256: str,
        selected_profile_sha256: str, selected_plan_sha256: str,
        confirm_candidate_sha256: str, acknowledge_metadata_only: bool) -> dict:
    if acknowledge_metadata_only is not True:
        raise ValueError('remote_json_review_requires_explicit_acknowledgement')
    candidate = preview_remote_readonly_data_knowledge(
        database, before_run_id, after_run_id, profiles=profiles, store=site_store,
        knowledge_sha256=knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256)
    if digest(candidate) != confirm_candidate_sha256:
        raise ValueError('remote_json_review_requires_exact_candidate_hash')
    record = RemoteReadonlyDataKnowledgeReview(
        before_run_id=before_run_id, after_run_id=after_run_id,
        candidate_sha256=confirm_candidate_sha256,
        candidate_source_sha256=candidate_source_sha256(candidate),
        profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256,
        knowledge_sha256=knowledge_sha256,
        page_fingerprint_sha256=candidate['page_fingerprint_sha256'],
        recorded_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
    checksum = RemoteReadonlyDataKnowledgeReviewStore(review_store).register(record)
    receipt = {'schema_version': '1.0', 'mode': 'manual_remote_json_metadata_review',
               'status': 'metadata_review_recorded', 'review_sha256': checksum,
               'candidate_sha256': confirm_candidate_sha256,
               'metadata_reviewed': True, 'task_retrieval_authorized': False,
               'execution_authorized': False, 'collection_authorized': False,
               'training_ready': False}
    validator('remote_readonly_data_knowledge_review_receipt').validate(receipt)
    return receipt


def prepare_live_remote_readonly_data_knowledge(
        database: Path, *, profiles: Path, site_store: Path, review_store: Path,
        review_sha256: str, selected_profile_sha256: str,
        selected_plan_sha256: str) -> LiveRemoteReadonlyDataKnowledgePin:
    record = RemoteReadonlyDataKnowledgeReviewStore(review_store).get(review_sha256)
    if (record.profile_sha256 != selected_profile_sha256
            or record.plan_sha256 != selected_plan_sha256):
        raise ValueError('remote_json_review_scope_changed')
    candidate = preview_remote_readonly_data_knowledge(
        database, record.before_run_id, record.after_run_id,
        profiles=profiles, store=site_store,
        knowledge_sha256=record.knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256)
    if (candidate_source_sha256(candidate) != record.candidate_source_sha256
            or candidate['page_fingerprint_sha256'] != record.page_fingerprint_sha256):
        raise ValueError('remote_json_review_source_changed')
    page_store = SiteKnowledgeStore(site_store, WebApplicationProfiles(profiles))
    page = page_store.get(record.knowledge_sha256)
    inspection = page_store.inspect(
        record.knowledge_sha256, selected_profile_sha256=selected_profile_sha256,
        current_fingerprint_sha256=record.page_fingerprint_sha256)
    if (inspection['status'] != 'draft_match'
            or page.page_key != candidate['page_key']
            or page.revision != candidate['draft_revision']):
        raise ValueError('remote_json_review_page_stale')
    pin = LiveRemoteReadonlyDataKnowledgePin(
        review_sha256=review_sha256, knowledge_sha256=record.knowledge_sha256,
        profile_sha256=selected_profile_sha256,
        plan_sha256=selected_plan_sha256,
        source_snapshot_sha256=candidate['snapshot_sha256'],
        page_key=page.page_key, draft_revision=page.revision,
        landmark_keys=page.landmark_keys,
        page_fingerprint_sha256=record.page_fingerprint_sha256)
    validator('remote_readonly_data_knowledge_live_pin').validate(pin.model_dump())
    return pin


def recheck_remote_readonly_data_knowledge(
        database: Path, current_run_id: str, *, profiles: Path, site_store: Path,
        review_store: Path, review_sha256: str, selected_profile_sha256: str,
        selected_plan_sha256: str) -> dict:
    if not isinstance(current_run_id, str) or RUN_ID.fullmatch(current_run_id) is None:
        raise ValueError('remote_json_review_invalid_current_run')
    record = RemoteReadonlyDataKnowledgeReviewStore(review_store).get(review_sha256)
    if (record.profile_sha256 != selected_profile_sha256
            or record.plan_sha256 != selected_plan_sha256
            or current_run_id in (record.before_run_id, record.after_run_id)):
        raise ValueError('remote_json_review_scope_or_run_changed')
    with audit_snapshot(database) as (snapshot, identity):
        candidate = preview_remote_readonly_data_knowledge(
            database, record.before_run_id, record.after_run_id,
            profiles=profiles, store=site_store,
            knowledge_sha256=record.knowledge_sha256,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            _source=nullcontext((snapshot, identity)))
        if (candidate_source_sha256(candidate) != record.candidate_source_sha256
                or candidate['page_fingerprint_sha256'] != record.page_fingerprint_sha256):
            raise ValueError('remote_json_review_source_changed')
        started = snapshot.execute('''SELECT run_id,started_at FROM runs
            WHERE run_id IN (?,?)''', (record.after_run_id, current_run_id)).fetchall()
        start_times = {row['run_id']: row['started_at'] for row in started}
        if (len(start_times) != 2
                or not isinstance(start_times[record.after_run_id], str)
                or not isinstance(start_times[current_run_id], str)
                or start_times[record.after_run_id] >= start_times[current_run_id]):
            raise ValueError('remote_json_review_fresh_run_order_invalid')
        reviewed_at = datetime.fromisoformat(record.recorded_at.replace('Z', '+00:00'))
        current_started_at = datetime.fromisoformat(start_times[current_run_id])
        if current_started_at.tzinfo is None:
            raise ValueError('remote_json_review_fresh_run_time_invalid')
        if '.' not in record.recorded_at:
            reviewed_at += timedelta(seconds=1)
        if current_started_at <= reviewed_at:
            raise ValueError('remote_json_review_run_precedes_review')
        current, transport = _inspect_bundle_learning_source(
            database, current_run_id, profiles=profiles,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            data_bundle=True, include_verified_responses=True,
            source=nullcontext((snapshot, identity)))
        current_fingerprint = digest({'version': FINGERPRINT_VERSION, **transport})
        if (current['snapshot_sha256'] != identity['sha256']
                or current_fingerprint != record.page_fingerprint_sha256):
            raise ValueError('remote_json_review_current_bundle_stale')
    page_store = SiteKnowledgeStore(site_store, WebApplicationProfiles(profiles))
    page = page_store.get(record.knowledge_sha256)
    if (page_store.inspect(
            record.knowledge_sha256,
            selected_profile_sha256=selected_profile_sha256,
            current_fingerprint_sha256=record.page_fingerprint_sha256)['status']
            != 'draft_match' or page.page_key != candidate['page_key']
            or page.revision != candidate['draft_revision']):
        raise ValueError('remote_json_review_page_stale')
    report = {'schema_version': '1.0', 'mode': 'remote_json_knowledge_recheck',
              'status': 'reviewed_metadata_matched',
              'review_sha256': review_sha256,
              'knowledge_sha256': record.knowledge_sha256,
              'profile_sha256': selected_profile_sha256,
              'plan_sha256': selected_plan_sha256,
              'current_run_ref': current['run_ref'],
              'page_key': candidate['page_key'],
              'draft_revision': candidate['draft_revision'],
              'landmark_keys': page.landmark_keys,
              'page_fingerprint_sha256': record.page_fingerprint_sha256,
              'metadata_reviewed': True, 'current_readback_bound': True,
              'origin_verified': False, 'account_verified': False,
              'site_outcome_verified': False,
              'task_retrieval_authorized': False,
              'execution_authorized': False,
              'collection_authorized': False, 'training_ready': False}
    validator('remote_readonly_data_knowledge_recheck').validate(report)
    return report


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise SystemExit('JSON bundle review unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Review or recheck JSON-bundle metadata',
                                   allow_abbrev=False)
    parser.add_argument('command', choices=['register', 'recheck'])
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--site-store', type=Path, required=True)
    parser.add_argument('--review-store', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--before-run-id')
    parser.add_argument('--after-run-id')
    parser.add_argument('--knowledge-sha256')
    parser.add_argument('--confirm-candidate-sha256')
    parser.add_argument('--acknowledge-metadata-only', action='store_true')
    parser.add_argument('--current-run-id')
    parser.add_argument('--review-sha256')
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'register':
            if (arguments.current_run_id is not None or arguments.review_sha256 is not None
                    or arguments.before_run_id is None or arguments.after_run_id is None
                    or arguments.knowledge_sha256 is None
                    or arguments.confirm_candidate_sha256 is None):
                raise ValueError('remote_json_review_invalid_register_arguments')
            report = register_remote_readonly_data_knowledge_review(
                arguments.database, arguments.before_run_id, arguments.after_run_id,
                profiles=arguments.profiles, site_store=arguments.site_store,
                review_store=arguments.review_store,
                knowledge_sha256=arguments.knowledge_sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                selected_plan_sha256=arguments.selected_plan_sha256,
                confirm_candidate_sha256=arguments.confirm_candidate_sha256,
                acknowledge_metadata_only=arguments.acknowledge_metadata_only)
        else:
            if (arguments.current_run_id is None or arguments.review_sha256 is None
                    or arguments.before_run_id is not None or arguments.after_run_id is not None
                    or arguments.knowledge_sha256 is not None
                    or arguments.confirm_candidate_sha256 is not None
                    or arguments.acknowledge_metadata_only):
                raise ValueError('remote_json_review_invalid_recheck_arguments')
            report = recheck_remote_readonly_data_knowledge(
                arguments.database, arguments.current_run_id,
                profiles=arguments.profiles, site_store=arguments.site_store,
                review_store=arguments.review_store,
                review_sha256=arguments.review_sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'JSON bundle review unavailable: missing, stale or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
