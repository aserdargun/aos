"""Bounded reviewed document inputs for separately consented owned skill planning."""

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .knowledge import (EvidenceFlags, Hash, Identifier, KnowledgeHit, KnowledgeScope,
                        KnowledgeSearch, UTC_PATTERN, _utc, text_sha256)
from .knowledge_answer import StoreIdentity
from .workspace_identity import WorkspaceIdentity


CONTEXT_VERSION = 'aos-owned-skill-knowledge-v1'
CONTEXT_PROTOCOL_PIN = 'owned_skill_knowledge_context_protocol'
CONTEXT_SCHEMA_PIN = 'owned_skill_knowledge_context_schema_sha256'
FLAGS = {'untrusted': True, 'execution_authorized': False, 'training_ready': False, 'gold': False,
         'manual_approval_required': True, 'causality_verified': False, 'scope_authorization_verified': False}


def _check(condition):
    if not condition:
        raise ValueError('owned_skill_knowledge_invalid')


def _typed(model, value):
    try:
        parsed = model.model_validate_json(canonical(value)).model_dump(mode='json')
        _check(canonical(parsed) == canonical(value))
        return parsed
    except Exception:
        raise ValueError('owned_skill_knowledge_invalid') from None


class OwnedSkillKnowledgeFlags(EvidenceFlags):
    manual_approval_required: Literal[True]
    causality_verified: Literal[False]
    scope_authorization_verified: Literal[False]

    @field_validator('manual_approval_required', 'causality_verified',
                     'scope_authorization_verified', mode='before')
    @classmethod
    def exact_flags(cls, value):
        if type(value) is not bool:
            raise ValueError('owned_skill_knowledge_exact_boolean_required')
        return value


class OwnedSkillKnowledgeCitation(KnowledgeHit):
    citation_id: str = Field(pattern=r'^c[1-4]$')


class OwnedSkillKnowledgeContext(OwnedSkillKnowledgeFlags):
    context_version: Literal['aos-owned-skill-knowledge-v1']
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    citations: list[OwnedSkillKnowledgeCitation] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def citation_bindings(self):
        _check(sum(len(hit.text) for hit in self.citations) <= 2048
               and len({(hit.document_sha256, hit.chunk_index) for hit in self.citations}) == len(self.citations))
        for index, hit in enumerate(self.citations, 1):
            _check(hit.citation_id == 'c' + str(index) and hit.chunk_sha256 == text_sha256(hit.text)
                   and hit.end - hit.start == len(hit.text) and '\x00' not in hit.text)
        return self


class OwnedSkillKnowledgePayload(TypedModel):
    context_version: Literal['aos-owned-skill-knowledge-v1']
    context_sha256: Hash
    context_text: str = Field(min_length=1, max_length=8192)
    untrusted: Literal[True]

    @field_validator('untrusted', mode='before')
    @classmethod
    def exact_untrusted(cls, value):
        if value is not True:
            raise ValueError('owned_skill_knowledge_untrusted_required')
        return value

    @model_validator(mode='after')
    def context_binding(self):
        value = json.loads(self.context_text)
        _typed(OwnedSkillKnowledgeContext, value)
        _check(self.context_text == canonical(value) and self.context_sha256 == text_sha256(self.context_text))
        return self


def planning_knowledge_schema_sha256():
    return digest(json.loads((REPO_ROOT / 'schemas/owned_skill_knowledge_payload.schema.json').read_text()))


def validate_planning_knowledge_pins(pins):
    _check(pins.get(CONTEXT_PROTOCOL_PIN) == CONTEXT_VERSION
           and pins.get(CONTEXT_SCHEMA_PIN) == planning_knowledge_schema_sha256())


def build_planning_knowledge_context(retrieval: dict, query: str) -> str:
    retrieval = _typed(KnowledgeSearch, retrieval)
    hits = retrieval['hits']
    _check(1 <= len(hits) <= retrieval['top_k'] <= 4 and 256 <= retrieval['context_chars'] <= 2048
           and retrieval['used_context_chars'] == sum(len(hit['text']) for hit in hits)
           and retrieval['used_context_chars'] <= retrieval['context_chars']
           and retrieval['query_sha256'] == text_sha256(query))
    value = _typed(OwnedSkillKnowledgeContext, {'context_version': CONTEXT_VERSION,
        'scope': retrieval['scope'], 'query': query,
        'citations': [dict(hit, citation_id='c' + str(index)) for index, hit in enumerate(hits, 1)], **FLAGS})
    text = canonical(value)
    _check(len(text) <= 8192)
    return text


