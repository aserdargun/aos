import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel
from .scientist_lab_context import ScientistFieldIntent
from .scientist_protocol import _reject_constant, _unique_object


Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
RunId = Annotated[str, Field(pattern=r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')]
Reason = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[a-z0-9_-]+$')]
EXPERIENCE_BOUND = 512 * 1024


class ScientistExperienceEligibility(TypedModel):
    eligible: bool
    reasons: list[Reason] = Field(max_length=32)


class ScientistExperienceProvenance(TypedModel):
    source_count: int = Field(ge=0, le=1000)
    manifest_sha256s: list[Hash] = Field(max_length=1000)
    usage_profile: Literal['noncommercial_research']


class ScientistExperienceRecord(TypedModel):
    record_id: str = Field(min_length=1, max_length=128)
    experiment_id: str | None = Field(default=None, pattern=r'^exp_[a-f0-9]{32}$')
    sequence: int = Field(ge=0, le=1000)
    kind: Literal['baseline', 'proposal', 'cpu-mode-stream']
    status: str = Field(min_length=1, max_length=64)
    method: str | None = Field(max_length=128)
    move: str | None = Field(max_length=64)
    decision: Literal['KEEP', 'KEEP_SIMPLER', 'DISCARD', 'REJECT'] | None
    reason: Reason | None
    score: float | None = Field(ge=-1, le=3)
    score_kind: Literal['dev-suite', 'not-scored']
    model_id: str | None = Field(max_length=256)
    model_receipt_status: Literal['not-applicable-cpu', 'fixture-or-unverified', 'absent', 'invalid',
                                  'identity-mismatch', 'reported-local-identity-matched']
    experiment_sha256: Hash | None = None
    trajectory_sha256: Hash | None = None
    provenance_summary: ScientistExperienceProvenance
    training_eligibility: ScientistExperienceEligibility
    history_eligibility: ScientistExperienceEligibility

    @model_validator(mode='after')
    def evidence_bounds(self):
        if self.training_eligibility.eligible:
            raise ValueError('Experience readback cannot grant training eligibility')
        if self.history_eligibility.eligible:
            if (self.history_eligibility.reasons or self.kind != 'proposal' or self.status != 'scored'
                    or self.decision not in {'KEEP', 'KEEP_SIMPLER', 'DISCARD'} or self.score is None
                    or not self.experiment_id or not self.experiment_sha256 or not self.trajectory_sha256):
                raise ValueError('History reference lacks measured proposal identities')
        elif not self.history_eligibility.reasons:
            raise ValueError('Ineligible history requires explicit reasons')
        if self.kind != 'cpu-mode-stream' and any(value is None for value in
                (self.experiment_id, self.experiment_sha256, self.trajectory_sha256)):
            raise ValueError('Ledger record lacks immutable experiment and trajectory references')
        return self


class ScientistExperienceFeedback(TypedModel):
    same_run_recent_limit: Literal[30]
    cross_run_reuse: Literal[False]

    @field_validator('cross_run_reuse', mode='before')
    @classmethod
    def no_reuse_authority(cls, value):
        if value is not False:
            raise ValueError('Experience view gives no cross-run reuse authority')
        return value


class ScientistContextUsage(TypedModel):
    status: Literal['admitted-only', 'context-bound']
    context_bound_proposal_count: int = Field(ge=0, le=200)

    @model_validator(mode='after')
    def explicit_usage(self):
        if (self.status == 'context-bound') != (self.context_bound_proposal_count > 0):
            raise ValueError('Context usage label and observed count disagree')
        return self


class ScientistFieldContextUsage(ScientistContextUsage):
    field_context_sha256: Hash
    snapshot_sha256: Hash
    intent: ScientistFieldIntent
    asset_identity: Literal['user_supplied']
    use: Literal['advisory-only']


class ScientistPriorFindingsUsage(ScientistContextUsage):
    snapshot_sha256: Hash
    source_run_id: RunId
    source_report_sha256: Hash
    record_count: int = Field(ge=1, le=8)
    scope: Literal['historical-advisory-only']


class ScientistExperience(TypedModel):
    schema_version: Literal['run-experience.v1'] = Field(alias='schema')
    run_id: RunId
    report_sha256: Hash
    purpose: Literal['mode-grid', 'mode-stream', 'baseline', 'research']
    verification: Literal['report-and-ledger-hash-verified']
    record_limit: Literal[200]
    records: list[ScientistExperienceRecord] = Field(max_length=200)
    feedback: ScientistExperienceFeedback
    training_started: Literal[False]
    holdout_included: Literal[False]
    protected_records_excluded: bool
    field_context_usage: ScientistFieldContextUsage | None = None
    prior_findings_usage: ScientistPriorFindingsUsage | None = None

    @field_validator('training_started', 'holdout_included', mode='before')
    @classmethod
    def no_effect_claims(cls, value):
        if value is not False:
            raise ValueError('Experience readback cannot include training or holdout authority')
        return value

    @model_validator(mode='after')
    def unique_records(self):
        identifiers = [record.record_id for record in self.records]
        experiments = [record.experiment_id for record in self.records if record.experiment_id is not None]
        if len(set(identifiers)) != len(identifiers) or len(set(experiments)) != len(experiments):
            raise ValueError('Experience contains duplicate record identities')
        return self


class ScientistExperienceReadback(TypedModel):
    schema_version: Literal['aos.scientist-experience-readback.v1'] = 'aos.scientist-experience-readback.v1'
    run_id: RunId
    report_sha256: Hash
    experience: ScientistExperience
    independent_report_verified: Literal[True] = True
    ledger_verification: Literal['scientist-reported-not-independently-replayed'] = 'scientist-reported-not-independently-replayed'
    execution_authorized: Literal[False] = False


def verify_scientist_experience(raw, *, report, request):
    if type(raw) is not bytes or len(raw) > EXPERIENCE_BOUND:
        raise ValueError('Scientist experience response exceeds its bound')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    experience = ScientistExperience.model_validate(value, strict=True)
    purpose = 'mode-grid' if report.report.get('provider') == 'mode-grid' else report.report.get('purpose', 'research')
    if purpose not in {'mode-grid', 'mode-stream', 'baseline', 'research'}:
        purpose = 'research'
    if (experience.run_id != report.run_id or experience.report_sha256 != report.report_sha256
            or experience.purpose != purpose or purpose == 'baseline'):
        raise ValueError('Experience differs from independently verified research report')
    field = experience.field_context_usage
    if (field is None) != (request.field_intent is None) or field is not None and field.intent != request.field_intent:
        raise ValueError('Field context usage differs from original approved intent')
    if field is not None:
        context = {'schema': 'field-study-context.v1', 'intent': field.intent.model_dump(mode='json'),
            'snapshot_sha256': field.snapshot_sha256, 'asset_identity': field.asset_identity, 'use': field.use}
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if hashlib.sha256(encoded).hexdigest() != field.field_context_sha256:
            raise ValueError('Field context content hash differs')
    prior = experience.prior_findings_usage
    selection = request.prior_experience
    if (prior is None) != (selection is None) or prior is not None and (
            prior.source_run_id != selection.source_run_id or prior.source_report_sha256 != selection.source_report_sha256
            or prior.record_count != len(selection.records)):
        raise ValueError('Prior findings usage differs from original approved references')
    return ScientistExperienceReadback(run_id=experience.run_id, report_sha256=experience.report_sha256,
                                      experience=experience)
