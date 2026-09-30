"""Canonical wire text preserves pinned Python JSON numbers across browser clients."""

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .knowledge import Hash, Identifier
from .owned_skill_knowledge import OwnedSkillKnowledgePreview, OwnedSkillKnowledgeStartRequest
from .owned_skill_knowledge_service import OwnedSkillKnowledgeReport
from .owned_skill_planner import BonsaiOwnedSkillPlanner
from .owned_skill_planning import validate_planning_bundle


def _check(condition):
    if not condition:
        raise ValueError('owned_skill_knowledge_transport_invalid')


def _preview_strings(preview):
    return {'preview_canonical': canonical(preview),
            'preview_body_canonical': canonical({key: value for key, value in preview.items()
                                                 if key != 'confirm_sha256'}),
            'model_pins_canonical': canonical(preview['model_pins'])}


class OwnedSkillKnowledgePreviewTransport(TypedModel):
    schema_version: Literal['1.1']
    preview: OwnedSkillKnowledgePreview
    preview_canonical: str = Field(min_length=1, max_length=131072)
    preview_body_canonical: str = Field(min_length=1, max_length=131072)
    model_pins_canonical: str = Field(min_length=1, max_length=131072)

    @model_validator(mode='after')
    def canonical_binding(self):
        preview = self.preview.model_dump(mode='json')
        _check(all(getattr(self, key) == value for key, value in _preview_strings(preview).items()))
        return self


class OwnedSkillKnowledgeReportTransport(TypedModel):
    schema_version: Literal['1.1']
    report: OwnedSkillKnowledgeReport
    preview_canonical: str = Field(min_length=1, max_length=131072)
    preview_body_canonical: str = Field(min_length=1, max_length=131072)
    model_pins_canonical: str = Field(min_length=1, max_length=131072)
    intent_canonical: str = Field(min_length=1, max_length=131072)
    model_request_canonical: str = Field(min_length=1, max_length=131072)

    @model_validator(mode='after')
    def canonical_binding(self):
        bundle = self.report.bundle
        _check({'knowledge', 'model_request', 'real_model', 'deployment', 'model_pins'} <= set(bundle)
               and type(bundle['knowledge']) is dict)
        knowledge = bundle['knowledge']
        _check({'intent', 'intent_sha256', 'dispatch'} <= set(knowledge)
               and type(knowledge['intent']) is dict and type(knowledge['dispatch']) is dict
               and 'preview' in knowledge['intent'] and 'request_sha256' in knowledge['dispatch'])
        intent = knowledge['intent']
        preview = intent['preview']
        _check(type(preview) is dict and 'model_pins' in preview)
        _check(all(getattr(self, key) == value for key, value in _preview_strings(preview).items())
               and self.intent_canonical == canonical(intent)
               and self.model_request_canonical == canonical(bundle['model_request'])
               and self.report.bundle_canonical == canonical(bundle)
               and self.report.planning_bundle_sha256 == digest(bundle)
               and knowledge['intent_sha256'] == digest(intent)
               and knowledge['dispatch']['request_sha256'] == digest(bundle['model_request'])
               and self.report.real_model is bundle['real_model']
               and self.report.model_request_verified is self.report.real_model
               and self.report.knowledge_applied is self.report.real_model)
        planner = BonsaiOwnedSkillPlanner.__new__(BonsaiOwnedSkillPlanner)
        planner.identity = bundle['deployment']
        planner.pins = bundle['model_pins']
        validate_planning_bundle(bundle, planner)
        return self


class OwnedSkillKnowledgeCanonicalStartRequest(TypedModel):
    schema_version: Literal['1.1']
    preview_canonical: str = Field(min_length=1, max_length=131072)
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
    def canonical_binding(self):
        self.normalized_start()
        return self

    def normalized_start(self):
        try:
            preview = json.loads(self.preview_canonical)
            _check(self.preview_canonical == canonical(preview))
            value = {'schema_version': '1.0', 'preview': preview, 'confirm_sha256': self.confirm_sha256,
                     'inference_consent': self.inference_consent, 'storage_consent': self.storage_consent,
                     'lease_id': self.lease_id, 'generation': self.generation}
            normalized = OwnedSkillKnowledgeStartRequest.model_validate(value).model_dump(mode='json')
            _check(canonical(normalized) == canonical(value))
            return normalized
        except Exception:
            raise ValueError('owned_skill_knowledge_transport_invalid') from None


def preview_transport(preview):
    return OwnedSkillKnowledgePreviewTransport.model_validate(
        {'schema_version': '1.1', 'preview': preview, **_preview_strings(preview)}).model_dump(mode='json')


def report_transport(report):
    bundle = report['bundle']
    intent = bundle['knowledge']['intent']
    return OwnedSkillKnowledgeReportTransport.model_validate(
        {'schema_version': '1.1', 'report': report, **_preview_strings(intent['preview']),
         'intent_canonical': canonical(intent),
         'model_request_canonical': canonical(bundle['model_request'])}).model_dump(mode='json')
