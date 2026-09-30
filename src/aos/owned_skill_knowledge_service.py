"""Separately consented reviewed context for one non-authorizing S2 proposal."""

from datetime import datetime, timedelta, timezone
import json
import os
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .knowledge import Hash, text_sha256, validate_knowledge_retrieval
from .knowledge_answer import _identity
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .owned_skill_knowledge import (
    CONTEXT_VERSION, OwnedSkillKnowledgePreview, OwnedSkillKnowledgePreviewRequest,
    build_planning_knowledge_context, planning_knowledge_payload,
    validate_planning_knowledge_preview)
from .owned_skill_planning import planning_case_key
from .owned_skill_planner import parse_owned_skill_goal
from .task_knowledge import TaskKnowledgeFlags
from .workspace_identity import open_existing_workspace, workspace_identity


class PlanningKnowledgeIntent(TypedModel):
    schema_version: Literal['1.0']
    preview: OwnedSkillKnowledgePreview
    inference_consent: Literal[True]
    storage_consent: Literal[True]

    @model_validator(mode='before')
    @classmethod
    def exact_consent(cls, value):
        if value.get('inference_consent') is not True or value.get('storage_consent') is not True:
            raise ValueError('owned_skill_knowledge_consent_required')
        return value


class PlanningKnowledgeDispatch(TypedModel):
    schema_version: Literal['1.0']
    planning_id: str = Field(pattern=r'^planning-[a-f0-9]{32}$')
    intent_sha256: Hash
    preview_sha256: Hash
    context_sha256: Hash
    request_sha256: Hash
    deployment_id: str = Field(min_length=1, max_length=128)


class PlanningKnowledgeBinding(TypedModel):
    intent_sha256: Hash
    intent: PlanningKnowledgeIntent
    dispatch: PlanningKnowledgeDispatch


