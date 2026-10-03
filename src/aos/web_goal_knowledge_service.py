"""Host-reviewed document context for one separately consented free-goal proposal."""

from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Literal

from pydantic import Field, model_validator

from .contracts import AOSFault, TypedModel, canonical, digest
from .dataset import validator
from .knowledge import Hash, KnowledgeScope, text_sha256, validate_knowledge_retrieval
from .knowledge_answer import _identity
from .learning_event_outbox import _directory
from .owned_skill_knowledge import CONTEXT_VERSION, build_planning_knowledge_context
from .owned_form_candidate_execution import _read_private_child
from .web_goal_planner import BonsaiWebGoalPlanner, WebGoalCatalog, WebGoalPlan
from .web_goal_planning import WebGoalKnowledgeReview, validate_web_goal_knowledge_review
from .workspace_identity import open_existing_workspace, workspace_identity


class WebGoalKnowledgePreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    goal: str = Field(min_length=1, max_length=4096)
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=4)
    context_chars: int = Field(ge=256, le=2048)
    confirm_catalog_sha256: Hash
    lease_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    generation: int = Field(ge=0)


class WebGoalKnowledgeStartRequest(TypedModel):
    schema_version: Literal['1.0']
    review: WebGoalKnowledgeReview
    confirm_knowledge_sha256: Hash
    inference_consent: Literal[True]
    storage_consent: Literal[True]
    lease_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    generation: int = Field(ge=0)


class WebGoalKnowledgeReportRequest(TypedModel):
    schema_version: Literal['1.0']
    bundle_sha256: Hash


