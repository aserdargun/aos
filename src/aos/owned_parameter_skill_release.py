"""Human-reviewed manual development versions and anchored selection history."""

import os
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .knowledge import Hash, KnowledgeScope
from .knowledge_answer import StoreIdentity
from .owned_form_candidate_execution import _read_private_child
from .owned_parameter_skill_candidate import _hash
from .owned_parameter_skill_review import (
    OwnedParameterSkillReviewSession, _ReviewFlags, _directory_identity_from_workspace,
)
from .web_application import Key
from .web_goal_execution_journal import MAX_RECORD_BYTES, WebGoalExecutionJournal, _directory_identity
from .workspace_identity import open_existing_workspace


RELEASE_CONFIRMATION = 'RELEASE_MANUAL_SKILL'
SELECT_CONFIRMATION = 'SELECT_MANUAL_SKILL'
ROLLBACK_CONFIRMATION = 'ROLLBACK_MANUAL_SKILL'
RECOVERY_CONFIRMATION = 'RESTORE_ANCHORED_RELEASE_RECORD'
MAX_RELEASE_RECORDS = 64
RELEASE_NAMES = re.compile(r'^([a-f0-9]{64})\.(release|selection)\.json$')


class OwnedParameterSkillReleaseFamily(TypedModel):
    provenance_kind: Literal['manual_authored_synthetic_project']
    scope: KnowledgeScope
    task_key: Key
    field_binding_sha256: Hash
    outcome_sha256: Hash


