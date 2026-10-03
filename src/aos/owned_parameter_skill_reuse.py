"""Read-only compilation of new parameters for selected manual development skills."""

from pathlib import Path
from typing import Literal
import hashlib

from pydantic import Field, field_validator, model_validator

from .contracts import TypedModel, canonical, digest
from .knowledge import Hash, KnowledgeScope
from .owned_form_fixture import owned_record_form_bodies
from .owned_parameter_project import owned_parameter_project_review, read_owned_parameter_project
from .owned_parameter_skill_candidate import _hash
from .owned_parameter_skill_release import (
    OwnedParameterSkillRelease, OwnedParameterSkillReleaseSession,
    OwnedParameterSkillSelection, _history,
)
from .owned_parameter_skill_review import _ReviewFlags
from .site_skill_case_binding import (
    SiteSkillCaseInputs, SiteSkillFormFieldBinding, parameter_variant_sha256,
)
from .site_skill_form_recipe import (
    SiteSkillFormRecipeInvocation, compile_site_skill_form_recipe_invocation,
    revalidate_site_skill_form_recipe_invocation,
)
from .site_skill_form_recipe_audit import audit_site_skill_form_recipe_execution
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Key
from .web_goal_execution_binding import fields_for_parameters
from .web_goal_execution_journal import MAX_RECORD_BYTES
from .web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
from .web_https_form_transport import WebHTTPSFormPlan, plan_web_https_form


MODE = 'owned_parameter_skill_manual_reuse'


def _typed(model, value):
    if isinstance(value, TypedModel):
        value = value.model_dump(mode='json')
    return model.model_validate_json(canonical(value))


class OwnedParameterSkillReuseSource(TypedModel):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_manual_reuse_source']
    release: OwnedParameterSkillRelease
    release_sha256: Hash
    selection: OwnedParameterSkillSelection
    selection_sha256: Hash
    candidate_sha256: Hash
    review_sha256: Hash
    family_sha256: Hash
    scope: KnowledgeScope
    manifest_sha256: Hash
    source_fingerprint_sha256: Hash
    source_run_ref: Hash
    source_invocation_sha256: Hash
    source_parameter_variant_sha256: Hash
    profile_sha256: Hash
    task_sha256: Hash
    skill_sha256: Hash
    recipe_sha256: Hash
    field_binding_sha256: Hash
    certificate_sha256: Hash
    entry_sha256: Hash
    origin: str = Field(pattern=r'^https://w3-owned-form\.aos\.invalid:[1-9][0-9]{0,4}$')

    @model_validator(mode='after')
    def exact_selected_source(self):
        release = self.release.model_dump(mode='json')
        selection = self.selection.model_dump(mode='json')
        if (digest(release) != self.release_sha256 or digest(selection) != self.selection_sha256
                or selection['release_sha256'] != self.release_sha256
                or any(getattr(self, key) != release[key] or release[key] != selection[key]
                       for key in ('candidate_sha256', 'review_sha256', 'family_sha256',
                                   'source_fingerprint_sha256'))
                or self.scope != self.release.scope or self.scope != self.selection.scope
                or self.manifest_sha256 != self.release.manifest_sha256
                or self.source_run_ref != self.release.source_run_ref
                or self.recipe_sha256 != self.release.recipe_sha256
                or self.field_binding_sha256 != self.release.family.field_binding_sha256
                or any(release[key] != selection[key] for key in (
                    'database_identity', 'release_directory_sha256', 'release_parent_identity'))):
            raise ValueError('owned_parameter_reuse_source_binding_changed')
        return self


class OwnedParameterSkillReuseField(TypedModel):
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=128)


class OwnedParameterSkillReuseRecordConfig(TypedModel):
    scope: KnowledgeScope
    fields: list[OwnedParameterSkillReuseField] = Field(min_length=2, max_length=8)
    outcome_field: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    marker_id: Literal['outcome']

    @model_validator(mode='after')
    def exact_record(self):
        owned_record_form_bodies(self.model_dump(mode='json'))
        return self


