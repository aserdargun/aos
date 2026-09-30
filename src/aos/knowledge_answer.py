"""Consent-bound extractive answers over private reviewed document evidence."""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from .contracts import TypedModel, canonical, digest
from .knowledge import (EVIDENCE_FLAGS, EvidenceFlags, Hash, Identifier, KnowledgeScope,
                        KnowledgeSearch, SearchRequest, text_sha256, validate_knowledge_retrieval)
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child


class AnswerAuthority(TypedModel):
    session_id: str = Field(min_length=1, max_length=128)
    lease_id: Identifier
    generation: int = Field(ge=0)


class StoreIdentity(TypedModel):
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    owner: int = Field(ge=0)
    mode: int = Field(ge=0)


class KnowledgeAnswerPreview(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_answer_preview']
    scope: KnowledgeScope
    question: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=8)
    context_chars: int = Field(ge=256, le=8192)
    retrieval: KnowledgeSearch
    deployment: dict
    authority: AnswerAuthority
    store_identity: StoreIdentity
    confirm_sha256: Hash


class KnowledgeAnswerStatus(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_answer_status']
    available: Literal[True]
    answer_id: str | None
    status: Literal['idle', 'pending', 'ready', 'needs_human', 'cancelled', 'failed']
    bundle_sha256: Hash | None
    real_model: bool
    model_called: bool
    citation_binding_verified: bool
    error_code: str | None


class KnowledgeAnswerReport(KnowledgeAnswerStatus):
    kind: Literal['knowledge_answer_report']
    preview: KnowledgeAnswerPreview | None
    model_request: dict | None
    model_response: dict | None
    bundle: 'KnowledgeAnswerBundle | None'
    historical_binding_verified: bool
    current_source_valid: bool
    current_authority_valid: bool
    semantic_relevance_verified: Literal[False]

    @field_validator('semantic_relevance_verified', mode='before')
    @classmethod
    def exact_semantic_flag(cls, value):
        if value is not False:
            raise ValueError('knowledge_answer_semantic_flag_invalid')
        return value


class AnswerAuthorityRequest(TypedModel):
    schema_version: Literal['1.0']
    lease_id: Identifier
    generation: int = Field(ge=0)


class AnswerPreviewRequest(AnswerAuthorityRequest):
    scope: KnowledgeScope
    question: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=8)
    context_chars: int = Field(ge=256, le=8192)


class AnswerStartRequest(AnswerAuthorityRequest):
    preview: KnowledgeAnswerPreview
    confirm_sha256: Hash
    consent: Literal[True]

    @field_validator('consent', mode='before')
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError('knowledge_answer_consent_required')
        return value


class AnswerStatusRequest(TypedModel):
    schema_version: Literal['1.0']
    answer_id: str | None = Field(default=None, pattern=r'^answer-[a-f0-9]{32}$')


class AnswerReportRequest(TypedModel):
    schema_version: Literal['1.0']
    answer_id: str = Field(pattern=r'^answer-[a-f0-9]{32}$')


class AnswerCancelRequest(TypedModel):
    schema_version: Literal['1.0']
    answer_id: str = Field(pattern=r'^answer-[a-f0-9]{32}$')