class OwnedSkillKnowledgeReport(TaskKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_skill_knowledge_report']
    planning_bundle_sha256: Hash
    bundle: dict
    bundle_canonical: str = Field(min_length=1, max_length=131072)
    historical_binding_verified: Literal[True]
    current_source_valid: bool
    current_authority_valid: bool
    dispatch_recorded: Literal[True]
    model_request_verified: bool
    knowledge_applied: bool
    real_model: bool
    downstream_verified: Literal[False]
    semantic_relevance_verified: Literal[False]


def validate_planning_knowledge_binding(bundle, planner):
    binding = PlanningKnowledgeBinding.model_validate(bundle['knowledge']).model_dump(mode='json')
    preview = validate_planning_knowledge_preview(binding['intent']['preview'])
    dispatch = binding['dispatch']
    context = planning_knowledge_payload(preview)
    expected_request = planner.request_body(bundle['request']['goal'], bundle['evidence'], request_context=context)
    expected_dispatch = {'schema_version': '1.0', 'planning_id': bundle['planning_id'],
                         'intent_sha256': binding['intent_sha256'],
                         'preview_sha256': preview['confirm_sha256'],
                         'context_sha256': preview['context_sha256'],
                         'request_sha256': digest(bundle['model_request']),
                         'deployment_id': bundle['deployment']['deployment_id']}
    if (bundle['schema_version'] != '1.2' or binding != bundle['knowledge']
            or digest(binding['intent']) != binding['intent_sha256']
            or dispatch != expected_dispatch or bundle['model_request'] != expected_request
            or preview['goal'] != bundle['request']['goal']
            or preview['case_key'] != bundle['request']['case_key']
            or preview['authority'] != bundle['authority']
            or preview['evidence'] != bundle['evidence']
            or preview['deployment'] != bundle['deployment'] or preview['model_pins'] != bundle['model_pins']
            or bundle['synthetic'] is not all(hit['synthetic'] for hit in preview['retrieval']['hits'])):
        raise ValueError('owned_skill_knowledge_binding_invalid')
    return binding


class OwnedSkillKnowledgeService:
    def __init__(self, scheduler, store):
        self.scheduler = scheduler
        self.store = store
        self.planning = scheduler.owned_skill_planning
        self.consumed = set()

    def _workspace(self):
        descriptor = open_existing_workspace(self.scheduler.settings.workspace)
        try:
            return workspace_identity(self.scheduler.settings.workspace, descriptor).model_dump(mode='json')
        finally:
            os.close(descriptor)

    def _store_identity(self):
        descriptor = _directory(self.store.root, create=False)
        try:
            return _identity(descriptor)
        finally:
            os.close(descriptor)

    def preview(self, **arguments):
        from .task_knowledge import FLAGS

        request = OwnedSkillKnowledgePreviewRequest.model_validate(arguments).model_dump(mode='json')
        if self.scheduler.closed or self.scheduler.restart_quiesced or self.scheduler.reserved:
            raise ValueError('owned_skill_knowledge_requires_idle_session')
        value = parse_owned_skill_goal(request['goal'])
        case_key = planning_case_key(request['goal'])
        authority, evidence = self.planning.prepare(case_key, value, request['lease_id'], request['generation'])
        retrieval = self.store.search(scope=request['scope'], query=request['query'],
                                      top_k=request['top_k'], context_chars=request['context_chars'])
        if not retrieval['hits']:
            raise ValueError('owned_skill_knowledge_empty_retrieval')
        context = build_planning_knowledge_context(retrieval, request['query'])
        preview = {**FLAGS, 'schema_version': '1.0', 'kind': 'owned_skill_knowledge_preview',
                   'preview_id': 'owned-skill-knowledge-preview-' + uuid4().hex,
                   'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                   **{key: request[key] for key in ('goal', 'scope', 'query', 'top_k', 'context_chars')},
                   'case_key': case_key, 'retrieval': retrieval, 'authority': authority, 'evidence': evidence,
                   'deployment': json.loads(canonical(self.planning.planner.identity)),
                   'model_pins': json.loads(canonical(self.planning.planner.pins)),
                   'workspace_identity': self._workspace(), 'store_identity': self._store_identity(),
                   'context_version': CONTEXT_VERSION, 'context_text': context,
                   'context_sha256': text_sha256(context)}
        preview['confirm_sha256'] = digest(preview)
        preview = validate_planning_knowledge_preview(preview)
        self.check_current(preview)
        return preview

    def _check_authority(self, preview, *, check_plan=True):
        preview = validate_planning_knowledge_preview(preview)
        if (self.scheduler.closed or self.scheduler.restart_quiesced
                or self._workspace() != preview['workspace_identity']
                or self.planning.planner.identity != preview['deployment']
                or self.planning.planner.pins != preview['model_pins']):
            raise ValueError('owned_skill_knowledge_identity_changed')
        authority = preview['authority']
        desktop = self.scheduler.controller.state()
        if (desktop['owner'] != 'AGENT' or desktop['status'] != 'running'
                or desktop['lease_id'] != authority['lease_id']
                or desktop['generation'] != authority['generation']
                or desktop['session_id'] != authority['desktop_session_id']
                or desktop['runtime_id'] != authority['runtime_id']):
            raise ValueError('owned_skill_knowledge_control_changed')
        if check_plan:
            current_authority, current_evidence = self.planning.prepare(
                preview['case_key'], parse_owned_skill_goal(preview['goal']),
                authority['lease_id'], authority['generation'])
            if current_authority != authority or current_evidence != preview['evidence']:
                raise ValueError('owned_skill_knowledge_skill_changed')
        return preview

    def check_current(self, preview, *, check_plan=True):
        preview = self._check_authority(preview, check_plan=check_plan)
        validate_knowledge_retrieval(self.store, preview['retrieval'], preview['store_identity'])
        return preview

    def _write(self, name, value):
        descriptor = self.planning._open_directory(create=True)
        try:
            _write_private_child(descriptor, name, canonical(value).encode())
        finally:
            os.close(descriptor)

    def _read(self, name):
        descriptor = self.planning._open_directory()
        try:
            raw = _read_private_child(descriptor, name, 131072)
        finally:
            os.close(descriptor)
        value = json.loads(raw)
        if canonical(value).encode() != raw:
            raise ValueError('owned_skill_knowledge_private_record_changed')
        return value

    def begin(self, **arguments):
        from .owned_skill_knowledge import OwnedSkillKnowledgeStartRequest

        request = OwnedSkillKnowledgeStartRequest.model_validate(arguments).model_dump(mode='json')
        preview = self.check_current(request['preview'])
        if (self.scheduler.reserved or request['confirm_sha256'] != preview['confirm_sha256']
                or request['lease_id'] != preview['authority']['lease_id']
                or request['generation'] != preview['authority']['generation']
                or datetime.fromisoformat(preview['expires_at']) <= datetime.now(timezone.utc)
                or preview['preview_id'] in self.consumed):
            raise ValueError('owned_skill_knowledge_confirmation_expired_or_used')
        intent = PlanningKnowledgeIntent.model_validate({'schema_version': '1.0', 'preview': preview,
            'inference_consent': request['inference_consent'],
            'storage_consent': request['storage_consent']}).model_dump(mode='json')
        checksum = digest(intent)
        self._write('knowledge-intent-' + checksum + '.json', intent)
        self.consumed.add(preview['preview_id'])
        return self.planning.begin(preview['goal'], request['lease_id'], request['generation'],
                                   knowledge={'intent_sha256': checksum, 'intent': intent})

    def dispatch(self, current, knowledge):
        preview = self.check_current(knowledge['intent']['preview'])
        if self._read('knowledge-intent-' + knowledge['intent_sha256'] + '.json') != knowledge['intent']:
            raise ValueError('owned_skill_knowledge_intent_changed')
        request = self.planning.planner.request_body(
            preview['goal'], preview['evidence'], request_context=planning_knowledge_payload(preview))
        dispatch = PlanningKnowledgeDispatch.model_validate({
            'schema_version': '1.0', 'planning_id': current['planning_id'],
            'intent_sha256': knowledge['intent_sha256'], 'preview_sha256': preview['confirm_sha256'],
            'context_sha256': preview['context_sha256'], 'request_sha256': digest(request),
            'deployment_id': preview['deployment']['deployment_id']}).model_dump(mode='json')
        self._write('knowledge-dispatch-' + knowledge['intent_sha256'] + '.json', dispatch)
        current['knowledge_dispatch'] = dispatch

    def check_binding(self, bundle, *, check_plan=True):
        binding = validate_planning_knowledge_binding(bundle, self.planning.planner)
        if (self._read('knowledge-intent-' + binding['intent_sha256'] + '.json') != binding['intent']
                or self._read('knowledge-dispatch-' + binding['intent_sha256'] + '.json') != binding['dispatch']):
            raise ValueError('owned_skill_knowledge_private_binding_changed')
        self.check_current(binding['intent']['preview'], check_plan=check_plan)
        return binding

    def report(self, checksum):
        from .task_knowledge import FLAGS

        bundle = self.planning.load(checksum)
        binding = validate_planning_knowledge_binding(bundle, self.planning.planner)
        if (self._read('knowledge-intent-' + binding['intent_sha256'] + '.json') != binding['intent']
                or self._read('knowledge-dispatch-' + binding['intent_sha256'] + '.json') != binding['dispatch']):
            raise ValueError('owned_skill_knowledge_private_binding_changed')
        preview = binding['intent']['preview']
        current_source, current_authority = False, False
        try:
            validate_knowledge_retrieval(self.store, preview['retrieval'], preview['store_identity'])
            current_source = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
        try:
            self._check_authority(preview, check_plan=False)
            current_authority = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
        verified = bundle['real_model'] is True
        return OwnedSkillKnowledgeReport.model_validate({**FLAGS,
            'schema_version': '1.0', 'kind': 'owned_skill_knowledge_report',
            'planning_bundle_sha256': checksum, 'bundle': bundle, 'bundle_canonical': canonical(bundle),
            'historical_binding_verified': True, 'current_source_valid': current_source,
            'current_authority_valid': current_authority, 'dispatch_recorded': True,
            'model_request_verified': verified, 'knowledge_applied': verified, 'real_model': bundle['real_model'],
            'downstream_verified': False, 'semantic_relevance_verified': False}).model_dump(mode='json')
