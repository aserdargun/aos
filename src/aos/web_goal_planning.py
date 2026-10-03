"""Private, source-bound web goal proposals; never execution permission."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
from uuid import uuid4
from typing import Literal

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .knowledge import KnowledgeSearch, Hash, text_sha256, UTC_PATTERN
from .knowledge_answer import StoreIdentity
from .owned_skill_knowledge import (OwnedSkillKnowledgePayload, build_planning_knowledge_context,
                                    validate_planning_knowledge_payload)
from .workspace_identity import WorkspaceIdentity
from .owned_form_candidate_execution import _read_private_child, _write_private_child
from .owned_skill_planning import OwnedSkillPlanning
from .web_goal_planner import WebGoalCatalog, WebGoalPlan


def _validate_record(name, value):
    if not validator(name).is_valid(value):
        raise ValueError('web_goal_record_schema_invalid')


class WebGoalKnowledgeReview(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['web_goal_knowledge_review']
    goal: str = Field(min_length=1, max_length=4096)
    authority: dict
    catalog_sha256: Hash
    deployment_sha256: Hash
    query: str = Field(min_length=1, max_length=512)
    retrieval: KnowledgeSearch
    workspace_identity: WorkspaceIdentity
    store_identity: StoreIdentity
    payload: OwnedSkillKnowledgePayload
    expires_at: str = Field(pattern=UTC_PATTERN)
    confirm_sha256: Hash

    @model_validator(mode='after')
    def review_binding(self):
        value = self.model_dump(mode='json')
        expected = build_planning_knowledge_context(value['retrieval'], self.query)
        if (self.payload.context_text != expected
                or self.payload.context_sha256 != text_sha256(expected)
                or self.confirm_sha256 != digest({key: item for key, item in value.items() if key != 'confirm_sha256'})
                or datetime.fromisoformat(self.expires_at).tzinfo is None):
            raise ValueError('web_goal_knowledge_review_invalid')
        return self


def validate_web_goal_knowledge_review(value):
    try:
        parsed = WebGoalKnowledgeReview.model_validate_json(canonical(value)).model_dump(mode='json')
        if canonical(parsed) != canonical(value):
            raise ValueError('web_goal_knowledge_review_changed')
        return parsed
    except Exception:
        raise ValueError('web_goal_knowledge_review_invalid') from None


class WebGoalPlanning(OwnedSkillPlanning):
    @property
    def reserved(self):
        if super().reserved:
            return True
        try:
            return self.unresolved_execution_review(allowed_review=getattr(self, '_admitting_review', None))
        except (ValueError, OSError, TypeError, KeyError):
            return True

    def status(self):
        result = super().status()
        try:
            result['execution_review_pending'] = self.unresolved_execution_review()
        except (ValueError, OSError, TypeError, KeyError):
            result['execution_review_pending'] = True
        if self.current is not None and self.current['status'] == 'consumed' and 'job_id' in self.current:
            result['job_id'] = self.current['job_id']
        return result

    def begin(self, goal, lease_id, generation, *, confirm_catalog_sha256, inference_consent=False,
              knowledge_review=None, confirm_knowledge_sha256=None, storage_consent=False):
        if (self.closed or self.reserved or self.calls >= 8 or inference_consent is not True
                or self.unresolved_execution_review()
                or type(lease_id) is not str or not lease_id
                or type(generation) is not int or generation < 0):
            raise ValueError('web_goal_planning_not_admitted')
        authority, catalog = self.prepare(lease_id, generation)
        if not isinstance(catalog, WebGoalCatalog):
            raise ValueError('web_goal_catalog_not_typed')
        catalog = WebGoalCatalog.model_validate(catalog.model_dump(mode='json'))
        if confirm_catalog_sha256 != digest(catalog.model_dump(mode='json')):
            raise ValueError('web_goal_catalog_confirmation_changed')
        knowledge = None
        context = None
        if knowledge_review is not None:
            review = validate_web_goal_knowledge_review(knowledge_review)
            if (storage_consent is not True or inference_consent is not True
                    or confirm_knowledge_sha256 != review['confirm_sha256']
                    or datetime.fromisoformat(review['expires_at']) <= datetime.now(timezone.utc)
                    or review['goal'] != goal or review['authority'] != authority
                    or review['catalog_sha256'] != confirm_catalog_sha256
                    or review['deployment_sha256'] != digest(self.planner.identity)):
                raise ValueError('web_goal_knowledge_not_confirmed')
            knowledge = {'review': review, 'inference_consent': True, 'storage_consent': True}
            context = self._current_knowledge(knowledge)
        elif confirm_knowledge_sha256 is not None or storage_consent is not False:
            raise ValueError('web_goal_knowledge_review_required')
        self.planner.request_body(goal, [catalog.model_dump(mode='json')], request_context=context)
        request = {'goal': goal, 'lease_id': lease_id, 'generation': generation,
                   'inference_consent': True, 'confirm_catalog_sha256': confirm_catalog_sha256}
        self.calls += 1
        self.current = {'planning_id': 'planning-' + uuid4().hex, 'status': 'pending',
                        'bundle_sha256': None, 'model_called': False, 'real_model': False}
        self.task = asyncio.create_task(self._run_goal(
            self.current, deepcopy(request), deepcopy(authority), catalog, deepcopy(knowledge)))
        return self.status()

    def _current_knowledge(self, knowledge):
        review = validate_web_goal_knowledge_review(knowledge['review'])
        checker = getattr(self, 'knowledge_current', None)
        if (not callable(checker)
                or datetime.fromisoformat(review['expires_at']) <= datetime.now(timezone.utc)):
            raise ValueError('web_goal_knowledge_checker_required')
        checker(deepcopy(review))
        return validate_planning_knowledge_payload(review['payload'])

    def unresolved_execution_review(self, *, allowed_review=None):
        try:
            descriptor = self._open_directory()
        except FileNotFoundError:
            return False
        try:
            names = set(os.listdir(descriptor))
            if len(names) > 64:
                raise ValueError('web_goal_journal_inventory_exceeded')
            for name in names:
                if not re.fullmatch(r'[a-f0-9]{64}\.review\.json', name):
                    continue
                record = json.loads(_read_private_child(descriptor, name, 131072))
                _validate_record('web_goal_execution_review', record)
                accepted_name = name.replace('.review.json', '.accepted.json')
                if accepted_name not in names:
                    if allowed_review is not None and record == allowed_review:
                        continue
                    return True
                receipt = json.loads(_read_private_child(descriptor, accepted_name, 131072))
                _validate_record('web_goal_execution_review', receipt)
                if (record['stage'] != 'intent' or receipt['stage'] != 'accepted'
                        or receipt != (record | {'stage': 'accepted', 'job_id': receipt['job_id']})):
                    raise ValueError('web_goal_review_receipt_changed')
            return False
        finally:
            os.close(descriptor)

    def _current_catalog(self, current, request, authority, catalog):
        if self.closed or current is not self.current or current['status'] != 'pending':
            raise ValueError('web_goal_planning_cancelled')
        refreshed_authority, refreshed_catalog = self.prepare(request['lease_id'], request['generation'])
        if (not isinstance(refreshed_catalog, WebGoalCatalog)
                or canonical(refreshed_authority) != canonical(authority)
                or refreshed_catalog.model_dump(mode='json') != catalog.model_dump(mode='json')):
            raise ValueError('web_goal_planning_source_changed')
        return refreshed_catalog

    async def _run_goal(self, current, request, authority, catalog, knowledge=None):
        try:
            self._current_catalog(current, request, authority, catalog)
            evidence = [catalog.model_dump(mode='json')]
            context = None if knowledge is None else self._current_knowledge(knowledge)
            intent = {'schema_version': '1.0' if knowledge is None else '1.1', 'stage': 'intent',
                      'planning_id': current['planning_id'], 'request': request,
                      'authority': authority, 'catalog': catalog.model_dump(mode='json'),
                      'deployment': deepcopy(self.planner.identity),
                      'model_request': self.planner.request_body(request['goal'], evidence, request_context=context),
                      'model_response': None, 'intent_sha256': None,
                      'execution_authorized': False, 'activation_authorized': False,
                      'training_ready': False, 'downstream_verified': False}
            if knowledge is not None:
                intent['knowledge'] = knowledge
            intent_sha256 = self._persist(intent)
            await self.yield_gpu()
            self._current_catalog(current, request, authority, catalog)
            current['model_called'] = True
            current['real_model'] = self.planner.identity['real_model']
            arguments = {'inference_consent': True,
                'current_catalog': lambda: self._current_catalog(current, request, authority, catalog)}
            if knowledge is not None:
                arguments.update(request_context=context,
                    current_knowledge=lambda: self._current_knowledge(knowledge))
            result = await self.planner.plan(request['goal'], evidence, **arguments)
            self._current_catalog(current, request, authority, catalog)
            result = WebGoalPlan.model_validate(result.model_dump(mode='json'))
            result.validate_catalog(catalog)
            if knowledge is not None:
                self._current_knowledge(knowledge)
                if (getattr(self.planner, 'last_request', None) != intent['model_request']
                        or getattr(self.planner, 'last_response', None) != result.model_dump(mode='json')):
                    raise ValueError('web_goal_knowledge_actual_acknowledgement_missing')
            bundle = intent | {'stage': 'proposal', 'intent_sha256': intent_sha256,
                               'model_response': result.model_dump(mode='json')}
            checksum = self._persist(bundle)
            self._current_catalog(current, request, authority, catalog)
            if knowledge is not None:
                from .web_goal_knowledge_service import WebGoalKnowledgeAcknowledgement

                acknowledgement = WebGoalKnowledgeAcknowledgement.model_validate({
                    'schema_version': '1.0', 'kind': 'web_goal_knowledge_acknowledgement',
                    'planning_id': current['planning_id'], 'bundle_sha256': checksum,
                    'intent_sha256': intent_sha256,
                    'review_sha256': knowledge['review']['confirm_sha256'],
                    'context_sha256': knowledge['review']['payload']['context_sha256'],
                    'model_request_sha256': digest(intent['model_request']),
                    'model_response_sha256': digest(bundle['model_response']),
                    'deployment_sha256': digest(bundle['deployment']),
                    'real_model': bundle['deployment']['real_model'],
                    'acknowledgement_code_path': 'bonsai-web-goal-native-last-request-response-v1'}
                ).model_dump(mode='json')
                self._current_knowledge(knowledge)
                descriptor = self._open_directory()
                try:
                    _write_private_child(descriptor, checksum + '.knowledge-ack.json', canonical(acknowledgement).encode())
                finally:
                    os.close(descriptor)
            current.update(bundle_sha256=checksum,
                           status='ready' if result.decision == 'propose_skill' else 'needs_human')
        except asyncio.CancelledError:
            current['status'] = 'cancelled'
            raise
        except Exception:
            if current['status'] != 'cancelled':
                current['status'] = 'failed'

    def load(self, checksum, *, _intent=False):
        if type(checksum) is not str or re.fullmatch(r'[a-f0-9]{64}', checksum) is None:
            raise ValueError('web_goal_bundle_hash_invalid')
        descriptor = self._open_directory()
        try:
            content = _read_private_child(descriptor, checksum + '.json', 131072)
        finally:
            os.close(descriptor)
        os.close(self._open_directory())
        bundle = json.loads(content)
        _validate_record('web_goal_knowledge_bundle' if bundle.get('schema_version') == '1.1'
                         else 'web_goal_planning_bundle', bundle)
        if _intent and bundle['stage'] != 'intent':
            raise ValueError('web_goal_proposal_requires_original_intent')
        catalog = WebGoalCatalog.model_validate(bundle['catalog'])
        context = None
        if 'knowledge' in bundle:
            knowledge = bundle['knowledge']
            review = validate_web_goal_knowledge_review(knowledge['review'])
            if (knowledge['inference_consent'] is not True or knowledge['storage_consent'] is not True
                    or review['goal'] != bundle['request']['goal']
                    or review['authority'] != bundle['authority']
                    or review['catalog_sha256'] != digest(bundle['catalog'])
                    or review['deployment_sha256'] != digest(bundle['deployment'])):
                raise ValueError('web_goal_knowledge_binding_changed')
            context = validate_planning_knowledge_payload(review['payload'])
        if (canonical(bundle).encode() != content or digest(bundle) != checksum
                or bundle['request']['confirm_catalog_sha256'] != digest(catalog.model_dump(mode='json'))
                or bundle['deployment'] != self.planner.identity
                or bundle['model_request'] != self.planner.request_body(
                    bundle['request']['goal'], [catalog.model_dump(mode='json')], request_context=context)):
            raise ValueError('web_goal_bundle_changed')
        if bundle['stage'] == 'proposal':
            intent = self.load(bundle['intent_sha256'], _intent=True)
            if intent != (bundle | {'stage': 'intent', 'intent_sha256': None, 'model_response': None}):
                raise ValueError('web_goal_intent_binding_changed')
            result = WebGoalPlan.model_validate(bundle['model_response'])
            result.validate_catalog(catalog)
        return bundle

    def select(self, checksum, lease_id, generation):
        if (self.closed or self.reserved or self.current is None or self.current['status'] != 'ready'
                or checksum != self.current['bundle_sha256'] or type(generation) is not int):
            raise ValueError('web_goal_proposal_not_current')
        bundle = self.load(checksum)
        if 'knowledge' in bundle:
            self._current_knowledge(bundle['knowledge'])
        request = bundle['request']
        authority, catalog = self.prepare(lease_id, generation)
        if (request['lease_id'] != lease_id or request['generation'] != generation
                or canonical(authority) != canonical(bundle['authority'])
                or catalog.model_dump(mode='json') != bundle['catalog']):
            raise ValueError('web_goal_proposal_control_or_source_changed')
        result = WebGoalPlan.model_validate(bundle['model_response'])
        result.validate_catalog(catalog)
        return result

    def record_execution_review(self, checksum, preview_sha256, lease_id, generation):
        proposal = self.select(checksum, lease_id, generation)
        bundle = self.load(checksum)
        record = {'schema_version': '1.0', 'stage': 'intent', 'proposal_bundle_sha256': checksum,
                  'preview_sha256': preview_sha256, 'authority': bundle['authority'],
                  'proposal_sha256': digest(proposal.model_dump(mode='json')),
                  'human_confirmed': True, 'job_id': None, 'execution_authorized': False,
                  'training_ready': False}
        _validate_record('web_goal_execution_review', record)
        descriptor = self._open_directory()
        try:
            _write_private_child(descriptor, checksum + '.review.json', canonical(record).encode())
        finally:
            os.close(descriptor)
        self.current['status'] = 'bound'
        return record

    def record_started_job(self, record, job_id):
        receipt = record | {'stage': 'accepted', 'job_id': job_id}
        _validate_record('web_goal_execution_review', receipt)
        descriptor = self._open_directory()
        try:
            original = _read_private_child(descriptor, record['proposal_bundle_sha256'] + '.review.json', 131072)
            if original != canonical(record).encode():
                raise ValueError('web_goal_execution_review_changed')
            _write_private_child(descriptor, record['proposal_bundle_sha256'] + '.accepted.json', canonical(receipt).encode())
        finally:
            os.close(descriptor)
        self.current.update(status='consumed', job_id=job_id)

    def pending_execution_review(self, checksum):
        bundle = self.load(checksum)
        descriptor = self._open_directory()
        try:
            record = json.loads(_read_private_child(descriptor, checksum + '.review.json', 131072))
            try:
                _read_private_child(descriptor, checksum + '.accepted.json', 131072)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('web_goal_start_already_acknowledged')
        finally:
            os.close(descriptor)
        _validate_record('web_goal_execution_review', record)
        if (self.current is None or self.current['status'] != 'bound'
                or self.current['bundle_sha256'] != checksum or record['stage'] != 'intent'
                or record['proposal_bundle_sha256'] != checksum
                or record['authority'] != bundle['authority']
                or record['proposal_sha256'] != digest(bundle['model_response'])):
            raise ValueError('web_goal_pending_start_binding_changed')
        return record

    @contextmanager
    def admit_reviewed_start(self, record):
        if (getattr(self, '_admitting_review', None) is not None or self.current is None
                or self.current['status'] != 'bound'
                or record['proposal_bundle_sha256'] != self.current['bundle_sha256']):
            raise ValueError('web_goal_start_admission_changed')
        descriptor = self._open_directory()
        try:
            stored = _read_private_child(descriptor, record['proposal_bundle_sha256'] + '.review.json', 131072)
            if stored != canonical(record).encode():
                raise ValueError('web_goal_start_review_changed')
        finally:
            os.close(descriptor)
        self._admitting_review = record
        try:
            yield
        finally:
            self._admitting_review = None

    def execution_receipt(self, checksum):
        bundle = self.load(checksum)
        descriptor = self._open_directory()
        try:
            record = json.loads(_read_private_child(descriptor, checksum + '.review.json', 131072))
            receipt = json.loads(_read_private_child(descriptor, checksum + '.accepted.json', 131072))
        finally:
            os.close(descriptor)
        for value in (record, receipt):
            _validate_record('web_goal_execution_review', value)
        if (record['stage'] != 'intent' or receipt['stage'] != 'accepted'
                or record['proposal_bundle_sha256'] != checksum
                or record['authority'] != bundle['authority']
                or record['proposal_sha256'] != digest(bundle['model_response'])
                or receipt != (record | {'stage': 'accepted', 'job_id': receipt['job_id']})):
            raise ValueError('web_goal_execution_receipt_binding_changed')
        return receipt
