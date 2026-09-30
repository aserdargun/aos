"""Private metadata review and fresh-run reuse of bounded remote route knowledge."""

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
from .remote_route_evidence import (RUN_ID, _review_remote_route_evidence,
                                    complete_link_sample_sha256)
from .remote_route_knowledge import preview_remote_route_knowledge
from .site_knowledge import SiteKnowledgeStore
from .web_application import Checksum, Key, WebApplicationProfiles
from .workspace_identity import open_existing_workspace


MAX_REVIEW_BYTES = 8192


class RemoteRouteKnowledgeReview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['manual_remote_route_metadata_review'] = 'manual_remote_route_metadata_review'
    before_run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    after_run_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')
    candidate_sha256: Checksum
    candidate_source_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    knowledge_sha256: Checksum
    page_fingerprint_sha256: Checksum
    route_index: int = Field(ge=0, le=7)
    recorded_at: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$')
    metadata_reviewed: Literal[True] = True
    origin_verified: Literal[False] = False
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


class RemoteRouteKnowledgeCandidatePreview(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['remote_route_metadata_candidate_preview'] = 'remote_route_metadata_candidate_preview'
    candidate_sha256: Checksum
    candidate: dict

    @model_validator(mode='after')
    def exact_candidate(self):
        validator('remote_route_knowledge').validate(self.candidate)
        if digest(self.candidate) != self.candidate_sha256:
            raise ValueError('remote_route_candidate_hash_mismatch')
        return self


class LiveRemoteRouteKnowledgePin(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['historical_remote_route_review_pin'] = 'historical_remote_route_review_pin'
    review_sha256: Checksum
    knowledge_sha256: Checksum
    profile_sha256: Checksum
    plan_sha256: Checksum
    source_snapshot_sha256: Checksum
    route_index: int = Field(ge=0, le=7)
    page_key: Key
    draft_revision: int = Field(ge=1, le=100)
    landmark_keys: list[Key] = Field(max_length=32)
    outgoing_page_keys: list[Key] = Field(max_length=16)
    outgoing_route_indices: list[int] = Field(default_factory=list, max_length=16)
    outgoing_fingerprint_sha256: list[Checksum] = Field(default_factory=list, max_length=16)
    page_fingerprint_sha256: Checksum
    link_sample_sha256: Checksum | None = None
    metadata_reviewed: Literal[True] = True
    current_readback_bound: Literal[False] = False
    task_retrieval_authorized: Literal[False] = False
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def bound_outgoing_links(self):
        if (bool(self.outgoing_page_keys) != (self.link_sample_sha256 is not None)
                or len(self.outgoing_page_keys) != len(self.outgoing_route_indices)
                or len(self.outgoing_page_keys) != len(self.outgoing_fingerprint_sha256)
                or len(set(self.outgoing_route_indices)) != len(self.outgoing_route_indices)
                or any(type(index) is not int or not 0 <= index <= 7
                       for index in self.outgoing_route_indices)):
            raise ValueError('remote_route_review_outgoing_links_unbound')
        return self


def candidate_source_sha256(candidate: dict) -> str:
    return digest({key: value for key, value in candidate.items()
                   if key != 'snapshot_sha256'})


class RemoteRouteKnowledgeReviewStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if not self.root.is_relative_to(REPO_ROOT / 'data'):
            raise ValueError('remote_route_review_requires_private_data_root')

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
    def _get(directory: int, checksum: str) -> RemoteRouteKnowledgeReview:
        if (not isinstance(checksum, str) or len(checksum) != 64
                or any(character not in '0123456789abcdef' for character in checksum)):
            raise ValueError('remote_route_review_invalid_checksum')
        name = checksum + '.json'
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_REVIEW_BYTES):
                raise ValueError('remote_route_review_invalid_private_file')
            payload = os.read(descriptor, MAX_REVIEW_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                      'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('remote_route_review_file_changed')
            record = RemoteRouteKnowledgeReview.model_validate_json(payload)
            if (payload != canonical(record.model_dump()).encode()
                    or hashlib.sha256(payload).hexdigest() != checksum
                    or not validator('remote_route_knowledge_review').is_valid(record.model_dump())):
                raise ValueError('remote_route_review_content_changed')
            return record
        finally:
            os.close(descriptor)

    def get(self, checksum: str) -> RemoteRouteKnowledgeReview:
        directory = self._directory()
        try:
            return self._get(directory, checksum)
        finally:
            os.close(directory)

    def register(self, record: RemoteRouteKnowledgeReview) -> str:
        record = RemoteRouteKnowledgeReview.model_validate(record.model_dump())
        if not validator('remote_route_knowledge_review').is_valid(record.model_dump()):
            raise ValueError('remote_route_review_invalid_record')
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
                raise ValueError('remote_route_review_store_limit')
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


def register_remote_route_knowledge_review(
        database: Path, before_run_id: str, after_run_id: str, *, profiles: Path,
        site_store: Path, review_store: Path, knowledge_sha256: str,
        selected_profile_sha256: str, selected_plan_sha256: str,
        route_index: int, confirm_candidate_sha256: str,
        acknowledge_metadata_only: bool) -> dict:
    if acknowledge_metadata_only is not True:
        raise ValueError('remote_route_review_requires_explicit_acknowledgement')
    candidate = preview_remote_route_knowledge(
        database, before_run_id, after_run_id, profiles=profiles, store=site_store,
        knowledge_sha256=knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, route_index=route_index)
    if digest(candidate) != confirm_candidate_sha256:
        raise ValueError('remote_route_review_requires_exact_candidate_hash')
    record = RemoteRouteKnowledgeReview(
        before_run_id=before_run_id, after_run_id=after_run_id,
        candidate_sha256=confirm_candidate_sha256,
        candidate_source_sha256=candidate_source_sha256(candidate),
        profile_sha256=selected_profile_sha256, plan_sha256=selected_plan_sha256,
        knowledge_sha256=knowledge_sha256,
        page_fingerprint_sha256=candidate['page_fingerprint_sha256'],
        route_index=route_index,
        recorded_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
    checksum = RemoteRouteKnowledgeReviewStore(review_store).register(record)
    receipt = {'schema_version': '1.0', 'mode': 'manual_remote_route_metadata_review',
               'status': 'metadata_review_recorded', 'review_sha256': checksum,
               'candidate_sha256': confirm_candidate_sha256,
               'metadata_reviewed': True, 'task_retrieval_authorized': False,
               'execution_authorized': False, 'collection_authorized': False,
               'training_ready': False}
    if not validator('remote_route_knowledge_review_receipt').is_valid(receipt):
        raise ValueError('remote_route_review_invalid_receipt')
    return receipt


def prepare_live_remote_route_knowledge(
        database: Path, *, profiles: Path, site_store: Path,
        review_store: Path, review_sha256: str, selected_profile_sha256: str,
        selected_plan_sha256: str) -> LiveRemoteRouteKnowledgePin:
    record = RemoteRouteKnowledgeReviewStore(review_store).get(review_sha256)
    if (record.profile_sha256 != selected_profile_sha256
            or record.plan_sha256 != selected_plan_sha256):
        raise ValueError('remote_route_review_scope_changed')
    candidate = preview_remote_route_knowledge(
        database, record.before_run_id, record.after_run_id,
        profiles=profiles, store=site_store,
        knowledge_sha256=record.knowledge_sha256,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256, route_index=record.route_index)
    if (candidate_source_sha256(candidate) != record.candidate_source_sha256
            or candidate['page_fingerprint_sha256'] != record.page_fingerprint_sha256):
        raise ValueError('remote_route_review_source_changed')
    page_store = SiteKnowledgeStore(site_store, WebApplicationProfiles(profiles))
    page = page_store.get(record.knowledge_sha256)
    inspection = page_store.inspect(
        record.knowledge_sha256, selected_profile_sha256=selected_profile_sha256,
        current_fingerprint_sha256=record.page_fingerprint_sha256)
    if (inspection['status'] != 'draft_match' or page.page_key != candidate['page_key']
            or page.revision != candidate['draft_revision']):
        raise ValueError('remote_route_review_page_stale')
    pin = LiveRemoteRouteKnowledgePin(
        review_sha256=review_sha256, knowledge_sha256=record.knowledge_sha256,
        profile_sha256=selected_profile_sha256, plan_sha256=selected_plan_sha256,
        source_snapshot_sha256=candidate['snapshot_sha256'],
        route_index=record.route_index, page_key=page.page_key,
        draft_revision=page.revision, landmark_keys=page.landmark_keys,
        outgoing_page_keys=page.outgoing_page_keys,
        outgoing_route_indices=candidate.get('outgoing_route_indices', []),
        outgoing_fingerprint_sha256=[
            page_store.get(checksum).page_fingerprint_sha256
            for checksum in candidate.get('outgoing_knowledge_sha256', [])],
        page_fingerprint_sha256=record.page_fingerprint_sha256,
        link_sample_sha256=candidate.get('link_sample_sha256'))
    if not validator('remote_route_knowledge_live_pin').is_valid(pin.model_dump()):
        raise ValueError('remote_route_review_invalid_live_pin')
    return pin


def retrieve_remote_route_knowledge(
        database: Path, current_run_id: str, *, profiles: Path, site_store: Path,
        review_store: Path, review_sha256: str, selected_profile_sha256: str,
        selected_plan_sha256: str) -> dict:
    if not isinstance(current_run_id, str) or RUN_ID.fullmatch(current_run_id) is None:
        raise ValueError('remote_route_review_invalid_current_run')
    pin = prepare_live_remote_route_knowledge(
        database, profiles=profiles, site_store=site_store,
        review_store=review_store, review_sha256=review_sha256,
        selected_profile_sha256=selected_profile_sha256,
        selected_plan_sha256=selected_plan_sha256)
    record = RemoteRouteKnowledgeReviewStore(review_store).get(review_sha256)
    if current_run_id in (record.before_run_id, record.after_run_id):
        raise ValueError('remote_route_review_scope_or_run_changed')
    with audit_snapshot(database) as (snapshot, identity):
        current = _review_remote_route_evidence(
            database, current_run_id, profiles=profiles,
            selected_profile_sha256=selected_profile_sha256,
            selected_plan_sha256=selected_plan_sha256,
            source=nullcontext((snapshot, identity)))
        if current['snapshot_sha256'] != identity['sha256']:
            raise ValueError('remote_route_review_source_changed')
        row = snapshot.execute('SELECT started_at FROM runs WHERE run_id=?',
                               (current_run_id,)).fetchone()
        if row is None or not isinstance(row['started_at'], str):
            raise ValueError('remote_route_review_fresh_run_missing')
        started_at = datetime.fromisoformat(row['started_at'])
        reviewed_at = datetime.fromisoformat(record.recorded_at.replace('Z', '+00:00'))
        if started_at.tzinfo is None:
            raise ValueError('remote_route_review_fresh_run_time_invalid')
        if '.' not in record.recorded_at:
            reviewed_at += timedelta(seconds=1)
        if started_at <= reviewed_at:
            raise ValueError('remote_route_review_run_precedes_review')
    if (current['snapshot_sha256'] != pin.source_snapshot_sha256
            or pin.route_index >= current['route_count']
            or current['routes'][pin.route_index]['page_fingerprint_sha256']
            != pin.page_fingerprint_sha256
            or pin.link_sample_sha256 is not None and complete_link_sample_sha256(
                current['routes'][pin.route_index]) != pin.link_sample_sha256):
        raise ValueError('remote_route_review_current_route_stale')
    if any(index >= current['route_count'] or
           current['routes'][index]['page_fingerprint_sha256'] != fingerprint
           for index, fingerprint in zip(pin.outgoing_route_indices,
                                         pin.outgoing_fingerprint_sha256)):
        raise ValueError('remote_route_review_current_target_stale')
    report = {'schema_version': '1.0', 'mode': 'remote_route_knowledge_reuse',
            'status': 'reviewed_metadata_reused', 'review_sha256': review_sha256,
            'knowledge_sha256': pin.knowledge_sha256,
            'profile_sha256': selected_profile_sha256,
            'plan_sha256': selected_plan_sha256,
            'current_run_ref': current['run_ref'], 'route_index': pin.route_index,
            'page_key': pin.page_key, 'draft_revision': pin.draft_revision,
            'landmark_keys': pin.landmark_keys,
            'outgoing_page_keys': pin.outgoing_page_keys,
            'page_fingerprint_sha256': pin.page_fingerprint_sha256,
            'metadata_reviewed': True, 'current_readback_bound': True,
            'origin_verified': False, 'account_verified': False,
            'site_outcome_verified': False, 'task_retrieval_authorized': False,
            'execution_authorized': False, 'collection_authorized': False,
            'training_ready': False}
    if pin.link_sample_sha256 is not None:
        report['link_sample_sha256'] = pin.link_sample_sha256
    if not validator('remote_route_knowledge_reuse').is_valid(report):
        raise ValueError('remote_route_review_invalid_reuse')
    return report


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit('Remote route review unavailable: invalid arguments.')


def main(argv: list[str] | None = None) -> None:
    parser = PrivateArgumentParser(description='Review or reuse remote route metadata',
                                   allow_abbrev=False)
    parser.add_argument('command', choices=['register', 'retrieve'])
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--site-store', type=Path, required=True)
    parser.add_argument('--review-store', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--before-run-id')
    parser.add_argument('--after-run-id')
    parser.add_argument('--knowledge-sha256')
    parser.add_argument('--route-index', type=int)
    parser.add_argument('--confirm-candidate-sha256')
    parser.add_argument('--acknowledge-metadata-only', action='store_true')
    parser.add_argument('--current-run-id')
    parser.add_argument('--review-sha256')
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'register':
            if (arguments.current_run_id is not None or arguments.review_sha256 is not None
                    or arguments.before_run_id is None or arguments.after_run_id is None
                    or arguments.knowledge_sha256 is None or arguments.route_index is None
                    or arguments.confirm_candidate_sha256 is None):
                raise ValueError('remote_route_review_invalid_register_arguments')
            report = register_remote_route_knowledge_review(
                arguments.database, arguments.before_run_id, arguments.after_run_id,
                profiles=arguments.profiles, site_store=arguments.site_store,
                review_store=arguments.review_store,
                knowledge_sha256=arguments.knowledge_sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                selected_plan_sha256=arguments.selected_plan_sha256,
                route_index=arguments.route_index,
                confirm_candidate_sha256=arguments.confirm_candidate_sha256,
                acknowledge_metadata_only=arguments.acknowledge_metadata_only)
        else:
            if (arguments.current_run_id is None or arguments.review_sha256 is None
                    or arguments.before_run_id is not None or arguments.after_run_id is not None
                    or arguments.knowledge_sha256 is not None or arguments.route_index is not None
                    or arguments.confirm_candidate_sha256 is not None
                    or arguments.acknowledge_metadata_only):
                raise ValueError('remote_route_review_invalid_retrieve_arguments')
            report = retrieve_remote_route_knowledge(
                arguments.database, arguments.current_run_id,
                profiles=arguments.profiles, site_store=arguments.site_store,
                review_store=arguments.review_store,
                review_sha256=arguments.review_sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                selected_plan_sha256=arguments.selected_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, RecursionError):
        parser.exit(1, 'Remote route review unavailable: missing, stale or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
