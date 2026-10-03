"""Private review candidates from audited, manually authored synthetic bootstraps."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, State, canonical, digest
from .dataset_audit import audit_snapshot
from .dataset_reviews import source_fingerprint
from .knowledge import Hash, KnowledgeScope
from .knowledge_answer import StoreIdentity
from .owned_form_candidate_execution import _read_private_child
from .owned_parameter_project import read_owned_parameter_project
from .owned_parameter_project_execution import (
    OwnedParameterProjectExecution, OwnedParameterProjectExecutionIntent,
    OwnedParameterProjectExecutionJournal, OwnedParameterProjectExecutionReceipt,
    OwnedParameterProjectExecutionRunBinding, _BootstrapFlags, _audit_matches,
    _observation_matches,
)
from .site_skill_case_binding import SiteSkillFormFieldBinding
from .site_skill_form_recipe import SiteSkillFormRecipe
from .site_skill_form_recipe_audit import (
    SiteSkillFormRecipeAuditReport, audit_site_skill_form_recipe_execution,
)
from .web_goal_execution_binding import fields_for_parameters
from .web_goal_execution_journal import (
    MAX_INTENTS, MAX_RECORD_BYTES, WebGoalExecutionJournal, _directory_identity,
)
from .web_https_form_transport import WebHTTPSFormPlan
from .workspace_identity import open_existing_workspace


HUMAN_CONFIRMATION = 'PUBLISH_MANUAL_CANDIDATE'
CANDIDATE_NAMES = re.compile(r'^([a-f0-9]{64})\.json$')


def _hash(value):
    if type(value) is not str or re.fullmatch(r'[a-f0-9]{64}', value) is None:
        raise ValueError('owned_parameter_candidate_hash_invalid')
    return value


def _audit_evidence(report):
    return {key: value for key, value in report.items() if key != 'source_snapshot_sha256'}


class OwnedParameterSkillCandidate(_BootstrapFlags):
    schema_version: Literal['1.0']
    kind: Literal['audited_manual_bootstrap_candidate']
    provenance_kind: Literal['manual_authored_synthetic_project']
    synthetic: Literal[True]
    development_only: Literal[True]
    status: Literal['awaiting_manual_review']
    verification_scope: Literal['synthetic_manual_bootstrap_run_and_whole_record']
    intent_sha256: Hash
    manifest_sha256: Hash
    bootstrap_intent: OwnedParameterProjectExecutionIntent
    run_binding_sha256: Hash
    bootstrap_run_binding: OwnedParameterProjectExecutionRunBinding
    receipt_sha256: Hash
    bootstrap_receipt: OwnedParameterProjectExecutionReceipt
    recipe_audit: SiteSkillFormRecipeAuditReport = Field(
        description='Historical accepted recipe audit, reverified against a fresh frozen database snapshot.')
    recipe_audit_sha256: Hash
    source_snapshot_sha256: Hash = Field(
        description='Historical whole-database snapshot pin from the original accepted bootstrap audit.')
    source_fingerprint_sha256: Hash = Field(
        description='Stable original run, desktop task, parent runtime and form-binding content fingerprint.')
    database_identity: StoreIdentity
    candidate_directory_sha256: Hash
    candidate_parent_identity: StoreIdentity
    scope: KnowledgeScope
    recipe: SiteSkillFormRecipe
    field_bindings: list[SiteSkillFormFieldBinding] = Field(min_length=2, max_length=8)
    parameters: dict[str, str] = Field(min_length=2, max_length=8)
    form_plan: WebHTTPSFormPlan
    form_body: str = Field(min_length=1, max_length=8192)
    form_body_sha256: Hash
    manual_recipe_execution_verified: Literal[True]
    task_terminal_verified: Literal[True]
    record_outcome_verified: Literal[True]
    reviewed: Literal[False] = False
    account_scope_verified: Literal[False] = False
    held_out_independence_verified: Literal[False] = False
    execution_authorized: Literal[False] = False
    runtime_started: Literal[False] = False
    model_calls: Literal[0] = 0

    @field_validator('synthetic', 'development_only', 'manual_recipe_execution_verified',
                     'task_terminal_verified', 'record_outcome_verified', mode='before')
    @classmethod
    def true_claims(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_candidate_claim_invalid')
        return value

    @field_validator('reviewed', 'account_scope_verified', 'held_out_independence_verified',
                     'execution_authorized', 'runtime_started', mode='before')
    @classmethod
    def false_claims(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_candidate_claim_invalid')
        return value

    @field_validator('model_calls', mode='before')
    @classmethod
    def no_model_calls(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError('owned_parameter_candidate_model_evidence_invalid')
        return value

    @model_validator(mode='after')
    def exact_provenance(self):
        intent = self.bootstrap_intent.model_dump(mode='json')
        bound = self.bootstrap_run_binding.model_dump(mode='json')
        receipt = self.bootstrap_receipt.model_dump(mode='json')
        audit = self.recipe_audit.model_dump(mode='json')
        source = intent['source']
        invocation = source['invocation']
        body = self.form_body.encode('ascii')
        if (self.intent_sha256 != digest(intent)
                or self.manifest_sha256 != source['manifest_sha256']
                or bound['intent_sha256'] != self.intent_sha256
                or self.run_binding_sha256 != digest(bound)
                or receipt['intent_sha256'] != self.intent_sha256
                or receipt['run_binding_sha256'] != self.run_binding_sha256
                or receipt['run_identity'] != bound['run_identity']
                or bound['run_identity']['owner_lease_id'] != intent['authority']['lease_id']
                or bound['run_identity']['skill_invocation_sha256'] != digest(invocation)
                or self.receipt_sha256 != digest(receipt)
                or self.recipe_audit_sha256 != digest(audit)
                or self.source_snapshot_sha256 != audit['source_snapshot_sha256']
                or _audit_evidence(audit) != _audit_evidence(receipt['recipe_audit'])
                or self.scope.model_dump(mode='json') != source['scope']
                or digest(self.recipe.model_dump(mode='json')) != invocation['recipe_sha256']
                or digest([binding.model_dump(mode='json') for binding in self.field_bindings])
                != invocation['field_binding_sha256']
                or dict(fields_for_parameters(self.parameters, self.field_bindings)) != source['expected_fields']
                or digest(self.form_plan.model_dump(mode='json')) != invocation['form_plan_sha256']
                or hashlib.sha256(body).hexdigest() != self.form_body_sha256
                or self.form_body_sha256 != self.form_plan.body_sha256
                or len(body) != self.form_plan.body_bytes):
            raise ValueError('owned_parameter_candidate_provenance_changed')
        _audit_matches(source, bound['run_identity'], audit)
        _audit_matches(source, bound['run_identity'], receipt['recipe_audit'])
        _observation_matches(source, receipt['observation'])
        return self


class _CandidateStore(WebGoalExecutionJournal):
    def _read_candidate(self, descriptor, checksum):
        content = _read_private_child(descriptor, _hash(checksum) + '.json', MAX_RECORD_BYTES)
        candidate = OwnedParameterSkillCandidate.model_validate_json(content).model_dump(mode='json')
        if canonical(candidate).encode() != content or digest(candidate) != checksum:
            raise ValueError('owned_parameter_candidate_store_changed')
        return candidate

    def _inventory(self, descriptor, workspace, identity):
        names = os.listdir(descriptor)
        if len(names) > MAX_INTENTS or any(CANDIDATE_NAMES.fullmatch(name) is None for name in names):
            raise ValueError('owned_parameter_candidate_store_inventory_invalid')
        return {name[:64]: self._read_candidate(descriptor, name[:64]) for name in names}


def _database_identity(descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        raise ValueError('owned_parameter_candidate_database_invalid')
    return StoreIdentity(device=metadata.st_dev, inode=metadata.st_ino,
                         owner=metadata.st_uid, mode=metadata.st_mode).model_dump(mode='json')


@contextmanager
def _private_database(path):
    parent = open_existing_workspace(path.parent)
    descriptor = None
    current_parent = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        identity = _database_identity(descriptor)
        yield identity
        linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(linked.st_mode)
                or (linked.st_dev, linked.st_ino, linked.st_uid, linked.st_mode, linked.st_nlink)
                != (identity['device'], identity['inode'], identity['owner'], identity['mode'], 1)
                or _database_identity(descriptor) != identity):
            raise ValueError('owned_parameter_candidate_database_changed')
        current_parent = open_existing_workspace(path.parent)
        if (os.fstat(current_parent).st_dev, os.fstat(current_parent).st_ino) != (
                os.fstat(parent).st_dev, os.fstat(parent).st_ino):
            raise ValueError('owned_parameter_candidate_database_parent_changed')
    finally:
        if current_parent is not None:
            os.close(current_parent)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _terminal_source(snapshot, intent, bound):
    authority = intent['authority']
    run = bound['run_identity']
    job = snapshot.execute('SELECT * FROM desktop_tasks WHERE job_id=?',
                           (intent['job_id'],)).fetchone()
    session = snapshot.execute('SELECT runtime_id FROM desktop_sessions WHERE session_id=?',
                               (authority['desktop_session_id'],)).fetchone()
    actual_run = snapshot.execute('SELECT * FROM runs WHERE run_id=?', (run['run_id'],)).fetchone()
    runtime = snapshot.execute('SELECT state_json FROM runtime_states WHERE run_id=?',
                               (run['run_id'],)).fetchone()
    if (bound['intent_sha256'] != digest(intent) or job is None or session is None
            or actual_run is None or runtime is None
            or authority['manager_session'] != authority['desktop_session_id']
            or session['runtime_id'] != authority['runtime_id']
            or job['session_id'] != authority['desktop_session_id']
            or job['lease_id'] != authority['lease_id']
            or job['generation'] != authority['generation']
            or job['kind'] != 'browser_remote_form' or job['status'] != 'succeeded'
            or job['run_id'] != run['run_id'] or job['runtime_id'] != run['runtime_id']
            or job['real_model'] != 0 or actual_run['status'] != 'succeeded'
            or actual_run['outcome'] != 'passed' or actual_run['task_id'] != run['task_id']
            or snapshot.execute('SELECT count(*) FROM model_calls WHERE run_id=?',
                                (run['run_id'],)).fetchone()[0] != 0):
        raise ValueError('owned_parameter_candidate_original_run_changed')
    state = State.model_validate_json(runtime['state_json'])
    if (state.task_kind != 'browser_remote_form' or state.phase.value != 'SUCCEEDED'
            or any(getattr(state, field) != value for field, value in run.items())):
        raise ValueError('owned_parameter_candidate_original_state_changed')
    bindings = {}
    for table in ('desktop_remote_form_bindings', 'desktop_remote_form_state_bindings'):
        row = snapshot.execute(f'SELECT * FROM {table} WHERE run_id=?', (run['run_id'],)).fetchone()
        if row is None or row['job_id'] != intent['job_id'] or row['browser_runtime_id'] != run['runtime_id']:
            raise ValueError('owned_parameter_candidate_original_binding_changed')
        bindings[table] = dict(row)
    return digest({'run_content_sha256': source_fingerprint(snapshot, run['run_id']),
                   'desktop_task': dict(job), 'desktop_runtime_id': session['runtime_id'],
                   'bindings': bindings})


class OwnedParameterSkillCandidateSession:
    def __init__(self, directory, manifest_sha256, database, journal_directory, candidate_directory):
        self.directory = Path(directory).absolute()
        self.manifest_sha256 = _hash(manifest_sha256)
        self.database = Path(database).absolute()
        self.journal = OwnedParameterProjectExecutionJournal(journal_directory)
        self.candidates = _CandidateStore(candidate_directory)
        paths = (self.directory, self.database, self.journal.directory, self.candidates.directory)
        authored_paths = (self.directory, self.candidates.directory)
        runtime_paths = (self.database, self.journal.directory)
        data_directory = REPO_ROOT / 'data'
        if (any('..' in path.parts for path in paths)
                or any(path == REPO_ROOT or REPO_ROOT in path.parents for path in authored_paths)
                or any((path == REPO_ROOT or REPO_ROOT in path.parents)
                       and (data_directory not in path.parents or path == data_directory / 'README.md')
                       for path in runtime_paths)
                or self.candidates.directory in (self.directory, self.database, self.journal.directory)
                or self.candidates.directory in self.directory.parents
                or self.candidates.directory in self.journal.directory.parents
                or self.journal.directory in self.candidates.directory.parents
                or self.candidates.directory in self.database.parents):
            raise ValueError('owned_parameter_candidate_paths_invalid')

    def _derive(self, intent_sha256):
        intent_sha256 = _hash(intent_sha256)
        parent = open_existing_workspace(self.candidates.directory.parent)
        try:
            candidate_parent_identity = _directory_identity(parent)
        finally:
            os.close(parent)
        source = read_owned_parameter_project(self.directory, self.manifest_sha256)
        with self.journal._locked() as (descriptor, workspace, journal_identity):
            records = self.journal._inventory(descriptor, workspace, journal_identity)
            entry = self.journal._entry(records, intent_sha256)
            intent, bound, receipt = entry['intent'], entry['bound'], entry['accepted']
            if (bound is None or receipt is None
                    or intent['source'] != OwnedParameterProjectExecution._source(source)):
                raise ValueError('owned_parameter_candidate_accepted_source_required')
            with _private_database(self.database) as database_identity:
                with audit_snapshot(self.database) as (snapshot, snapshot_identity):
                    fingerprint = _terminal_source(snapshot, intent, bound)
                    audit = audit_site_skill_form_recipe_execution(
                        source['skill_store'], source['skill_plan'], source['case_inputs'],
                        source['case_key'], source['profiles'], source['task'],
                        source['form_plan'], source['state_plan'], source['field_bindings'],
                        source['recipe'], source['invocation'], self.database,
                        bound['run_identity']['run_id'], _audited_snapshot=(snapshot, snapshot_identity))
                    if _audit_evidence(audit) != _audit_evidence(receipt['recipe_audit']):
                        raise ValueError('owned_parameter_candidate_receipt_audit_changed')
                    candidate = OwnedParameterSkillCandidate.model_validate_json(canonical({
                        'schema_version': '1.0', 'kind': 'audited_manual_bootstrap_candidate',
                        'provenance_kind': 'manual_authored_synthetic_project',
                        'synthetic': True, 'development_only': True, 'status': 'awaiting_manual_review',
                        'verification_scope': receipt['verification_scope'],
                        'intent_sha256': intent_sha256, 'manifest_sha256': self.manifest_sha256,
                        'bootstrap_intent': intent, 'bootstrap_run_binding': bound,
                        'run_binding_sha256': digest(bound), 'bootstrap_receipt': receipt,
                        'receipt_sha256': digest(receipt), 'recipe_audit': receipt['recipe_audit'],
                        'recipe_audit_sha256': receipt['recipe_audit_sha256'],
                        'source_snapshot_sha256': receipt['recipe_audit']['source_snapshot_sha256'],
                        'source_fingerprint_sha256': fingerprint, 'database_identity': database_identity,
                        'candidate_directory_sha256': digest({'directory': str(self.candidates.directory)}),
                        'candidate_parent_identity': candidate_parent_identity,
                        'scope': source['record_config']['scope'],
                        'recipe': source['recipe'].model_dump(mode='json'),
                        'field_bindings': [item.model_dump(mode='json') for item in source['field_bindings']],
                        'parameters': source['parameters'], 'form_plan': source['form_plan'].model_dump(mode='json'),
                        'form_body': source['body'].decode('ascii'),
                        'form_body_sha256': hashlib.sha256(source['body']).hexdigest(),
                        'manual_recipe_execution_verified': True, 'task_terminal_verified': True,
                        'record_outcome_verified': True,
                    })).model_dump(mode='json')
            current = read_owned_parameter_project(self.directory, self.manifest_sha256)
            if (OwnedParameterProjectExecution._source(current) != intent['source']
                    or current['body'] != source['body']
                    or self.journal._inventory(descriptor, workspace, journal_identity) != records):
                raise ValueError('owned_parameter_candidate_source_changed')
        if len(canonical(candidate).encode()) > MAX_RECORD_BYTES:
            raise ValueError('owned_parameter_candidate_too_large')
        return candidate

    def preview(self, intent_sha256):
        candidate = self._derive(intent_sha256)
        return candidate, digest(candidate)

    def publish(self, intent_sha256, confirm_candidate_sha256, human_confirmation):
        if type(human_confirmation) is not str or human_confirmation != HUMAN_CONFIRMATION:
            raise ValueError('owned_parameter_candidate_human_confirmation_required')
        checksum = _hash(confirm_candidate_sha256)
        candidate, actual_checksum = self.preview(intent_sha256)
        if actual_checksum != checksum:
            raise ValueError('owned_parameter_candidate_confirmation_changed')
        with self.candidates._locked(create=True, write=True) as (descriptor, workspace, identity):
            records = self.candidates._inventory(descriptor, workspace, identity)
            if checksum not in records and len(records) >= MAX_INTENTS:
                raise ValueError('owned_parameter_candidate_store_full')
            current, current_checksum = self.preview(intent_sha256)
            if current_checksum != checksum or current != candidate:
                raise ValueError('owned_parameter_candidate_confirmation_changed')
            self.candidates._write(descriptor, checksum + '.json', candidate)
        return candidate, checksum

    def read(self, candidate_sha256):
        checksum = _hash(candidate_sha256)
        with self.candidates._locked() as (descriptor, workspace, identity):
            candidate = self.candidates._inventory(descriptor, workspace, identity).get(checksum)
            if candidate is None:
                raise ValueError('owned_parameter_candidate_missing')
            current = self._derive(candidate['intent_sha256'])
            if current != candidate:
                raise ValueError('owned_parameter_candidate_current_provenance_changed')
        return candidate, checksum


def candidate_summary(candidate, checksum):
    parsed = OwnedParameterSkillCandidate.model_validate_json(canonical(candidate)).model_dump(mode='json')
    if _hash(checksum) != digest(parsed):
        raise ValueError('owned_parameter_candidate_summary_hash_changed')
    return {key: parsed[key] for key in (
        'schema_version', 'kind', 'provenance_kind', 'synthetic', 'development_only',
        'intent_sha256', 'manifest_sha256', 'status', 'verification_scope',
        'manual_recipe_execution_verified', 'task_terminal_verified', 'record_outcome_verified',
        'native_model_verified', 'released_skill_verified', 'site_outcome_verified',
        'account_scope_verified', 'held_out_independence_verified', 'reviewed',
        'activation_authorized', 'execution_authorized', 'training_ready',
        'gpu_release_verified', 'replay_authorized', 'runtime_started', 'model_calls',
    )} | {'candidate_sha256': checksum, 'scope': parsed['scope'],
         'recipe_sha256': digest(parsed['recipe']), 'run_ref': parsed['recipe_audit']['run_ref']}
