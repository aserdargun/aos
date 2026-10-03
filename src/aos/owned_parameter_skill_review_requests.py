from typing import Literal

from pydantic import Field

from .contracts import TypedModel
from .web_application import Checksum


class OwnedParameterSkillReviewPreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    candidate_sha256: Checksum


class OwnedParameterSkillReviewAcceptRequest(OwnedParameterSkillReviewPreviewRequest):
    confirm_review_sha256: Checksum
    human_confirmation: Literal['ACCEPT_MANUAL_REVIEW']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillReviewReadRequest(TypedModel):
    schema_version: Literal['1.0']
    review_sha256: Checksum


class OwnedParameterSkillReviewRevokeRequest(OwnedParameterSkillReviewReadRequest):
    confirm_revocation_sha256: Checksum
    human_confirmation: Literal['REVOKE_MANUAL_REVIEW']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillReviewRecoveryPreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    record_sha256: Checksum


class OwnedParameterSkillReviewRecoverRequest(OwnedParameterSkillReviewRecoveryPreviewRequest):
    confirm_recovery_sha256: Checksum
    human_confirmation: Literal['RESTORE_ANCHORED_REVIEW_RECORD']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)