class _ReleaseRecord(_ReviewFlags):
    schema_version: Literal['1.0']
    reviewed: Literal[True]
    review_sha256: Hash
    candidate_sha256: Hash
    source_fingerprint_sha256: Hash
    scope: KnowledgeScope
    family_sha256: Hash
    release_directory_sha256: Hash
    release_parent_identity: StoreIdentity
    database_identity: StoreIdentity
    actor: Literal['local_authenticated_user']

    @field_validator('reviewed', mode='before')
    @classmethod
    def exact_reviewed(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_release_review_claim_invalid')
        return value


class OwnedParameterSkillRelease(_ReleaseRecord):
    kind: Literal['owned_parameter_skill_manual_release']
    purpose: Literal['development_release']
    family: OwnedParameterSkillReleaseFamily
    revision: int = Field(ge=1, le=MAX_RELEASE_RECORDS, strict=True)
    parent_release_sha256: Hash | None
    review_directory_sha256: Hash
    manifest_sha256: Hash
    source_run_ref: Hash
    recipe_sha256: Hash

    @model_validator(mode='after')
    def exact_family(self):
        if (self.family_sha256 != digest(self.family.model_dump(mode='json'))
                or self.scope != self.family.scope
                or (self.revision == 1) != (self.parent_release_sha256 is None)):
            raise ValueError('owned_parameter_release_family_changed')
        return self


class OwnedParameterSkillSelection(_ReleaseRecord):
    kind: Literal['owned_parameter_skill_manual_selection']
    purpose: Literal['development_selected']
    operation: Literal['select', 'rollback']
    sequence: int = Field(ge=1, le=MAX_RELEASE_RECORDS, strict=True)
    previous_selection_sha256: Hash | None
    previous_release_sha256: Hash | None
    release_sha256: Hash

    @model_validator(mode='after')
    def exact_previous(self):
        if ((self.sequence == 1) != (self.previous_selection_sha256 is None)
                or (self.sequence == 1) != (self.previous_release_sha256 is None)
                or self.operation == 'rollback' and self.sequence == 1):
            raise ValueError('owned_parameter_selection_previous_invalid')
        return self


MODELS = {'release': OwnedParameterSkillRelease, 'selection': OwnedParameterSkillSelection}


def _family(candidate):
    return OwnedParameterSkillReleaseFamily.model_validate_json(canonical({
        'provenance_kind': candidate['provenance_kind'], 'scope': candidate['scope'],
        'task_key': candidate['recipe']['task_key'],
        'field_binding_sha256': digest(candidate['field_bindings']),
        'outcome_sha256': digest(candidate['recipe']['outcome']),
    })).model_dump(mode='json')


def _record(value):
    stage = {'owned_parameter_skill_manual_release': 'release',
             'owned_parameter_skill_manual_selection': 'selection'}.get(value.get('kind'))
    if stage is None:
        raise ValueError('owned_parameter_release_record_invalid')
    record = MODELS[stage].model_validate_json(canonical(value)).model_dump(mode='json')
    if len(canonical(record).encode()) > MAX_RECORD_BYTES:
        raise ValueError('owned_parameter_release_record_too_large')
    return record, digest(record), stage


def _history(records):
    families = {}
    for record in records.values():
        group = families.setdefault(record['family_sha256'], {'releases': [], 'selections': []})
        group['releases' if record['kind'] == 'owned_parameter_skill_manual_release'
              else 'selections'].append(record)
    for group in families.values():
        releases = sorted(group['releases'], key=lambda record: record['revision'])
        selections = sorted(group['selections'], key=lambda record: record['sequence'])
        group.update(releases=releases, selections=selections)
        by_hash, candidates = {}, set()
        previous = None
        for index, release in enumerate(releases, 1):
            if (release['revision'] != index
                    or release['parent_release_sha256'] != (digest(previous) if previous else None)
                    or release['candidate_sha256'] in candidates):
                raise ValueError('owned_parameter_release_chain_invalid')
            by_hash[digest(release)] = release
            candidates.add(release['candidate_sha256'])
            previous = release
        previous, selected = None, set()
        for index, selection in enumerate(selections, 1):
            release = by_hash.get(selection['release_sha256'])
            if (release is None or selection['sequence'] != index
                    or selection['previous_selection_sha256'] != (digest(previous) if previous else None)
                    or selection['previous_release_sha256'] != (previous['release_sha256'] if previous else None)
                    or any(selection[key] != release[key] for key in (
                        'review_sha256', 'candidate_sha256', 'source_fingerprint_sha256',
                        'scope', 'database_identity', 'release_directory_sha256', 'release_parent_identity'))):
                raise ValueError('owned_parameter_selection_chain_invalid')
            if previous is not None:
                previous_release = by_hash[previous['release_sha256']]
                if selection['operation'] == 'select':
                    allowed = release['revision'] > previous_release['revision']
                else:
                    allowed = (release['revision'] < previous_release['revision']
                               and selection['release_sha256'] in selected)
                if not allowed:
                    raise ValueError('owned_parameter_selection_direction_invalid')
            elif selection['operation'] != 'select':
                raise ValueError('owned_parameter_selection_direction_invalid')
            selected.add(selection['release_sha256'])
            previous = selection
    return families


class OwnedParameterSkillReleaseRecovery(_ReviewFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_release_recovery']
    operation: Literal['restore_anchored_release_record']
    reviewed: Literal[False]
    record_sha256: Hash
    record_stage: Literal['release', 'selection']
    record: OwnedParameterSkillRelease | OwnedParameterSkillSelection
    release_sha256: Hash
    family_sha256: Hash
    scope: KnowledgeScope
    database_identity: StoreIdentity
    release_directory_sha256: Hash
    release_parent_identity: StoreIdentity
    release_store_identity: StoreIdentity

    @field_validator('reviewed', mode='before')
    @classmethod
    def reviewed_is_false(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_release_recovery_claim_invalid')
        return value

    @model_validator(mode='after')
    def exact_record(self):
        record = self.record.model_dump(mode='json')
        proposal = self.model_dump(mode='json')
        release = isinstance(self.record, OwnedParameterSkillRelease)
        if (digest(record) != self.record_sha256
                or self.record_stage != ('release' if release else 'selection')
                or self.release_sha256 != (self.record_sha256 if release else record['release_sha256'])
                or any(proposal[key] != record[key] for key in (
                    'family_sha256', 'scope', 'database_identity', 'release_directory_sha256',
                    'release_parent_identity'))):
            raise ValueError('owned_parameter_release_recovery_binding_changed')
        return self


class _ReleaseStore(WebGoalExecutionJournal):
    def __init__(self, directory, history):
        super().__init__(directory)
        self.history = history

    def _inventory(self, descriptor, workspace, identity, *, allow_missing=False):
        names = os.listdir(descriptor)
        if (len(names) > MAX_RELEASE_RECORDS
                or any(RELEASE_NAMES.fullmatch(name) is None for name in names)):
            raise ValueError('owned_parameter_release_inventory_invalid')
        anchored = self.history.records(digest({'directory': str(self.directory)}))
        if not set(names).issubset(anchored) or not allow_missing and set(names) != set(anchored):
            raise ValueError('owned_parameter_release_anchored_inventory_changed')
        for filename, record in anchored.items():
            content = (_read_private_child(descriptor, filename, MAX_RECORD_BYTES)
                       if filename in names else None)
            if content is not None and canonical(record).encode() != content:
                raise ValueError('owned_parameter_release_record_changed')
            if record['release_parent_identity'] != _directory_identity_from_workspace(workspace):
                raise ValueError('owned_parameter_release_destination_changed')
        _history(anchored)
        return anchored

    def _write(self, descriptor, filename, record):
        self.history.authorize(record)
        super()._write(descriptor, filename, record)

    def _materialize(self, descriptor, filename, record):
        super()._write(descriptor, filename, record)


class OwnedParameterSkillReleaseSession:
    def __init__(self, review_session, release_directory):
        if not isinstance(review_session, OwnedParameterSkillReviewSession):
            raise ValueError('owned_parameter_release_review_session_required')
        from .owned_parameter_skill_release_history import OwnedParameterSkillReleaseHistory

        self.review_session = review_session
        candidate = review_session.candidate_session
        self.store = _ReleaseStore(release_directory, OwnedParameterSkillReleaseHistory(candidate.database))
        directory = self.store.directory
        protected = (candidate.database, candidate.journal.directory, candidate.candidates.directory,
                     review_session.store.directory)
        if (directory == REPO_ROOT or REPO_ROOT in directory.parents
                or directory == candidate.directory or directory in candidate.directory.parents
                or any(directory == path or directory in path.parents or path in directory.parents
                       for path in protected)):
            raise ValueError('owned_parameter_release_paths_invalid')

    def _destination(self):
        descriptor = open_existing_workspace(self.store.directory.parent)
        try:
            identity = _directory_identity(descriptor)
        finally:
            os.close(descriptor)
        return {'release_directory_sha256': digest({'directory': str(self.store.directory)}),
                'release_parent_identity': identity}

    def _optional_inventory(self):
        try:
            with self.store._locked() as (descriptor, workspace, identity):
                return self.store._inventory(descriptor, workspace, identity)
        except FileNotFoundError:
            if (self.store._journal_identity is not None
                    or self.store.history.records(digest({'directory': str(self.store.directory)}))):
                raise ValueError('owned_parameter_release_store_missing') from None
            return {}

    def _source(self, review_sha256):
        status, _checksum = self.review_session.read(_hash(review_sha256))
        if status['status'] != 'accepted' or not status['reviewed']:
            raise ValueError('owned_parameter_release_review_not_accepted')
        review = status['review']
        candidate, _checksum = self.review_session.candidate_session.read(review['candidate_sha256'])
        workspace_pin = candidate['bootstrap_intent']['execution_workspace_identity']['path_sha256']
        if any(digest({'workspace': str(path)}) == workspace_pin
               for path in (self.store.directory, *self.store.directory.parents)):
            raise ValueError('owned_parameter_release_execution_workspace_forbidden')
        return review, candidate

    def _proposal(self, review_sha256, expected_parent_release_sha256, records):
        if expected_parent_release_sha256 is not None:
            _hash(expected_parent_release_sha256)
        review, candidate = self._source(review_sha256)
        family = _family(candidate)
        releases = _history(records).get(digest(family), {}).get('releases', [])
        latest = releases[-1] if releases else None
        if (latest is not None and latest['review_sha256'] == review_sha256
                and latest['parent_release_sha256'] == expected_parent_release_sha256):
            self._current(latest)
            return latest, digest(latest)
        if expected_parent_release_sha256 != (digest(latest) if latest else None):
            raise ValueError('owned_parameter_release_parent_stale')
        if any(existing['candidate_sha256'] == review['candidate_sha256'] for existing in releases):
            raise ValueError('owned_parameter_release_candidate_already_released')
        release = OwnedParameterSkillRelease.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_release',
            'purpose': 'development_release', 'synthetic': True, 'development_only': True,
            'reviewed': True, 'actor': 'local_authenticated_user', 'review_sha256': review_sha256,
            'family': family, 'family_sha256': digest(family),
            'revision': len(releases) + 1, 'parent_release_sha256': expected_parent_release_sha256,
            'database_identity': candidate['database_identity'], **self._destination(),
            **{key: review[key] for key in ('candidate_sha256', 'source_fingerprint_sha256',
                'scope', 'review_directory_sha256', 'manifest_sha256', 'source_run_ref', 'recipe_sha256')},
        })).model_dump(mode='json')
        return release, digest(release)

    def _current(self, release):
        review, candidate = self._source(release['review_sha256'])
        if (any(release[key] != review[key] for key in ('candidate_sha256', 'source_fingerprint_sha256',
                'scope', 'review_directory_sha256', 'manifest_sha256', 'source_run_ref', 'recipe_sha256'))
                or release['database_identity'] != candidate['database_identity']
                or release['family'] != _family(candidate)
                or any(release[key] != value for key, value in self._destination().items())):
            raise ValueError('owned_parameter_release_source_changed')

    def preview(self, review_sha256, expected_parent_release_sha256=None):
        return self._proposal(review_sha256, expected_parent_release_sha256, self._optional_inventory())

    def release(self, review_sha256, expected_parent_release_sha256,
                confirm_release_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != RELEASE_CONFIRMATION:
            raise ValueError('owned_parameter_release_human_confirmation_required')
        checksum = _hash(confirm_release_sha256)
        preview, actual = self.preview(review_sha256, expected_parent_release_sha256)
        if checksum != actual:
            raise ValueError('owned_parameter_release_confirmation_changed')
        with self.store._locked(create=True, write=True) as (descriptor, workspace, identity):
            with self.review_session.store._locked():
                records = self.store._inventory(descriptor, workspace, identity)
                record, actual = self._proposal(review_sha256, expected_parent_release_sha256, records)
                if checksum != actual or record != preview:
                    raise ValueError('owned_parameter_release_confirmation_changed')
                self.store._write(descriptor, checksum + '.release.json', record)
        return record, checksum

    def read(self, release_sha256):
        checksum = _hash(release_sha256)
        records = self._optional_inventory()
        release = records.get(checksum + '.release.json')
        if release is None:
            raise ValueError('owned_parameter_release_missing')
        self._current(release)
        return release, checksum

    def _selection(self, release_sha256, expected_selection_sha256, operation, records):
        _hash(release_sha256)
        if expected_selection_sha256 is not None:
            _hash(expected_selection_sha256)
        if operation not in ('select', 'rollback'):
            raise ValueError('owned_parameter_selection_operation_invalid')
        release = records.get(release_sha256 + '.release.json')
        if release is None:
            raise ValueError('owned_parameter_release_missing')
        self._current(release)
        group = _history(records)[release['family_sha256']]
        selections = group['selections']
        latest = selections[-1] if selections else None
        if (latest is not None and latest['release_sha256'] == release_sha256
                and latest['previous_selection_sha256'] == expected_selection_sha256
                and latest['operation'] == operation):
            return latest, digest(latest)
        if expected_selection_sha256 != (digest(latest) if latest else None):
            raise ValueError('owned_parameter_selection_head_stale')
        selection = OwnedParameterSkillSelection.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_selection',
            'purpose': 'development_selected', 'synthetic': True, 'development_only': True,
            'reviewed': True, 'actor': 'local_authenticated_user', 'operation': operation,
            'sequence': len(selections) + 1, 'previous_selection_sha256': expected_selection_sha256,
            'previous_release_sha256': latest['release_sha256'] if latest else None,
            'release_sha256': release_sha256,
            **{key: release[key] for key in ('review_sha256', 'candidate_sha256',
                'source_fingerprint_sha256', 'scope', 'family_sha256', 'release_directory_sha256',
                'release_parent_identity', 'database_identity')},
        })).model_dump(mode='json')
        checksum = digest(selection)
        _history(records | {checksum + '.selection.json': selection})
        return selection, checksum

    def selection_preview(self, release_sha256, expected_selection_sha256=None, operation='select'):
        return self._selection(release_sha256, expected_selection_sha256, operation,
                               self._optional_inventory())

    def select(self, release_sha256, expected_selection_sha256, operation,
               confirm_selection_sha256, human_confirmation):
        confirmation = {'select': SELECT_CONFIRMATION, 'rollback': ROLLBACK_CONFIRMATION}.get(operation)
        if confirmation is None or type(human_confirmation) is not str or human_confirmation != confirmation:
            raise ValueError('owned_parameter_selection_human_confirmation_required')
        checksum = _hash(confirm_selection_sha256)
        with self.store._locked(write=True) as (descriptor, workspace, identity):
            with self.review_session.store._locked():
                records = self.store._inventory(descriptor, workspace, identity)
                selection, actual = self._selection(release_sha256, expected_selection_sha256, operation, records)
                if checksum != actual:
                    raise ValueError('owned_parameter_selection_confirmation_changed')
                self.store._write(descriptor, checksum + '.selection.json', selection)
        return selection, checksum

    def _recovery(self, record_sha256, descriptor, workspace, identity):
        checksum = _hash(record_sha256)
        records = self.store._inventory(descriptor, workspace, identity, allow_missing=True)
        matches = [(filename, record) for filename, record in records.items()
                   if filename.startswith(checksum + '.')]
        if len(matches) != 1:
            raise ValueError('owned_parameter_release_recovery_anchor_missing')
        filename, record = matches[0]
        stage = filename.split('.')[1]
        release_sha256 = checksum if stage == 'release' else record['release_sha256']
        self._current(records[release_sha256 + '.release.json'])
        try:
            os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('owned_parameter_release_recovery_record_not_missing')
        recovery = OwnedParameterSkillReleaseRecovery.model_validate_json(canonical({
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_release_recovery',
            'operation': 'restore_anchored_release_record', 'synthetic': True,
            'development_only': True, 'reviewed': False,
            'record_sha256': checksum, 'record_stage': stage, 'record': record,
            'release_sha256': release_sha256, 'release_store_identity': identity,
            **{key: record[key] for key in ('family_sha256', 'scope', 'database_identity',
                'release_directory_sha256', 'release_parent_identity')},
        })).model_dump(mode='json')
        return recovery, digest(recovery)

    def recovery_preview(self, record_sha256):
        with self.store._locked() as (descriptor, workspace, identity):
            with self.review_session.store._locked():
                return self._recovery(record_sha256, descriptor, workspace, identity)

    def recover(self, record_sha256, confirm_recovery_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != RECOVERY_CONFIRMATION:
            raise ValueError('owned_parameter_release_recovery_human_confirmation_required')
        checksum = _hash(confirm_recovery_sha256)
        with self.store._locked(write=True) as (descriptor, workspace, identity):
            with self.review_session.store._locked():
                recovery, actual = self._recovery(record_sha256, descriptor, workspace, identity)
                if checksum != actual:
                    raise ValueError('owned_parameter_release_recovery_confirmation_changed')
                self.store._materialize(descriptor,
                    record_sha256 + '.' + recovery['record_stage'] + '.json', recovery['record'])
        return recovery, checksum

    def inventory(self):
        families = []
        for family_sha256, group in sorted(_history(self._optional_inventory()).items()):
            releases, selections = group['releases'], group['selections']
            latest = selections[-1] if selections else None
            families.append({'family_sha256': family_sha256, 'family': releases[0]['family'],
                'releases': [release_summary(record, digest(record)) for record in releases],
                'selection_sha256': digest(latest) if latest else None,
                'selected_release_sha256': latest['release_sha256'] if latest else None,
                'sequence': len(selections),
                'selection': release_summary(latest, digest(latest)) if latest else None})
        return {'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_release_inventory',
                'synthetic': True, 'development_only': True, 'source_current_verified': False,
                'execution_authorized': False, 'activation_authorized': False, 'training_ready': False,
                'families': families}


def release_summary(payload, checksum):
    if payload.get('kind') == 'owned_parameter_skill_release_recovery':
        proposal = OwnedParameterSkillReleaseRecovery.model_validate_json(canonical(payload)).model_dump(mode='json')
        if _hash(checksum) != digest(proposal):
            raise ValueError('owned_parameter_release_summary_changed')
        return {key: value for key, value in proposal.items() if key != 'record'} | {
            'recovery_sha256': checksum, 'source_current_verified': True}
    record, actual, stage = _record(payload)
    if _hash(checksum) != actual:
        raise ValueError('owned_parameter_release_summary_changed')
    summary = {key: record[key] for key in (
        'schema_version', 'kind', 'purpose', 'synthetic', 'development_only', 'reviewed',
        'native_model_verified', 'released_skill_verified', 'site_outcome_verified',
        'account_scope_verified', 'held_out_independence_verified', 'activation_authorized',
        'execution_authorized', 'training_ready', 'gpu_release_verified', 'replay_authorized',
        'runtime_started', 'model_calls', 'review_sha256', 'candidate_sha256', 'scope', 'family_sha256')}
    summary['record_sha256'] = checksum
    if stage == 'release':
        return summary | {'release_sha256': checksum, **{key: record[key] for key in (
            'revision', 'parent_release_sha256', 'recipe_sha256')}}
    return summary | {'selection_sha256': checksum, **{key: record[key] for key in (
        'release_sha256', 'sequence', 'operation', 'previous_selection_sha256', 'previous_release_sha256')}}
