"""Private symbolic skill drafts; never an executor or an authority source."""

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .lifecycle import private_directory
from .site_knowledge import SiteKnowledgeStore
from .web_application import Checksum, Key, WebApplicationProfiles
from .workspace_identity import open_existing_workspace


MAX_SKILL_BYTES = 16384
MAX_SKILL_DRAFTS = 1000
EventId = Annotated[str, Field(pattern=r'^learning-[a-f0-9]{64}$')]


class SiteSkillDraft(TypedModel):
    schema_version: Literal['1.0']
    profile_sha256: Checksum
    application_key: Key
    tenant_key: Key
    account_role: Key
    task_key: Key
    page_key: Key
    page_draft_sha256: Checksum
    skill_key: Key
    model_role: Literal['system1', 'system2']
    candidate_kind: Literal['finite_action_choice', 'workflow_plan']
    revision: int = Field(ge=1, le=100)
    previous_sha256: Checksum | None
    parameter_keys: list[Key] = Field(max_length=16)
    precondition_keys: list[Key] = Field(max_length=16)
    step_keys: list[Key] = Field(min_length=1, max_length=16)
    expected_outcome_key: Key
    source_event_ids: list[EventId] = Field(min_length=1, max_length=32)
    source_verification_ids: list[Key] = Field(max_length=32)
    source_kind: Literal['manual_candidate']
    status: Literal['draft']
    execution_authorized: Literal[False]
    collection_authorized: Literal[False]
    training_ready: Literal[False]
    activation_authorized: Literal[False]

    @field_validator('source_event_ids')
    @classmethod
    def valid_event_ids(cls, values):
        if any(re.fullmatch(r'learning-[a-f0-9]{64}', value) is None for value in values):
            raise ValueError('invalid_skill_event_id')
        return values

    @field_validator('parameter_keys', 'precondition_keys', 'step_keys',
                     'source_event_ids', 'source_verification_ids')
    @classmethod
    def canonical_keys(cls, values):
        if values != sorted(set(values)):
            raise ValueError('skill_refs_not_canonical')
        return values

    @field_validator('execution_authorized', 'collection_authorized', 'training_ready',
                     'activation_authorized', mode='before')
    @classmethod
    def false_is_boolean(cls, value):
        if value is not False:
            raise ValueError('skill_cannot_authorize')
        return value

    @model_validator(mode='after')
    def valid_draft(self):
        if (self.revision == 1) != (self.previous_sha256 is None):
            raise ValueError('skill_revision_requires_parent')
        expected = 'finite_action_choice' if self.model_role == 'system1' else 'workflow_plan'
        if self.candidate_kind != expected:
            raise ValueError('skill_role_kind_mismatch')
        return self


