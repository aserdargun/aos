"""Explicit human review and permanent revocation of manual bootstrap candidates."""

import os
from pathlib import Path
import re
import stat
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .knowledge import Hash, KnowledgeScope
from .knowledge_answer import StoreIdentity
from .owned_form_candidate_execution import _read_private_child
from .owned_parameter_project_execution import _BootstrapFlags
from .owned_parameter_skill_candidate import OwnedParameterSkillCandidateSession, _hash
from .web_goal_execution_journal import (
    MAX_INTENTS, MAX_RECORD_BYTES, WebGoalExecutionJournal, _directory_identity,
)
from .workspace_identity import open_existing_workspace


ACCEPT_CONFIRMATION = 'ACCEPT_MANUAL_REVIEW'
REVOKE_CONFIRMATION = 'REVOKE_MANUAL_REVIEW'
RECOVERY_CONFIRMATION = 'RESTORE_ANCHORED_REVIEW_RECORD'
REVIEW_NAMES = re.compile(r'^([a-f0-9]{64})\.(accept|revoke)\.json$')


class _ReviewFlags(_BootstrapFlags):
    synthetic: Literal[True]
    development_only: Literal[True]
    execution_authorized: Literal[False] = False
    account_scope_verified: Literal[False] = False
    held_out_independence_verified: Literal[False] = False
    runtime_started: Literal[False] = False
    model_calls: Literal[0] = 0

    @field_validator('synthetic', 'development_only', mode='before')
    @classmethod
    def true_claims(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value

    @field_validator('execution_authorized', 'account_scope_verified',
                     'held_out_independence_verified', 'runtime_started', mode='before')
    @classmethod
    def false_claims(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value

    @field_validator('model_calls', mode='before')
    @classmethod
    def no_model_calls(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value


class OwnedParameterSkillReview(_ReviewFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_manual_review']
    decision: Literal['accept']
    review_scope: Literal['synthetic_manual_recipe_review']
    reviewer: Literal['local_authenticated_user']
    reviewed: Literal[True]
    candidate_sha256: Hash
    intent_sha256: Hash
    manifest_sha256: Hash
    source_fingerprint_sha256: Hash
    source_run_ref: Hash
    source_invocation_sha256: Hash
    recipe_audit_sha256: Hash
    recipe_sha256: Hash
    field_binding_sha256: Hash
    form_body_sha256: Hash
    scope: KnowledgeScope
    review_directory_sha256: Hash
    review_parent_identity: StoreIdentity

    @field_validator('reviewed', mode='before')
    @classmethod
    def reviewed_is_true(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value


class OwnedParameterSkillReviewRevocation(_ReviewFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_manual_review_revocation']
    decision: Literal['revoke']
    review_scope: Literal['synthetic_manual_recipe_review']
    reviewer: Literal['local_authenticated_user']
    reviewed: Literal[False]
    review_sha256: Hash
    candidate_sha256: Hash
    source_fingerprint_sha256: Hash
    scope: KnowledgeScope
    review_directory_sha256: Hash
    review_parent_identity: StoreIdentity

    @field_validator('reviewed', mode='before')
    @classmethod
    def reviewed_is_false(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value


class OwnedParameterSkillReviewRevocationEntry(TypedModel):
    revocation_sha256: Hash
    revocation: OwnedParameterSkillReviewRevocation

    @model_validator(mode='after')
    def exact_revocation_hash(self):
        if self.revocation_sha256 != digest(self.revocation.model_dump(mode='json')):
            raise ValueError('owned_parameter_review_revocation_changed')
        return self


class OwnedParameterSkillReviewStatus(_ReviewFlags):
    model_config = {'json_schema_extra': {'allOf': [
        {'if': {'properties': {'status': {'const': 'accepted'}}},
         'then': {'properties': {'reviewed': {'const': True}, 'revocations': {'maxItems': 0}}},
         'else': {'properties': {'reviewed': {'const': False}, 'revocations': {'minItems': 1}}}}
    ]}}

    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_manual_review_status']
    review_sha256: Hash
    review: OwnedParameterSkillReview
    revocations: list[OwnedParameterSkillReviewRevocationEntry] = Field(max_length=1)
    status: Literal['accepted', 'revoked']
    reviewed: bool = Field(strict=True)

    @model_validator(mode='after')
    def exact_status(self):
        review = self.review.model_dump(mode='json')
        revoked = bool(self.revocations)
        if (self.review_sha256 != digest(review)
                or self.status != ('revoked' if revoked else 'accepted')
                or self.reviewed != (not revoked)):
            raise ValueError('owned_parameter_review_status_changed')
        for entry in self.revocations:
            _revocation_matches(review, entry.revocation.model_dump(mode='json'))
        return self


class OwnedParameterSkillReviewRecovery(_ReviewFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_review_recovery']
    operation: Literal['restore_anchored_review_record']
    reviewed: Literal[False]
    record_sha256: Hash
    record_stage: Literal['accept', 'revoke']
    record: OwnedParameterSkillReview | OwnedParameterSkillReviewRevocation
    candidate_sha256: Hash
    review_sha256: Hash
    scope: KnowledgeScope
    source_fingerprint_sha256: Hash
    review_directory_sha256: Hash
    review_parent_identity: StoreIdentity
    review_store_identity: StoreIdentity

    @field_validator('reviewed', mode='before')
    @classmethod
    def reviewed_is_false(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_review_claim_invalid')
        return value

    @model_validator(mode='after')
    def exact_record(self):
        record = self.record.model_dump(mode='json')
        accepted = isinstance(self.record, OwnedParameterSkillReview)
        bound = {'candidate_sha256': self.candidate_sha256, 'scope': self.scope.model_dump(mode='json'),
                 'source_fingerprint_sha256': self.source_fingerprint_sha256,
                 'review_directory_sha256': self.review_directory_sha256,
                 'review_parent_identity': self.review_parent_identity.model_dump(mode='json')}
        if (self.record_sha256 != digest(record)
                or self.record_stage != ('accept' if accepted else 'revoke')
                or self.review_sha256 != (self.record_sha256 if accepted else record['review_sha256'])
                or any(value != record[key] for key, value in bound.items())):
            raise ValueError('owned_parameter_review_recovery_binding_changed')
        return self


def _revocation_matches(review, revocation):
    if (revocation['review_sha256'] != digest(review)
            or any(revocation[key] != review[key] for key in (
                'candidate_sha256', 'source_fingerprint_sha256', 'scope',
                'review_directory_sha256', 'review_parent_identity'))):
        raise ValueError('owned_parameter_review_revocation_binding_changed')


class _ReviewStore(WebGoalExecutionJournal):
    def __init__(self, directory, history):
        super().__init__(directory)
        self.history = history
        self._observed_names = set()

    def _write(self, descriptor, filename, record):
        self.history.authorize(record)
        self._materialize(descriptor, filename, record)

    def _materialize(self, descriptor, filename, record):
        super()._write(descriptor, filename, record)
        self._observed_names.add(filename)

    def _read_record(self, descriptor, checksum, stage):
        model = OwnedParameterSkillReview if stage == 'accept' else OwnedParameterSkillReviewRevocation
        content = _read_private_child(descriptor, checksum + '.' + stage + '.json', MAX_RECORD_BYTES)
        record = model.model_validate_json(content).model_dump(mode='json')
        if canonical(record).encode() != content or digest(record) != checksum:
            raise ValueError('owned_parameter_review_store_record_changed')
        return record

    def _inventory(self, descriptor, workspace, identity, *, allow_missing=False):
        names = os.listdir(descriptor)
        if len(names) > MAX_INTENTS * 2 or any(REVIEW_NAMES.fullmatch(name) is None for name in names):
            raise ValueError('owned_parameter_review_store_inventory_invalid')
        if not allow_missing and not self._observed_names.issubset(names):
            raise ValueError('owned_parameter_review_observed_record_missing')
        anchored = self.history.records(digest({'directory': str(self.directory)}))
        if (len(anchored) > MAX_INTENTS * 2
                or any(REVIEW_NAMES.fullmatch(name) is None for name in anchored)
                or not set(names).issubset(anchored)):
            raise ValueError('owned_parameter_review_unanchored_record')
        if not allow_missing and set(names) != set(anchored):
            raise ValueError('owned_parameter_review_anchored_record_missing')
        for name in names:
            checksum, stage = REVIEW_NAMES.fullmatch(name).groups()
            if self._read_record(descriptor, checksum, stage) != anchored[name]:
                raise ValueError('owned_parameter_review_history_record_changed')
        reviews, revocations = {}, {}
        for name, record in anchored.items():
            checksum, stage = REVIEW_NAMES.fullmatch(name).groups()
            model = OwnedParameterSkillReview if stage == 'accept' else OwnedParameterSkillReviewRevocation
            parsed = model.model_validate_json(canonical(record)).model_dump(mode='json')
            if parsed != record or digest(record) != checksum:
                raise ValueError('owned_parameter_review_history_record_changed')
            if stage == 'accept':
                if (record['review_directory_sha256'] != digest({'directory': str(self.directory)})
                        or record['review_parent_identity'] != _directory_identity_from_workspace(workspace)):
                    raise ValueError('owned_parameter_review_store_destination_changed')
                reviews[checksum] = record
            else:
                review_sha256 = record['review_sha256']
                if review_sha256 in revocations:
                    raise ValueError('owned_parameter_review_duplicate_revocation')
                revocations[review_sha256] = (checksum, record)
        if len(reviews) > MAX_INTENTS:
            raise ValueError('owned_parameter_review_store_full')
        candidates = set()
        for review in reviews.values():
            if review['candidate_sha256'] in candidates:
                raise ValueError('owned_parameter_review_duplicate_candidate')
            candidates.add(review['candidate_sha256'])
        for review_sha256, (_checksum, revocation) in revocations.items():
            if review_sha256 not in reviews:
                raise ValueError('owned_parameter_review_orphan_revocation')
            _revocation_matches(reviews[review_sha256], revocation)
        self._observed_names.update(names)
        return reviews, revocations


def _directory_identity_from_workspace(workspace):
    return {'device': workspace['device'], 'inode': workspace['inode'],
            'owner': workspace['owner_uid'], 'mode': stat.S_IFDIR | 0o700}


class OwnedParameterSkillReviewSession:
    def __init__(self, candidate_session, review_directory):
        if not isinstance(candidate_session, OwnedParameterSkillCandidateSession):
            raise ValueError('owned_parameter_review_candidate_session_required')
        from .owned_parameter_skill_review_history import OwnedParameterSkillReviewHistory

        self.candidate_session = candidate_session
        self.store = _ReviewStore(review_directory, OwnedParameterSkillReviewHistory(candidate_session.database))
        directory = self.store.directory
        protected = (candidate_session.database, candidate_session.journal.directory,
                     candidate_session.candidates.directory)
        if (directory == REPO_ROOT or REPO_ROOT in directory.parents
                or directory == candidate_session.directory
                or directory in candidate_session.directory.parents
                or any(directory == path or directory in path.parents or path in directory.parents
                       for path in protected)):
            raise ValueError('owned_parameter_review_paths_invalid')

    def _destination(self):
        descriptor = open_existing_workspace(self.store.directory.parent)
        try:
            identity = _directory_identity(descriptor)
        finally:
            os.close(descriptor)
        return {'review_directory_sha256': digest({'directory': str(self.store.directory)}),
                'review_parent_identity': identity}

    def _proposal(self, candidate_sha256):
        candidate, checksum = self.candidate_session.read(_hash(candidate_sha256))
        if checksum != candidate_sha256 or digest(candidate) != checksum:
            raise ValueError('owned_parameter_review_candidate_changed')
        workspace_pin = candidate['bootstrap_intent']['execution_workspace_identity']['path_sha256']
        if any(digest({'workspace': str(path)}) == workspace_pin
               for path in (self.store.directory, *self.store.directory.parents)):
            raise ValueError('owned_parameter_review_execution_workspace_forbidden')
        review = OwnedParameterSkillReview.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_review',
            'synthetic': True, 'development_only': True, 'decision': 'accept',
            'review_scope': 'synthetic_manual_recipe_review', 'reviewer': 'local_authenticated_user',
            'reviewed': True, 'candidate_sha256': checksum,
            **{key: candidate[key] for key in ('intent_sha256', 'manifest_sha256',
                'source_fingerprint_sha256', 'recipe_audit_sha256', 'form_body_sha256', 'scope')},
            'source_run_ref': candidate['recipe_audit']['run_ref'],
            'source_invocation_sha256': candidate['bootstrap_run_binding']['run_identity']['skill_invocation_sha256'],
            'recipe_sha256': digest(candidate['recipe']),
            'field_binding_sha256': digest(candidate['field_bindings']),
            **self._destination(),
        })).model_dump(mode='json')
        return review, digest(review)

    def _current(self, review, checksum):
        current, current_sha256 = self._proposal(review['candidate_sha256'])
        if current != review or current_sha256 != checksum:
            raise ValueError('owned_parameter_review_source_changed')

    def _optional_inventory(self):
        try:
            with self.store._locked() as (descriptor, workspace, identity):
                return self.store._inventory(descriptor, workspace, identity)
        except FileNotFoundError:
            if (self.store._journal_identity is not None
                    or self.store.history.records(digest({'directory': str(self.store.directory)}))):
                raise ValueError('owned_parameter_review_store_missing') from None
            return {}, {}

    def preview(self, candidate_sha256):
        review, checksum = self._proposal(candidate_sha256)
        reviews, revocations = self._optional_inventory()
        if checksum in revocations:
            raise ValueError('owned_parameter_review_permanently_revoked')
        if any(existing['candidate_sha256'] == candidate_sha256 and existing != review
               for existing in reviews.values()):
            raise ValueError('owned_parameter_review_candidate_already_bound')
        return review, checksum

    def accept(self, candidate_sha256, confirm_review_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != ACCEPT_CONFIRMATION:
            raise ValueError('owned_parameter_review_human_confirmation_required')
        checksum = _hash(confirm_review_sha256)
        review, actual_checksum = self.preview(candidate_sha256)
        if checksum != actual_checksum:
            raise ValueError('owned_parameter_review_confirmation_changed')
        with self.store._locked(create=True, write=True) as (descriptor, workspace, identity):
            reviews, revocations = self.store._inventory(descriptor, workspace, identity)
            if checksum in revocations:
                raise ValueError('owned_parameter_review_permanently_revoked')
            if checksum not in reviews and len(reviews) >= MAX_INTENTS:
                raise ValueError('owned_parameter_review_store_full')
            if any(existing['candidate_sha256'] == candidate_sha256 and existing != review
                   for existing in reviews.values()):
                raise ValueError('owned_parameter_review_candidate_already_bound')
            self._current(review, checksum)
            self.store._write(descriptor, checksum + '.accept.json', review)
        return review, checksum

    def _status(self, reviews, revocations, checksum):
        review = reviews.get(checksum)
        if review is None:
            raise ValueError('owned_parameter_review_not_accepted')
        self._current(review, checksum)
        revoked = checksum in revocations
        entries = []
        if revoked:
            revocation_sha256, revocation = revocations[checksum]
            entries.append({'revocation_sha256': revocation_sha256, 'revocation': revocation})
        status = OwnedParameterSkillReviewStatus.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_review_status',
            'synthetic': True, 'development_only': True, 'review_sha256': checksum,
            'review': review, 'revocations': entries,
            'status': 'revoked' if revoked else 'accepted', 'reviewed': not revoked,
        })).model_dump(mode='json')
        return status, digest(status)

    def read(self, review_sha256):
        checksum = _hash(review_sha256)
        with self.store._locked() as (descriptor, workspace, identity):
            reviews, revocations = self.store._inventory(descriptor, workspace, identity)
            return self._status(reviews, revocations, checksum)

    def revocation_preview(self, review_sha256):
        status, _status_sha256 = self.read(review_sha256)
        review = status['review']
        revocation = OwnedParameterSkillReviewRevocation.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_review_revocation',
            'synthetic': True, 'development_only': True, 'decision': 'revoke',
            'review_scope': 'synthetic_manual_recipe_review', 'reviewer': 'local_authenticated_user',
            'reviewed': False, 'review_sha256': review_sha256,
            **{key: review[key] for key in ('candidate_sha256', 'source_fingerprint_sha256', 'scope',
                'review_directory_sha256', 'review_parent_identity')},
        })).model_dump(mode='json')
        _revocation_matches(review, revocation)
        return revocation, digest(revocation)

    def revoke(self, review_sha256, confirm_revocation_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != REVOKE_CONFIRMATION:
            raise ValueError('owned_parameter_review_human_confirmation_required')
        checksum = _hash(confirm_revocation_sha256)
        revocation, actual_checksum = self.revocation_preview(review_sha256)
        if checksum != actual_checksum:
            raise ValueError('owned_parameter_review_revocation_confirmation_changed')
        with self.store._locked(write=True) as (descriptor, workspace, identity):
            reviews, revocations = self.store._inventory(descriptor, workspace, identity)
            self._status(reviews, revocations, review_sha256)
            existing = revocations.get(review_sha256)
            if existing is not None and existing != (checksum, revocation):
                raise ValueError('owned_parameter_review_revocation_conflict')
            self.store._write(descriptor, checksum + '.revoke.json', revocation)
        return revocation, checksum

    def _recovery(self, record_sha256, descriptor, workspace, identity):
        record_sha256 = _hash(record_sha256)
        reviews, revocations = self.store._inventory(descriptor, workspace, identity, allow_missing=True)
        stage = 'accept' if record_sha256 in reviews else 'revoke'
        if stage == 'accept':
            record = reviews[record_sha256]
            review_sha256 = record_sha256
        else:
            matches = [(review_sha256, record) for review_sha256, (checksum, record) in revocations.items()
                       if checksum == record_sha256]
            if len(matches) != 1:
                raise ValueError('owned_parameter_review_recovery_anchor_missing')
            review_sha256, record = matches[0]
        self._current(reviews[review_sha256], review_sha256)
        filename = record_sha256 + '.' + stage + '.json'
        try:
            os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('owned_parameter_review_recovery_record_not_missing')
        recovery = OwnedParameterSkillReviewRecovery.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_review_recovery',
            'operation': 'restore_anchored_review_record', 'synthetic': True,
            'development_only': True, 'reviewed': False,
            'record_sha256': record_sha256, 'record_stage': stage, 'record': record,
            'review_sha256': review_sha256, 'review_store_identity': identity,
            **{key: record[key] for key in ('candidate_sha256', 'scope', 'source_fingerprint_sha256',
                                           'review_directory_sha256', 'review_parent_identity')},
        })).model_dump(mode='json')
        return recovery, digest(recovery)

    def recovery_preview(self, record_sha256):
        with self.store._locked() as (descriptor, workspace, identity):
            return self._recovery(record_sha256, descriptor, workspace, identity)

    def recover(self, record_sha256, confirm_recovery_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != RECOVERY_CONFIRMATION:
            raise ValueError('owned_parameter_review_human_confirmation_required')
        checksum = _hash(confirm_recovery_sha256)
        with self.store._locked(write=True) as (descriptor, workspace, identity):
            recovery, actual_checksum = self._recovery(record_sha256, descriptor, workspace, identity)
            if checksum != actual_checksum:
                raise ValueError('owned_parameter_review_recovery_confirmation_changed')
            self.store._materialize(descriptor, record_sha256 + '.' + recovery['record_stage'] + '.json',
                                    recovery['record'])
        return recovery, checksum


def review_summary(payload, checksum):
    models = {'owned_parameter_skill_manual_review': OwnedParameterSkillReview,
              'owned_parameter_skill_manual_review_status': OwnedParameterSkillReviewStatus,
              'owned_parameter_skill_manual_review_revocation': OwnedParameterSkillReviewRevocation,
              'owned_parameter_skill_review_recovery': OwnedParameterSkillReviewRecovery}
    model = models.get(payload.get('kind'))
    if model is None:
        raise ValueError('owned_parameter_review_summary_invalid')
    record = model.model_validate_json(canonical(payload)).model_dump(mode='json')
    if _hash(checksum) != digest(record):
        raise ValueError('owned_parameter_review_summary_changed')
    summary = {key: record[key] for key in (
        'schema_version', 'kind', 'synthetic', 'development_only', 'reviewed',
        'native_model_verified', 'released_skill_verified', 'site_outcome_verified',
        'account_scope_verified', 'held_out_independence_verified', 'activation_authorized',
        'execution_authorized', 'training_ready', 'gpu_release_verified', 'replay_authorized',
        'runtime_started', 'model_calls')}
    if model is OwnedParameterSkillReviewStatus:
        return summary | {'status_sha256': checksum, 'review_sha256': record['review_sha256'],
                          'status': record['status'], 'candidate_sha256': record['review']['candidate_sha256'],
                          'scope': record['review']['scope'],
                          'revocation_sha256': (record['revocations'][0]['revocation_sha256']
                                                if record['revocations'] else None)}
    if model is OwnedParameterSkillReview:
        return summary | {'review_sha256': checksum, 'candidate_sha256': record['candidate_sha256'],
                          'decision': record['decision'], 'review_scope': record['review_scope'],
                          'scope': record['scope']}
    if model is OwnedParameterSkillReviewRecovery:
        return summary | {'recovery_sha256': checksum, 'record_sha256': record['record_sha256'],
                          'record_stage': record['record_stage'], 'review_sha256': record['review_sha256'],
                          'candidate_sha256': record['candidate_sha256'], 'scope': record['scope'],
                          'operation': record['operation']}
    return summary | {'revocation_sha256': checksum, 'review_sha256': record['review_sha256'],
                      'candidate_sha256': record['candidate_sha256'], 'decision': record['decision'],
                      'review_scope': record['review_scope'], 'scope': record['scope']}
