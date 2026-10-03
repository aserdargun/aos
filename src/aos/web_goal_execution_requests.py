from typing import Literal

from pydantic import Field, field_validator

from .contracts import TypedModel
from .web_application import Checksum


class ParameterWebGoalPreviewRequest(TypedModel):
    schema_version: Literal['1.0']
    lease_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class ParameterWebGoalStartRequest(ParameterWebGoalPreviewRequest):
    confirm_sha256: Checksum
    human_confirmation: Literal[True]

    @field_validator('human_confirmation', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('parameter_web_goal_confirmation_required')
        return value


class ParameterWebGoalReportRequest(TypedModel):
    schema_version: Literal['1.0']
    intent_sha256: Checksum
