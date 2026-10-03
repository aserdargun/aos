"""Host-authored private sources for bounded synthetic multi-field form projects."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Literal

from pydantic import Field, field_validator

from .contracts import TypedModel, canonical, digest, now
from .lifecycle import private_directory
from .owned_form_fixture import HOST, owned_record_form_bodies
from .owned_form_invocation_session import _write_private
from .site_knowledge import SiteKnowledgeStore, SitePageDraft
from .site_skill import SiteSkillDraft, SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                     parameter_variant_sha256)
from .site_skill_form_recipe import (SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe,
                                     compile_site_skill_form_recipe_invocation)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import Checksum, WebApplicationProfile, WebApplicationProfiles
from .web_application_binding import WebTaskContract
from .web_goal_execution_binding import fields_for_parameters
from .web_https_form_state_probe import WebHTTPSFormStatePlan, form_state_marker_sha256
from .web_https_form_transport import WebHTTPSFormPlan, plan_web_https_form


APPLICATIONS = ('synthetic-crm-note', 'synthetic-inventory-note')
MANIFEST_NAME = 'parameter-project-manifest.json'
MODE = 'owned_synthetic_parameter_project'
_SOURCES = {
    'remote-entry-task.json', 'remote-form-plan.json', 'remote-form-state-plan.json',
    'remote-form-skill-plan.json', 'remote-form-skill-case-inputs.json',
    'remote-form-skill-field-bindings.json', 'remote-form-skill-recipe.json',
    'site-page-draft.json', 'site-skill-draft.json', 'parameter-review.json',
    'synthetic-authoring-source.json', 'owned-record-config.json',
    'owned-form-certificate.pem', 'owned-form-key.pem',
}


class OwnedParameterProjectIdentity(TypedModel):
    device: int = Field(ge=0, strict=True)
    inode: int = Field(ge=1, strict=True)


class OwnedParameterProjectManifest(TypedModel):
    schema_version: Literal['1.0']
    synthetic: Literal[True]
    mode: Literal['owned_synthetic_parameter_project']
    application_key: Literal['synthetic-crm-note', 'synthetic-inventory-note']
    port: int = Field(ge=1, le=65535, strict=True)
    origin: str = Field(pattern=r'^https://w3-owned-form\.aos\.invalid:[1-9][0-9]{0,4}$')
    directory_identity: OwnedParameterProjectIdentity
    profile_sha256: Checksum
    skill_sha256: Checksum
    page_sha256: Checksum
    task_sha256: Checksum
    form_plan_sha256: Checksum
    state_plan_sha256: Checksum
    recipe_sha256: Checksum
    invocation_sha256: Checksum
    parameters_review_sha256: Checksum
    certificate_sha256: Checksum
    case_key: Literal['dev-query']
    sources: dict[str, Checksum]
    source_identities: dict[str, OwnedParameterProjectIdentity]
    execution_authorized: Literal[False]
    execution_performed: Literal[False]
    source_evidence_verified: Literal[False]
    skill_reviewed: Literal[False]
    site_outcome_verified: Literal[False]
    activation_authorized: Literal[False]
    training_ready: Literal[False]

    @field_validator('execution_authorized', 'execution_performed',
                     'source_evidence_verified', 'skill_reviewed', 'site_outcome_verified',
                     'activation_authorized', 'training_ready', mode='before')
    @classmethod
    def no_execution_claims(cls, value):
        if value is not False:
            raise ValueError('owned_parameter_project_cannot_grant_authority')
        return value


def owned_parameter_project_review(application_key: str, parameters: dict[str, str]) -> dict:
    """Validate and preview host-authored synthetic inputs without creating any files."""
    if (type(application_key) is not str or application_key not in APPLICATIONS
            or type(parameters) is not dict or set(parameters) != {'record-id', 'note-text'}
            or any(type(value) is not str or not 1 <= len(value) <= 128
                   or len(value.encode('utf-8')) > 256 or not value.isprintable()
                   for value in parameters.values())):
        raise ValueError('owned_parameter_project_parameters_invalid')
    return {
        'schema_version': '1.0', 'synthetic': True, 'application_key': application_key,
        'scope': {'application_id': application_key,
                  'tenant_id': 'synthetic-tenant', 'account_role': 'editor'},
        'parameters': dict(sorted(parameters.items())),
        'parameter_bounds': {key: {'min_chars': 1, 'max_chars': 128, 'max_utf8_bytes': 256}
                             for key in sorted(parameters)},
        'execution_authorized': False, 'training_ready': False,
    }


def owned_parameter_project_review_sha256(application_key: str,
                                          parameters: dict[str, str]) -> str:
    return digest(owned_parameter_project_review(application_key, parameters))


def _bytes(value) -> bytes:
    return canonical(value.model_dump(mode='json')
                     if hasattr(value, 'model_dump') else value).encode('utf-8')


def _identity(path: Path) -> dict:
    metadata = path.stat(follow_symlinks=False)
    return {'device': metadata.st_dev, 'inode': metadata.st_ino}


def _origin(port: int) -> str:
    if type(port) is not int or not 1 <= port <= 65535 or port == 443:
        raise ValueError('owned_parameter_project_port_invalid')
    return f'https://{HOST}:{port}'


def provision_owned_parameter_project(
        directory: Path, port: int, *, application_key: str, parameters: dict[str, str],
        confirm_parameters_sha256: str, human_confirmation: bool) -> dict:
    """Persist a reviewed source bundle; this creates no execution or release admission."""
    review = owned_parameter_project_review(application_key, parameters)
    origin = _origin(port)
    if human_confirmation is not True or confirm_parameters_sha256 != digest(review):
        raise ValueError('owned_parameter_project_review_required')
    root = Path(directory).absolute()
    parent = private_directory(root.parent)
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)
    profiles = WebApplicationProfiles(root / 'profiles')
    profile = WebApplicationProfile.model_validate({
        'schema_version': '1.0', 'application_key': application_key, 'revision': 1,
        'previous_sha256': None, 'environment': 'staging',
        'tenant_key': review['scope']['tenant_id'], 'account_role': review['scope']['account_role'],
        'entry_url': origin + '/entry', 'allowed_origins': [origin],
        'task_keys': ['annotate-record'],
        'learning': {'system1': 'requested', 'system2': 'disabled',
                     'data_rights_ref': 'synthetic-authoring-only', 'retention_days': 1,
                     'raw_screenshots': False, 'automatic_training': False,
                     'automatic_promotion': False}})
    profile_sha256 = digest(profile.model_dump(mode='json'))
    profiles.register(profile, confirm_sha256=profile_sha256)
    task = WebTaskContract(
        profile_sha256=profile_sha256, task_key='annotate-record',
        entry_url=profile.entry_url, allowed_origins=profile.allowed_origins,
        tools=['browser.click', 'browser.fill', 'browser.navigate',
               'browser.snapshot', 'browser.verify'],
        max_pages=4, max_navigations=6, max_actions=8, max_seconds=300,
        verification_ref='synthetic-whole-record')
    identity_field, identity_label = (
        ('contact_name', 'Contact name') if application_key == APPLICATIONS[0]
        else ('item_code', 'Item code'))
    bindings = [SiteSkillFormFieldBinding(parameter_key='record-id', form_field_name=identity_field),
                SiteSkillFormFieldBinding(parameter_key='note-text', form_field_name='note')]
    fields = fields_for_parameters(review['parameters'], bindings)
    labels = {identity_field: identity_label, 'note': 'Note'}
    config = {'scope': review['scope'],
              'fields': [{'name': name, 'label': labels[name], 'value': value}
                         for name, value in fields],
              'outcome_field': 'note', 'marker_id': 'outcome'}
    bodies = owned_record_form_bodies(config)
    form_plan = plan_web_https_form(
        profiles, task, submit_url=origin + '/submit', receipt_url=origin + '/receipt',
        body_sha256=hashlib.sha256(bodies['form_body']).hexdigest(),
        body_bytes=len(bodies['form_body']))
    state_plan = WebHTTPSFormStatePlan(
        profile_sha256=profile_sha256, task_sha256=digest(task.model_dump(mode='json')),
        form_plan_sha256=digest(form_plan.model_dump(mode='json')), state_url=origin + '/state',
        expected_before_sha256=hashlib.sha256(bodies['before']).hexdigest(),
        expected_after_sha256=hashlib.sha256(bodies['after']).hexdigest(), marker_id='outcome',
        expected_before_marker_sha256=form_state_marker_sha256(bodies['before'], 'outcome'),
        expected_after_marker_sha256=form_state_marker_sha256(bodies['after'], 'outcome'),
        submitted_field_name='note')
    pages = SiteKnowledgeStore(root / 'site-knowledge', profiles)
    page = SitePageDraft(
        schema_version='1.0', profile_sha256=profile_sha256, application_key=application_key,
        tenant_key=profile.tenant_key, account_role=profile.account_role,
        page_key='record-form', revision=1, previous_sha256=None, origin=origin,
        route_template='/entry', page_fingerprint_sha256=hashlib.sha256(bodies['entry']).hexdigest(),
        recorded_at=now()[:19] + 'Z', landmark_keys=['record-form'], outgoing_page_keys=[],
        source_kind='manual_draft', status='draft', execution_authorized=False,
        collection_authorized=False, training_ready=False)
    page_sha256 = digest(page.model_dump(mode='json'))
    pages.register(page, confirm_sha256=page_sha256)
    authored = {'schema_version': '1.0', 'synthetic': True,
                'kind': 'synthetic_manual_authoring', 'parameters_review_sha256': digest(review),
                'profile_sha256': profile_sha256, 'page_sha256': page_sha256,
                'execution_performed': False, 'outcomes_verified': False,
                'training_ready': False}
    steps = [{'step_key': operation.replace('_', '-'), 'operation': operation}
             for operation in SUPPORTED_RECIPE_ORDERS[0]]
    skill = SiteSkillDraft(
        schema_version='1.0', profile_sha256=profile_sha256, application_key=application_key,
        tenant_key=profile.tenant_key, account_role=profile.account_role,
        task_key=task.task_key, page_key=page.page_key, page_draft_sha256=page_sha256,
        skill_key='annotate-record', model_role='system1', candidate_kind='finite_action_choice',
        revision=1, previous_sha256=None, parameter_keys=sorted(parameters),
        precondition_keys=['baseline-known', 'form-available'],
        step_keys=sorted(step['step_key'] for step in steps), expected_outcome_key='record-note-saved',
        source_event_ids=['learning-' + digest(authored)], source_verification_ids=[],
        source_kind='manual_candidate', status='draft', execution_authorized=False,
        collection_authorized=False, training_ready=False, activation_authorized=False)
    skills = SiteSkillStore(root / 'site-skills', profiles, pages)
    skill_sha256 = digest(skill.model_dump(mode='json'))
    skills.register(skill, confirm_sha256=skill_sha256)
    maps = [review['parameters'],
            {'record-id': 'Synthetic held-out A', 'note-text': 'Synthetic held-out note A'},
            {'record-id': 'Synthetic held-out B', 'note-text': 'Synthetic held-out note B'}]
    if parameter_variant_sha256(maps[0]) in {parameter_variant_sha256(item) for item in maps[1:]}:
        maps[1]['note-text'] += ' alternate'
        maps[2]['note-text'] += ' alternate'
    case_keys = ['dev-query', 'heldout-alpha', 'heldout-beta']
    plan = SiteSkillValidationPlan.model_validate({
        'schema_version': '1.0', 'synthetic': True, 'skill_sha256': skill_sha256,
        'profile_sha256': profile_sha256, 'page_draft_sha256': page_sha256,
        'task_key': task.task_key, 'model_role': 'system1',
        'source_variant_sha256': [digest(authored)],
        'cases': [{'case_key': key, 'cohort': 'development' if index == 0 else 'held_out',
                   'parameter_keys': sorted(values),
                   'parameter_variant_sha256': parameter_variant_sha256(values),
                   'expected_outcome_key': skill.expected_outcome_key}
                  for index, (key, values) in enumerate(zip(case_keys, maps, strict=True))]})
    inputs = SiteSkillCaseInputs.model_validate({
        'schema_version': '1.0', 'synthetic': True, 'plan_sha256': digest(plan.model_dump(mode='json')),
        'cases': [{'case_key': key, 'parameters': values}
                  for key, values in zip(case_keys, maps, strict=True)]})
    recipe = SiteSkillFormRecipe.model_validate_json(canonical({
        'schema_version': '1.0', 'synthetic': True, 'skill_sha256': skill_sha256,
        'profile_sha256': profile_sha256, 'page_draft_sha256': page_sha256,
        'task_key': task.task_key, 'task_sha256': digest(task.model_dump(mode='json')),
        'model_role': 'system1', 'field_binding_sha256': digest([item.model_dump() for item in bindings]),
        'preconditions': [{'precondition_key': 'baseline-known', 'kind': 'declared_state_before',
                           'checked_at_operation': 'read_state_before'},
                          {'precondition_key': 'form-available', 'kind': 'entry_form_available',
                           'checked_at_operation': 'fill_form'}],
        'outcome': {'outcome_key': skill.expected_outcome_key,
                    'kind': 'submitted_field_state_transition', 'parameter_key': 'note-text',
                    'form_field_name': 'note', 'verified_at_operation': 'read_state_after'},
        'steps': steps, 'execution_authorized': False, 'collection_authorized': False,
        'reviewed': False, 'activation_authorized': False, 'training_ready': False}))
    invocation = compile_site_skill_form_recipe_invocation(
        skills, plan, inputs, 'dev-query', profiles, task, form_plan, state_plan, bindings, recipe)
    sources = {
        'remote-entry-task.json': task, 'remote-form-plan.json': form_plan,
        'remote-form-state-plan.json': state_plan, 'remote-form-skill-plan.json': plan,
        'remote-form-skill-case-inputs.json': inputs,
        'remote-form-skill-field-bindings.json': [item.model_dump(mode='json') for item in bindings],
        'remote-form-skill-recipe.json': recipe, 'site-page-draft.json': page,
        'site-skill-draft.json': skill, 'parameter-review.json': review,
        'synthetic-authoring-source.json': authored, 'owned-record-config.json': config}
    for name, value in sources.items():
        _write_private(root / name, _bytes(value))
    certificate, key = root / 'owned-form-certificate.pem', root / 'owned-form-key.pem'
    try:
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                        '-subj', '/CN=' + HOST, '-addext', 'subjectAltName=DNS:' + HOST,
                        '-keyout', str(key), '-out', str(certificate)],
                       check=True, capture_output=True, timeout=20, umask=0o077)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('owned_parameter_project_certificate_generation_failed') from exc
    certificate.chmod(0o600)
    key.chmod(0o600)
    names = sorted(_SOURCES | {
        f'profiles/{profile_sha256}.json', f'site-knowledge/{page_sha256}.json',
        f'site-skills/{skill_sha256}.json'})
    pins = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
    manifest = OwnedParameterProjectManifest(
        schema_version='1.0', synthetic=True, mode=MODE, application_key=application_key,
        port=port, origin=origin, directory_identity=_identity(root),
        profile_sha256=profile_sha256, skill_sha256=skill_sha256, page_sha256=page_sha256,
        task_sha256=digest(task.model_dump(mode='json')),
        form_plan_sha256=digest(form_plan.model_dump(mode='json')),
        state_plan_sha256=digest(state_plan.model_dump(mode='json')),
        recipe_sha256=digest(recipe.model_dump(mode='json')), invocation_sha256=digest(invocation),
        parameters_review_sha256=digest(review), certificate_sha256=pins[certificate.name],
        case_key='dev-query', sources=pins,
        source_identities={name: _identity(root / name) for name in names},
        execution_authorized=False, execution_performed=False, source_evidence_verified=False,
        skill_reviewed=False, site_outcome_verified=False, activation_authorized=False, training_ready=False)
    _write_private(root / MANIFEST_NAME, _bytes(manifest))
    return read_owned_parameter_project(root, digest(manifest.model_dump(mode='json')))


def _checked_sources(root: Path, expected_manifest_sha256: str):
    from .local_app import private_read

    descriptor = private_directory(root)
    try:
        identity = {'device': os.fstat(descriptor).st_dev, 'inode': os.fstat(descriptor).st_ino}
    finally:
        os.close(descriptor)
    content = private_read(root / MANIFEST_NAME)
    if hashlib.sha256(content).hexdigest() != expected_manifest_sha256:
        raise ValueError('owned_parameter_project_manifest_pin_changed')
    manifest = OwnedParameterProjectManifest.model_validate_json(content)
    expected_sources = _SOURCES | {
        f'profiles/{manifest.profile_sha256}.json',
        f'site-knowledge/{manifest.page_sha256}.json',
        f'site-skills/{manifest.skill_sha256}.json'}
    if (content != _bytes(manifest) or manifest.origin != _origin(manifest.port)
            or manifest.directory_identity.model_dump() != identity
            or set(manifest.sources) != expected_sources
            or set(manifest.source_identities) != expected_sources):
        raise ValueError('owned_parameter_project_manifest_invalid')
    values = {}
    for name, pin in manifest.sources.items():
        path = root / name
        if _identity(path) != manifest.source_identities[name].model_dump():
            raise ValueError('owned_parameter_project_source_identity_changed')
        payload = private_read(path)
        if hashlib.sha256(payload).hexdigest() != pin:
            raise ValueError('owned_parameter_project_source_pin_changed')
        values[name] = payload
    if manifest.certificate_sha256 != manifest.sources['owned-form-certificate.pem']:
        raise ValueError('owned_parameter_project_certificate_pin_changed')
    return manifest, values


def read_owned_parameter_project(directory: Path, manifest_sha256: str) -> dict:
    """Freshly verify private identities, hashes and canonical compiler sources."""
    root = Path(directory).absolute()
    manifest, content = _checked_sources(root, manifest_sha256)

    def model(name, kind):
        value = kind.model_validate_json(content[name])
        if _bytes(value) != content[name]:
            raise ValueError('owned_parameter_project_source_not_canonical')
        return value

    def value(name):
        result = json.loads(content[name])
        if _bytes(result) != content[name]:
            raise ValueError('owned_parameter_project_source_not_canonical')
        return result

    review = value('parameter-review.json')
    expected_review = owned_parameter_project_review(manifest.application_key, review['parameters'])
    if review != expected_review or digest(review) != manifest.parameters_review_sha256:
        raise ValueError('owned_parameter_project_review_changed')
    profiles = WebApplicationProfiles(root / 'profiles')
    profile = profiles.get(manifest.profile_sha256)
    pages = SiteKnowledgeStore(root / 'site-knowledge', profiles)
    skills = SiteSkillStore(root / 'site-skills', profiles, pages)
    page = model('site-page-draft.json', SitePageDraft)
    skill = model('site-skill-draft.json', SiteSkillDraft)
    task = model('remote-entry-task.json', WebTaskContract)
    plan = model('remote-form-skill-plan.json', SiteSkillValidationPlan)
    inputs = model('remote-form-skill-case-inputs.json', SiteSkillCaseInputs)
    bindings = [SiteSkillFormFieldBinding.model_validate(item)
                for item in value('remote-form-skill-field-bindings.json')]
    recipe = model('remote-form-skill-recipe.json', SiteSkillFormRecipe)
    form_plan = model('remote-form-plan.json', WebHTTPSFormPlan)
    state_plan = model('remote-form-state-plan.json', WebHTTPSFormStatePlan)
    config = value('owned-record-config.json')
    authored = value('synthetic-authoring-source.json')
    expected_authored = {
        'schema_version': '1.0', 'synthetic': True, 'kind': 'synthetic_manual_authoring',
        'parameters_review_sha256': digest(review), 'profile_sha256': manifest.profile_sha256,
        'page_sha256': manifest.page_sha256, 'execution_performed': False,
        'outcomes_verified': False, 'training_ready': False}
    fields = fields_for_parameters(review['parameters'], bindings)
    bodies = owned_record_form_bodies(config)
    invocation = compile_site_skill_form_recipe_invocation(
        skills, plan, inputs, manifest.case_key, profiles, task, form_plan, state_plan, bindings, recipe)
    identities = {'profile_sha256': profile, 'page_sha256': page, 'skill_sha256': skill,
                  'task_sha256': task, 'form_plan_sha256': form_plan,
                  'state_plan_sha256': state_plan, 'recipe_sha256': recipe}
    if (any(digest(item.model_dump(mode='json')) != getattr(manifest, name)
            for name, item in identities.items())
            or pages.get(manifest.page_sha256) != page or skills.get(manifest.skill_sha256) != skill
            or profile.application_key != manifest.application_key
            or profile.entry_url != manifest.origin + '/entry'
            or profile.allowed_origins != [manifest.origin]
            or (task.entry_url, form_plan.entry_url, form_plan.submit_url,
                form_plan.receipt_url, state_plan.state_url) != (
                    manifest.origin + '/entry', manifest.origin + '/entry',
                    manifest.origin + '/submit', manifest.origin + '/receipt',
                    manifest.origin + '/state')
            or profile.tenant_key != review['scope']['tenant_id']
            or profile.account_role != review['scope']['account_role']
            or authored != expected_authored or skill.source_event_ids != ['learning-' + digest(authored)]
            or skill.source_verification_ids or plan.source_variant_sha256 != [digest(authored)]
            or inputs.cases[0].parameters != review['parameters']
            or config['scope'] != review['scope']
            or [(item['name'], item['value']) for item in config['fields']] != list(fields)
            or dict(fields).keys() != ({'contact_name', 'note'} if manifest.application_key == APPLICATIONS[0]
                                     else {'item_code', 'note'})
            or config['outcome_field'] != 'note'
            or hashlib.sha256(bodies['form_body']).hexdigest() != form_plan.body_sha256
            or len(bodies['form_body']) != form_plan.body_bytes
            or hashlib.sha256(bodies['before']).hexdigest() != state_plan.expected_before_sha256
            or hashlib.sha256(bodies['after']).hexdigest() != state_plan.expected_after_sha256
            or form_state_marker_sha256(bodies['before'], 'outcome')
            != state_plan.expected_before_marker_sha256
            or form_state_marker_sha256(bodies['after'], 'outcome')
            != state_plan.expected_after_marker_sha256
            or hashlib.sha256(bodies['entry']).hexdigest() != page.page_fingerprint_sha256
            or digest(invocation) != manifest.invocation_sha256):
        raise ValueError('owned_parameter_project_sources_mismatch')
    final_manifest, final_content = _checked_sources(root, manifest_sha256)
    if final_manifest != manifest or final_content != content:
        raise ValueError('owned_parameter_project_sources_changed')
    return {
        'directory': root, 'manifest': manifest.model_dump(mode='json'),
        'manifest_sha256': manifest_sha256, 'source_pins': dict(manifest.sources),
        'mode': MODE, **{name: getattr(manifest, name) for name in identities},
        'invocation_sha256': manifest.invocation_sha256,
        'parameters_review_sha256': manifest.parameters_review_sha256,
        'profiles_root': profiles.root, 'profiles': profiles, 'task': task,
        'skill_store': skills, 'skill_plan': plan, 'case_inputs': inputs,
        'field_bindings': bindings, 'recipe': recipe, 'invocation': invocation,
        'form_plan': form_plan, 'state_plan': state_plan, 'case_key': manifest.case_key,
        'parameters': dict(review['parameters']), 'record_config': config,
        'fields': fields, 'body': bodies['form_body'], 'bodies': bodies,
        'certificate_file': root / 'owned-form-certificate.pem',
        'key_file': root / 'owned-form-key.pem', 'certificate_sha256': manifest.certificate_sha256,
    }


def verify_owned_parameter_project_manifest(directory: Path, manifest_sha256: str) -> dict:
    """Verify and return only hash/scope metadata, excluding private parameter values."""
    return read_owned_parameter_project(directory, manifest_sha256)['manifest']