class WebGoalKnowledgeAcknowledgement(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_knowledge_acknowledgement']
    planning_id: str = Field(pattern=r'^planning-[a-f0-9]{32}$')
    bundle_sha256: Hash
    intent_sha256: Hash
    review_sha256: Hash
    context_sha256: Hash
    model_request_sha256: Hash
    model_response_sha256: Hash
    deployment_sha256: Hash
    real_model: bool
    acknowledgement_code_path: Literal['bonsai-web-goal-native-last-request-response-v1']


class WebGoalKnowledgeSourceStatus(TypedModel):
    document_sha256: Hash
    content_sha256: Hash
    review_sha256: Hash
    chunk_sha256: Hash
    source_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    synthetic: bool
    available: bool
    current: bool
    expired: bool | None
    current_review_status: Literal['accepted', 'rejected', 'revoked', 'pending', 'unavailable']
    current_review_sha256: Hash | None
    source_binding_valid: bool


class WebGoalKnowledgeReport(TypedModel):
    model_config = {'json_schema_extra': {'allOf': [
        {'if': {'properties': {'actual_model_use_verified': {'const': True}}},
         'then': {'properties': {'real_model': {'const': True},
                                'model_acknowledgement_recorded': {'const': True}}},
         'else': {'anyOf': [{'properties': {'real_model': {'const': False}}},
                           {'properties': {'model_acknowledgement_recorded': {'const': False}}}]}},
        {'if': {'properties': {'model_acknowledgement_recorded': {'const': True}}},
         'then': {'properties': {'acknowledgement': {'type': 'object'},
                                'acknowledgement_canonical': {'type': 'string'},
                                'acknowledgement_sha256': {'type': 'string'}, 'stage': {'const': 'proposal'}}},
         'else': {'properties': {'acknowledgement': {'type': 'null'},
                                'acknowledgement_canonical': {'type': 'null'},
                                'acknowledgement_sha256': {'type': 'null'}}}},
        {'if': {'properties': {'proposal_recorded': {'const': True}}},
         'then': {'properties': {'stage': {'const': 'proposal'}}},
         'else': {'properties': {'stage': {'const': 'intent'}}}},
        {'if': {'properties': {'current_review_valid': {'const': True}}},
         'then': {'properties': {'current_source_valid': {'const': True},
                                'current_authority_valid': {'const': True}, 'review_expired': {'const': False}}},
         'else': {'anyOf': [{'properties': {'current_source_valid': {'const': False}}},
                           {'properties': {'current_authority_valid': {'const': False}}},
                           {'properties': {'review_expired': {'const': True}}}]}}
    ]}}

    schema_version: Literal['1.0']
    kind: Literal['web_goal_knowledge_report']
    bundle_sha256: Hash
    intent_sha256: Hash | None
    planning_id: str = Field(pattern=r'^planning-[a-f0-9]{32}$')
    stage: Literal['intent', 'proposal']
    review_sha256: Hash
    context_sha256: Hash
    model_request_sha256: Hash
    model_response_sha256: Hash | None
    deployment_sha256: Hash
    model_request_canonical: str = Field(min_length=1, max_length=131072)
    model_response_canonical: str | None = Field(max_length=8192)
    deployment_canonical: str = Field(min_length=1, max_length=32768)
    acknowledgement_sha256: Hash | None
    acknowledgement: WebGoalKnowledgeAcknowledgement | None
    acknowledgement_canonical: str | None = Field(max_length=8192)
    bundle: dict
    bundle_canonical: str = Field(min_length=1, max_length=131072)
    historical_binding_verified: Literal[True]
    model_input_bound: Literal[True]
    proposal_recorded: bool
    real_model: bool
    model_acknowledgement_recorded: bool
    actual_model_use_verified: bool
    current_review_valid: bool
    current_source_valid: bool
    current_authority_valid: bool
    current_authority_check: Literal['current_planning_admission']
    review_expired: bool
    sources: list[WebGoalKnowledgeSourceStatus] = Field(min_length=1, max_length=8)
    errors: list[Literal['current_source_invalid', 'current_authority_invalid', 'review_expired']] = Field(max_length=3)
    semantic_relevance_verified: Literal[False]
    downstream_verified: Literal[False]
    training_ready: Literal[False]
    execution_authorized: Literal[False]
    activation_authorized: Literal[False]
    gpu_release_verified: Literal[False]

    @model_validator(mode='after')
    def evidence_consistency(self):
        recorded = self.acknowledgement is not None
        if (self.model_acknowledgement_recorded != recorded
                or recorded != (self.acknowledgement_canonical is not None)
                or recorded != (self.acknowledgement_sha256 is not None)
                or self.actual_model_use_verified != (recorded and self.real_model)
                or self.proposal_recorded != (self.stage == 'proposal')
                or recorded and self.stage != 'proposal'
                or self.current_review_valid != (
                    self.current_source_valid and self.current_authority_valid and not self.review_expired)):
            raise ValueError('web_goal_knowledge_report_evidence_inconsistent')
        if recorded:
            acknowledgement = self.acknowledgement.model_dump(mode='json')
            if (self.acknowledgement_canonical != canonical(acknowledgement)
                    or self.acknowledgement_sha256 != digest(acknowledgement)):
                raise ValueError('web_goal_knowledge_report_acknowledgement_changed')
        return self


class WebGoalKnowledgeService:
    def __init__(self, scheduler, store):
        self.scheduler = scheduler
        self.store = store
        self.planning = scheduler.web_goal_planning

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

    def preview(self, *, goal, scope, query, top_k, context_chars, confirm_catalog_sha256,
                lease_id, generation):
        if self.scheduler.reserved or self.scheduler.closed or self.scheduler.restart_quiesced:
            raise ValueError('web_goal_knowledge_requires_idle_session')
        authority, catalog = self.planning.prepare(lease_id, generation)
        if digest(catalog.model_dump(mode='json')) != confirm_catalog_sha256:
            raise ValueError('web_goal_knowledge_catalog_confirmation_changed')
        if scope != {'application_id': catalog.application_key, 'tenant_id': catalog.tenant_key,
                     'account_role': catalog.account_role}:
            raise ValueError('web_goal_knowledge_scope_does_not_match_catalog')
        retrieval = self.store.search(scope=scope, query=query, top_k=top_k, context_chars=context_chars)
        context = build_planning_knowledge_context(retrieval, query)
        payload = {'context_version': CONTEXT_VERSION, 'context_text': context,
                   'context_sha256': text_sha256(context), 'untrusted': True}
        self.planning.planner.request_body(goal, [catalog.model_dump(mode='json')], request_context=payload)
        review = {'schema_version': '1.0', 'kind': 'web_goal_knowledge_review', 'goal': goal,
                  'authority': authority, 'catalog_sha256': confirm_catalog_sha256,
                  'deployment_sha256': digest(self.planning.planner.identity),
                  'query': query, 'retrieval': retrieval,
                  'workspace_identity': self._workspace(), 'store_identity': self._store_identity(),
                  'payload': payload, 'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        review['confirm_sha256'] = digest(review)
        return self.check_current(review)

    def check_current(self, review):
        review = validate_web_goal_knowledge_review(review)
        if (self.scheduler.closed or self.scheduler.restart_quiesced
                or self._workspace() != review['workspace_identity']
                or digest(self.planning.planner.identity) != review['deployment_sha256']
                or datetime.fromisoformat(review['expires_at']) <= datetime.now(timezone.utc)):
            raise ValueError('web_goal_knowledge_identity_or_expiry_changed')
        authority = review['authority']
        refreshed, catalog = self.planning.prepare(authority['lease_id'], authority['generation'])
        if (refreshed != authority or digest(catalog.model_dump(mode='json')) != review['catalog_sha256']):
            raise ValueError('web_goal_knowledge_authority_or_catalog_changed')
        if review['retrieval']['scope'] != {
                'application_id': catalog.application_key, 'tenant_id': catalog.tenant_key,
                'account_role': catalog.account_role}:
            raise ValueError('web_goal_knowledge_scope_does_not_match_catalog')
        validate_knowledge_retrieval(self.store, review['retrieval'], review['store_identity'])
        return review

    def _report_record(self, checksum, *, acknowledgement=False):
        if type(checksum) is not str or re.fullmatch(r'[a-f0-9]{64}', checksum) is None:
            raise ValueError('web_goal_knowledge_report_hash_invalid')
        filename = checksum + ('.knowledge-ack.json' if acknowledgement else '.json')
        descriptor = self.planning._open_directory()
        try:
            content = _read_private_child(descriptor, filename, 131072)
        finally:
            os.close(descriptor)
        os.close(self.planning._open_directory())
        record = json.loads(content)
        if (canonical(record).encode() != content
                or not acknowledgement and digest(record) != checksum):
            raise ValueError('web_goal_knowledge_report_record_changed')
        return record

    def _historical_binding(self, checksum):
        bundle = self._report_record(checksum)
        if not validator('web_goal_knowledge_bundle').is_valid(bundle):
            raise ValueError('web_goal_knowledge_report_record_invalid')
        review = validate_web_goal_knowledge_review(bundle['knowledge']['review'])
        catalog = WebGoalCatalog.model_validate(bundle['catalog'])
        deployment = bundle['deployment']
        if (type(deployment.get('real_model')) is not bool or type(deployment.get('pins')) is not dict
                or deployment['real_model'] is True and (
                    deployment.get('kind') != 'bonsai_native_web_goal_planner'
                    or deployment.get('deployment_id') != 'bonsai-' + digest(deployment['pins']))
                or bundle['knowledge']['inference_consent'] is not True
                or bundle['knowledge']['storage_consent'] is not True
                or review['goal'] != bundle['request']['goal']
                or review['authority'] != bundle['authority']
                or bundle['request']['lease_id'] != bundle['authority'].get('lease_id')
                or bundle['request']['generation'] != bundle['authority'].get('generation')
                or review['catalog_sha256'] != digest(bundle['catalog'])
                or bundle['request']['confirm_catalog_sha256'] != review['catalog_sha256']
                or review['deployment_sha256'] != digest(deployment)
                or review['retrieval']['scope'] != {'application_id': catalog.application_key,
                    'tenant_id': catalog.tenant_key, 'account_role': catalog.account_role}):
            raise ValueError('web_goal_knowledge_report_binding_changed')
        historical_planner = BonsaiWebGoalPlanner.__new__(BonsaiWebGoalPlanner)
        historical_planner.identity = deployment
        historical_planner.pins = deployment['pins']
        expected_request = historical_planner.request_body(bundle['request']['goal'],
            [catalog.model_dump(mode='json')], request_context=review['payload'])
        if bundle['model_request'] != expected_request:
            raise ValueError('web_goal_knowledge_report_request_changed')
        if bundle['stage'] == 'proposal':
            intent = self._report_record(bundle['intent_sha256'])
            if not validator('web_goal_knowledge_bundle').is_valid(intent):
                raise ValueError('web_goal_knowledge_report_record_invalid')
            if intent != (bundle | {'stage': 'intent', 'intent_sha256': None, 'model_response': None}):
                raise ValueError('web_goal_knowledge_report_intent_changed')
            result = WebGoalPlan.model_validate(bundle['model_response'])
            result.validate_catalog(catalog)
        return bundle, review

    def _report_sources(self, review):
        current_source = False
        try:
            validate_knowledge_retrieval(self.store, review['retrieval'], review['store_identity'])
            current_source = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
        sources = []
        for hit in review['retrieval']['hits']:
            source = {key: hit[key] for key in ('document_sha256', 'content_sha256',
                'review_sha256', 'chunk_sha256', 'source_id', 'title', 'synthetic')}
            source.update(available=False, current=False, expired=None,
                          current_review_status='unavailable', current_review_sha256=None,
                          source_binding_valid=False)
            try:
                inspection = self.store.inspect(scope=review['retrieval']['scope'],
                                                document_sha256=hit['document_sha256'])
                document = inspection['document']
                chunk = document['chunks'][hit['chunk_index']]
                bound = (inspection['current'] and not inspection['expired']
                    and inspection['review_status'] == 'accepted'
                    and inspection['review_sha256'] == hit['review_sha256']
                    and all(document[key] == hit[key] for key in
                            ('content_sha256', 'source_id', 'title', 'synthetic', 'revision'))
                    and all(chunk[key] == hit[key] for key in ('start', 'end', 'text', 'chunk_sha256')))
                source.update(available=True, current=inspection['current'], expired=inspection['expired'],
                    current_review_status=inspection['review_status'],
                    current_review_sha256=inspection['review_sha256'], source_binding_valid=bound)
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                pass
            sources.append(source)
        return current_source and all(source['source_binding_valid'] for source in sources), sources

    def _report_authority(self, review):
        try:
            if (self.scheduler.closed or self.scheduler.restart_quiesced or self.planning.closed
                    or self._workspace() != review['workspace_identity']
                    or digest(self.planning.planner.identity) != review['deployment_sha256']):
                return False
            authority = review['authority']
            current, catalog = self.planning.prepare(authority['lease_id'], authority['generation'])
            return current == authority and digest(catalog.model_dump(mode='json')) == review['catalog_sha256']
        except (OSError, ValueError, TypeError, KeyError, AOSFault):
            return False

    def report(self, *, bundle_sha256):
        bundle, review = self._historical_binding(bundle_sha256)
        acknowledgement = None
        try:
            acknowledgement = self._report_record(bundle_sha256, acknowledgement=True)
        except FileNotFoundError:
            pass
        request_sha256 = digest(bundle['model_request'])
        response_sha256 = None if bundle['model_response'] is None else digest(bundle['model_response'])
        if acknowledgement is not None:
            if not validator('web_goal_knowledge_acknowledgement').is_valid(acknowledgement):
                raise ValueError('web_goal_knowledge_report_acknowledgement_invalid')
            parsed = WebGoalKnowledgeAcknowledgement.model_validate_json(canonical(acknowledgement)).model_dump(mode='json')
            expected = {'schema_version': '1.0', 'kind': 'web_goal_knowledge_acknowledgement',
                'planning_id': bundle['planning_id'], 'bundle_sha256': bundle_sha256,
                'intent_sha256': bundle['intent_sha256'], 'review_sha256': review['confirm_sha256'],
                'context_sha256': review['payload']['context_sha256'],
                'model_request_sha256': request_sha256, 'model_response_sha256': response_sha256,
                'deployment_sha256': digest(bundle['deployment']),
                'real_model': bundle['deployment']['real_model'],
                'acknowledgement_code_path': 'bonsai-web-goal-native-last-request-response-v1'}
            if bundle['stage'] != 'proposal' or parsed != acknowledgement or acknowledgement != expected:
                raise ValueError('web_goal_knowledge_report_acknowledgement_changed')
        current_source, sources = self._report_sources(review)
        current_authority = self._report_authority(review)
        expired = datetime.fromisoformat(review['expires_at']) <= datetime.now(timezone.utc)
        errors = []
        if not current_source:
            errors.append('current_source_invalid')
        if not current_authority:
            errors.append('current_authority_invalid')
        if expired:
            errors.append('review_expired')
        real_model = bundle['deployment']['real_model']
        report = {'schema_version': '1.0', 'kind': 'web_goal_knowledge_report',
            'bundle_sha256': bundle_sha256, 'intent_sha256': bundle['intent_sha256'],
            'planning_id': bundle['planning_id'], 'stage': bundle['stage'],
            'review_sha256': review['confirm_sha256'], 'context_sha256': review['payload']['context_sha256'],
            'model_request_sha256': request_sha256, 'model_response_sha256': response_sha256,
            'deployment_sha256': digest(bundle['deployment']),
            'model_request_canonical': canonical(bundle['model_request']),
            'model_response_canonical': None if bundle['model_response'] is None else canonical(bundle['model_response']),
            'deployment_canonical': canonical(bundle['deployment']),
            'acknowledgement_sha256': None if acknowledgement is None else digest(acknowledgement),
            'acknowledgement': acknowledgement,
            'acknowledgement_canonical': None if acknowledgement is None else canonical(acknowledgement),
            'bundle': bundle, 'bundle_canonical': canonical(bundle),
            'historical_binding_verified': True, 'model_input_bound': True,
            'proposal_recorded': bundle['stage'] == 'proposal', 'real_model': real_model,
            'model_acknowledgement_recorded': acknowledgement is not None,
            'actual_model_use_verified': acknowledgement is not None and real_model,
            'current_review_valid': current_source and current_authority and not expired,
            'current_source_valid': current_source, 'current_authority_valid': current_authority,
            'current_authority_check': 'current_planning_admission',
            'review_expired': expired, 'sources': sources, 'errors': errors,
            'semantic_relevance_verified': False, 'downstream_verified': False, 'training_ready': False,
            'execution_authorized': False, 'activation_authorized': False, 'gpu_release_verified': False}
        return WebGoalKnowledgeReport.model_validate(report).model_dump(mode='json')