class KnowledgeAnswerBundle(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_answer_bundle']
    answer_id: str = Field(pattern=r'^answer-[a-f0-9]{32}$')
    preview: KnowledgeAnswerPreview
    consent: dict
    deployment: dict
    authority: AnswerAuthority
    evidence: list[dict] = Field(min_length=1, max_length=8)
    model_request: dict
    model_response: dict
    real_model: bool
    synthetic: bool
    citation_binding_verified: Literal[True]
    semantic_relevance_verified: Literal[False]

    @field_validator('citation_binding_verified', 'semantic_relevance_verified', mode='before')
    @classmethod
    def exact_verification_flags(cls, value):
        if type(value) is not bool:
            raise ValueError('knowledge_answer_verification_flag_invalid')
        return value


KnowledgeAnswerReport.model_rebuild()


REQUESTS = {'preview': AnswerPreviewRequest, 'start': AnswerStartRequest,
            'status': AnswerStatusRequest, 'report': AnswerReportRequest,
            'cancel': AnswerCancelRequest}
RESPONSES = {'preview': KnowledgeAnswerPreview, 'start': KnowledgeAnswerStatus,
             'status': KnowledgeAnswerStatus, 'report': KnowledgeAnswerReport,
             'cancel': KnowledgeAnswerStatus}


def _copy(value):
    return json.loads(canonical(value))


def _identity(descriptor):
    metadata = os.fstat(descriptor)
    return {'device': metadata.st_dev, 'inode': metadata.st_ino,
            'owner': metadata.st_uid, 'mode': metadata.st_mode}


def answer_evidence(preview):
    return [{'id': 'c' + str(index), 'document_sha256': hit['document_sha256'],
             'chunk_sha256': hit['chunk_sha256'], 'text': hit['text']}
            for index, hit in enumerate(preview['retrieval']['hits'], 1)]


def _validate_preview(preview):
    preview = KnowledgeAnswerPreview.model_validate(preview).model_dump(mode='json')
    retrieval = preview['retrieval']
    if (digest({key: value for key, value in preview.items() if key != 'confirm_sha256'})
            != preview['confirm_sha256'] or not retrieval['hits']
            or retrieval['scope'] != preview['scope']
            or retrieval['query_sha256'] != text_sha256(preview['question'])
            or retrieval['top_k'] != preview['top_k']
            or retrieval['context_chars'] != preview['context_chars']
            or retrieval['used_context_chars'] != sum(len(hit['text']) for hit in retrieval['hits'])
            or retrieval['used_context_chars'] > preview['context_chars']
            or len(retrieval['hits']) > preview['top_k']
            or len({(hit['document_sha256'], hit['chunk_index']) for hit in retrieval['hits']})
            != len(retrieval['hits'])
            or any(text_sha256(hit['text']) != hit['chunk_sha256']
                   or hit['end'] - hit['start'] != len(hit['text']) for hit in retrieval['hits'])
            or type(preview['deployment'].get('real_model')) is not bool):
        raise ValueError('knowledge_answer_preview_invalid')
    return preview


def _validate_selection(response, evidence):
    if (type(response) is not dict or set(response) != {'schema_version', 'needs_human', 'quotes'}
            or response['schema_version'] != '1.0' or type(response['needs_human']) is not bool
            or type(response['quotes']) is not list or len(response['quotes']) > 4
            or response['needs_human'] != (not response['quotes'])):
        raise ValueError('knowledge_answer_model_invalid')
    citations = {item['id']: item['text'] for item in evidence}
    used_ids = set()
    used_texts = set()
    for quote in response['quotes']:
        if (type(quote) is not dict or set(quote) != {'citation_id', 'text'}
                or type(quote['citation_id']) is not str or quote['citation_id'] not in citations
                or type(quote['text']) is not str or not 1 <= len(quote['text']) <= 512
                or not quote['text'].strip() or '\x00' in quote['text']
                or quote['text'] not in citations[quote['citation_id']]
                or quote['citation_id'] in used_ids or quote['text'] in used_texts):
            raise ValueError('knowledge_answer_model_invalid')
        used_ids.add(quote['citation_id'])
        used_texts.add(quote['text'])


def _validate_deployment(answerer):
    identity = answerer.identity
    if type(identity) is not dict or type(identity.get('real_model')) is not bool:
        raise ValueError('knowledge_answer_deployment_invalid')
    if identity['real_model']:
        from .knowledge_answer_model import BonsaiKnowledgeAnswerer
        if (not isinstance(answerer, BonsaiKnowledgeAnswerer)
                or identity.get('kind') != 'bonsai_native_knowledge_answerer'
                or identity.get('pins') != answerer.pins
                or identity.get('deployment_id') != 'bonsai-' + digest(answerer.pins)
                or answerer.pins.get('knowledge_answer_protocol') != 'aos-knowledge-answer-selection-v1'
                or answerer.pins.get('max_output_tokens') != 768):
            raise ValueError('knowledge_answer_deployment_invalid')


def validate_answer_bundle(bundle, answerer):
    try:
        _validate_deployment(answerer)
        bundle = KnowledgeAnswerBundle.model_validate(bundle).model_dump(mode='json')
        preview = _validate_preview(bundle['preview'])
        evidence = answer_evidence(preview)
        if (bundle['deployment'] != preview['deployment']
                or bundle['deployment'] != answerer.identity
                or bundle['authority'] != preview['authority']
                or bundle['real_model'] is not bundle['deployment']['real_model']
                or bundle['synthetic'] != all(hit['synthetic'] for hit in preview['retrieval']['hits'])
                or bundle['evidence'] != evidence
                or bundle['consent'] != {'inference_consent': True, 'storage_consent': True,
                                         'confirm_sha256': preview['confirm_sha256']}
                or any(type(bundle['consent'][key]) is not bool
                       for key in ('inference_consent', 'storage_consent'))
                or bundle['model_request'] != answerer.request_body(preview['question'], evidence)):
            raise ValueError('binding')
        _validate_selection(bundle['model_response'], evidence)
        parsed = answerer.parse_response(canonical(bundle['model_response']), evidence)
        if parsed.model_dump(mode='json') != bundle['model_response']:
            raise ValueError('response')
    except Exception:
        raise ValueError('knowledge_answer_bundle_invalid') from None
    return bundle


class KnowledgeAnswerService:
    def __init__(self, store, answerer, directory: Path, authority, yield_gpu):
        self.store = store
        self.answerer = answerer
        self.directory = Path(directory).absolute()
        self.authority = authority
        self.yield_gpu = yield_gpu
        self.task = None
        self.current = None
        self.answers = {}
        self.calls = 0
        self.closed = False
        self.directory_identity = None
        self.parent_identity = None

    @property
    def reserved(self):
        return (self.current is not None and self.current['status'] == 'pending'
                or self.task is not None and not self.task.done())

    def _authority(self, lease_id, generation):
        request = AnswerAuthorityRequest.model_validate({'schema_version': '1.0',
            'lease_id': lease_id, 'generation': generation})
        authority = AnswerAuthority.model_validate(self.authority(
            request.lease_id, request.generation)).model_dump(mode='json')
        if authority['lease_id'] != lease_id or authority['generation'] != generation:
            raise ValueError('knowledge_answer_authority_changed')
        return authority

    def _store_identity(self):
        descriptor = _directory(self.store.root, create=False)
        try:
            return _identity(descriptor)
        finally:
            os.close(descriptor)

    def preview(self, scope, question, top_k, context_chars, lease_id, generation):
        if self.closed:
            raise ValueError('knowledge_answer_unavailable')
        _validate_deployment(self.answerer)
        request = SearchRequest.model_validate({'schema_version': '1.0', 'scope': scope,
            'query': question, 'top_k': top_k, 'context_chars': context_chars}).model_dump(mode='json')
        authority = self._authority(lease_id, generation)
        retrieval = self.store.search(scope=request['scope'], query=question,
                                     top_k=top_k, context_chars=context_chars)
        if not retrieval['hits']:
            raise ValueError('knowledge_answer_no_evidence')
        identity = self._store_identity()
        value = EVIDENCE_FLAGS | {'schema_version': '1.0', 'kind': 'knowledge_answer_preview',
            'scope': request['scope'], 'question': question, 'top_k': top_k,
            'context_chars': context_chars, 'retrieval': retrieval,
            'deployment': _copy(self.answerer.identity), 'authority': authority,
            'store_identity': identity}
        result = _validate_preview(value | {'confirm_sha256': digest(value)})
        self._sources_current(result)
        if self._authority(lease_id, generation) != authority:
            raise ValueError('knowledge_answer_authority_changed')
        return result

    def _sources_current(self, preview):
        validate_knowledge_retrieval(self.store, preview['retrieval'], preview['store_identity'])

    def _check_current(self, current, preview):
        if self.closed or self.current is not current or current['status'] != 'pending':
            raise ValueError('knowledge_answer_cancelled')
        _validate_deployment(self.answerer)
        if (self.answerer.identity != preview['deployment']
                or self._authority(preview['authority']['lease_id'], preview['authority']['generation'])
                != preview['authority']):
            raise ValueError('knowledge_answer_authority_changed')
        self._sources_current(preview)

    def begin(self, preview, confirm_sha256, consent, lease_id, generation):
        if self.closed or self.reserved or self.calls >= 8:
            raise ValueError('knowledge_answer_unavailable')
        if consent is not True:
            raise ValueError('knowledge_answer_consent_required')
        try:
            preview = _validate_preview(preview)
            expected = self.preview(preview['scope'], preview['question'], preview['top_k'],
                                    preview['context_chars'], lease_id, generation)
            if (type(confirm_sha256) is not str or confirm_sha256 != preview['confirm_sha256']
                    or preview != expected):
                raise ValueError('confirmation')
        except Exception:
            raise ValueError('knowledge_answer_confirmation_changed') from None
        current = {'answer_id': 'answer-' + uuid4().hex, 'status': 'pending',
                   'bundle_sha256': None, 'real_model': False, 'model_called': False,
                   'citation_binding_verified': False, 'error_code': None}
        self.current = current
        self.answers[current['answer_id']] = current
        self.calls += 1
        try:
            self.task = asyncio.create_task(self._run(current, _copy(preview)))
        except Exception:
            current.update(status='failed', error_code='knowledge_answer_unavailable')
            raise ValueError('knowledge_answer_unavailable') from None
        return self.status()

    async def _run(self, current, preview):
        previous_hook = getattr(self.answerer, 'before_model_call', None)
        def before_model_call():
            if previous_hook is not None:
                previous_hook()
            self._check_current(current, preview)
            current['model_called'] = True
            current['real_model'] = preview['deployment']['real_model']
        try:
            self._check_current(current, preview)
            self.answerer.before_model_call = before_model_call
            await self.yield_gpu()
            self._check_current(current, preview)
            evidence = answer_evidence(preview)
            model_request = _copy(self.answerer.request_body(preview['question'], evidence))
            self._check_current(current, preview)
            result = await self.answerer.plan(preview['question'], _copy(evidence))
            self._check_current(current, preview)
            if (not current['model_called']
                    or preview['deployment']['real_model']
                    and getattr(self.answerer, 'last_request', None) != model_request):
                raise ValueError('knowledge_answer_model_binding_invalid')
            bundle = EVIDENCE_FLAGS | {'schema_version': '1.0', 'kind': 'knowledge_answer_bundle',
                'answer_id': current['answer_id'], 'preview': preview,
                'consent': {'inference_consent': True, 'storage_consent': True,
                            'confirm_sha256': preview['confirm_sha256']},
                'deployment': preview['deployment'], 'authority': preview['authority'],
                'evidence': evidence, 'model_request': model_request,
                'model_response': result.model_dump(mode='json'),
                'real_model': preview['deployment']['real_model'],
                'synthetic': all(hit['synthetic'] for hit in preview['retrieval']['hits']),
                'citation_binding_verified': True, 'semantic_relevance_verified': False}
            bundle = validate_answer_bundle(bundle, self.answerer)
            self._check_current(current, preview)
            checksum = self._persist(bundle)
            self._check_current(current, preview)
            current.update(bundle_sha256=checksum, citation_binding_verified=True,
                           status='needs_human' if bundle['model_response']['needs_human'] else 'ready')
        except asyncio.CancelledError:
            current.update(status='cancelled', error_code=None)
            raise
        except Exception:
            if current['status'] != 'cancelled':
                current.update(status='failed', error_code='knowledge_answer_failed',
                               citation_binding_verified=False, bundle_sha256=None)
        finally:
            self.answerer.before_model_call = previous_hook

    def status(self, answer_id=None):
        current = self.current if answer_id is None else self.answers.get(answer_id)
        if answer_id is not None and current is None:
            raise ValueError('knowledge_answer_unknown')
        values = current or {'answer_id': None, 'status': 'idle', 'bundle_sha256': None,
            'real_model': False, 'model_called': False, 'citation_binding_verified': False, 'error_code': None}
        return KnowledgeAnswerStatus.model_validate(EVIDENCE_FLAGS | {
            'schema_version': '1.0', 'kind': 'knowledge_answer_status', 'available': True} | values).model_dump(mode='json')

    def _open_directory(self, create=False):
        parent = _directory(self.directory.parent, create=False)
        descriptor = None
        try:
            parent_identity = _identity(parent)
            if self.parent_identity is not None and parent_identity != self.parent_identity:
                raise ValueError('knowledge_answer_directory_changed')
            if create:
                try:
                    os.mkdir(self.directory.name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            descriptor = _directory(self.directory, create=False)
            identity = _identity(descriptor)
            linked = os.stat(self.directory.name, dir_fd=parent, follow_symlinks=False)
            if (identity != {'device': linked.st_dev, 'inode': linked.st_ino,
                             'owner': linked.st_uid, 'mode': linked.st_mode}
                    or self.directory_identity is not None and identity != self.directory_identity):
                raise ValueError('knowledge_answer_directory_changed')
            self.parent_identity, self.directory_identity = parent_identity, identity
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(parent)

    def _persist(self, bundle):
        checksum = digest(bundle)
        descriptor = self._open_directory(create=True)
        try:
            _write_private_child(descriptor, checksum + '.json', canonical(bundle).encode())
        finally:
            os.close(descriptor)
        if self.load(checksum) != bundle:
            raise ValueError('knowledge_answer_publication_changed')
        return checksum

    def load(self, checksum):
        if type(checksum) is not str or re.fullmatch(r'[a-f0-9]{64}', checksum) is None:
            raise ValueError('knowledge_answer_hash_invalid')
        try:
            descriptor = self._open_directory()
            try:
                content = _read_private_child(descriptor, checksum + '.json', 262144)
            finally:
                os.close(descriptor)
            os.close(self._open_directory())
            bundle = json.loads(content)
            if canonical(bundle).encode() != content or digest(bundle) != checksum:
                raise ValueError('hash')
            return validate_answer_bundle(bundle, self.answerer)
        except Exception:
            raise ValueError('knowledge_answer_bundle_invalid') from None

    def report(self, answer_id):
        status = self.status(answer_id)
        bundle = self.load(status['bundle_sha256']) if status['bundle_sha256'] else None
        if bundle is not None and (bundle['answer_id'] != answer_id
                or bundle['real_model'] != status['real_model']
                or status['status'] != ('needs_human' if bundle['model_response']['needs_human'] else 'ready')):
            raise ValueError('knowledge_answer_bundle_invalid')
        sources_current = authority_current = False
        if bundle is not None:
            try:
                self._sources_current(bundle['preview'])
                sources_current = True
            except Exception:
                pass
            try:
                authority = bundle['authority']
                authority_current = (self._authority(authority['lease_id'], authority['generation']) == authority
                                     and self.answerer.identity == bundle['deployment'] and not self.closed)
            except Exception:
                pass
        return KnowledgeAnswerReport.model_validate(status | {'kind': 'knowledge_answer_report',
            'preview': bundle['preview'] if bundle else None,
            'model_request': bundle['model_request'] if bundle else None,
            'model_response': bundle['model_response'] if bundle else None,
            'bundle': bundle,
            'historical_binding_verified': bundle is not None,
            'current_source_valid': sources_current, 'current_authority_valid': authority_current,
            'semantic_relevance_verified': False}).model_dump(mode='json')

    async def cancel(self, answer_id=None):
        current, task = self.current, self.task
        if answer_id is not None and (current is None or current['answer_id'] != answer_id):
            raise ValueError('knowledge_answer_unknown')
        if current is not None and current['status'] == 'pending':
            current.update(status='cancelled', error_code=None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    await task
        if self.task is task:
            self.task = None
        return self.status()

    async def close(self):
        self.closed = True
        await self.cancel()
