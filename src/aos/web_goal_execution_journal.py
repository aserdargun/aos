"""Immutable start/run/result bindings for one host-authorized web execution."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .knowledge import Hash
from .knowledge_answer import StoreIdentity
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .site_skill_form_recipe_audit import SiteSkillFormRecipeAuditReport
from .web_goal_execution_binding import (
    Identifier, WebGoalExecutionBinding, WebGoalExecutionConfirmation, WebGoalWholeRecordReadback,
    _current, _typed,
)
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity


MAX_RECORD_BYTES = 131072
MAX_INTENTS = 32
RECORD_NAMES = re.compile(r'^([a-f0-9]{64})\.(intent|bound|accepted)\.json$')


class WebGoalExecutionRunIdentity(TypedModel):
    task_id: str = Field(pattern=r'^task-[a-f0-9]{32}$')
    run_id: str = Field(pattern=r'^run-[a-f0-9]{32}$')
    runtime_id: Identifier
    deployment_id: Identifier
    owner_lease_id: Identifier
    skill_invocation_sha256: Hash


class _JournalRecord(TypedModel):
    @field_validator('dispatch_authorized', 'execution_verified', 'record_outcome_verified',
                     'training_ready', 'gpu_release_verified', 'task_terminal_verified',
                     'recipe_execution_verified', 'site_outcome_verified', 'activation_authorized',
                     'confirmation_consumed', 'replay_authorized', mode='before', check_fields=False)
    @classmethod
    def exact_boolean_evidence(cls, value):
        if type(value) is not bool:
            raise ValueError('web_goal_execution_journal_exact_boolean_required')
        return value


class WebGoalExecutionIntent(_JournalRecord):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_execution_intent']
    binding: WebGoalExecutionBinding
    confirmation: WebGoalExecutionConfirmation
    binding_sha256: Hash
    confirmation_sha256: Hash
    source_sha256: Hash
    authority_sha256: Hash
    workspace_identity: WorkspaceIdentity
    journal_identity: StoreIdentity
    dispatch_authorized: Literal[False]
    training_ready: Literal[False]
    gpu_release_verified: Literal[False]

    @model_validator(mode='after')
    def exact_confirmation(self):
        binding = self.binding.model_dump(mode='json')
        confirmation = self.confirmation.model_dump(mode='json')
        if (self.binding_sha256 != digest(binding)
                or self.confirmation_sha256 != digest(confirmation)
                or self.source_sha256 != digest(binding['source'])
                or self.authority_sha256 != digest(binding['authority'])
                or self.confirmation.binding_sha256 != self.binding_sha256
                or self.confirmation.source_sha256 != self.source_sha256
                or self.confirmation.authority != self.binding.authority):
            raise ValueError('web_goal_execution_journal_confirmation_changed')
        return self


class WebGoalExecutionRunBinding(_JournalRecord):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_execution_run_binding']
    intent_sha256: Hash
    run_identity: WebGoalExecutionRunIdentity
    execution_runtime_id: Identifier
    run_ref: Hash
    execution_verified: Literal[False]
    record_outcome_verified: Literal[False]
    training_ready: Literal[False]
    gpu_release_verified: Literal[False]

    @model_validator(mode='after')
    def exact_run_reference(self):
        if (self.run_ref != digest({'run_id': self.run_identity.run_id})
                or self.execution_runtime_id != self.run_identity.runtime_id):
            raise ValueError('web_goal_execution_journal_run_changed')
        return self


class WebGoalExecutionTerminalReceipt(_JournalRecord):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_execution_terminal_receipt']
    intent_sha256: Hash
    run_binding_sha256: Hash
    run_identity: WebGoalExecutionRunIdentity
    terminal_status: Literal['succeeded']
    recipe_audit: SiteSkillFormRecipeAuditReport
    recipe_audit_sha256: Hash
    readback: WebGoalWholeRecordReadback
    readback_sha256: Hash
    task_terminal_verified: Literal[True]
    recipe_execution_verified: Literal[True]
    record_outcome_verified: Literal[True]
    verification_scope: Literal['synthetic_bound_run_and_whole_record']
    site_outcome_verified: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]
    gpu_release_verified: Literal[False]

    @model_validator(mode='after')
    def exact_proof_hashes(self):
        if (self.recipe_audit_sha256 != digest(self.recipe_audit.model_dump(mode='json'))
                or self.readback_sha256 != digest(self.readback.model_dump(mode='json'))
                or self.recipe_audit.run_ref != digest({'run_id': self.run_identity.run_id})
                or self.recipe_audit.invocation_sha256 != self.run_identity.skill_invocation_sha256):
            raise ValueError('web_goal_execution_journal_terminal_proof_changed')
        return self


class WebGoalExecutionJournalStatus(_JournalRecord):
    model_config = {'json_schema_extra': {'allOf': [
        {'if': {'properties': {'status': {'const': 'accepted_verified'}}},
         'then': {'properties': {'run_binding_sha256': {'type': 'string'}, 'run_ref': {'type': 'string'},
             'receipt_sha256': {'type': 'string'}, 'reserved': {'const': False},
             'task_terminal_verified': {'const': True}, 'record_outcome_verified': {'const': True}}},
         'else': {'properties': {'receipt_sha256': {'type': 'null'}, 'reserved': {'const': True},
             'task_terminal_verified': {'const': False}, 'record_outcome_verified': {'const': False}}}},
        {'if': {'properties': {'status': {'const': 'uncertain_before_run_binding'}}},
         'then': {'properties': {'run_binding_sha256': {'type': 'null'}, 'run_ref': {'type': 'null'}}},
         'else': {'properties': {'run_binding_sha256': {'type': 'string'}, 'run_ref': {'type': 'string'}}}}
    ]}}

    schema_version: Literal['1.0']
    kind: Literal['web_goal_execution_journal_status']
    intent_sha256: Hash
    binding_sha256: Hash
    confirmation_sha256: Hash
    authority_sha256: Hash
    source_sha256: Hash
    run_binding_sha256: Hash | None
    run_ref: Hash | None
    receipt_sha256: Hash | None
    status: Literal['uncertain_before_run_binding', 'awaiting_independent_verification', 'accepted_verified']
    reserved: bool
    confirmation_consumed: Literal[True]
    task_terminal_verified: bool
    record_outcome_verified: bool
    replay_authorized: Literal[False]
    site_outcome_verified: Literal[False]
    training_ready: Literal[False]
    gpu_release_verified: Literal[False]

    @model_validator(mode='after')
    def exact_durable_stage(self):
        bound = self.run_binding_sha256 is not None
        accepted = self.receipt_sha256 is not None
        status = ('accepted_verified' if accepted else 'awaiting_independent_verification'
                  if bound else 'uncertain_before_run_binding')
        if (bound != (self.run_ref is not None) or accepted and not bound
                or self.status != status or self.reserved == accepted
                or self.task_terminal_verified != accepted or self.record_outcome_verified != accepted):
            raise ValueError('web_goal_execution_journal_status_inconsistent')
        return self


MODELS = {'intent': ('web_goal_execution_intent', WebGoalExecutionIntent),
          'bound': ('web_goal_execution_run_binding', WebGoalExecutionRunBinding),
          'accepted': ('web_goal_execution_terminal_receipt', WebGoalExecutionTerminalReceipt)}


def _directory_identity(descriptor):
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError('web_goal_execution_journal_directory_invalid')
    return {'device': metadata.st_dev, 'inode': metadata.st_ino,
            'owner': metadata.st_uid, 'mode': metadata.st_mode}


class WebGoalExecutionJournal:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        if '..' in self.directory.parts:
            raise ValueError('web_goal_execution_journal_path_invalid')
        self._workspace_identity = None
        self._journal_identity = None

    def _linked(self, descriptor, parent_descriptor):
        identity = _directory_identity(descriptor)
        linked = os.stat(self.directory.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (not stat.S_ISDIR(linked.st_mode)
                or (linked.st_dev, linked.st_ino, linked.st_uid, linked.st_mode)
                != (identity['device'], identity['inode'], identity['owner'], identity['mode'])):
            raise ValueError('web_goal_execution_journal_directory_changed')
        current_parent = open_existing_workspace(self.directory.parent)
        current_child = None
        try:
            before_parent = workspace_identity(self.directory.parent, parent_descriptor)
            if (_directory_identity(current_parent) != _directory_identity(parent_descriptor)
                    or workspace_identity(self.directory.parent, current_parent) != before_parent):
                raise ValueError('web_goal_execution_journal_workspace_changed')
            current_child = open_existing_workspace(self.directory)
            if _directory_identity(current_child) != identity:
                raise ValueError('web_goal_execution_journal_directory_changed')
        finally:
            if current_child is not None:
                os.close(current_child)
            os.close(current_parent)

    @contextmanager
    def _locked(self, *, create=False, write=False):
        parent_descriptor = open_existing_workspace(self.directory.parent)
        descriptor = None
        try:
            _directory_identity(parent_descriptor)
            workspace = workspace_identity(self.directory.parent, parent_descriptor).model_dump(mode='json')
            if self._workspace_identity is not None and workspace != self._workspace_identity:
                raise ValueError('web_goal_execution_journal_workspace_changed')
            if create:
                try:
                    os.mkdir(self.directory.name, 0o700, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                except FileExistsError:
                    pass
            descriptor = os.open(self.directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=parent_descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            identity = _directory_identity(descriptor)
            self._linked(descriptor, parent_descriptor)
            if self._journal_identity is not None and identity != self._journal_identity:
                raise ValueError('web_goal_execution_journal_directory_changed')
            self._workspace_identity = workspace
            self._journal_identity = identity
            yield descriptor, workspace, identity
            self._linked(descriptor, parent_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _read(self, descriptor, filename, stage):
        content = _read_private_child(descriptor, filename, MAX_RECORD_BYTES)
        try:
            value = json.loads(content)
            schema, model = MODELS[stage]
            if not validator(schema).is_valid(value):
                raise ValueError('schema')
            parsed = model.model_validate_json(canonical(value)).model_dump(mode='json')
            if canonical(parsed).encode() != content:
                raise ValueError('canonical')
            return parsed
        except (ValueError, TypeError, KeyError):
            raise ValueError('web_goal_execution_journal_record_invalid') from None

    def _validate_run(self, intent, run_identity, expected_execution_runtime_id):
        run = _typed(WebGoalExecutionRunIdentity, run_identity)
        binding = intent['binding']
        if (type(expected_execution_runtime_id) is not str
                or run.runtime_id != expected_execution_runtime_id
                or run.owner_lease_id != binding['authority']['lease_id']
                or run.skill_invocation_sha256 != digest(binding['invocation'])):
            raise ValueError('web_goal_execution_journal_run_identity_changed')
        return run.model_dump(mode='json')

    def _validate_terminal(self, intent, bound, receipt):
        if (receipt['intent_sha256'] != digest(intent)
                or receipt['run_binding_sha256'] != digest(bound)
                or receipt['run_identity'] != bound['run_identity']):
            raise ValueError('web_goal_execution_journal_terminal_identity_changed')
        binding = intent['binding']
        invocation = binding['invocation']
        audit = receipt['recipe_audit']
        for field in ('profile_sha256', 'task_sha256', 'skill_sha256', 'skill_plan_sha256',
                      'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256',
                      'field_binding_sha256', 'recipe_sha256', 'case_key'):
            if audit[field] != invocation[field]:
                raise ValueError('web_goal_execution_journal_trajectory_changed')
        if (audit['symbolic_step_keys'] != [step['step_key'] for step in invocation['steps']]
                or [stage['stage'] for stage in audit['stages']]
                != [step['operation'] for step in invocation['steps']]
                or audit['run_ref'] != bound['run_ref']
                or audit['invocation_sha256'] != digest(invocation)):
            raise ValueError('web_goal_execution_journal_trajectory_changed')
        readback = receipt['readback']
        request = {'method': 'GET', 'url': binding['oracle']['readback_url'],
            'profile_sha256': binding['source']['profile_sha256'],
            'task_sha256': binding['source']['task_sha256'],
            'source_run_ref': binding['source']['source_run_ref'],
            'binding_sha256': intent['binding_sha256'], 'oracle_sha256': digest(binding['oracle'])}
        expected = {'binding_sha256': intent['binding_sha256'],
            'confirmation_sha256': intent['confirmation_sha256'], 'oracle_sha256': digest(binding['oracle']),
            'source_sha256': intent['source_sha256'], 'request_sha256': digest(request),
            'observed_record_sha256': binding['oracle']['expected_record_sha256']}
        if any(readback[field] != checksum for field, checksum in expected.items()):
            raise ValueError('web_goal_execution_journal_readback_changed')

    def _inventory(self, descriptor, workspace, identity):
        names = os.listdir(descriptor)
        if len(names) > MAX_INTENTS * 3 or any(RECORD_NAMES.fullmatch(name) is None for name in names):
            raise ValueError('web_goal_execution_journal_inventory_invalid')
        records = {}
        for filename in names:
            key, stage = RECORD_NAMES.fullmatch(filename).groups()
            record = self._read(descriptor, filename, stage)
            if stage == 'intent':
                if (key != record['confirmation_sha256'] or record['workspace_identity'] != workspace
                        or record['journal_identity'] != identity):
                    raise ValueError('web_goal_execution_journal_intent_changed')
                checksum = digest(record)
                if checksum in records:
                    raise ValueError('web_goal_execution_journal_duplicate_intent')
                records[checksum] = {'intent': record, 'bound': None, 'accepted': None}
        if len(records) > MAX_INTENTS:
            raise ValueError('web_goal_execution_journal_inventory_invalid')
        for filename in names:
            key, stage = RECORD_NAMES.fullmatch(filename).groups()
            if stage == 'intent':
                continue
            if key not in records:
                raise ValueError('web_goal_execution_journal_orphan_record')
            record = self._read(descriptor, filename, stage)
            if record['intent_sha256'] != key:
                raise ValueError('web_goal_execution_journal_record_identity_changed')
            records[key][stage] = record
        for entry in records.values():
            if entry['bound'] is not None:
                self._validate_run(entry['intent'], entry['bound']['run_identity'],
                                   entry['bound']['execution_runtime_id'])
            if entry['accepted'] is not None:
                if entry['bound'] is None:
                    raise ValueError('web_goal_execution_journal_unbound_receipt')
                self._validate_terminal(entry['intent'], entry['bound'], entry['accepted'])
        return records

    def _entry(self, records, checksum):
        if type(checksum) is not str or re.fullmatch(r'[a-f0-9]{64}', checksum) is None:
            raise ValueError('web_goal_execution_journal_intent_hash_invalid')
        if checksum not in records:
            raise ValueError('web_goal_execution_journal_intent_missing')
        return records[checksum]

    def _write(self, descriptor, filename, record):
        content = canonical(record).encode()
        if len(content) > MAX_RECORD_BYTES:
            raise ValueError('web_goal_execution_journal_record_too_large')
        try:
            _write_private_child(descriptor, filename, content)
        except FileExistsError:
            if _read_private_child(descriptor, filename, MAX_RECORD_BYTES) != content:
                raise ValueError('web_goal_execution_journal_record_already_bound') from None
        if _read_private_child(descriptor, filename, MAX_RECORD_BYTES) != content:
            raise ValueError('web_goal_execution_journal_publication_changed')

    @property
    def reserved(self):
        try:
            with self._locked() as (descriptor, workspace, identity):
                records = self._inventory(descriptor, workspace, identity)
                return any(entry['accepted'] is None for entry in records.values())
        except FileNotFoundError:
            return self._journal_identity is not None
        except (OSError, ValueError, TypeError, KeyError):
            return True

    def begin(self, binding, confirmation, *, current_source, current_authority):
        binding = _typed(WebGoalExecutionBinding, binding)
        confirmation = _typed(WebGoalExecutionConfirmation, confirmation)
        _current(binding, current_source, current_authority)
        with self._locked(create=True, write=True) as (descriptor, workspace, identity):
            records = self._inventory(descriptor, workspace, identity)
            if (len(records) >= MAX_INTENTS or any(entry['accepted'] is None for entry in records.values())
                    or any(entry['intent']['confirmation_sha256'] == digest(confirmation.model_dump(mode='json'))
                           for entry in records.values())):
                raise ValueError('web_goal_execution_journal_consumed_or_unresolved')
            intent = WebGoalExecutionIntent.model_validate_json(canonical({
                'schema_version': '1.0', 'kind': 'web_goal_execution_intent',
                'binding': binding.model_dump(mode='json'), 'confirmation': confirmation.model_dump(mode='json'),
                'binding_sha256': digest(binding.model_dump(mode='json')),
                'confirmation_sha256': digest(confirmation.model_dump(mode='json')),
                'source_sha256': digest(binding.source.model_dump(mode='json')),
                'authority_sha256': digest(binding.authority.model_dump(mode='json')),
                'workspace_identity': workspace, 'journal_identity': identity,
                'dispatch_authorized': False, 'training_ready': False, 'gpu_release_verified': False,
            })).model_dump(mode='json')
            _current(binding, current_source, current_authority)
            self._write(descriptor, intent['confirmation_sha256'] + '.intent.json', intent)
            return digest(intent)

    def bind_run(self, intent_sha256, run_identity, *, expected_execution_runtime_id,
                 current_source, current_authority):
        with self._locked(write=True) as (descriptor, workspace, identity):
            records = self._inventory(descriptor, workspace, identity)
            entry = self._entry(records, intent_sha256)
            binding = _typed(WebGoalExecutionBinding, entry['intent']['binding'])
            _current(binding, current_source, current_authority)
            run = self._validate_run(entry['intent'], run_identity, expected_execution_runtime_id)
            if any(other['bound'] is not None and other['bound']['run_identity']['run_id'] == run['run_id']
                   for checksum, other in records.items() if checksum != intent_sha256):
                raise ValueError('web_goal_execution_journal_run_reused')
            record = WebGoalExecutionRunBinding.model_validate_json(canonical({
                'schema_version': '1.0', 'kind': 'web_goal_execution_run_binding',
                'intent_sha256': intent_sha256, 'run_identity': run, 'run_ref': digest({'run_id': run['run_id']}),
                'execution_runtime_id': expected_execution_runtime_id,
                'execution_verified': False, 'record_outcome_verified': False,
                'training_ready': False, 'gpu_release_verified': False,
            })).model_dump(mode='json')
            if entry['bound'] is not None and entry['bound'] != record:
                raise ValueError('web_goal_execution_journal_run_already_bound')
            _current(binding, current_source, current_authority)
            if entry['bound'] is None:
                self._write(descriptor, intent_sha256 + '.bound.json', record)
            return record

    def finish(self, intent_sha256, *, run_identity, terminal_status, recipe_audit, readback,
               current_source, current_authority):
        with self._locked(write=True) as (descriptor, workspace, identity):
            entry = self._entry(self._inventory(descriptor, workspace, identity), intent_sha256)
            binding = _typed(WebGoalExecutionBinding, entry['intent']['binding'])
            _current(binding, current_source, current_authority)
            if entry['bound'] is None:
                raise ValueError('web_goal_execution_journal_terminal_run_not_bound')
            run = self._validate_run(entry['intent'], run_identity, entry['bound']['execution_runtime_id'])
            if entry['bound']['run_identity'] != run:
                raise ValueError('web_goal_execution_journal_terminal_run_not_bound')
            audit = _typed(SiteSkillFormRecipeAuditReport, recipe_audit).model_dump(mode='json')
            readback = _typed(WebGoalWholeRecordReadback, readback).model_dump(mode='json')
            receipt = WebGoalExecutionTerminalReceipt.model_validate_json(canonical({
                'schema_version': '1.0', 'kind': 'web_goal_execution_terminal_receipt',
                'intent_sha256': intent_sha256, 'run_binding_sha256': digest(entry['bound']),
                'run_identity': run, 'terminal_status': terminal_status, 'recipe_audit': audit,
                'recipe_audit_sha256': digest(audit), 'readback': readback, 'readback_sha256': digest(readback),
                'task_terminal_verified': True, 'recipe_execution_verified': True, 'record_outcome_verified': True,
                'verification_scope': 'synthetic_bound_run_and_whole_record', 'site_outcome_verified': False,
                'activation_authorized': False, 'training_ready': False, 'gpu_release_verified': False,
            })).model_dump(mode='json')
            self._validate_terminal(entry['intent'], entry['bound'], receipt)
            if entry['accepted'] is not None and entry['accepted'] != receipt:
                raise ValueError('web_goal_execution_journal_terminal_already_bound')
            _current(binding, current_source, current_authority)
            if entry['accepted'] is None:
                self._write(descriptor, intent_sha256 + '.accepted.json', receipt)
            return receipt

    def inspect(self, intent_sha256):
        with self._locked() as (descriptor, workspace, identity):
            entry = self._entry(self._inventory(descriptor, workspace, identity), intent_sha256)
            intent, bound, receipt = entry['intent'], entry['bound'], entry['accepted']
            verified = receipt is not None
            status = ('accepted_verified' if verified else 'awaiting_independent_verification'
                      if bound is not None else 'uncertain_before_run_binding')
            return WebGoalExecutionJournalStatus.model_validate({
                'schema_version': '1.0', 'kind': 'web_goal_execution_journal_status',
                'intent_sha256': intent_sha256,
                **{field: intent[field] for field in ('binding_sha256', 'confirmation_sha256',
                                                    'authority_sha256', 'source_sha256')},
                'run_binding_sha256': None if bound is None else digest(bound),
                'run_ref': None if bound is None else bound['run_ref'],
                'receipt_sha256': None if receipt is None else digest(receipt), 'status': status,
                'reserved': not verified, 'confirmation_consumed': True,
                'task_terminal_verified': verified, 'record_outcome_verified': verified,
                'replay_authorized': False, 'site_outcome_verified': False,
                'training_ready': False, 'gpu_release_verified': False,
            }).model_dump(mode='json')
