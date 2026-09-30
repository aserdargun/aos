"""Private reviewed uploaded text; deterministic retrieval never grants authority."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Annotated, Literal
import unicodedata

from pydantic import Field, field_validator

from .contracts import TypedModel, canonical, digest
from .learning_event_outbox import _directory
from .owned_form_candidate_execution import _read_private_child, _write_private_child


Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')]
LIMITS = {'max_documents': 128, 'max_revisions': 32, 'max_text_chars': 32768,
          'max_text_bytes': 65536, 'chunk_chars': 1024, 'max_chunks_per_document': 32,
          'max_query_chars': 512, 'max_top_k': 8, 'max_context_chars': 8192}
MAX_REVIEWS = 512
MAX_FILE_BYTES = 524288
EVIDENCE_FLAGS = {'untrusted': True, 'execution_authorized': False, 'training_ready': False, 'gold': False}
UTC_PATTERN = r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$'


def text_sha256(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _tokens(value):
    folded = unicodedata.normalize('NFKD', value.casefold())
    folded = ''.join(character for character in folded if unicodedata.category(character) != 'Mn')
    return set(re.findall(r'\w+', folded, flags=re.UNICODE))


def _utc(value):
    if type(value) is not str or re.fullmatch(UTC_PATTERN, value) is None:
        raise ValueError('knowledge_utc_time_required')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError('knowledge_utc_time_required')
    return instant


class KnowledgeScope(TypedModel):
    application_id: Identifier
    tenant_id: Identifier
    account_role: Identifier


class EvidenceFlags(TypedModel):
    untrusted: Literal[True]
    execution_authorized: Literal[False]
    training_ready: Literal[False]
    gold: Literal[False]

    @field_validator('untrusted', 'execution_authorized', 'training_ready', 'gold', mode='before')
    @classmethod
    def exact_evidence_flags(cls, value):
        if type(value) is not bool:
            raise ValueError('knowledge_exact_boolean_required')
        return value


class KnowledgeUpload(TypedModel):
    scope: KnowledgeScope
    source_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=32768)
    previous_sha256: Hash | None
    expires_at: str = Field(min_length=20, max_length=40, pattern=UTC_PATTERN,
                            json_schema_extra={'format': 'date-time'})
    rights_attested: Literal[True]
    storage_consent: Literal[True]
    synthetic: bool

    @field_validator('rights_attested', 'storage_consent', mode='before')
    @classmethod
    def explicit_consent(cls, value):
        if value is not True:
            raise ValueError('knowledge_explicit_consent_required')
        return value

    @field_validator('title')
    @classmethod
    def bounded_title(cls, value):
        if not value.strip() or '\x00' in value or len(value.encode('utf-8')) > 800:
            raise ValueError('knowledge_title_invalid')
        return value

    @field_validator('text')
    @classmethod
    def bounded_text(cls, value):
        if not value.strip() or '\x00' in value or len(value.encode('utf-8')) > 65536:
            raise ValueError('knowledge_text_invalid')
        return value

    @field_validator('expires_at')
    @classmethod
    def utc_expiry(cls, value):
        _utc(value)
        return value


class KnowledgeChunk(TypedModel):
    index: int = Field(ge=0, le=31)
    start: int = Field(ge=0, le=32767)
    end: int = Field(ge=1, le=32768)
    text: str = Field(min_length=1, max_length=1024)
    chunk_sha256: Hash


class KnowledgeDocument(KnowledgeUpload, EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['uploaded_text_knowledge']
    revision: int = Field(ge=1, le=32)
    content_sha256: Hash
    chunks: list[KnowledgeChunk] = Field(min_length=1, max_length=32)


class KnowledgePublicationPreview(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_publication_preview']
    document: KnowledgeDocument
    document_sha256: Hash
    preview_sha256: Hash


class KnowledgeReviewPreview(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_review_preview']
    scope: KnowledgeScope
    document_sha256: Hash
    decision: Literal['accept', 'reject', 'revoke']
    previous_review_sha256: Hash | None
    preview_sha256: Hash


class KnowledgeReview(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_human_review']
    scope: KnowledgeScope
    document_sha256: Hash
    decision: Literal['accept', 'reject', 'revoke']
    previous_review_sha256: Hash | None
    created_at: str = Field(pattern=UTC_PATTERN, json_schema_extra={'format': 'date-time'})


class KnowledgeInspection(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_document_inspection']
    document_sha256: Hash
    document: KnowledgeDocument
    current: bool
    expired: bool
    review_status: Literal['pending', 'accepted', 'rejected', 'revoked']
    review_sha256: Hash | None


class KnowledgeCatalogItem(TypedModel):
    document_sha256: Hash
    source_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1, le=32)
    content_sha256: Hash
    expires_at: str = Field(pattern=UTC_PATTERN, json_schema_extra={'format': 'date-time'})
    synthetic: bool
    current: bool
    expired: bool
    review_status: Literal['pending', 'accepted', 'rejected', 'revoked']
    review_sha256: Hash | None


class KnowledgeLimits(TypedModel):
    max_documents: Literal[128]
    max_revisions: Literal[32]
    max_text_chars: Literal[32768]
    max_text_bytes: Literal[65536]
    chunk_chars: Literal[1024]
    max_chunks_per_document: Literal[32]
    max_query_chars: Literal[512]
    max_top_k: Literal[8]
    max_context_chars: Literal[8192]


class KnowledgeCatalog(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_catalog']
    scope: KnowledgeScope
    documents: list[KnowledgeCatalogItem] = Field(max_length=128)
    limits: KnowledgeLimits


class KnowledgeHit(TypedModel):
    document_sha256: Hash
    content_sha256: Hash
    review_sha256: Hash
    source_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1, le=32)
    chunk_index: int = Field(ge=0, le=31)
    chunk_sha256: Hash
    start: int = Field(ge=0, le=32767)
    end: int = Field(ge=1, le=32768)
    text: str = Field(min_length=1, max_length=1024)
    score: int = Field(ge=1, le=512)
    synthetic: bool


class KnowledgeSearch(EvidenceFlags):
    schema_version: Literal['1.0']
    kind: Literal['knowledge_search']
    scope: KnowledgeScope
    query_sha256: Hash
    method: Literal['deterministic_lexical']
    top_k: int = Field(ge=1, le=8)
    context_chars: int = Field(ge=256, le=8192)
    used_context_chars: int = Field(ge=0, le=8192)
    hits: list[KnowledgeHit] = Field(max_length=8)


class ScopeRequest(TypedModel):
    schema_version: Literal['1.0']
    scope: KnowledgeScope


class UploadRequest(KnowledgeUpload):
    schema_version: Literal['1.0']


class InspectRequest(ScopeRequest):
    document_sha256: Hash


class ReviewPreviewRequest(InspectRequest):
    decision: Literal['accept', 'reject', 'revoke']


class SearchRequest(ScopeRequest):
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=8)
    context_chars: int = Field(ge=256, le=8192)

    @field_validator('query')
    @classmethod
    def bounded_query(cls, value):
        if not value.strip() or '\x00' in value or len(value.encode('utf-8')) > 2048:
            raise ValueError('knowledge_query_invalid')
        return value


class PublishRequest(TypedModel):
    schema_version: Literal['1.0']
    preview: KnowledgePublicationPreview
    confirm_sha256: Hash
    lease_id: Identifier
    generation: int = Field(ge=0)


class ReviewRequest(TypedModel):
    schema_version: Literal['1.0']
    preview: KnowledgeReviewPreview
    confirm_sha256: Hash
    lease_id: Identifier
    generation: int = Field(ge=0)


REQUESTS = {'publish-preview': UploadRequest, 'publish': PublishRequest,
            'review-preview': ReviewPreviewRequest, 'review': ReviewRequest,
            'inspect': InspectRequest, 'catalog': ScopeRequest, 'search': SearchRequest}
RESPONSES = {'publish-preview': KnowledgePublicationPreview, 'publish': KnowledgeInspection,
             'review-preview': KnowledgeReviewPreview, 'review': KnowledgeInspection,
             'inspect': KnowledgeInspection, 'catalog': KnowledgeCatalog, 'search': KnowledgeSearch}


def _chunks(text):
    return [{'index': index, 'start': start, 'end': min(start + 1024, len(text)),
             'text': text[start:start + 1024], 'chunk_sha256': text_sha256(text[start:start + 1024])}
            for index, start in enumerate(range(0, len(text), 1024))]


class KnowledgeStore:
    def __init__(self, root: Path, *, clock=None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _snapshot(self, *, write=False):
        try:
            descriptor = _directory(self.root, create=write)
        except FileNotFoundError:
            if write:
                raise
            yield None, {}, {}
            return
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            documents, reviews = self._load(descriptor)
            yield descriptor, documents, reviews
            self._check_root(descriptor)
        finally:
            os.close(descriptor)

    def _check_root(self, descriptor):
        current = _directory(self.root, create=False)
        try:
            before, after = os.fstat(descriptor), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError('knowledge_root_changed')
        finally:
            os.close(current)

    def _load(self, descriptor):
        documents, reviews = {}, {}
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries, 1):
                if count > LIMITS['max_documents'] + MAX_REVIEWS:
                    raise ValueError('knowledge_store_limit')
                match = re.fullmatch(r'(document|review)-([a-f0-9]{64})\.json', entry.name)
                if match is None:
                    raise ValueError('knowledge_store_inventory_invalid')
                kind, checksum = match.groups()
                payload = _read_private_child(descriptor, entry.name, MAX_FILE_BYTES)
                value = json.loads(payload)
                model = KnowledgeDocument if kind == 'document' else KnowledgeReview
                model.model_validate(value)
                if payload != canonical(value).encode() or digest(value) != checksum:
                    raise ValueError('knowledge_store_integrity_invalid')
                if kind == 'document':
                    if value['chunks'] != _chunks(value['text']) or value['content_sha256'] != text_sha256(value['text']):
                        raise ValueError('knowledge_chunk_integrity_invalid')
                    documents[checksum] = value
                else:
                    _utc(value['created_at'])
                    reviews[checksum] = value
        if len(documents) > 128 or len(reviews) > MAX_REVIEWS:
            raise ValueError('knowledge_store_limit')
        for checksum, document in documents.items():
            previous = document['previous_sha256']
            if previous is None:
                if document['revision'] != 1:
                    raise ValueError('knowledge_lineage_invalid')
            else:
                parent = documents.get(previous)
                if (parent is None or parent['scope'] != document['scope']
                        or parent['source_id'] != document['source_id']
                        or document['revision'] != parent['revision'] + 1):
                    raise ValueError('knowledge_lineage_invalid')
            self._head(documents, document['scope'], document['source_id'])
        for checksum, review in reviews.items():
            document = documents.get(review['document_sha256'])
            previous = review['previous_review_sha256']
            parent = reviews.get(previous) if previous is not None else None
            if (document is None or review['scope'] != document['scope']
                    or previous is not None and (parent is None
                        or parent['document_sha256'] != review['document_sha256'])
                    or review['decision'] == 'revoke' and (parent is None or parent['decision'] != 'accept')
                    or parent is not None and (parent['decision'] == 'revoke'
                        or _utc(review['created_at']) < _utc(parent['created_at']))):
                raise ValueError('knowledge_review_lineage_invalid')
            self._review_head(reviews, review['document_sha256'])
        return documents, reviews

    def _head(self, documents, scope, source_id):
        matches = {checksum: value for checksum, value in documents.items()
                   if value['scope'] == scope and value['source_id'] == source_id}
        if not matches:
            return None
        if len({value['revision'] for value in matches.values()}) != len(matches):
            raise ValueError('knowledge_lineage_fork')
        return max(matches, key=lambda checksum: matches[checksum]['revision'])

    def _review_head(self, reviews, checksum):
        matches = {key: value for key, value in reviews.items() if value['document_sha256'] == checksum}
        parents = {value['previous_review_sha256'] for value in matches.values()}
        heads = set(matches) - parents
        if matches and len(heads) != 1:
            raise ValueError('knowledge_review_fork')
        if len([value for value in matches.values() if value['previous_review_sha256'] is None]) > 1:
            raise ValueError('knowledge_review_fork')
        head = next(iter(heads)) if heads else None
        current, visited = head, set()
        while current is not None:
            if current in visited or current not in matches:
                raise ValueError('knowledge_review_lineage_invalid')
            visited.add(current)
            current = matches[current]['previous_review_sha256']
        if visited != set(matches):
            raise ValueError('knowledge_review_lineage_invalid')
        return head

    def _expiry(self, document):
        expiry = _utc(document['expires_at'])
        if not self.clock() < expiry <= self.clock() + timedelta(days=30):
            raise ValueError('knowledge_publication_expiry_invalid')

    def _publication(self, upload, documents):
        head = self._head(documents, upload['scope'], upload['source_id'])
        if upload['previous_sha256'] != head:
            raise ValueError('knowledge_current_parent_required')
        self._expiry(upload)
        document = KnowledgeDocument.model_validate(upload | EVIDENCE_FLAGS | {
            'schema_version': '1.0', 'kind': 'uploaded_text_knowledge',
            'revision': documents[head]['revision'] + 1 if head else 1,
            'content_sha256': text_sha256(upload['text']), 'chunks': _chunks(upload['text'])}).model_dump()
        preview = {'schema_version': '1.0', 'kind': 'knowledge_publication_preview',
                   'document': document, 'document_sha256': digest(document)}
        return preview | {'preview_sha256': digest(preview)}

    def publish_preview(self, **arguments):
        upload = KnowledgeUpload.model_validate(arguments).model_dump()
        with self._snapshot() as (descriptor, documents, reviews):
            if len(documents) >= 128:
                raise ValueError('knowledge_store_limit')
            return self._publication(upload, documents)

    def publish(self, *, preview, confirm_sha256):
        preview = KnowledgePublicationPreview.model_validate(preview).model_dump()
        if confirm_sha256 != preview['preview_sha256']:
            raise ValueError('knowledge_confirmation_required')
        upload = {key: preview['document'][key] for key in KnowledgeUpload.model_fields}
        with self._snapshot(write=True) as (descriptor, documents, reviews):
            checksum = preview['document_sha256']
            if checksum in documents:
                if documents[checksum] != preview['document'] or digest({key: value for key, value in preview.items() if key != 'preview_sha256'}) != confirm_sha256:
                    raise ValueError('knowledge_publication_changed')
                return self._inspection(upload['scope'], checksum, documents, reviews)
            if len(documents) >= 128 or self._publication(upload, documents) != preview:
                raise ValueError('knowledge_publication_changed')
            self._check_root(descriptor)
            _write_private_child(descriptor, 'document-' + checksum + '.json', canonical(preview['document']).encode())
            documents[checksum] = preview['document']
            return self._inspection(upload['scope'], checksum, documents, reviews)

    def _inspection(self, scope, document_sha256, documents, reviews):
        document = documents.get(document_sha256)
        if document is None or document['scope'] != scope:
            raise ValueError('knowledge_exact_scope_required')
        review_hash = self._review_head(reviews, document_sha256)
        status = {'accept': 'accepted', 'reject': 'rejected', 'revoke': 'revoked'}
        return KnowledgeInspection.model_validate(EVIDENCE_FLAGS | {
            'schema_version': '1.0', 'kind': 'knowledge_document_inspection',
            'document_sha256': document_sha256, 'document': document,
            'current': self._head(documents, scope, document['source_id']) == document_sha256,
            'expired': _utc(document['expires_at']) <= self.clock(),
            'review_status': status[reviews[review_hash]['decision']] if review_hash else 'pending',
            'review_sha256': review_hash}).model_dump()

    def inspect(self, *, scope, document_sha256):
        scope = KnowledgeScope.model_validate(scope).model_dump()
        with self._snapshot() as (descriptor, documents, reviews):
            return self._inspection(scope, document_sha256, documents, reviews)

    def _review_preview(self, scope, document_sha256, decision, documents, reviews):
        inspection = self._inspection(scope, document_sha256, documents, reviews)
        if (decision != 'revoke' and (not inspection['current'] or inspection['expired'])
                or inspection['review_status'] == 'revoked'
                or decision == 'revoke' and inspection['review_status'] != 'accepted'):
            raise ValueError('knowledge_review_not_current')
        value = {'schema_version': '1.0', 'kind': 'knowledge_review_preview',
                 'scope': scope, 'document_sha256': document_sha256, 'decision': decision,
                 'previous_review_sha256': inspection['review_sha256']}
        return KnowledgeReviewPreview.model_validate(value | {'preview_sha256': digest(value)}).model_dump()

    def review_preview(self, *, scope, document_sha256, decision):
        scope = KnowledgeScope.model_validate(scope).model_dump()
        with self._snapshot() as (descriptor, documents, reviews):
            return self._review_preview(scope, document_sha256, decision, documents, reviews)

    def review(self, *, preview, confirm_sha256):
        preview = KnowledgeReviewPreview.model_validate(preview).model_dump()
        with self._snapshot(write=True) as (descriptor, documents, reviews):
            expected = self._review_preview(preview['scope'], preview['document_sha256'], preview['decision'], documents, reviews)
            if preview != expected or confirm_sha256 != expected['preview_sha256'] or len(reviews) >= MAX_REVIEWS:
                raise ValueError('knowledge_review_confirmation_changed')
            record = {key: value for key, value in preview.items() if key not in {'kind', 'preview_sha256'}}
            record |= {'kind': 'knowledge_human_review', 'created_at': self.clock().isoformat()}
            checksum = digest(record)
            self._check_root(descriptor)
            _write_private_child(descriptor, 'review-' + checksum + '.json', canonical(record).encode())
            reviews[checksum] = record
            return self._inspection(preview['scope'], preview['document_sha256'], documents, reviews)

    def catalog(self, *, scope):
        scope = KnowledgeScope.model_validate(scope).model_dump()
        with self._snapshot() as (descriptor, documents, reviews):
            items = []
            for checksum, document in sorted(documents.items(), key=lambda item: (item[1]['source_id'], item[1]['revision'])):
                if document['scope'] != scope:
                    continue
                inspection = self._inspection(scope, checksum, documents, reviews)
                items.append({key: (inspection[key] if key in inspection else document[key])
                              for key in KnowledgeCatalogItem.model_fields})
            return KnowledgeCatalog.model_validate(EVIDENCE_FLAGS | {'schema_version': '1.0', 'kind': 'knowledge_catalog',
                'scope': scope, 'documents': items, 'limits': LIMITS}).model_dump()

    def search(self, *, scope, query, top_k, context_chars):
        request = SearchRequest.model_validate({'schema_version': '1.0', 'scope': scope,
            'query': query, 'top_k': top_k, 'context_chars': context_chars}).model_dump()
        scope = request['scope']
        tokens = _tokens(query)
        if not tokens:
            raise ValueError('knowledge_query_terms_required')
        candidates = []
        with self._snapshot() as (descriptor, documents, reviews):
            for checksum, document in documents.items():
                if document['scope'] != scope:
                    continue
                inspection = self._inspection(scope, checksum, documents, reviews)
                if not inspection['current'] or inspection['expired'] or inspection['review_status'] != 'accepted':
                    continue
                for chunk in document['chunks']:
                    terms = _tokens(chunk['text'])
                    score = len(tokens & terms)
                    if score:
                        candidates.append(KnowledgeHit.model_validate({
                            'document_sha256': checksum, 'content_sha256': document['content_sha256'],
                            'review_sha256': inspection['review_sha256'], 'source_id': document['source_id'],
                            'title': document['title'], 'revision': document['revision'],
                            'chunk_index': chunk['index'], 'chunk_sha256': chunk['chunk_sha256'],
                            'start': chunk['start'], 'end': chunk['end'], 'text': chunk['text'],
                            'score': score, 'synthetic': document['synthetic']}).model_dump())
            hits, used = [], 0
            for candidate in sorted(candidates, key=lambda item: (-item['score'], item['document_sha256'], item['chunk_index'])):
                if len(hits) == top_k:
                    break
                if used + len(candidate['text']) > context_chars:
                    continue
                hits.append(candidate)
                used += len(candidate['text'])
            return KnowledgeSearch.model_validate(EVIDENCE_FLAGS | {'schema_version': '1.0', 'kind': 'knowledge_search',
                'scope': scope, 'query_sha256': text_sha256(query), 'method': 'deterministic_lexical',
                'top_k': top_k, 'context_chars': context_chars, 'used_context_chars': used, 'hits': hits}).model_dump()


def validate_knowledge_retrieval(store, retrieval, store_identity):
    retrieval = KnowledgeSearch.model_validate(retrieval).model_dump(mode='json')
    with store._snapshot() as (descriptor, documents, reviews):
        if descriptor is None:
            raise ValueError('knowledge_retrieval_source_changed')
        metadata = os.fstat(descriptor)
        identity = {'device': metadata.st_dev, 'inode': metadata.st_ino,
                    'owner': metadata.st_uid, 'mode': metadata.st_mode}
        if (identity != store_identity or not retrieval['hits']
                or len(retrieval['hits']) > retrieval['top_k']
                or retrieval['used_context_chars'] != sum(len(hit['text']) for hit in retrieval['hits'])
                or retrieval['used_context_chars'] > retrieval['context_chars']
                or len({(hit['document_sha256'], hit['chunk_index']) for hit in retrieval['hits']})
                != len(retrieval['hits'])):
            raise ValueError('knowledge_retrieval_source_changed')
        for hit in retrieval['hits']:
            inspection = store._inspection(retrieval['scope'], hit['document_sha256'], documents, reviews)
            document = inspection['document']
            if hit['chunk_index'] >= len(document['chunks']):
                raise ValueError('knowledge_retrieval_source_changed')
            chunk = document['chunks'][hit['chunk_index']]
            if (not inspection['current'] or inspection['expired']
                    or inspection['review_status'] != 'accepted'
                    or inspection['review_sha256'] != hit['review_sha256']
                    or document['content_sha256'] != hit['content_sha256']
                    or document['source_id'] != hit['source_id'] or document['title'] != hit['title']
                    or document['revision'] != hit['revision'] or document['synthetic'] != hit['synthetic']
                    or any(chunk[key] != hit[key] for key in ('start', 'end', 'text', 'chunk_sha256'))):
                raise ValueError('knowledge_retrieval_source_changed')
    return retrieval