class OwnedSkillKnowledgePreview(OwnedSkillKnowledgeFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_skill_knowledge_preview']
    preview_id: str = Field(pattern=r'^owned-skill-knowledge-preview-[a-f0-9]{32}$')
    expires_at: str = Field(pattern=UTC_PATTERN, json_schema_extra={'format': 'date-time'})
    goal: str = Field(min_length=1, max_length=256)
    case_key: str = Field(pattern=r'^plan-[a-f0-9]{32}$')
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=4)
    context_chars: int = Field(ge=256, le=2048)
    retrieval: KnowledgeSearch
    authority: dict
    evidence: list[dict] = Field(min_length=1, max_length=1)
    deployment: dict
    model_pins: dict
    workspace_identity: WorkspaceIdentity
    store_identity: StoreIdentity
    context_version: Literal['aos-owned-skill-knowledge-v1']
    context_text: str = Field(min_length=1, max_length=8192)
    context_sha256: Hash
    confirm_sha256: Hash

    @model_validator(mode='after')
    def preview_binding(self):
        from .owned_skill_planner import _host_plan_inputs

        _utc(self.expires_at)
        value = self.model_dump(mode='json')
        try:
            admitted = _host_plan_inputs(self.goal, self.evidence)
        except Exception:
            raise ValueError('owned_skill_knowledge_invalid') from None
        authority = self.authority
        identity_keys = {'manager_session', 'desktop_session_id', 'runtime_id', 'lease_id'}
        hash_keys = {'reuse_admission_sha256', 'source_manifest_sha256', 'source_run_ref',
            'source_invocation_sha256', 'source_fingerprint_sha256', 'family_sha256',
            'release_sha256', 'selection_sha256', 'review_sha256', 'candidate_sha256', 'recipe_sha256'}
        _check(set(authority) == identity_keys | hash_keys | {'generation'})
        for key in identity_keys:
            _check(type(authority[key]) is str and 1 <= len(authority[key]) <= 128
                   and all(character.isascii() and (character.isalnum() or character in '_-')
                           for character in authority[key]))
        for key in hash_keys:
            _check(type(authority[key]) is str and len(authority[key]) == 64
                   and all(character in '0123456789abcdef' for character in authority[key]))
        _check(type(authority['generation']) is int and authority['generation'] >= 0
               and self.case_key == 'plan-' + digest({'goal': self.goal})[:32]
               and admitted['requested_case_key'] == self.case_key
               and self.retrieval.scope == self.scope and self.retrieval.top_k == self.top_k
               and self.retrieval.context_chars == self.context_chars
               and self.context_text == build_planning_knowledge_context(value['retrieval'], self.query)
               and self.context_sha256 == text_sha256(self.context_text)
               and set(self.deployment) == {'deployment_id', 'kind', 'real_model', 'pins'}
               and type(self.deployment['real_model']) is bool
               and self.deployment['pins'] == self.model_pins)
        validate_planning_knowledge_pins(self.model_pins)
        if self.deployment['real_model']:
            _check(self.deployment['kind'] == 'bonsai_native_owned_skill_planner'
                   and self.deployment['deployment_id'] == 'bonsai-' + digest(self.model_pins))
        else:
            _check(self.deployment['kind'] == 'fixture_owned_skill_planner'
                   and self.deployment['deployment_id'] == 'fixture-' + digest(self.model_pins))
        _check(self.confirm_sha256 == digest({key: item for key, item in value.items() if key != 'confirm_sha256'}))
        return self


class OwnedSkillKnowledgePreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    goal: str = Field(min_length=1, max_length=256)
    scope: KnowledgeScope
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=4)
    context_chars: int = Field(ge=256, le=2048)
    lease_id: Identifier
    generation: int = Field(ge=0)


class OwnedSkillKnowledgeStartRequest(TypedModel):
    schema_version: Literal['1.0']
    preview: OwnedSkillKnowledgePreview
    confirm_sha256: Hash
    inference_consent: Literal[True]
    storage_consent: Literal[True]
    lease_id: Identifier
    generation: int = Field(ge=0)

    @field_validator('inference_consent', 'storage_consent', mode='before')
    @classmethod
    def exact_consent(cls, value):
        if value is not True:
            raise ValueError('owned_skill_knowledge_consent_required')
        return value

    @model_validator(mode='after')
    def start_binding(self):
        _check(self.confirm_sha256 == self.preview.confirm_sha256
               and self.lease_id == self.preview.authority['lease_id']
               and self.generation == self.preview.authority['generation'])
        return self


class OwnedSkillKnowledgeReportRequest(TypedModel):
    schema_version: Literal['1.0']
    planning_bundle_sha256: Hash


def validate_planning_knowledge_preview(value: dict) -> dict:
    return _typed(OwnedSkillKnowledgePreview, value)


def planning_knowledge_payload(preview: dict) -> dict:
    preview = validate_planning_knowledge_preview(preview)
    return validate_planning_knowledge_payload({key: preview[key] for key in (
        'context_version', 'context_sha256', 'context_text', 'untrusted')})


def validate_planning_knowledge_payload(value: dict) -> dict:
    return _typed(OwnedSkillKnowledgePayload, value)
