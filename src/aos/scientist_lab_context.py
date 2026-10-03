from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel


class ScientistFieldIntent(TypedModel):
    asset_id: str = Field(min_length=1, max_length=128, pattern=r'^[^\x00-\x1f\x7f]*$')
    goal_kind: Literal['digital_twin', 'predictive_maintenance']
    objective: str = Field(min_length=1, max_length=600, pattern=r'^[^\x00-\x1f\x7f]*$')

    @field_validator('asset_id', 'objective', mode='before')
    @classmethod
    def plain_text(cls, value):
        if not isinstance(value, str) or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError('Field intent requires plain text without control characters')
        value = value.strip()
        if not value:
            raise ValueError('Field intent must be nonempty plain text')
        return value


class ScientistPriorRecordRef(TypedModel):
    experiment_id: str = Field(pattern=r'^exp_[a-f0-9]{32}$')
    experiment_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    trajectory_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class ScientistPriorExperienceSelection(TypedModel):
    source_run_id: str = Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
    source_report_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    records: list[ScientistPriorRecordRef] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def unique_records(self):
        if len({record.experiment_id for record in self.records}) != len(self.records):
            raise ValueError('Duplicate prior experiment selection')
        return self
