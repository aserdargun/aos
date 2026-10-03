from typing import Literal

from pydantic import Field

from .contracts import TypedModel
from .web_application import Checksum


class OwnedParameterSkillPreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    intent_sha256: Checksum


class OwnedParameterSkillPublishRequest(OwnedParameterSkillPreviewRequest):
    confirm_candidate_sha256: Checksum
    human_confirmation: Literal['PUBLISH_MANUAL_CANDIDATE']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillReadRequest(TypedModel):
    schema_version: Literal['1.0']
    candidate_sha256: Checksum
