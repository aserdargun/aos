from typing import Annotated, Literal

from pydantic import Field, field_validator

from .contracts import TypedModel
from .knowledge import Hash
from .web_application import Key


class OwnedParameterSkillReusePreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    release_sha256: Hash
    selection_sha256: Hash
    parameters: dict[Key, Annotated[str, Field(strict=True, min_length=1, max_length=256)]] = Field(
        min_length=2, max_length=8)
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(strict=True, ge=0)


class OwnedParameterSkillReuseStartRequest(OwnedParameterSkillReusePreviewRequest):
    confirm_sha256: Hash
    human_confirmation: Literal[True]

    @field_validator('human_confirmation', mode='before')
    @classmethod
    def exact_confirmation(cls, value):
        if value is not True:
            raise ValueError('owned_parameter_reuse_requires_explicit_confirmation')
        return value


class OwnedParameterSkillReuseStatusRequest(TypedModel):
    schema_version: Literal['1.0']


class OwnedParameterSkillReuseReadRequest(OwnedParameterSkillReuseStatusRequest):
    intent_sha256: Hash


class OwnedParameterSkillReuseNextPreviewRequest(OwnedParameterSkillReusePreviewRequest):
    previous_intent_sha256: Hash
    previous_receipt_sha256: Hash


class OwnedParameterSkillReuseNextStartRequest(OwnedParameterSkillReuseStartRequest):
    previous_intent_sha256: Hash
    previous_receipt_sha256: Hash


REQUESTS = {'preview': OwnedParameterSkillReusePreviewRequest,
            'start': OwnedParameterSkillReuseStartRequest,
            'status': OwnedParameterSkillReuseStatusRequest,
            'read': OwnedParameterSkillReuseReadRequest,
            'next-preview': OwnedParameterSkillReuseNextPreviewRequest,
            'next-start': OwnedParameterSkillReuseNextStartRequest}
