import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Annotated, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .lifecycle import private_directory
from .workspace_identity import open_existing_workspace


Key = Annotated[str, Field(pattern='^[a-z][a-z0-9_-]{0,63}$')]
Checksum = Annotated[str, Field(pattern='^[a-f0-9]{64}$')]
MAX_PROFILE_BYTES = 65536
MAX_PROFILES = 1000


def canonical_origin(value: str) -> tuple[str, bool]:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
            or any(character in value for character in ('\\', '%', '?', '#'))):
        raise ValueError('invalid_profile_url')
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or parsed.username is not None or parsed.password is not None:
        raise ValueError('invalid_profile_url')
    host = parsed.hostname
    if not host:
        raise ValueError('invalid_profile_url')
    local = host == 'localhost'
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (not local and (len(host) > 253 or '.' not in host or not re.search('[a-z]$', host)
                          or not all(re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                     for label in host.split('.')))):
            raise ValueError('invalid_profile_host') from None
        hostname = host
    else:
        local = str(address) in {'127.0.0.1', '::1'}
        if not local and not address.is_global:
            raise ValueError('private_profile_address_not_supported')
        hostname = '[' + address.compressed + ']' if address.version == 6 else address.compressed
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid_profile_port')
    origin = parsed.scheme + '://' + hostname
    if port is not None and port != (443 if parsed.scheme == 'https' else 80):
        origin += ':' + str(port)
    if parsed.netloc != origin.split('://', 1)[1]:
        raise ValueError('noncanonical_profile_origin')
    if parsed.scheme == 'http' and not local:
        raise ValueError('remote_profile_requires_https')
    return origin, local


