"""Immutable, non-executable draft page knowledge scoped to one web profile revision."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .lifecycle import private_directory
from .web_application import Checksum, Key, WebApplicationProfiles, canonical_origin
from .workspace_identity import open_existing_workspace


MAX_KNOWLEDGE_BYTES = 16384
MAX_KNOWLEDGE_REVISIONS = 1000
ROUTE_SEGMENT = re.compile(r'(?:[a-z][a-z0-9_-]*|\{[a-z][a-z0-9_]*\})\Z')


class SitePageDraft(TypedModel):
    schema_version: Literal['1.0']
    profile_sha256: Checksum
    application_key: Key
    tenant_key: Key
    account_role: Key
    page_key: Key
    revision: int = Field(ge=1, le=100)
    previous_sha256: Checksum | None
    origin: str = Field(min_length=1, max_length=300)
    route_template: str = Field(min_length=1, max_length=256)
    page_fingerprint_sha256: Checksum
    recorded_at: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
    landmark_keys: list[Key] = Field(max_length=32)
    outgoing_page_keys: list[Key] = Field(max_length=16)
    source_kind: Literal['manual_draft']
    status: Literal['draft']
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('origin')
    @classmethod
    def exact_origin(cls, value):
        if canonical_origin(value)[0] != value:
            raise ValueError('site_page_origin_not_canonical')
        return value

    @field_validator('route_template')
    @classmethod
    def symbolic_route(cls, value):
        if value != '/' and (not value.startswith('/') or value.endswith('/')
                             or any(ROUTE_SEGMENT.fullmatch(segment) is None for segment in value[1:].split('/'))):
            raise ValueError('site_page_route_not_symbolic')
        return value

    @field_validator('recorded_at')
    @classmethod
    def valid_utc_time(cls, value):
        try:
            datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError as error:
            raise ValueError('site_page_invalid_recorded_at') from error
        return value

    @field_validator('landmark_keys', 'outgoing_page_keys')
    @classmethod
    def unique_sorted_keys(cls, values):
        if values != sorted(set(values)):
            raise ValueError('site_page_keys_not_canonical')
        return values

    @field_validator('execution_authorized', 'collection_authorized', 'training_ready', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('site_page_cannot_authorize')
        return value

    @model_validator(mode='after')
    def valid_revision(self):
        if (self.revision == 1) != (self.previous_sha256 is None):
            raise ValueError('site_page_revision_requires_parent')
        if self.page_key in self.outgoing_page_keys:
            raise ValueError('site_page_self_link')
        return self


def validate_page_profile(profiles: WebApplicationProfiles, page: SitePageDraft):
    profile = profiles.get(page.profile_sha256)
    if (profile.application_key != page.application_key or profile.tenant_key != page.tenant_key
            or profile.account_role != page.account_role or page.origin not in profile.allowed_origins):
        raise ValueError('site_page_profile_scope_mismatch')
    return profile


class SiteKnowledgeStore:
    def __init__(self, root: Path, profiles: WebApplicationProfiles):
        self.root = root
        self.profiles = profiles

    def directory(self, *, create=False):
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

    def _get(self, directory: int, checksum: str) -> SitePageDraft:
        if not isinstance(checksum, str) or re.fullmatch('[a-f0-9]{64}', checksum) is None:
            raise ValueError('invalid_site_page_checksum')
        descriptor = os.open(checksum + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_KNOWLEDGE_BYTES):
                raise ValueError('invalid_private_site_page')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_KNOWLEDGE_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(checksum + '.json', dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) > MAX_KNOWLEDGE_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('site_page_file_changed')
            page = SitePageDraft.model_validate_json(payload)
            if payload != canonical(page.model_dump()).encode() or hashlib.sha256(payload).hexdigest() != checksum:
                raise ValueError('site_page_content_hash_mismatch')
            return page
        finally:
            os.close(descriptor)

    def _lineage(self, directory: int, page: SitePageDraft):
        current = page
        while current.previous_sha256 is not None:
            previous = self._get(directory, current.previous_sha256)
            if (previous.revision + 1 != current.revision or any(
                    getattr(previous, field) != getattr(current, field)
                    for field in ('profile_sha256', 'application_key', 'tenant_key', 'account_role', 'page_key'))):
                raise ValueError('site_page_lineage_mismatch')
            current = previous

    def get(self, checksum: str) -> SitePageDraft:
        directory = self.directory()
        try:
            page = self._get(directory, checksum)
            self._lineage(directory, page)
            validate_page_profile(self.profiles, page)
            return page
        finally:
            os.close(directory)

    def register(self, page: SitePageDraft, *, confirm_sha256: str) -> str:
        page = SitePageDraft.model_validate_json(page.model_dump_json())
        validate_page_profile(self.profiles, page)
        checksum = digest(page.model_dump())
        if checksum != confirm_sha256:
            raise ValueError('exact_site_page_confirmation_required')
        directory = self.directory(create=True)
        temporary = '.page-' + uuid4().hex
        try:
            self._lineage(directory, page)
            try:
                self._get(directory, checksum)
                return checksum
            except FileNotFoundError:
                pass
            if len(os.listdir(directory)) >= MAX_KNOWLEDGE_REVISIONS:
                raise ValueError('site_page_store_limit_exceeded')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(canonical(page.model_dump()).encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, checksum + '.json', src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                self._get(directory, checksum)
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

    def inspect(self, checksum: str, *, selected_profile_sha256: str,
                current_fingerprint_sha256: str) -> dict:
        page = self.get(checksum)
        if (not isinstance(selected_profile_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', selected_profile_sha256) is None
                or not isinstance(current_fingerprint_sha256, str)
                or re.fullmatch('[a-f0-9]{64}', current_fingerprint_sha256) is None):
            raise ValueError('invalid_site_page_inspection_input')
        revisions = {
            report['knowledge_sha256']: self.get(report['knowledge_sha256'])
            for report in self.list(profile_sha256=page.profile_sha256, page_key=page.page_key)
        }
        if sum(revision.previous_sha256 is None for revision in revisions.values()) != 1:
            raise ValueError('ambiguous_site_page_roots')
        children = {}
        for revision_sha256, revision in revisions.items():
            if revision.previous_sha256 is not None:
                children.setdefault(revision.previous_sha256, []).append(revision_sha256)
        if any(len(descendants) != 1 for descendants in children.values()):
            raise ValueError('ambiguous_site_page_revision')
        superseded = checksum in children
        status = ('draft_match' if selected_profile_sha256 == page.profile_sha256
                  and current_fingerprint_sha256 == page.page_fingerprint_sha256
                  and not superseded else 'stale')
        return {'schema_version': '1.0', 'knowledge_sha256': checksum,
                'profile_sha256': page.profile_sha256, 'page_key': page.page_key,
                'revision': page.revision, 'status': status,
                'execution_authorized': False, 'collection_authorized': False,
                'training_ready': False}

    def list(self, *, profile_sha256: str, page_key: str | None = None) -> list[dict]:
        self.profiles.get(profile_sha256)
        if page_key is not None and (not isinstance(page_key, str)
                                     or re.fullmatch('[a-z][a-z0-9_-]{0,63}', page_key) is None):
            raise ValueError('invalid_site_page_key')
        try:
            directory = self.directory()
        except FileNotFoundError:
            return []
        try:
            names = os.listdir(directory)
            if len(names) > MAX_KNOWLEDGE_REVISIONS:
                raise ValueError('site_page_store_limit_exceeded')
            reports = []
            for filename in sorted(names):
                if re.fullmatch(r'\.page-[a-f0-9]{32}', filename):
                    continue
                if re.fullmatch(r'[a-f0-9]{64}\.json', filename) is None:
                    raise ValueError('unexpected_site_page_store_file')
                checksum = filename[:-5]
                page = self._get(directory, checksum)
                self._lineage(directory, page)
                validate_page_profile(self.profiles, page)
                if page.profile_sha256 == profile_sha256 and (page_key is None or page.page_key == page_key):
                    reports.append({'schema_version': '1.0', 'knowledge_sha256': checksum,
                                    'profile_sha256': page.profile_sha256, 'page_key': page.page_key,
                                    'revision': page.revision, 'status': 'draft',
                                    'execution_authorized': False, 'collection_authorized': False,
                                    'training_ready': False})
            return sorted(reports, key=lambda item: (item['page_key'], item['revision'], item['knowledge_sha256']))
        finally:
            os.close(directory)


def read_page_source(path: Path) -> SitePageDraft:
    parent = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_KNOWLEDGE_BYTES:
                raise ValueError('invalid_site_page_source')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_KNOWLEDGE_BYTES + 1)
            if len(payload) > MAX_KNOWLEDGE_BYTES:
                raise ValueError('site_page_source_too_large')
            return SitePageDraft.model_validate_json(payload)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def main():
    parser = argparse.ArgumentParser(description='Private, non-executable site page drafts')
    parser.add_argument('--profiles', type=Path, default=REPO_ROOT / 'data/web-applications')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/site-knowledge')
    commands = parser.add_subparsers(dest='command', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--page', type=Path, required=True)
    register = commands.add_parser('register')
    register.add_argument('--page', type=Path, required=True)
    register.add_argument('--confirm-sha256', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--sha256', required=True)
    inspect.add_argument('--selected-profile-sha256', required=True)
    inspect.add_argument('--current-fingerprint-sha256', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--profile-sha256', required=True)
    listing.add_argument('--page-key')
    arguments = parser.parse_args()
    store = SiteKnowledgeStore(arguments.store, WebApplicationProfiles(arguments.profiles))
    try:
        if arguments.command in {'preview', 'register'}:
            page = read_page_source(arguments.page)
            validate_page_profile(store.profiles, page)
            checksum = digest(page.model_dump())
            if arguments.command == 'register':
                store.register(page, confirm_sha256=arguments.confirm_sha256)
            report = {'schema_version': '1.0', 'knowledge_sha256': checksum,
                      'profile_sha256': page.profile_sha256, 'page_key': page.page_key,
                      'revision': page.revision, 'status': 'draft',
                      'execution_authorized': False, 'collection_authorized': False,
                      'training_ready': False}
        elif arguments.command == 'inspect':
            report = store.inspect(arguments.sha256,
                selected_profile_sha256=arguments.selected_profile_sha256,
                current_fingerprint_sha256=arguments.current_fingerprint_sha256)
        else:
            report = {'schema_version': '1.0', 'mode': 'draft_metadata_list',
                      'pages': store.list(profile_sha256=arguments.profile_sha256,
                                          page_key=arguments.page_key)}
    except (OSError, ValueError):
        raise SystemExit('Site knowledge draft operation failed') from None
    print(canonical(report))


if __name__ == '__main__':
    main()
