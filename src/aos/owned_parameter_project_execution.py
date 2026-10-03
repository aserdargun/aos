"""Private, one-use execution receipts for manually authored synthetic projects."""

import json
import os
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import State, TypedModel, canonical, digest
from .knowledge import Hash, KnowledgeScope
from .owned_form_candidate_execution import _read_private_child
from .owned_parameter_project import OwnedParameterProjectManifest
from .site_skill_form_recipe import SiteSkillFormRecipeInvocation
from .site_skill_form_recipe_audit import SiteSkillFormRecipeAuditReport
from .web_goal_execution_binding import (
    WebGoalExecutionAuthority, WebGoalWholeRecordObservation, _typed, _unique_pairs,
)
from .web_goal_execution_journal import (
    MAX_INTENTS, MAX_RECORD_BYTES, RECORD_NAMES, WebGoalExecutionJournal,
    WebGoalExecutionRunIdentity,
)
from .workspace_identity import WorkspaceIdentity, open_existing_workspace, workspace_identity
from .knowledge_answer import StoreIdentity


PINS = ('profile_sha256', 'task_sha256', 'skill_sha256', 'skill_plan_sha256',
        'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256',
        'field_binding_sha256', 'recipe_sha256', 'case_key')


class _BootstrapFlags(TypedModel):
    released_skill_verified: Literal[False] = False
    native_model_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    activation_authorized: Literal[False] = False
    training_ready: Literal[False] = False
    gpu_release_verified: Literal[False] = False
    replay_authorized: Literal[False] = False

    @field_validator('released_skill_verified', 'native_model_verified', 'site_outcome_verified',
                     'activation_authorized', 'training_ready', 'gpu_release_verified',
                     'replay_authorized', mode='before')
    @classmethod
    def exact_false(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_bootstrap_claim_invalid')
        return value


class OwnedParameterProjectExecutionSource(TypedModel):
    manifest_sha256: Hash
    manifest: OwnedParameterProjectManifest
    invocation: SiteSkillFormRecipeInvocation
    scope: KnowledgeScope
    expected_fields: dict[str, str] = Field(min_length=2, max_length=8)

    @model_validator(mode='after')
    def exact_source(self):
        if (self.manifest_sha256 != digest(self.manifest.model_dump(mode='json'))
                or self.manifest.invocation_sha256 != digest(self.invocation.model_dump(mode='json'))):
            raise ValueError('owned_parameter_bootstrap_source_invalid')
        return self


class OwnedParameterProjectExecutionIntent(_BootstrapFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_project_manual_bootstrap_intent']
    synthetic: Literal[True]
    job_id: str = Field(pattern=r'^job-[a-f0-9]{32}$')
    source: OwnedParameterProjectExecutionSource
    source_sha256: Hash
    authority: WebGoalExecutionAuthority
    workspace_identity: WorkspaceIdentity
    execution_workspace_identity: WorkspaceIdentity
    journal_identity: StoreIdentity

    @model_validator(mode='after')
    def exact_source_hash(self):
        if self.source_sha256 != digest(self.source.model_dump(mode='json')):
            raise ValueError('owned_parameter_bootstrap_source_changed')
        return self


class OwnedParameterProjectExecutionRunBinding(_BootstrapFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_project_manual_bootstrap_run_binding']
    intent_sha256: Hash
    run_identity: WebGoalExecutionRunIdentity


class OwnedParameterProjectExecutionReceipt(_BootstrapFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_project_manual_bootstrap_receipt']
    intent_sha256: Hash
    run_binding_sha256: Hash
    run_identity: WebGoalExecutionRunIdentity
    recipe_audit: SiteSkillFormRecipeAuditReport
    recipe_audit_sha256: Hash
    observation: WebGoalWholeRecordObservation
    observation_sha256: Hash
    terminal_status: Literal['succeeded']
    verification_scope: Literal['synthetic_manual_bootstrap_run_and_whole_record']
    task_terminal_verified: Literal[True]
    recipe_execution_verified: Literal[True]
    record_outcome_verified: Literal[True]

    @field_validator('task_terminal_verified', 'recipe_execution_verified',
                     'record_outcome_verified', mode='before')
    @classmethod
    def exact_true(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_bootstrap_claim_invalid')
        return value

    @model_validator(mode='after')
    def exact_hashes(self):
        if (self.recipe_audit_sha256 != digest(self.recipe_audit.model_dump(mode='json'))
                or self.observation_sha256 != digest(self.observation.model_dump(mode='json'))):
            raise ValueError('owned_parameter_bootstrap_receipt_changed')
        return self


class OwnedParameterProjectExecutionStatusEntry(TypedModel):
    intent_sha256: Hash
    job_id: str = Field(pattern=r'^job-[a-f0-9]{32}$')
    manifest_sha256: Hash
    status: Literal['unresolved', 'accepted_verified']
    receipt_sha256: Hash | None

    @model_validator(mode='after')
    def exact_stage(self):
        if (self.status == 'accepted_verified') != (self.receipt_sha256 is not None):
            raise ValueError('owned_parameter_bootstrap_status_invalid')
        return self


class OwnedParameterProjectExecutionStatus(_BootstrapFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_project_manual_bootstrap_status']
    entries: list[OwnedParameterProjectExecutionStatusEntry] = Field(max_length=MAX_INTENTS)
    reserved: bool

    @model_validator(mode='after')
    def exact_reservation(self):
        if self.reserved != any(entry.status == 'unresolved' for entry in self.entries):
            raise ValueError('owned_parameter_bootstrap_status_invalid')
        return self


MODELS = {'intent': OwnedParameterProjectExecutionIntent,
          'bound': OwnedParameterProjectExecutionRunBinding,
          'accepted': OwnedParameterProjectExecutionReceipt}


def _audit_matches(source, run, audit):
    invocation = source['invocation']
    if (audit['run_ref'] != digest({'run_id': run['run_id']})
            or audit['invocation_sha256'] != digest(invocation)
            or any(audit[field] != invocation[field] for field in PINS)
            or audit['symbolic_step_keys'] != [step['step_key'] for step in invocation['steps']]
            or [stage['stage'] for stage in audit['stages']]
            != [step['operation'] for step in invocation['steps']]):
        raise ValueError('owned_parameter_bootstrap_audit_changed')


def _observation_matches(source, observation):
    if (observation['scope'] != source['scope']
            or observation['record'] != source['expected_fields']
            or type(observation['reported_post_count']) is not int
            or observation['reported_post_count'] != 1
            or observation['effect_status'] != 'committed'):
        raise ValueError('owned_parameter_bootstrap_whole_record_changed')


class OwnedParameterProjectExecutionJournal(WebGoalExecutionJournal):
    def _read(self, descriptor, filename, stage):
        content = _read_private_child(descriptor, filename, MAX_RECORD_BYTES)
        value = MODELS[stage].model_validate_json(content).model_dump(mode='json')
        if canonical(value).encode() != content:
            raise ValueError('owned_parameter_bootstrap_journal_noncanonical')
        return value

    def _inventory(self, descriptor, workspace, identity):
        names = os.listdir(descriptor)
        if len(names) > MAX_INTENTS * 3 or any(RECORD_NAMES.fullmatch(name) is None for name in names):
            raise ValueError('owned_parameter_bootstrap_journal_inventory_invalid')
        records = {}
        for filename in names:
            checksum, stage = RECORD_NAMES.fullmatch(filename).groups()
            if stage == 'intent':
                intent = self._read(descriptor, filename, stage)
                if (digest(intent) != checksum or intent['workspace_identity'] != workspace
                        or intent['journal_identity'] != identity):
                    raise ValueError('owned_parameter_bootstrap_journal_identity_changed')
                records[checksum] = {'intent': intent, 'bound': None, 'accepted': None}
        if len(records) > MAX_INTENTS:
            raise ValueError('owned_parameter_bootstrap_journal_full')
        for filename in names:
            checksum, stage = RECORD_NAMES.fullmatch(filename).groups()
            if stage == 'intent':
                continue
            record = self._read(descriptor, filename, stage)
            if checksum not in records or record['intent_sha256'] != checksum:
                raise ValueError('owned_parameter_bootstrap_journal_orphan')
            records[checksum][stage] = record
        jobs, manifests, runs = set(), set(), set()
        for checksum, entry in records.items():
            intent, bound, receipt = entry['intent'], entry['bound'], entry['accepted']
            manifest = intent['source']['manifest_sha256']
            if intent['job_id'] in jobs or manifest in manifests:
                raise ValueError('owned_parameter_bootstrap_journal_duplicate')
            jobs.add(intent['job_id'])
            manifests.add(manifest)
            if bound is not None:
                run = bound['run_identity']
                if (run['run_id'] in runs
                        or run['owner_lease_id'] != intent['authority']['lease_id']
                        or run['skill_invocation_sha256'] != intent['source']['manifest']['invocation_sha256']):
                    raise ValueError('owned_parameter_bootstrap_journal_run_changed')
                runs.add(run['run_id'])
            if receipt is not None:
                if (bound is None or receipt['run_identity'] != bound['run_identity']
                        or receipt['run_binding_sha256'] != digest(bound)):
                    raise ValueError('owned_parameter_bootstrap_journal_receipt_changed')
                _audit_matches(intent['source'], bound['run_identity'], receipt['recipe_audit'])
                _observation_matches(intent['source'], receipt['observation'])
        return records


class OwnedParameterProjectExecution:
    def __init__(self, manager, project_bundle, journal_directory, *, current_source, source_auditor):
        self.manager = manager
        self.bundle = project_bundle
        self.loader = current_source
        self.auditor = source_auditor
        self.journal = OwnedParameterProjectExecutionJournal(journal_directory)
        workspace = manager.settings.workspace.absolute()
        directory = self.journal.directory
        if (workspace == directory or workspace in directory.parents or directory in workspace.parents
                or not callable(current_source) or not callable(source_auditor)):
            raise ValueError('owned_parameter_bootstrap_configuration_invalid')
        self.workspace_fd = open_existing_workspace(workspace)
        self.workspace = workspace_identity(workspace, self.workspace_fd)
        self.source = self._source(project_bundle)
        self.authority = None
        self.intent_sha256 = None
        self.job_id = None
        self.readback_attempted = False
        self.fixture = manager.remote_form_owned_fixture
        self.store = manager.store
        self.connection = manager.store.connection
        try:
            self._current()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _source(bundle):
        return _typed(OwnedParameterProjectExecutionSource, {
            'manifest_sha256': bundle['manifest_sha256'], 'manifest': bundle['manifest'],
            'invocation': bundle['invocation'], 'scope': bundle['record_config']['scope'],
            'expected_fields': dict(bundle['fields']),
        }).model_dump(mode='json')

    def _control(self):
        manager = self.manager
        control = manager.controller.state()
        return WebGoalExecutionAuthority(
            manager_session=manager.controller.session_id,
            desktop_session_id=manager.controller.session_id,
            runtime_id=control['runtime_id'], lease_id=control['lease_id'],
            generation=control['generation'], owner=control['owner'], status=control['status'],
        ).model_dump(mode='json')

    def _current(self):
        manager = self.manager
        descriptor = open_existing_workspace(manager.settings.workspace)
        try:
            if (self.workspace_fd is None or manager.closed or manager.restart_quiesced
                    or workspace_identity(manager.settings.workspace, descriptor) != self.workspace
                    or workspace_identity(manager.settings.workspace, self.workspace_fd) != self.workspace
                    or self.store is not manager.store
                    or self.connection is not manager.store.connection
                    or self._source(self.loader()) != self.source
                    or self.fixture is not manager.remote_form_owned_fixture
                    or getattr(self.fixture, 'record_mode', False) is not True
                    or manager.remote_form_owned_candidate_session is not None
                    or manager.remote_form_public_plan_sha256 is not None
                    or manager.remote_form_cookie is not None
                    or manager._owned_skill_reuse is not None
                    or manager.remote_entry_profile_sha256 != self.source['invocation']['profile_sha256']
                    or digest(manager.remote_entry_task.model_dump(mode='json')) != self.source['invocation']['task_sha256']
                    or digest(manager.remote_form_plan.model_dump(mode='json')) != self.source['invocation']['form_plan_sha256']
                    or digest(manager.remote_form_state_plan.model_dump(mode='json')) != self.source['invocation']['state_plan_sha256']
                    or manager.remote_form_skill_invocation_sha256 != digest(self.source['invocation'])
                    or canonical(manager.remote_form_skill_invocation) != canonical(self.source['invocation'])
                    or dict(manager.remote_form_fields) != self.source['expected_fields']):
                raise ValueError('owned_parameter_bootstrap_configuration_changed')
        finally:
            os.close(descriptor)
        if self.authority is not None and self._control() != self.authority:
            raise ValueError('owned_parameter_bootstrap_control_changed')
        manager.remote_form_owned_target.assert_plan(
            self.source['invocation']['profile_sha256'], self.source['invocation']['form_plan_sha256'])
        manager.remote_form_owned_target.assert_state_plan(self.source['invocation']['state_plan_sha256'])

    @property
    def reserved(self):
        if self.intent_sha256 is not None:
            try:
                with self.journal._locked() as (descriptor, workspace, identity):
                    records = self.journal._inventory(descriptor, workspace, identity)
                    if self.intent_sha256 not in records:
                        return True
                    return any(entry['accepted'] is None for entry in records.values())
            except (OSError, ValueError, TypeError, KeyError):
                return True
        return self.journal.reserved

    def reserve(self, job_id, lease_id, generation):
        if self.intent_sha256 is not None:
            raise ValueError('owned_parameter_bootstrap_already_consumed')
        self._current()
        authority = self._control()
        if (authority['lease_id'] != lease_id or type(generation) is not int
                or authority['generation'] != generation):
            raise ValueError('owned_parameter_bootstrap_control_changed')
        with self.journal._locked(create=True, write=True) as (descriptor, workspace, identity):
            records = self.journal._inventory(descriptor, workspace, identity)
            if (len(records) >= MAX_INTENTS or any(entry['accepted'] is None for entry in records.values())
                    or any(entry['intent']['source']['manifest_sha256'] == self.source['manifest_sha256']
                           for entry in records.values())):
                raise ValueError('owned_parameter_bootstrap_consumed_or_unresolved')
            intent = OwnedParameterProjectExecutionIntent.model_validate_json(canonical({
                'schema_version': '1.0', 'kind': 'owned_parameter_project_manual_bootstrap_intent',
                'synthetic': True, 'job_id': job_id, 'source': self.source,
                'source_sha256': digest(self.source), 'authority': authority,
                'workspace_identity': workspace, 'execution_workspace_identity': self.workspace.model_dump(mode='json'),
                'journal_identity': identity,
            })).model_dump(mode='json')
            self._current()
            if self._control() != authority:
                raise ValueError('owned_parameter_bootstrap_control_changed')
            checksum = digest(intent)
            self.journal._write(descriptor, checksum + '.intent.json', intent)
            self.authority, self.intent_sha256, self.job_id = authority, checksum, job_id
            return checksum

    def check_job(self, job_id):
        if self.intent_sha256 is None or job_id != self.job_id:
            raise ValueError('owned_parameter_bootstrap_job_not_admitted')
        self._current()
        with self.journal._locked() as (descriptor, workspace, identity):
            entry = self.journal._entry(self.journal._inventory(descriptor, workspace, identity), self.intent_sha256)
            intent = entry['intent']
            if (intent['job_id'] != job_id or intent['source'] != self.source
                    or intent['authority'] != self.authority
                    or intent['execution_workspace_identity'] != self.workspace.model_dump(mode='json')):
                raise ValueError('owned_parameter_bootstrap_intent_changed')

    def _job(self, job_id, *, check_journal=True):
        if check_journal:
            self.check_job(job_id)
        else:
            self._current()
        row = self.store.connection.execute('SELECT * FROM desktop_tasks WHERE job_id=?', (job_id,)).fetchone()
        if (row is None or row['session_id'] != self.authority['desktop_session_id']
                or row['kind'] != 'browser_remote_form' or row['lease_id'] != self.authority['lease_id']
                or row['generation'] != self.authority['generation']):
            raise ValueError('owned_parameter_bootstrap_job_changed')
        return row

    def _terminal(self, job_id, bound):
        row = self._job(job_id, check_journal=False)
        if bound is None or row['status'] != 'succeeded':
            raise ValueError('owned_parameter_bootstrap_terminal_not_verified')
        run = bound['run_identity']
        stored = self.store.state(run['run_id'])
        actual_run = self.store.connection.execute('SELECT status,task_id FROM runs WHERE run_id=?',
                                                   (run['run_id'],)).fetchone()
        if (row['run_id'] != run['run_id'] or row['runtime_id'] != run['runtime_id']
                or actual_run is None or actual_run['status'] != 'succeeded'
                or actual_run['task_id'] != run['task_id'] or stored.phase.value != 'SUCCEEDED'
                or stored.task_kind != 'browser_remote_form'
                or any(getattr(stored, field) != value for field, value in run.items())):
            raise ValueError('owned_parameter_bootstrap_terminal_changed')
        return run

    def bind_run(self, job_id, state):
        row = self._job(job_id)
        state = _typed(State, state)
        stored = self.store.state(state.run_id)
        run = _typed(WebGoalExecutionRunIdentity, {field: getattr(state, field)
            for field in WebGoalExecutionRunIdentity.model_fields}).model_dump(mode='json')
        if (state.task_kind != 'browser_remote_form' or stored != state
                or row['run_id'] != state.run_id or row['runtime_id'] != state.runtime_id
                or state.owner_lease_id != self.authority['lease_id']
                or state.skill_invocation_sha256 != digest(self.source['invocation'])):
            raise ValueError('owned_parameter_bootstrap_run_changed')
        with self.journal._locked(write=True) as (descriptor, workspace, identity):
            records = self.journal._inventory(descriptor, workspace, identity)
            entry = self.journal._entry(records, self.intent_sha256)
            if entry['accepted'] is not None:
                raise ValueError('owned_parameter_bootstrap_already_accepted')
            if any(other['bound'] is not None and other['bound']['run_identity']['run_id'] == run['run_id']
                   for checksum, other in records.items() if checksum != self.intent_sha256):
                raise ValueError('owned_parameter_bootstrap_run_reused')
            bound = OwnedParameterProjectExecutionRunBinding.model_validate({
                'schema_version': '1.0', 'kind': 'owned_parameter_project_manual_bootstrap_run_binding',
                'intent_sha256': self.intent_sha256, 'run_identity': run,
            }).model_dump(mode='json')
            self._current()
            self.journal._write(descriptor, self.intent_sha256 + '.bound.json', bound)
            return bound

    def finish(self, job_id):
        if job_id != self.job_id or self.intent_sha256 is None:
            raise ValueError('owned_parameter_bootstrap_job_not_admitted')
        with self.journal._locked(write=True) as (descriptor, workspace, identity):
            entry = self.journal._entry(self.journal._inventory(descriptor, workspace, identity), self.intent_sha256)
            if entry['accepted'] is not None:
                return entry['accepted']
            bound = entry['bound']
            run = self._terminal(job_id, bound)
            audit = _typed(SiteSkillFormRecipeAuditReport, self.auditor(run['run_id'])).model_dump(mode='json')
            _audit_matches(self.source, run, audit)
            self._terminal(job_id, bound)
            if self.readback_attempted:
                raise ValueError('owned_parameter_bootstrap_readback_consumed')
            self.readback_attempted = True
            payload = self.fixture.read_whole_record(
                profile_sha256=self.source['invocation']['profile_sha256'],
                form_plan_sha256=self.source['invocation']['form_plan_sha256'],
                state_plan_sha256=self.source['invocation']['state_plan_sha256'])
            if type(payload) is not bytes or not 1 <= len(payload) <= 8192:
                raise ValueError('owned_parameter_bootstrap_readback_invalid')
            observation = _typed(WebGoalWholeRecordObservation,
                                 json.loads(payload, object_pairs_hook=_unique_pairs)).model_dump(mode='json')
            _observation_matches(self.source, observation)
            self._terminal(job_id, bound)
            receipt = OwnedParameterProjectExecutionReceipt.model_validate_json(canonical({
                'schema_version': '1.0', 'kind': 'owned_parameter_project_manual_bootstrap_receipt',
                'intent_sha256': self.intent_sha256, 'run_binding_sha256': digest(bound),
                'run_identity': run, 'recipe_audit': audit, 'recipe_audit_sha256': digest(audit),
                'observation': observation, 'observation_sha256': digest(observation),
                'terminal_status': 'succeeded', 'verification_scope': 'synthetic_manual_bootstrap_run_and_whole_record',
                'task_terminal_verified': True, 'recipe_execution_verified': True, 'record_outcome_verified': True,
            })).model_dump(mode='json')
            self.journal._write(descriptor, self.intent_sha256 + '.accepted.json', receipt)
            return receipt

    def status(self):
        try:
            with self.journal._locked() as (descriptor, workspace, identity):
                records = self.journal._inventory(descriptor, workspace, identity)
                if self.intent_sha256 is not None and self.intent_sha256 not in records:
                    raise ValueError('owned_parameter_bootstrap_intent_missing')
                entries = [{'intent_sha256': checksum, 'job_id': entry['intent']['job_id'],
                            'manifest_sha256': entry['intent']['source']['manifest_sha256'],
                            'status': 'accepted_verified' if entry['accepted'] else 'unresolved',
                            'receipt_sha256': digest(entry['accepted']) if entry['accepted'] else None}
                           for checksum, entry in sorted(records.items())]
        except FileNotFoundError:
            if self.journal._journal_identity is not None:
                raise ValueError('owned_parameter_bootstrap_journal_missing') from None
            entries = []
        return OwnedParameterProjectExecutionStatus.model_validate({
                'schema_version': '1.0', 'kind': 'owned_parameter_project_manual_bootstrap_status',
                'entries': entries, 'reserved': any(entry['status'] == 'unresolved' for entry in entries),
                **_BootstrapFlags().model_dump(mode='json')}).model_dump(mode='json')

    def close(self):
        if self.workspace_fd is not None:
            os.close(self.workspace_fd)
            self.workspace_fd = None
