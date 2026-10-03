from typing import Literal

from pydantic import Field

from .contracts import TypedModel
from .web_application import Checksum


class OwnedParameterSkillReleasePreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    review_sha256: Checksum
    expected_parent_release_sha256: Checksum | None


class OwnedParameterSkillReleaseRequest(OwnedParameterSkillReleasePreviewRequest):
    confirm_release_sha256: Checksum
    human_confirmation: Literal['RELEASE_MANUAL_SKILL']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillReleaseReadRequest(TypedModel):
    schema_version: Literal['1.0']
    release_sha256: Checksum


class OwnedParameterSkillReleaseInventoryRequest(TypedModel):
    schema_version: Literal['1.0']


class OwnedParameterSkillReleaseRecoveryPreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    record_sha256: Checksum


class OwnedParameterSkillReleaseRecoverRequest(OwnedParameterSkillReleaseRecoveryPreviewRequest):
    confirm_recovery_sha256: Checksum
    human_confirmation: Literal['RESTORE_ANCHORED_RELEASE_RECORD']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillSelectionPreviewRequest(OwnedParameterSkillReleaseReadRequest):
    expected_selection_sha256: Checksum | None


class OwnedParameterSkillSelectRequest(OwnedParameterSkillSelectionPreviewRequest):
    confirm_selection_sha256: Checksum
    human_confirmation: Literal['SELECT_MANUAL_SKILL']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)


class OwnedParameterSkillRollbackRequest(OwnedParameterSkillSelectionPreviewRequest):
    confirm_selection_sha256: Checksum
    human_confirmation: Literal['ROLLBACK_MANUAL_SKILL']
    lease_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    generation: int = Field(ge=0, strict=True)