class LearningPreferences(TypedModel):
    system1: Literal['disabled', 'requested']
    system2: Literal['disabled', 'requested']
    data_rights_ref: Key | None
    retention_days: int = Field(ge=1, le=365)
    raw_screenshots: Literal[False]
    automatic_training: Literal[False]
    automatic_promotion: Literal[False]

    @field_validator('raw_screenshots', 'automatic_training', 'automatic_promotion', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('profile_cannot_enable_execution')
        return value

    @model_validator(mode='after')
    def require_rights_reference(self):
        if 'requested' in (self.system1, self.system2) and self.data_rights_ref is None:
            raise ValueError('learning_request_requires_rights_reference')
        return self


class WebApplicationProfile(TypedModel):
    schema_version: Literal['1.0']
    application_key: Key
    revision: int = Field(ge=1, le=100)
    previous_sha256: Checksum | None
    environment: Literal['local_test', 'staging', 'production']
    tenant_key: Key
    account_role: Key
    entry_url: str = Field(min_length=1, max_length=2048)
    allowed_origins: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=16)
    task_keys: list[Key] = Field(min_length=1, max_length=32)
    learning: LearningPreferences

    @field_validator('entry_url')
    @classmethod
    def entry_is_canonical(cls, value):
        origin, _local = canonical_origin(value)
        path = urlsplit(value).path
        if (not path.startswith('/') or not re.fullmatch('/[A-Za-z0-9._~/-]*', path)
                or '//' in path or any(part in {'.', '..'} for part in path.split('/'))
                or value != origin + path):
            raise ValueError('noncanonical_profile_entry')
        return value

    @field_validator('allowed_origins')
    @classmethod
    def origins_are_canonical(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate_profile_origin')
        for value in values:
            if canonical_origin(value)[0] != value:
                raise ValueError('profile_origin_must_not_have_path')
        return sorted(values)

    @field_validator('task_keys')
    @classmethod
    def task_keys_are_unique(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('duplicate_profile_task')
        return sorted(values)

    @model_validator(mode='after')
    def validate_scope(self):
        if (self.revision == 1) != (self.previous_sha256 is None):
            raise ValueError('profile_revision_requires_parent')
        if canonical_origin(self.entry_url)[0] not in self.allowed_origins:
            raise ValueError('profile_entry_origin_not_declared')
        if any(canonical_origin(origin)[1] != (self.environment == 'local_test') for origin in self.allowed_origins):
            raise ValueError('profile_environment_origin_mismatch')
        return self


def readonly_resource_url_parts(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('noncanonical_readonly_resource_url')
    base_url, separator, query = value.partition('?')
    WebApplicationProfile.entry_is_canonical(base_url)
    if not separator:
        return base_url, ''
    if not query or len(query) > 512 or urlsplit(value).query != query:
        raise ValueError('noncanonical_readonly_resource_url')
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=8, encoding='utf-8', errors='strict')
    except ValueError:
        raise ValueError('noncanonical_readonly_resource_url') from None
    if (not 1 <= len(pairs) <= 8 or len({key for key, _value in pairs}) != len(pairs)
            or any(re.fullmatch('[A-Za-z][A-Za-z0-9._~-]{0,63}', key) is None
                   or len(item) > 128
                   or any(not 32 <= ord(character) <= 126 for character in item)
                   for key, item in pairs)
            or urlencode(pairs, quote_via=quote, safe='-._~') != query):
        raise ValueError('noncanonical_readonly_resource_url')
    return base_url, query


class WebApplicationReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    application_key: Key
    revision: int = Field(ge=1, le=100)
    profile_sha256: Checksum
    status: Literal['draft'] = 'draft'
    requested_learning_roles: list[Literal['system1', 'system2']] = Field(max_length=2)
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False
    promotion_authorized: Literal[False] = False
    blockers: list[Literal['site_runtime_not_integrated', 'task_contracts_not_bound',
                          'rights_review_not_verified', 'learning_pipeline_not_integrated']] = Field(min_length=2, max_length=4)

    @field_validator('execution_authorized', 'collection_authorized', 'training_ready', 'promotion_authorized', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('profile_cannot_enable_execution')
        return value

    @model_validator(mode='after')
    def blockers_are_complete(self):
        if self.requested_learning_roles != sorted(set(self.requested_learning_roles)):
            raise ValueError('duplicate_or_unordered_learning_role')
        expected = ['site_runtime_not_integrated', 'task_contracts_not_bound']
        if self.requested_learning_roles:
            expected += ['rights_review_not_verified', 'learning_pipeline_not_integrated']
        if self.blockers != expected:
            raise ValueError('profile_report_blockers_differ')
        return self


def profile_report(profile: WebApplicationProfile) -> WebApplicationReport:
    roles = [role for role in ('system1', 'system2') if getattr(profile.learning, role) == 'requested']
    blockers = ['site_runtime_not_integrated', 'task_contracts_not_bound']
    if roles:
        blockers += ['rights_review_not_verified', 'learning_pipeline_not_integrated']
    return WebApplicationReport(application_key=profile.application_key, revision=profile.revision,
                                profile_sha256=digest(profile.model_dump()),
                                requested_learning_roles=roles, blockers=blockers)


def read_source(path: Path) -> WebApplicationProfile:
    parent = open_existing_workspace(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PROFILE_BYTES:
                raise ValueError('invalid_profile_file')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_PROFILE_BYTES + 1)
            if len(payload) > MAX_PROFILE_BYTES:
                raise ValueError('profile_file_too_large')
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate_profile_key')
                    result[key] = value
                return result

            return WebApplicationProfile.model_validate(json.loads(payload, object_pairs_hook=unique_pairs))
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


class WebApplicationProfiles:
    def __init__(self, root: Path):
        self.root = root

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

    def _get(self, directory, checksum):
        if not isinstance(checksum, str) or re.fullmatch('[a-f0-9]{64}', checksum) is None:
            raise ValueError('invalid_profile_checksum')
        descriptor = os.open(checksum + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_PROFILE_BYTES):
                raise ValueError('invalid_private_profile_file')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_PROFILE_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(checksum + '.json', dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) > MAX_PROFILE_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('profile_file_changed')
            profile = WebApplicationProfile.model_validate_json(payload)
            if payload != canonical(profile.model_dump()).encode() or hashlib.sha256(payload).hexdigest() != checksum:
                raise ValueError('profile_content_hash_mismatch')
            return profile
        finally:
            os.close(descriptor)

    def _lineage(self, directory, profile):
        current = profile
        while current.previous_sha256 is not None:
            previous = self._get(directory, current.previous_sha256)
            if (previous.revision + 1 != current.revision or any(
                    getattr(previous, field) != getattr(current, field)
                    for field in ('application_key', 'tenant_key', 'account_role'))):
                raise ValueError('profile_lineage_mismatch')
            current = previous

    def get(self, checksum):
        directory = self.directory()
        try:
            profile = self._get(directory, checksum)
            self._lineage(directory, profile)
            return profile
        finally:
            os.close(directory)

    def preview(self, profile: WebApplicationProfile) -> WebApplicationReport:
        profile = WebApplicationProfile.model_validate_json(profile.model_dump_json())
        if profile.previous_sha256 is not None:
            directory = self.directory()
            try:
                self._lineage(directory, profile)
            finally:
                os.close(directory)
        return profile_report(profile)

    def register(self, profile: WebApplicationProfile, *, confirm_sha256: str):
        profile = WebApplicationProfile.model_validate_json(profile.model_dump_json())
        report = profile_report(profile)
        if confirm_sha256 != report.profile_sha256:
            raise ValueError('exact_profile_confirmation_required')
        directory = self.directory(create=True)
        temporary = '.profile-' + uuid4().hex
        try:
            self._lineage(directory, profile)
            filename = report.profile_sha256 + '.json'
            try:
                existing = self._get(directory, report.profile_sha256)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                return report
            if len(os.listdir(directory)) >= MAX_PROFILES:
                raise ValueError('profile_store_limit_exceeded')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(canonical(profile.model_dump()).encode())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                self._get(directory, report.profile_sha256)
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            self._get(directory, report.profile_sha256)
            return report
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    def list(self):
        try:
            directory = self.directory()
        except FileNotFoundError:
            return []
        try:
            names = os.listdir(directory)
            if len(names) > MAX_PROFILES:
                raise ValueError('profile_store_limit_exceeded')
            reports = []
            for filename in sorted(names):
                if filename.startswith('.profile-'):
                    continue
                if re.fullmatch(r'[a-f0-9]{64}\.json', filename) is None:
                    raise ValueError('unexpected_profile_store_file')
                profile = self._get(directory, filename[:-5])
                self._lineage(directory, profile)
                reports.append(profile_report(profile).model_dump())
            return reports
        finally:
            os.close(directory)


def main():
    parser = argparse.ArgumentParser(description='Draft web application profiles; no browser, collection or training execution')
    parser.add_argument('--store', type=Path, default=REPO_ROOT / 'data/web-applications')
    commands = parser.add_subparsers(dest='command', required=True)
    preview = commands.add_parser('preview')
    preview.add_argument('--profile', type=Path, required=True)
    register = commands.add_parser('register')
    register.add_argument('--profile', type=Path, required=True)
    register.add_argument('--confirm-sha256', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--sha256', required=True)
    commands.add_parser('list')
    arguments = parser.parse_args()
    try:
        profiles = WebApplicationProfiles(arguments.store)
        if arguments.command in {'preview', 'register'}:
            profile = read_source(arguments.profile)
            report = (profiles.register(profile, confirm_sha256=arguments.confirm_sha256)
                      if arguments.command == 'register' else profiles.preview(profile))
            value = report.model_dump()
        elif arguments.command == 'inspect':
            value = profile_report(profiles.get(arguments.sha256)).model_dump()
        else:
            value = {'profiles': profiles.list()}
        print(canonical(value))
    except (ValueError, OSError):
        parser.exit(1, 'Web profile rejected; check schema, scope, checksum, lineage and private file permissions.\n')


if __name__ == '__main__':
    main()
