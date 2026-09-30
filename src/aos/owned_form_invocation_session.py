"""Builds the immutable private sources for one owned synthetic form session."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from .contracts import REPO_ROOT, canonical, digest
from .site_knowledge import SiteKnowledgeStore, SitePageDraft
from .site_skill import SiteSkillDraft, SiteSkillStore
from .site_skill_case_binding import (SiteSkillCaseInputs, SiteSkillFormFieldBinding,
                                      parameter_variant_sha256)
from .site_skill_form_invocation import compile_site_skill_form_invocation
from .site_skill_form_recipe import (SUPPORTED_RECIPE_ORDERS, SiteSkillFormRecipe,
                                     compile_site_skill_form_recipe_invocation)
from .site_skill_validation import SiteSkillValidationPlan
from .web_application import WebApplicationProfile, WebApplicationProfiles, profile_report
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         form_state_marker_sha256)
from .web_https_form_transport import (exact_form_fields, form_body,
                                       plan_web_https_form)
from .owned_form_fixture import (HOST as OWNED_FORM_HOST, STATE_AFTER,
                                 STATE_BEFORE)


OWNED_FORM_VALUE = 'alpha'
MANIFEST_NAME = 'manifest.json'


def _example(name: str) -> dict:
    return json.loads((REPO_ROOT / 'examples' / name).read_text())


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _bytes(value) -> bytes:
    return (canonical(value.model_dump(mode='json')).encode('utf-8')
            if hasattr(value, 'model_dump') else canonical(value).encode('utf-8'))


def provision_owned_synthetic_form_invocation(
        directory: Path, port: int, *, mode: str = 'owned_synthetic_form_invocation') -> dict:
    """Create one complete source-bound synthetic form bundle in a private session directory."""
    if (type(port) is not int or not 1 <= port <= 65535
            or mode not in {'owned_synthetic_form_invocation', 'owned_synthetic_form_recipe'}
            or directory.exists() or directory.is_symlink()):
        raise ValueError('owned_form_invocation_bundle_target_invalid')
    directory.mkdir(mode=0o700, parents=True)
    origin = f'https://{OWNED_FORM_HOST}:{port}'
    profile_value = _example('web_application_profile.json')['profile']
    profile = WebApplicationProfile.model_validate({
        **profile_value, 'entry_url': origin + '/entry', 'allowed_origins': [origin]})
    profile_sha256 = profile_report(profile).profile_sha256
    profiles_root = directory / 'profiles'
    profiles = WebApplicationProfiles(profiles_root)
    profiles.register(profile, confirm_sha256=profile_sha256)

    binding_fixture = _example('web_application_binding.json')
    task = WebTaskContract.model_validate({
        **binding_fixture['task'], 'profile_sha256': profile_sha256,
        'entry_url': profile.entry_url, 'allowed_origins': profile.allowed_origins})
    task_path = directory / 'remote-entry-task.json'
    task_bytes = _bytes(task)
    _write_private(task_path, task_bytes)

    fields = exact_form_fields('message', OWNED_FORM_VALUE)
    body = form_body(fields)
    form_plan = plan_web_https_form(
        profiles, task, submit_url=origin + '/submit', receipt_url=origin + '/receipt',
        body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
    state_before = STATE_BEFORE
    state_after = STATE_AFTER
    state_plan = WebHTTPSFormStatePlan(
        profile_sha256=profile_sha256, task_sha256=digest(task.model_dump()),
        form_plan_sha256=digest(form_plan.model_dump()), state_url=origin + '/state',
        expected_before_sha256=hashlib.sha256(state_before).hexdigest(),
        expected_after_sha256=hashlib.sha256(state_after).hexdigest(),
        marker_id='outcome', expected_before_marker_sha256=form_state_marker_sha256(
            state_before, 'outcome'), expected_after_marker_sha256=form_state_marker_sha256(
                state_after, 'outcome'), submitted_field_name='message')
    certificate_path = directory / 'owned-form-certificate.pem'
    key_path = directory / 'owned-form-key.pem'
    try:
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
            '-subj', f'/CN={OWNED_FORM_HOST}', '-addext',
            f'subjectAltName=DNS:{OWNED_FORM_HOST}',
            '-keyout', str(key_path), '-out', str(certificate_path)],
            check=True, capture_output=True, timeout=20, umask=0o077)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('owned_form_fixture_certificate_generation_failed') from exc
    os.chmod(certificate_path, 0o600)
    os.chmod(key_path, 0o600)
    certificate_pem = certificate_path.read_bytes()
    certificate_sha256 = hashlib.sha256(certificate_pem).hexdigest()

    page_fixture = _example('site_page_draft.json')['page']
    page = SitePageDraft.model_validate({
        **page_fixture, 'profile_sha256': profile_sha256,
        'application_key': profile.application_key, 'tenant_key': profile.tenant_key,
        'account_role': profile.account_role, 'origin': origin,
        'route_template': '/entry', 'outgoing_page_keys': []})
    page_sha256 = digest(page.model_dump())
    pages = SiteKnowledgeStore(directory / 'site-knowledge', profiles)
    pages.register(page, confirm_sha256=page_sha256)

    skill_fixture = _example('site_skill_draft.json')['skill']
    recipe_step_keys = {operation: operation.replace('_', '-')
                        for operation in SUPPORTED_RECIPE_ORDERS[1]}
    skill = SiteSkillDraft.model_validate({
        **skill_fixture, 'profile_sha256': profile_sha256,
        'application_key': profile.application_key, 'tenant_key': profile.tenant_key,
        'account_role': profile.account_role, 'task_key': task.task_key,
        'page_draft_sha256': page_sha256,
        **({'step_keys': sorted(recipe_step_keys.values()),
            'precondition_keys': ['baseline-known', 'form-available']}
           if mode == 'owned_synthetic_form_recipe' else {})})
    skill_sha256 = digest(skill.model_dump())
    skills = SiteSkillStore(directory / 'site-skills', profiles, pages)
    skills.register(skill, confirm_sha256=skill_sha256)

    case_fixture = _example('site_skill_case_binding.json')
    plan = SiteSkillValidationPlan.model_validate(case_fixture['plan']).model_copy(update={
        'skill_sha256': skill_sha256, 'profile_sha256': profile_sha256,
        'page_draft_sha256': page_sha256, 'task_key': task.task_key})
    selected_values = {'dev-query': 'alpha', 'heldout-alpha': 'beta', 'heldout-beta': 'gamma'}
    plan = plan.model_copy(update={'cases': [case.model_copy(update={
        'parameter_variant_sha256': parameter_variant_sha256(
            {'record-query': selected_values[case.case_key]})
    }) for case in plan.cases]})
    inputs = SiteSkillCaseInputs.model_validate(case_fixture['case_inputs']).model_copy(update={
        'plan_sha256': digest(plan.model_dump())})
    bindings = [SiteSkillFormFieldBinding(parameter_key='record-query',
                                          form_field_name='message')]
    recipe = None
    if mode == 'owned_synthetic_form_recipe':
        recipe = SiteSkillFormRecipe.model_validate({
            'schema_version': '1.0', 'synthetic': True,
            'skill_sha256': skill_sha256, 'profile_sha256': profile_sha256,
            'page_draft_sha256': page_sha256, 'task_key': task.task_key,
            'task_sha256': digest(task.model_dump()), 'model_role': 'system1',
            'field_binding_sha256': digest([item.model_dump() for item in bindings]),
            'preconditions': (
                {'precondition_key': 'baseline-known', 'kind': 'declared_state_before',
                 'checked_at_operation': 'read_state_before'},
                {'precondition_key': 'form-available', 'kind': 'entry_form_available',
                 'checked_at_operation': 'fill_form'}),
            'outcome': {'outcome_key': skill.expected_outcome_key,
                        'kind': 'submitted_field_state_transition',
                        'parameter_key': 'record-query', 'form_field_name': 'message',
                        'verified_at_operation': 'read_state_after'},
            'steps': tuple({'step_key': recipe_step_keys[operation],
                            'operation': operation}
                           for operation in SUPPORTED_RECIPE_ORDERS[1]),
            'execution_authorized': False, 'collection_authorized': False,
            'reviewed': False, 'activation_authorized': False, 'training_ready': False})
        invocation = compile_site_skill_form_recipe_invocation(
            skills, plan, inputs, 'dev-query', profiles, task, form_plan,
            state_plan, bindings, recipe)
    else:
        invocation = compile_site_skill_form_invocation(
            skills, plan, inputs, 'dev-query', profiles, task, form_plan, state_plan, bindings)
    source_values = {
        'remote-form-plan.json': _bytes(form_plan),
        'remote-form-value.txt': OWNED_FORM_VALUE.encode('utf-8'),
        'remote-form-state-plan.json': _bytes(state_plan),
        'remote-form-skill-plan.json': _bytes(plan),
        'remote-form-skill-case-inputs.json': _bytes(inputs),
        'remote-form-skill-field-bindings.json': _bytes(
            [item.model_dump(mode='json') for item in bindings]),
        'site-page-draft.json': _bytes(page),
        'site-skill-draft.json': _bytes(skill),
    }
    if recipe is not None:
        source_values['remote-form-skill-recipe.json'] = _bytes(recipe)
    for name, content in source_values.items():
        _write_private(directory / name, content)
    pins = {name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(source_values.items())}
    pins['remote-entry-task.json'] = hashlib.sha256(task_bytes).hexdigest()
    pins[certificate_path.name] = certificate_sha256
    pins[key_path.name] = hashlib.sha256(key_path.read_bytes()).hexdigest()
    manifest = {
        'schema_version': '2.0' if recipe is not None else '1.0', 'synthetic': True,
        'mode': mode, 'origin': origin,
        'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256,
        'form_plan_sha256': digest(form_plan.model_dump()),
        'state_plan_sha256': digest(state_plan.model_dump()),
        **({'recipe_sha256': digest(recipe.model_dump(mode='json'))}
           if recipe is not None else {}),
        'certificate_sha256': certificate_sha256,
        'invocation_sha256': hashlib.sha256(canonical(invocation).encode()).hexdigest(),
        'sources': pins,
        'claims': {'skill_executed': False, 'site_outcome_verified': False,
                   'reviewed': False, 'activation_authorized': False,
                   'training_ready': False}}
    manifest_bytes = canonical(manifest).encode('utf-8')
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    _write_private(directory / MANIFEST_NAME, manifest_bytes)
    return {
        'directory': directory, 'manifest': manifest,
        'manifest_sha256': manifest_sha256,
        'profile_sha256': profile_sha256, 'skill_sha256': skill_sha256,
        'form_plan_sha256': manifest['form_plan_sha256'],
        'state_plan_sha256': manifest['state_plan_sha256'],
        'invocation_sha256': manifest['invocation_sha256'],
        'source_pins': pins, 'invocation': invocation,
        'mode': mode, 'recipe_sha256': (manifest.get('recipe_sha256')),
        'recipe': recipe,
        'profiles_root': profiles_root, 'profiles': profiles,
        'task': task, 'form_plan': form_plan, 'state_plan': state_plan,
        'skill_store': skills, 'skill_plan': plan, 'case_inputs': inputs,
        'field_bindings': bindings, 'case_key': 'dev-query',
        'body': body, 'fields': fields,
        'certificate_file': certificate_path, 'key_file': key_path,
        'certificate_sha256': certificate_sha256,
    }


def verify_owned_form_invocation_manifest(directory: Path, manifest_sha256: str) -> dict:
    from .local_app import private_read

    root = Path(directory).absolute()
    manifest_content = private_read(root / MANIFEST_NAME, 16384)
    if hashlib.sha256(manifest_content).hexdigest() != manifest_sha256:
        raise ValueError('owned_form_invocation_manifest_pin_changed')
    manifest = json.loads(manifest_content)
    common_sources = {
        'remote-entry-task.json', 'remote-form-plan.json', 'remote-form-value.txt',
        'remote-form-state-plan.json', 'remote-form-skill-plan.json',
        'remote-form-skill-case-inputs.json', 'remote-form-skill-field-bindings.json',
        'site-page-draft.json', 'site-skill-draft.json',
        'owned-form-certificate.pem', 'owned-form-key.pem'}
    is_recipe = isinstance(manifest, dict) and manifest.get('mode') == 'owned_synthetic_form_recipe'
    expected_sources = common_sources | ({'remote-form-skill-recipe.json'} if is_recipe else set())
    expected_manifest_keys = ({'schema_version', 'synthetic', 'mode', 'origin',
                               'profile_sha256', 'skill_sha256', 'form_plan_sha256',
                               'state_plan_sha256', 'certificate_sha256', 'invocation_sha256',
                               'sources', 'claims', 'recipe_sha256'} if is_recipe else
                              {'schema_version', 'synthetic', 'mode', 'origin',
                               'profile_sha256', 'skill_sha256', 'form_plan_sha256',
                               'state_plan_sha256', 'certificate_sha256', 'invocation_sha256',
                               'sources', 'claims'})
    if (not isinstance(manifest, dict)
            or set(manifest) != expected_manifest_keys
            or manifest.get('schema_version') != ('2.0' if is_recipe else '1.0')
            or manifest.get('synthetic') is not True
            or manifest.get('mode') not in {
                'owned_synthetic_form_invocation', 'owned_synthetic_form_recipe'}
            or not isinstance(manifest.get('claims'), dict)
            or set(manifest['claims']) != {
                'skill_executed', 'site_outcome_verified', 'reviewed',
                'activation_authorized', 'training_ready'}
            or any(value is not False for value in manifest['claims'].values())
            or not isinstance(manifest.get('sources'), dict)
            or set(manifest['sources']) != expected_sources
            or canonical(manifest).encode('utf-8') != manifest_content):
        raise ValueError('owned_form_invocation_manifest_invalid')
    if is_recipe:
        from .site_skill_form_recipe import (SiteSkillFormRecipe,
                                             SUPPORTED_RECIPE_ORDERS)

        recipe_content = private_read(root / 'remote-form-skill-recipe.json', 16384)
        recipe = SiteSkillFormRecipe.model_validate_json(recipe_content)
        if (canonical(recipe.model_dump(mode='json')).encode() != recipe_content
                or digest(recipe.model_dump(mode='json')) != manifest['recipe_sha256']
                or manifest['sources']['remote-form-skill-recipe.json']
                != manifest['recipe_sha256']
                or tuple(step.operation for step in recipe.steps)
                != SUPPORTED_RECIPE_ORDERS[1]):
            raise ValueError('owned_form_recipe_source_changed')
    elif manifest.get('mode') != 'owned_synthetic_form_invocation':
        raise ValueError('owned_form_invocation_manifest_invalid')
    for name in ('profile_sha256', 'skill_sha256', 'form_plan_sha256',
                 'state_plan_sha256', 'certificate_sha256', 'invocation_sha256',
                 *(['recipe_sha256'] if is_recipe else [])):
        value = manifest[name]
        if (not isinstance(value, str) or len(value) != 64
                or any(character not in '0123456789abcdef' for character in value)):
            raise ValueError('owned_form_invocation_manifest_invalid')
    for name, expected in manifest['sources'].items():
        if (not isinstance(expected, str) or len(expected) != 64
                or any(character not in '0123456789abcdef' for character in expected)
                or hashlib.sha256(private_read(root / name, 20000)).hexdigest() != expected):
            raise ValueError('owned_form_invocation_source_pin_changed')
    if manifest['sources']['owned-form-certificate.pem'] != manifest['certificate_sha256']:
        raise ValueError('owned_form_invocation_certificate_pin_changed')
    if not isinstance(manifest['origin'], str):
        raise ValueError('owned_form_invocation_origin_invalid')
    try:
        parsed_origin = urlsplit(manifest['origin'])
        port = parsed_origin.port
    except ValueError as exc:
        raise ValueError('owned_form_invocation_origin_invalid') from exc
    if (parsed_origin.scheme != 'https' or parsed_origin.hostname != OWNED_FORM_HOST
            or port is None or not 1 <= port <= 65535 or parsed_origin.path
            or parsed_origin.query or parsed_origin.fragment
            or manifest['origin'] != f'https://{OWNED_FORM_HOST}:{port}'):
        raise ValueError('owned_form_invocation_origin_invalid')
    return manifest