class OwnedParameterSkillReuseAdmission(_ReviewFlags):
    schema_version: Literal['1.0']
    kind: Literal['owned_parameter_skill_manual_reuse_admission']
    status: Literal['compiled_manual_reuse_preview']
    reviewed: Literal[False]
    execution_performed: Literal[False]
    source: OwnedParameterSkillReuseSource
    parameters: dict[Key, str] = Field(min_length=2, max_length=8)
    parameter_variant_sha256: Hash
    case_key: Key
    field_bindings: list[SiteSkillFormFieldBinding] = Field(min_length=2, max_length=8)
    skill_plan: SiteSkillValidationPlan
    skill_plan_sha256: Hash
    case_inputs: SiteSkillCaseInputs
    case_inputs_sha256: Hash
    form_plan: WebHTTPSFormPlan
    form_plan_sha256: Hash
    state_plan: WebHTTPSFormStatePlan
    state_plan_sha256: Hash
    record_config: OwnedParameterSkillReuseRecordConfig
    record_config_sha256: Hash
    expected_record_sha256: Hash
    form_body_sha256: Hash
    invocation: SiteSkillFormRecipeInvocation
    invocation_sha256: Hash

    @field_validator('reviewed', 'execution_performed', mode='before')
    @classmethod
    def exact_reuse_false(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_reuse_cannot_claim_authority')
        return value

    @model_validator(mode='after')
    def exact_compile(self):
        parameters = owned_parameter_project_review(self.source.scope.application_id, self.parameters)['parameters']
        variant = parameter_variant_sha256(parameters)
        fields = fields_for_parameters(parameters, self.field_bindings)
        config = self.record_config.model_dump(mode='json')
        bodies = owned_record_form_bodies(config)
        pins = {'skill_plan_sha256': self.skill_plan, 'case_inputs_sha256': self.case_inputs,
                'form_plan_sha256': self.form_plan, 'state_plan_sha256': self.state_plan,
                'record_config_sha256': self.record_config, 'invocation_sha256': self.invocation}
        selected_inputs = [case for case in self.case_inputs.cases if case.case_key == self.case_key]
        selected_plan = [case for case in self.skill_plan.cases if case.case_key == self.case_key]
        if (any(getattr(self, key) != digest(value.model_dump(mode='json')) for key, value in pins.items())
                or self.parameter_variant_sha256 != variant
                or variant == self.source.source_parameter_variant_sha256
                or self.case_key != 'reuse-' + variant[:16]
                or self.record_config.scope != self.source.scope
                or [(field.name, field.value) for field in self.record_config.fields] != list(fields)
                or self.expected_record_sha256 != digest(dict(fields))
                or self.form_body_sha256 != hashlib.sha256(bodies['form_body']).hexdigest()
                or self.form_plan.body_sha256 != self.form_body_sha256
                or self.form_plan.body_bytes != len(bodies['form_body'])
                or hashlib.sha256(bodies['entry']).hexdigest() != self.source.entry_sha256
                or hashlib.sha256(bodies['before']).hexdigest() != self.state_plan.expected_before_sha256
                or hashlib.sha256(bodies['after']).hexdigest() != self.state_plan.expected_after_sha256
                or form_state_marker_sha256(bodies['before'], 'outcome') != self.state_plan.expected_before_marker_sha256
                or form_state_marker_sha256(bodies['after'], 'outcome') != self.state_plan.expected_after_marker_sha256
                or self.record_config.outcome_field != self.state_plan.submitted_field_name
                or self.state_plan.marker_id != 'outcome'
                or self.case_inputs.plan_sha256 != self.skill_plan_sha256
                or len(selected_inputs) != 1 or selected_inputs[0].parameters != parameters
                or len(selected_plan) != 1 or selected_plan[0].cohort != 'development'
                or selected_plan[0].parameter_variant_sha256 != variant
                or digest([field.model_dump(mode='json') for field in self.field_bindings])
                   != self.source.field_binding_sha256
                or self.invocation.case_key != self.case_key
                or self.invocation_sha256 == self.source.source_invocation_sha256
                or any(getattr(self.invocation, key) != getattr(self, key) for key in (
                    'skill_plan_sha256', 'case_inputs_sha256', 'form_plan_sha256', 'state_plan_sha256'))
                or any(getattr(self.invocation, key) != getattr(self.source, key) for key in (
                    'profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256', 'field_binding_sha256'))
                or self.skill_plan.profile_sha256 != self.source.profile_sha256
                or self.skill_plan.skill_sha256 != self.source.skill_sha256
                or self.skill_plan.task_key != self.source.release.family.task_key
                or self.form_plan.profile_sha256 != self.source.profile_sha256
                or self.state_plan.profile_sha256 != self.source.profile_sha256
                or self.form_plan.task_sha256 != self.source.task_sha256
                or self.state_plan.task_sha256 != self.source.task_sha256
                or self.state_plan.form_plan_sha256 != self.form_plan_sha256
                or (self.form_plan.entry_url, self.form_plan.submit_url,
                    self.form_plan.receipt_url, self.state_plan.state_url)
                   != tuple(self.source.origin + suffix for suffix in ('/entry', '/submit', '/receipt', '/state'))):
            raise ValueError('owned_parameter_reuse_compile_binding_changed')
        return self


class OwnedParameterSkillReuseSession:
    def __init__(self, release_session):
        if not isinstance(release_session, OwnedParameterSkillReleaseSession):
            raise ValueError('owned_parameter_reuse_release_session_required')
        self.release_session = release_session

    def _selected_source(self, release_sha256, selection_sha256):
        release_sha256, selection_sha256 = _hash(release_sha256), _hash(selection_sha256)
        releases = self.release_session
        with releases.store._locked() as (descriptor, workspace, identity):
            with releases.review_session.store._locked():
                records = releases.store._inventory(descriptor, workspace, identity)
                release = records.get(release_sha256 + '.release.json')
                if release is None:
                    raise ValueError('owned_parameter_reuse_release_missing')
                selections = _history(records)[release['family_sha256']]['selections']
                if (not selections or digest(selections[-1]) != selection_sha256
                        or selections[-1]['release_sha256'] != release_sha256):
                    raise ValueError('owned_parameter_reuse_selection_changed')
                releases._current(release)
                candidates = releases.review_session.candidate_session
                original = read_owned_parameter_project(candidates.directory, candidates.manifest_sha256)
                source = _typed(OwnedParameterSkillReuseSource, {
                    'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_reuse_source',
                    'release': release, 'release_sha256': release_sha256,
                    'selection': selections[-1], 'selection_sha256': selection_sha256,
                    **{key: release[key] for key in ('candidate_sha256', 'review_sha256', 'family_sha256',
                        'scope', 'manifest_sha256', 'source_fingerprint_sha256', 'source_run_ref', 'recipe_sha256')},
                    'source_invocation_sha256': original['invocation_sha256'],
                    'source_parameter_variant_sha256': parameter_variant_sha256(original['parameters']),
                    **{key: original[key] for key in ('profile_sha256', 'task_sha256', 'skill_sha256',
                                                     'certificate_sha256')},
                    'field_binding_sha256': original['invocation']['field_binding_sha256'],
                    'entry_sha256': hashlib.sha256(original['bodies']['entry']).hexdigest(),
                    'origin': original['manifest']['origin'],
                }).model_dump(mode='json')
        return source, original

    def _compile(self, release_sha256, selection_sha256, parameters):
        source, original = self._selected_source(release_sha256, selection_sha256)
        parameters = owned_parameter_project_review(source['scope']['application_id'], parameters)['parameters']
        if parameters == original['parameters']:
            raise ValueError('owned_parameter_reuse_parameters_unchanged')
        variant = parameter_variant_sha256(parameters)
        case_key = 'reuse-' + variant[:16]
        plan_data = original['skill_plan'].model_dump(mode='json')
        cases = original['case_inputs'].model_dump(mode='json')['cases']
        original_case = original['case_key']
        plan_data['cases'] = sorted([
            case if case['case_key'] != original_case else case | {
                'case_key': case_key, 'parameter_variant_sha256': variant}
            for case in plan_data['cases']], key=lambda case: case['case_key'])
        plan = _typed(SiteSkillValidationPlan, plan_data)
        inputs = _typed(SiteSkillCaseInputs, {
            'schema_version': '1.0', 'synthetic': True, 'plan_sha256': digest(plan.model_dump(mode='json')),
            'cases': sorted([case if case['case_key'] != original_case else {
                'case_key': case_key, 'parameters': parameters} for case in cases],
                key=lambda case: case['case_key']),
        })
        fields = fields_for_parameters(parameters, original['field_bindings'])
        config = original['record_config'] | {'fields': [field | {'value': dict(fields)[field['name']]}
                                                       for field in original['record_config']['fields']]}
        bodies = owned_record_form_bodies(config)
        form_plan = plan_web_https_form(
            original['profiles'], original['task'], submit_url=original['form_plan'].submit_url,
            receipt_url=original['form_plan'].receipt_url,
            body_sha256=hashlib.sha256(bodies['form_body']).hexdigest(), body_bytes=len(bodies['form_body']))
        state_plan = _typed(WebHTTPSFormStatePlan, original['state_plan'].model_dump(mode='json') | {
            'form_plan_sha256': digest(form_plan.model_dump(mode='json')),
            'expected_before_sha256': hashlib.sha256(bodies['before']).hexdigest(),
            'expected_after_sha256': hashlib.sha256(bodies['after']).hexdigest(),
            'expected_before_marker_sha256': form_state_marker_sha256(bodies['before'], config['marker_id']),
            'expected_after_marker_sha256': form_state_marker_sha256(bodies['after'], config['marker_id']),
        })
        invocation = compile_site_skill_form_recipe_invocation(
            original['skill_store'], plan, inputs, case_key, original['profiles'], original['task'],
            form_plan, state_plan, original['field_bindings'], original['recipe'])
        admission = _typed(OwnedParameterSkillReuseAdmission, {
            'schema_version': '1.0', 'kind': 'owned_parameter_skill_manual_reuse_admission',
            'status': 'compiled_manual_reuse_preview', 'synthetic': True, 'development_only': True,
            'reviewed': False, 'execution_performed': False, 'source': source,
            'parameters': parameters, 'parameter_variant_sha256': variant, 'case_key': case_key,
            'field_bindings': [binding.model_dump(mode='json') for binding in original['field_bindings']],
            'skill_plan': plan.model_dump(mode='json'), 'skill_plan_sha256': digest(plan.model_dump(mode='json')),
            'case_inputs': inputs.model_dump(mode='json'), 'case_inputs_sha256': digest(inputs.model_dump(mode='json')),
            'form_plan': form_plan.model_dump(mode='json'), 'form_plan_sha256': digest(form_plan.model_dump(mode='json')),
            'state_plan': state_plan.model_dump(mode='json'), 'state_plan_sha256': digest(state_plan.model_dump(mode='json')),
            'record_config': config, 'record_config_sha256': digest(config),
            'expected_record_sha256': digest(dict(fields)),
            'form_body_sha256': hashlib.sha256(bodies['form_body']).hexdigest(),
            'invocation': invocation, 'invocation_sha256': digest(invocation),
        }).model_dump(mode='json')
        if len(canonical(admission).encode()) > MAX_RECORD_BYTES:
            raise ValueError('owned_parameter_reuse_admission_too_large')
        current, _original = self._selected_source(release_sha256, selection_sha256)
        if current != source:
            raise ValueError('owned_parameter_reuse_source_changed')
        bundle = {key: original[key] for key in (
            'directory', 'manifest', 'manifest_sha256', 'source_pins', 'profiles_root', 'profiles',
            'task', 'skill_store', 'recipe', 'field_bindings', 'profile_sha256', 'task_sha256',
            'skill_sha256', 'recipe_sha256', 'certificate_file', 'key_file', 'certificate_sha256')}
        bundle.update(mode=MODE, admission=admission, admission_sha256=digest(admission),
            reuse_source=source, skill_plan=plan, case_inputs=inputs, case_key=case_key,
            parameters=parameters, field_binding_sha256=source['field_binding_sha256'],
            skill_plan_sha256=admission['skill_plan_sha256'], case_inputs_sha256=admission['case_inputs_sha256'],
            form_plan=form_plan, form_plan_sha256=admission['form_plan_sha256'],
            state_plan=state_plan, state_plan_sha256=admission['state_plan_sha256'],
            record_config=config, fields=fields, body=bodies['form_body'], bodies=bodies,
            invocation=invocation, invocation_sha256=admission['invocation_sha256'])
        return admission, bundle

    def preview(self, release_sha256, selection_sha256, parameters):
        admission, _bundle = self._compile(release_sha256, selection_sha256, parameters)
        return admission, digest(admission)

    def bundle(self, admission):
        expected = _typed(OwnedParameterSkillReuseAdmission, admission).model_dump(mode='json')
        source = expected['source']
        current, bundle = self._compile(source['release_sha256'], source['selection_sha256'], expected['parameters'])
        if current != expected:
            raise ValueError('owned_parameter_reuse_admission_changed')
        return bundle

    def current_source(self, admission):
        expected = _typed(OwnedParameterSkillReuseAdmission, admission).model_dump(mode='json')
        source = expected['source']
        current, _original = self._selected_source(source['release_sha256'], source['selection_sha256'])
        if current != source:
            raise ValueError('owned_parameter_reuse_source_changed')
        return current

    def revalidate(self, admission):
        bundle = self.bundle(admission)
        return revalidate_site_skill_form_recipe_invocation(
            bundle['invocation'], bundle['skill_store'], bundle['skill_plan'], bundle['case_inputs'],
            bundle['case_key'], bundle['profiles'], bundle['task'], bundle['form_plan'],
            bundle['state_plan'], bundle['field_bindings'], bundle['recipe'])

    def audit(self, admission, database, run_id):
        expected_database = self.release_session.review_session.candidate_session.database
        if Path(database).absolute() != expected_database:
            raise ValueError('owned_parameter_reuse_original_database_required')
        bundle = self.bundle(admission)
        return audit_site_skill_form_recipe_execution(
            bundle['skill_store'], bundle['skill_plan'], bundle['case_inputs'], bundle['case_key'],
            bundle['profiles'], bundle['task'], bundle['form_plan'], bundle['state_plan'],
            bundle['field_bindings'], bundle['recipe'], bundle['invocation'], database, run_id)