class SiteSkillStore:
    def __init__(self, root: Path, profiles: WebApplicationProfiles, pages: SiteKnowledgeStore):
        self.root = root
        self.profiles = profiles
        self.pages = pages

    def _scope(self, skill: SiteSkillDraft):
        profile = self.profiles.get(skill.profile_sha256)
        if (profile.application_key != skill.application_key or profile.tenant_key != skill.tenant_key
                or profile.account_role != skill.account_role or skill.task_key not in profile.task_keys
                or getattr(profile.learning, skill.model_role) != 'requested'):
            raise ValueError('skill_profile_scope_mismatch')
        page = self.pages.get(skill.page_draft_sha256)
        if (page.profile_sha256 != skill.profile_sha256 or page.application_key != skill.application_key
                or page.tenant_key != skill.tenant_key or page.account_role != skill.account_role
                or page.page_key != skill.page_key):
            raise ValueError('skill_page_scope_mismatch')

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

    def _get(self, directory: int, checksum: str) -> SiteSkillDraft:
        if not isinstance(checksum, str) or re.fullmatch('[a-f0-9]{64}', checksum) is None:
            raise ValueError('invalid_site_skill_checksum')
        descriptor = os.open(checksum + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > MAX_SKILL_BYTES):
                raise ValueError('invalid_private_site_skill')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                payload = stream.read(MAX_SKILL_BYTES + 1)
            after = os.fstat(descriptor)
            linked = os.stat(checksum + '.json', dir_fd=directory, follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink', 'st_mode', 'st_uid')
            if (len(payload) > MAX_SKILL_BYTES or len(payload) != before.st_size
                    or any(getattr(before, field) != getattr(current, field)
                           for field in fields for current in (after, linked))):
                raise ValueError('site_skill_file_changed')
            skill = SiteSkillDraft.model_validate_json(payload)
            if payload != canonical(skill.model_dump()).encode() or hashlib.sha256(payload).hexdigest() != checksum:
                raise ValueError('site_skill_content_hash_mismatch')
            return skill
        finally:
            os.close(descriptor)

    def _lineage(self, directory: int, skill: SiteSkillDraft):
        current = skill
        while current.previous_sha256 is not None:
            previous = self._get(directory, current.previous_sha256)
            if previous.revision + 1 != current.revision or any(
                    getattr(previous, field) != getattr(current, field)
                    for field in ('profile_sha256', 'application_key', 'tenant_key', 'account_role',
                                  'task_key', 'page_key', 'skill_key', 'model_role', 'candidate_kind')):
                raise ValueError('site_skill_lineage_mismatch')
            current = previous

    def preview(self, skill: SiteSkillDraft) -> dict:
        skill = SiteSkillDraft.model_validate(skill.model_dump())
        self._scope(skill)
        if skill.previous_sha256 is not None:
            directory = self.directory()
            try:
                self._lineage(directory, skill)
            finally:
                os.close(directory)
        return self._report(skill, digest(skill.model_dump()))

    @staticmethod
    def _report(skill: SiteSkillDraft, checksum: str) -> dict:
        return {'schema_version': '1.0', 'skill_sha256': checksum,
                'profile_sha256': skill.profile_sha256, 'skill_key': skill.skill_key,
                'model_role': skill.model_role, 'revision': skill.revision, 'status': 'draft',
                'execution_authorized': False, 'collection_authorized': False,
                'training_ready': False, 'activation_authorized': False}

    def get(self, checksum: str) -> SiteSkillDraft:
        directory = self.directory()
        try:
            skill = self._get(directory, checksum)
            self._lineage(directory, skill)
            self._scope(skill)
            return skill
        finally:
            os.close(directory)

    def register(self, skill: SiteSkillDraft, *, confirm_sha256: str) -> str:
        skill = SiteSkillDraft.model_validate(skill.model_dump())
        self._scope(skill)
        checksum = digest(skill.model_dump())
        if checksum != confirm_sha256:
            raise ValueError('exact_site_skill_confirmation_required')
        directory = self.directory(create=True)
        temporary = '.skill-' + uuid4().hex
        try:
            self._lineage(directory, skill)
            try:
                self._get(directory, checksum)
                return checksum
            except FileNotFoundError:
                pass
            if len(os.listdir(directory)) >= MAX_SKILL_DRAFTS:
                raise ValueError('site_skill_store_limit_exceeded')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(canonical(skill.model_dump()).encode())
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

    def list(self, *, profile_sha256: str, model_role: str) -> list[dict]:
        self.profiles.get(profile_sha256)
        if model_role not in ('system1', 'system2'):
            raise ValueError('invalid_skill_model_role')
        try:
            directory = self.directory()
        except FileNotFoundError:
            return []
        try:
            names = os.listdir(directory)
            if len(names) > MAX_SKILL_DRAFTS:
                raise ValueError('site_skill_store_limit_exceeded')
            reports = []
            for filename in sorted(names):
                if re.fullmatch(r'\.skill-[a-f0-9]{32}', filename):
                    continue
                if re.fullmatch(r'[a-f0-9]{64}\.json', filename) is None:
                    raise ValueError('unexpected_site_skill_store_file')
                checksum = filename[:-5]
                skill = self._get(directory, checksum)
                self._lineage(directory, skill)
                self._scope(skill)
                if skill.profile_sha256 == profile_sha256 and skill.model_role == model_role:
                    reports.append(self._report(skill, checksum))
            return sorted(reports, key=lambda item: (item['skill_key'], item['revision'], item['skill_sha256']))
        finally:
            os.close(directory)
