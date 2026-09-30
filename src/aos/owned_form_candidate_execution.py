"""Pure plan preparation and exact-port listener creation for owned candidate runs."""

import hashlib
import html
import json
import os
import re
import socket
import stat
from pathlib import Path
from uuid import uuid4

from .contracts import canonical, digest
from .workspace_identity import open_existing_workspace
from .site_skill_case_binding import (SiteSkillCaseInput, SiteSkillCaseInputs,
                                      SiteSkillFormFieldBinding,
                                      parameter_variant_sha256)
from .site_skill_form_recipe import compile_site_skill_form_recipe_invocation
from .site_skill_form_recipe_candidate_execution import prepare_candidate_execution
from .site_skill_validation import SiteSkillValidationCase, SiteSkillValidationPlan
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         form_state_marker_sha256)
from .web_https_form_transport import (WebHTTPSFormPlan, exact_form_fields,
                                       form_body, plan_web_https_form)


_CASE_KEY = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
_DEVELOPMENT_VALUE = re.compile(r'[A-Za-z0-9 _.-]{1,128}\Z')


class CandidateSkillView:
    def __init__(self, skill, profiles, pages):
        from .site_skill import SiteSkillStore

        self._store = SiteSkillStore.__new__(SiteSkillStore)
        self._store.root = pages.root.parent / '.unwritten-candidate-preview-store'
        self._store.profiles = profiles
        self._store.pages = pages
        self._skill = skill
        self._checksum = digest(skill.model_dump())

    def get(self, checksum):
        if checksum != self._checksum:
            raise ValueError('candidate_skill_pin_changed')
        self._store._scope(self._skill)
        return self._skill

    def preview(self, skill):
        return self._store.preview(skill)


def validate_candidate_case_input(case_key, development_value):
    if (not isinstance(case_key, str) or _CASE_KEY.fullmatch(case_key) is None
            or not isinstance(development_value, str)
            or _DEVELOPMENT_VALUE.fullmatch(development_value) is None
            or development_value != development_value.strip()):
        raise ValueError('owned_candidate_development_input_invalid')
    return case_key, development_value


def candidate_operator_field_args(form_fields):
    if (not isinstance(form_fields, tuple) or len(form_fields) != 1
            or not isinstance(form_fields[0], tuple) or len(form_fields[0]) != 2):
        raise ValueError('owned_candidate_execution_requires_one_parameter_binding')
    return form_fields[0]


def _planning_execution_values(candidate, inputs, invocation, field_bindings,
                               form_body_bytes):
    candidate = candidate.model_dump(mode='json') if hasattr(candidate, 'model_dump') else candidate
    inputs = inputs.model_dump(mode='json') if hasattr(inputs, 'model_dump') else inputs
    invocation = (invocation.model_dump(mode='json')
                  if hasattr(invocation, 'model_dump') else invocation)
    field_bindings = [item.model_dump(mode='json') if hasattr(item, 'model_dump') else item
                      for item in field_bindings]
    case_key = invocation['case_key']
    case_inputs = [item for item in inputs['cases'] if item['case_key'] == case_key]
    bindings = candidate['field_bindings']
    if (len(case_inputs) != 1 or len(bindings) != 1 or len(field_bindings) != 1
            or bindings != field_bindings):
        raise ValueError('owned_skill_planning_execution_case_binding_invalid')
    recipe_outcome = candidate['recipe']['outcome']
    binding = bindings[0]
    parameter_key = recipe_outcome['parameter_key']
    if (binding['parameter_key'] != parameter_key
            or recipe_outcome['form_field_name'] != binding['form_field_name']
            or set(case_inputs[0]['parameters']) != {parameter_key}):
        raise ValueError('owned_skill_planning_execution_field_binding_invalid')
    development_value = case_inputs[0]['parameters'][parameter_key]
    expected_body = form_body(exact_form_fields(binding['form_field_name'], development_value))
    if expected_body != form_body_bytes:
        raise ValueError('owned_skill_planning_execution_form_body_changed')
    steps = [{'step_key': item['step_key'], 'operation': item['operation']}
             for item in invocation['steps']]
    return case_key, development_value, steps


def assert_planning_execution_binding(planning_bundle, *, manifest,
                                      reuse_admission, candidate, case_key,
                                      development_value, steps):
    from .owned_skill_planning import validate_planning_bundle
    from .owned_skill_reuse_admission import validate_owned_skill_reuse_admission

    try:
        validate_owned_skill_reuse_admission(reuse_admission)
        validate_planning_bundle(planning_bundle, planner=None)
        if (manifest.get('schema_version') != '1.4'
                or planning_bundle.get('real_model') is not True
                or planning_bundle.get('deployment', {}).get('real_model') is not True
                or planning_bundle.get('deployment', {}).get('kind')
                != 'bonsai_native_owned_skill_planner'
                or planning_bundle.get('model_pins', {}).get('owned_skill_plan_protocol')
                != 'aos-owned-skill-plan-v2'):
            raise ValueError('planning_scope')
        _assert_planning_execution_pins(
            planning_bundle, manifest=manifest, reuse_admission=reuse_admission,
            candidate=candidate, case_key=case_key,
            development_value=development_value, steps=steps)
    except Exception as error:
        raise ValueError('owned_skill_planning_execution_binding_invalid') from error
    return planning_bundle


def _assert_planning_execution_pins(planning_bundle, *, manifest,
                                    reuse_admission, candidate, case_key,
                                    development_value, steps):
    try:
        if (manifest.get('planning_bundle_sha256') != digest(planning_bundle)
                or manifest.get('reuse_admission_sha256') != digest(reuse_admission)
                or manifest.get('candidate_sha256') != digest(candidate)
                or manifest.get('source_run_ref') != candidate['source_run_ref']
                or manifest.get('source_group_sha256') != candidate['source_group_sha256']
                or manifest.get('source_invocation_sha256')
                != candidate['source_context']['invocation_sha256']
                or manifest.get('recipe_sha256') != digest(candidate['recipe'])
                or candidate['recipe']['skill_sha256'] != digest(candidate['skill'])):
            raise ValueError('planning_manifest_pin')
        preview = reuse_admission['preview']
        candidate_context = candidate['source_context']
        authority = planning_bundle['authority']
        evidence = planning_bundle['evidence']
        response = planning_bundle['model_response']
        bindings = candidate['field_bindings']
        recipe = candidate['recipe']
        if (not isinstance(authority, dict) or not isinstance(evidence, list)
                or len(evidence) != 1 or len(bindings) != 1):
            raise ValueError('planning_shape')
        authority_expected = {
            'manager_session': reuse_admission['manager_session'],
            'desktop_session_id': reuse_admission['desktop_session_id'],
            'runtime_id': reuse_admission['runtime_id'],
            'lease_id': reuse_admission['lease_id'],
            'generation': reuse_admission['generation'],
            'reuse_admission_sha256': digest(reuse_admission),
            'source_manifest_sha256': candidate_context['manifest_sha256'],
            'source_run_ref': manifest['source_run_ref'],
            'source_invocation_sha256': manifest['source_invocation_sha256'],
            'source_fingerprint_sha256': candidate['source_fingerprint_sha256'],
            'family_sha256': preview['family_sha256'],
            'release_sha256': manifest['release_sha256'],
            'selection_sha256': manifest['selection_sha256'],
            'review_sha256': manifest['review_sha256'],
            'candidate_sha256': manifest['candidate_sha256'],
            'recipe_sha256': manifest['recipe_sha256'],
        }
        if (any(authority.get(key) != value for key, value in authority_expected.items())
                or preview.get('source_manifest_sha256')
                != candidate_context['manifest_sha256']
                or preview.get('source_run_ref') != manifest['source_run_ref']
                or preview.get('source_invocation_sha256')
                != manifest['source_invocation_sha256']
                or preview.get('source_fingerprint_sha256')
                != candidate['source_fingerprint_sha256']
                or preview.get('candidate_sha256') != manifest['candidate_sha256']
                or preview.get('review_sha256') != manifest['review_sha256']
                or preview.get('release_sha256') != manifest['release_sha256']
                or preview.get('selection_sha256') != manifest['selection_sha256']
                or preview.get('recipe_sha256') != manifest['recipe_sha256']
                or candidate['recipe']['steps'] != steps):
            raise ValueError('planning_authority')
        binding = bindings[0]
        evidence_item = evidence[0]
        expected_evidence = {
            'id': 'admitted-skill',
            'skill_key': candidate['skill']['skill_key'],
            'parameter_key': recipe['outcome']['parameter_key'],
            'form_field_name': binding['form_field_name'],
            'ordered_steps': steps,
            'requested_case_key': case_key,
            'requested_value': development_value,
        }
        if (evidence_item != expected_evidence
                or planning_bundle['request']['case_key'] != case_key
                or planning_bundle['request']['lease_id'] != reuse_admission['lease_id']
                or planning_bundle['request']['generation'] != reuse_admission['generation']
                or response.get('decision') != 'invoke_selected_skill'
                or response.get('case_key') != case_key
                or response.get('parameter_value') != development_value
                or response.get('steps') != steps
                or response.get('evidence_refs') != ['admitted-skill']
                or response.get('reason_code') != 'skill_match'
                or any(response.get(key) is not False for key in (
                    'execution_authorized', 'activation_authorized', 'training_ready'))):
            raise ValueError('planning_response')
    except Exception as error:
        raise ValueError('owned_skill_planning_execution_pins_invalid') from error


def prepare_candidate_form_run(candidate, candidate_sha256, source, case_key,
                               development_value):
    """Build fresh plans from explicit development input without registering or networking."""
    from .site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate

    case_key, development_value = validate_candidate_case_input(case_key, development_value)
    candidate = parse_site_skill_form_recipe_candidate(candidate)
    if digest(candidate.model_dump(mode='json')) != candidate_sha256:
        raise ValueError('owned_candidate_content_hash_changed')
    bindings = tuple(SiteSkillFormFieldBinding.model_validate_json(canonical(item.model_dump()))
                     for item in candidate.field_bindings)
    if len(bindings) != 1:
        raise ValueError('owned_candidate_execution_requires_one_parameter_binding')
    binding = bindings[0]
    if binding.parameter_key != candidate.recipe.outcome.parameter_key:
        raise ValueError('owned_candidate_outcome_parameter_changed')
    form_fields = exact_form_fields(binding.form_field_name, development_value)
    body = form_body(form_fields)
    form_plan = plan_web_https_form(
        source['profiles'], source['task'], submit_url=source['form_plan'].submit_url,
        receipt_url=source['form_plan'].receipt_url,
        body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
    state_after = ('<html><h1 id="outcome">' + html.escape(development_value, quote=True)
                   + '</h1></html>').encode('utf-8')
    old_state = source['state_plan']
    state_plan = WebHTTPSFormStatePlan.model_validate_json(canonical({
        **old_state.model_dump(mode='json'),
        'form_plan_sha256': digest(form_plan.model_dump()),
        'expected_after_sha256': hashlib.sha256(state_after).hexdigest(),
        'expected_after_marker_sha256': form_state_marker_sha256(
            state_after, old_state.marker_id),
    }))
    skill = candidate.skill
    original_plan = source['plan']
    original_inputs = source['inputs']
    source_development = [case for case in original_plan.cases
                         if case.cohort == 'development']
    held_out_keys = {case.case_key for case in original_plan.cases
                     if case.cohort == 'held_out'}
    if (len(source_development) != 1 or case_key in held_out_keys
            or set(skill.parameter_keys) != {binding.parameter_key}
            or original_plan.profile_sha256 != skill.profile_sha256
            or original_plan.page_draft_sha256 != skill.page_draft_sha256
            or original_plan.task_key != skill.task_key):
        raise ValueError('owned_candidate_execution_scope_changed')
    source_development_key = source_development[0].case_key
    input_cases = []
    for case in original_inputs.cases:
        selected_case_key = case_key if case.case_key == source_development_key else case.case_key
        value = (development_value if case.case_key == source_development_key
                 else f'unexecuted-structural-{case.case_key}')
        input_cases.append(SiteSkillCaseInput(
            case_key=selected_case_key, parameters={binding.parameter_key: value}))
    input_by_key = {item.case_key: item for item in input_cases}
    plan_cases = []
    selected_found = False
    for case in original_plan.cases:
        selected_case_key = case_key if case.case_key == source_development_key else case.case_key
        if case.case_key == source_development_key:
            selected_found = True
        parameters = input_by_key[selected_case_key].parameters
        plan_cases.append(SiteSkillValidationCase.model_validate_json(canonical({
            **case.model_dump(mode='json'),
            'case_key': selected_case_key,
            'parameter_keys': sorted(parameters),
            'parameter_variant_sha256': parameter_variant_sha256(parameters),
            'expected_outcome_key': skill.expected_outcome_key,
        })))
    plan_cases.sort(key=lambda item: item.case_key)
    input_cases.sort(key=lambda item: item.case_key)
    plan = SiteSkillValidationPlan.model_validate_json(canonical({
        **original_plan.model_dump(mode='json'),
        'skill_sha256': candidate.recipe.skill_sha256,
        'profile_sha256': candidate.profile_sha256,
        'page_draft_sha256': candidate.annotation.page_draft_sha256,
        'task_key': candidate.annotation.task_key,
        'source_variant_sha256': [candidate.source_parameter_variant_sha256],
        'cases': [item.model_dump(mode='json') for item in plan_cases],
    }))
    if not selected_found:
        raise ValueError('owned_candidate_development_case_missing')
    inputs = SiteSkillCaseInputs.model_validate_json(canonical({
        'schema_version': '1.0', 'synthetic': True,
        'plan_sha256': digest(plan.model_dump()),
        'cases': [item.model_dump(mode='json') for item in input_cases],
    }))
    skill_store = CandidateSkillView(candidate.skill, source['profiles'], source['pages'])
    invocation = compile_site_skill_form_recipe_invocation(
        skill_store, plan, inputs, case_key, source['profiles'], source['task'],
        form_plan, state_plan, list(bindings), candidate.recipe)
    admission = prepare_candidate_execution(
        candidate.model_dump(mode='json'), candidate_sha256, invocation, plan, inputs,
        case_key)
    return {
        'candidate': candidate.model_dump(mode='json'),
        'candidate_sha256': candidate_sha256,
        'source_run_ref': candidate.source_run_ref,
        'source_invocation_sha256': candidate.source_context.invocation_sha256,
        'source_group_sha256': candidate.source_group_sha256,
        'profile_sha256': candidate.profile_sha256,
        'skill_sha256': candidate.recipe.skill_sha256,
        'case_key': case_key,
        'parameter_variant_sha256': admission.parameter_variant_sha256,
        'form_fields': form_fields,
        'form_body': body,
        'state_after': state_after,
        'task': source['task'],
        'form_plan': form_plan,
        'state_plan': state_plan,
        'plan': plan,
        'inputs': inputs,
        'field_bindings': bindings,
        'recipe': candidate.recipe,
        'invocation': invocation,
        'invocation_sha256': admission.invocation_sha256,
        'recipe_sha256': admission.recipe_sha256,
        'admission': admission,
        'steps': [{'step_key': item.step_key, 'operation': item.operation}
                  for item in candidate.recipe.steps],
        'purpose': 'development_variation',
        'independent_held_out': False,
    }


def bind_exact_loopback_listener(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('owned_candidate_fixture_port_invalid')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(16)
        return listener.detach()
    except OSError as error:
        listener.close()
        raise ValueError('owned_candidate_fixture_exact_port_unavailable') from error


def _private_child(parent: Path, name: str) -> Path:
    if (not isinstance(name, str)
            or name not in {'candidate-execution-bundles', 'candidate-executions',
                            'candidate-reviews', 'revocations',
                            'owned-skill-release-catalog', 'releases', 'selections'}
            and re.fullmatch(r'[a-f0-9]{64}', name) is None):
        raise ValueError('owned_candidate_private_path_invalid')
    parent_fd = open_existing_workspace(parent)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=parent_fd)
        try:
            metadata = os.fstat(child_fd)
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ValueError('owned_candidate_private_directory_invalid')
            if (not stat.S_ISDIR(linked.st_mode)
                    or (metadata.st_dev, metadata.st_ino)
                    != (linked.st_dev, linked.st_ino)):
                raise ValueError('owned_candidate_private_directory_changed')
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)
    return parent / name


def candidate_execution_skill_store_path(source_directory: Path,
                                         candidate_sha256: str) -> Path:
    if re.fullmatch(r'[a-f0-9]{64}', candidate_sha256 or '') is None:
        raise ValueError('owned_candidate_execution_hash_invalid')
    candidate_root = _private_child(source_directory, 'candidate-executions')
    bundle_root = _private_child(candidate_root, candidate_sha256)
    return bundle_root / 'skills'


def _write_private_child(directory_fd: int, filename: str, content: bytes) -> None:
    temporary = '.candidate-' + uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, filename, src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd, follow_symlinks=False)
        linked = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(linked.st_mode) or linked.st_uid != os.getuid()
                or stat.S_IMODE(linked.st_mode) != 0o600 or linked.st_nlink != 2):
            raise ValueError('owned_candidate_execution_file_publication_invalid')
        os.unlink(temporary, dir_fd=directory_fd)
        final = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        if (final.st_dev, final.st_ino, final.st_nlink) != (
                linked.st_dev, linked.st_ino, 1):
            raise ValueError('owned_candidate_execution_file_publication_changed')
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _read_private_child(directory_fd: int, filename: str, limit: int) -> bytes:
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > limit):
            raise ValueError('owned_candidate_execution_file_invalid')
        content = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
        linked = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                  'st_nlink', 'st_mode', 'st_uid')
        if (len(content) != before.st_size or len(content) > limit
                or any(getattr(before, field) != getattr(current, field)
                       for current in (after, linked) for field in fields)):
            raise ValueError('owned_candidate_execution_file_changed')
        return content
    finally:
        os.close(descriptor)


def persist_candidate_execution_bundle(candidate_directory: Path,
                                       candidate_sha256: str,
                                       execution_sha256: str,
                                       prepared: dict, admission,
                                       review_sha256: str | None = None,
                                       release_sha256: str | None = None,
                                       selection_sha256: str | None = None,
                                       reuse_admission_sha256: str | None = None,
                                       reuse_admission: dict | None = None,
                                       planning_bundle_sha256: str | None = None,
                                       planning_bundle: dict | None = None,
                                       adapter_admission_sha256: str | None = None,
                                       adapter_admission: dict | None = None) -> tuple[Path, str]:
    if any(re.fullmatch(r'[a-f0-9]{64}', value or '') is None
           for value in (candidate_sha256, execution_sha256)):
        raise ValueError('owned_candidate_execution_hash_invalid')
    if review_sha256 is not None and re.fullmatch(r'[a-f0-9]{64}', review_sha256) is None:
        raise ValueError('owned_candidate_review_hash_invalid')
    if ((release_sha256 is None) != (selection_sha256 is None)
            or release_sha256 is not None and review_sha256 is None
            or any(re.fullmatch(r'[a-f0-9]{64}', value or '') is None
                   for value in (release_sha256, selection_sha256)
                   if value is not None)):
        raise ValueError('owned_candidate_release_selection_pin_invalid')
    if ((reuse_admission_sha256 is None) != (reuse_admission is None)
            or reuse_admission_sha256 is not None
            and re.fullmatch(r'[a-f0-9]{64}', reuse_admission_sha256) is None
            or reuse_admission is not None
            and digest(reuse_admission) != reuse_admission_sha256
            or reuse_admission_sha256 is not None
            and (review_sha256 is None or release_sha256 is None)):
        raise ValueError('owned_skill_reuse_admission_pin_invalid')
    if adapter_admission_sha256 is not None and planning_bundle_sha256 is not None:
        raise ValueError('owned_adapter_admission_pin_invalid')
    if ((planning_bundle_sha256 is None) != (planning_bundle is None)
            or planning_bundle_sha256 is not None
            and (re.fullmatch(r'[a-f0-9]{64}', planning_bundle_sha256) is None
                 or digest(planning_bundle) != planning_bundle_sha256
                 or review_sha256 is None or release_sha256 is None
                 or selection_sha256 is None or reuse_admission_sha256 is None)):
        raise ValueError('owned_skill_planning_bundle_pin_invalid')
    if ((adapter_admission_sha256 is None) != (adapter_admission is None)
            or adapter_admission_sha256 is not None
            and (re.fullmatch(r'[a-f0-9]{64}', adapter_admission_sha256) is None
                 or digest(adapter_admission) != adapter_admission_sha256
                 or review_sha256 is None or release_sha256 is None
                 or selection_sha256 is None or reuse_admission_sha256 is None
                 or planning_bundle_sha256 is not None)):
        raise ValueError('owned_adapter_admission_pin_invalid')
    artifact_names = (
        'candidate.json', 'validation-plan.json', 'case-inputs.json',
        'form-plan.json', 'state-plan.json', 'field-bindings.json',
        'recipe.json', 'invocation.json', 'admission.json', 'form-body.bin',
        'state-after.bin')
    values = {
        'candidate.json': prepared['candidate'],
        'validation-plan.json': prepared['plan'].model_dump(mode='json'),
        'case-inputs.json': prepared['inputs'].model_dump(mode='json'),
        'form-plan.json': prepared['form_plan'].model_dump(mode='json'),
        'state-plan.json': prepared['state_plan'].model_dump(mode='json'),
        'field-bindings.json': [item.model_dump(mode='json')
                                for item in prepared['field_bindings']],
        'recipe.json': prepared['recipe'].model_dump(mode='json'),
        'invocation.json': prepared['invocation'],
        'admission.json': admission.model_dump(mode='json'),
        'form-body.bin': prepared['form_body'],
        'state-after.bin': prepared['state_after'],
    }
    if reuse_admission is not None:
        values['reuse-admission.json'] = reuse_admission
        artifact_names = artifact_names + ('reuse-admission.json',)
    if planning_bundle is not None:
        values['planning-bundle.json'] = planning_bundle
        artifact_names = artifact_names + ('planning-bundle.json',)
    if adapter_admission is not None:
        values['adapter-admission.json'] = adapter_admission
        artifact_names = artifact_names + ('adapter-admission.json',)
    file_bytes = {name: (value if isinstance(value, bytes)
                         else canonical(value).encode('utf-8'))
                  for name, value in values.items()}
    if set(file_bytes) != set(artifact_names):
        raise ValueError('owned_candidate_execution_bundle_invalid')
    schema_version = ('1.5' if adapter_admission_sha256 is not None else
                      '1.4' if planning_bundle_sha256 is not None else
                      '1.3' if reuse_admission_sha256 is not None else
                      '1.2' if release_sha256 is not None else
                      '1.1' if review_sha256 is not None else '1.0')
    manifest = {
        'schema_version': schema_version, 'synthetic': True,
        'mode': 'owned_candidate_development',
        'candidate_sha256': candidate_sha256,
        'candidate_execution_sha256': execution_sha256,
        'preview_sha256': prepared['preview_sha256'],
        'source_run_id': prepared['source_run_id'],
        'source_run_ref': prepared['source_run_ref'],
        'source_invocation_sha256': prepared['source_invocation_sha256'],
        'source_group_sha256': prepared['source_group_sha256'],
        'parameter_variant_sha256': prepared['parameter_variant_sha256'],
        'invocation_sha256': prepared['invocation_sha256'],
        'recipe_sha256': prepared['recipe_sha256'],
        'admission_sha256': digest(admission.model_dump(mode='json')),
        'files': {name: hashlib.sha256(content).hexdigest()
                  for name, content in sorted(file_bytes.items())},
        'independent_held_out': False, 'dataset_ingestion_authorized': False,
        'training_ready': False, 'activation_authorized': False,
    }
    if review_sha256 is not None:
        manifest['review_sha256'] = review_sha256
    if release_sha256 is not None:
        manifest['release_sha256'] = release_sha256
        manifest['selection_sha256'] = selection_sha256
    if reuse_admission_sha256 is not None:
        manifest['reuse_admission_sha256'] = reuse_admission_sha256
    if planning_bundle_sha256 is not None:
        manifest['planning_bundle_sha256'] = planning_bundle_sha256
        case_key, development_value, steps = _planning_execution_values(
            prepared['candidate'], prepared['inputs'], prepared['invocation'],
            prepared['field_bindings'], prepared['form_body'])
        assert_planning_execution_binding(
            planning_bundle, manifest=manifest, reuse_admission=reuse_admission,
            candidate=prepared['candidate'], case_key=case_key,
            development_value=development_value, steps=steps)
        expected_execution_sha256 = digest({
            'preview_sha256': prepared['preview_sha256'],
            'admission_sha256': manifest['admission_sha256'],
            'source_run_ref': manifest['source_run_ref'],
            'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'reuse_admission_sha256': reuse_admission_sha256,
            'planning_bundle_sha256': planning_bundle_sha256,
        })
        if expected_execution_sha256 != execution_sha256:
            raise ValueError('owned_skill_planning_execution_identity_changed')
    if adapter_admission_sha256 is not None:
        manifest['adapter_admission_sha256'] = adapter_admission_sha256
        case_key, development_value, steps = _planning_execution_values(
            prepared['candidate'], prepared['inputs'], prepared['invocation'],
            prepared['field_bindings'], prepared['form_body'])
        base_preview = {
            'candidate_sha256': candidate_sha256,
            'source_run_ref': manifest['source_run_ref'],
            'source_invocation_sha256': manifest['source_invocation_sha256'],
            'source_group_sha256': manifest['source_group_sha256'],
            'profile_sha256': prepared['profile_sha256'],
            'skill_sha256': prepared['skill_sha256'],
            'case_key': case_key,
            'parameter_variant_sha256': manifest['parameter_variant_sha256'],
            'invocation_sha256': manifest['invocation_sha256'],
            'recipe_sha256': manifest['recipe_sha256'],
            'steps': steps,
            'purpose': 'development_variation', 'independent_held_out': False,
            'schema_version': '1.3', 'available': True, 'status': 'preview',
            'form_plan_sha256': digest(prepared['form_plan'].model_dump()),
            'state_plan_sha256': digest(prepared['state_plan'].model_dump()),
            'report': None, 'review_sha256': review_sha256,
            'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'reuse_admission_sha256': reuse_admission_sha256,
        }
        final_preview = dict(base_preview)
        final_preview['schema_version'] = '1.5'
        final_preview['adapter_admission_sha256'] = adapter_admission_sha256
        if digest(final_preview) != prepared['preview_sha256']:
            raise ValueError('owned_adapter_execution_preview_changed')
        from .owned_adapter_admission import assert_runtime_execution_binding
        assert_runtime_execution_binding(
            adapter_admission, manifest=manifest, reuse_admission=reuse_admission,
            base_preview=base_preview, case_key=case_key,
            development_value=development_value)
        expected_execution_sha256 = digest({
            'preview_sha256': prepared['preview_sha256'],
            'admission_sha256': manifest['admission_sha256'],
            'source_run_ref': manifest['source_run_ref'],
            'release_sha256': release_sha256,
            'selection_sha256': selection_sha256,
            'reuse_admission_sha256': reuse_admission_sha256,
            'adapter_admission_sha256': adapter_admission_sha256,
        })
        if expected_execution_sha256 != execution_sha256:
            raise ValueError('owned_adapter_execution_identity_changed')
    from .dataset import validator
    try:
        validator('owned_candidate_execution_bundle').validate(manifest)
    except Exception as error:
        raise ValueError('owned_candidate_execution_manifest_invalid') from error
    manifest_bytes = canonical(manifest).encode('utf-8')
    candidate_root = _private_child(candidate_directory.parent,
                                    candidate_directory.name)
    execution_root = _private_child(candidate_root, execution_sha256)
    parent_fd = open_existing_workspace(execution_root.parent)
    directory_fd = os.open(execution_root.name,
                           os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=parent_fd)
    try:
        initial = os.fstat(directory_fd)
        linked = os.stat(execution_root.name, dir_fd=parent_fd,
                         follow_symlinks=False)
        if ((initial.st_dev, initial.st_ino) != (linked.st_dev, linked.st_ino)
                or initial.st_uid != os.getuid()
                or stat.S_IMODE(initial.st_mode) != 0o700):
            raise ValueError('owned_candidate_execution_directory_changed')
        try:
            for name in artifact_names:
                _write_private_child(directory_fd, name, file_bytes[name])
            _write_private_child(directory_fd, 'manifest.json', manifest_bytes)
        except FileExistsError:
            loaded, checksum = load_candidate_execution_bundle(
                candidate_directory, execution_sha256)
            if (checksum != hashlib.sha256(manifest_bytes).hexdigest()
                    or loaded['manifest'] != manifest):
                raise ValueError('owned_candidate_execution_bundle_conflict')
    finally:
        current = os.fstat(directory_fd)
        linked = os.stat(execution_root.name, dir_fd=parent_fd,
                         follow_symlinks=False)
        changed = ((current.st_dev, current.st_ino, current.st_mode, current.st_uid)
                   != (linked.st_dev, linked.st_ino, linked.st_mode, linked.st_uid))
        os.close(directory_fd)
        os.close(parent_fd)
        if changed:
            raise ValueError('owned_candidate_execution_directory_changed')
    return execution_root, hashlib.sha256(manifest_bytes).hexdigest()


def load_candidate_execution_bundle(candidate_directory: Path,
                                    execution_sha256: str) -> tuple[dict, str]:
    if re.fullmatch(r'[a-f0-9]{64}', execution_sha256 or '') is None:
        raise ValueError('owned_candidate_execution_hash_invalid')
    execution_root = candidate_directory / execution_sha256
    parent_fd = open_existing_workspace(execution_root.parent)
    directory_fd = os.open(execution_root.name,
                           os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=parent_fd)
    try:
        metadata = os.fstat(directory_fd)
        linked_directory = os.stat(execution_root.name, dir_fd=parent_fd,
                                   follow_symlinks=False)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('owned_candidate_execution_directory_invalid')
        if ((metadata.st_dev, metadata.st_ino)
                != (linked_directory.st_dev, linked_directory.st_ino)):
            raise ValueError('owned_candidate_execution_directory_changed')
        manifest_bytes = _read_private_child(directory_fd, 'manifest.json', 16384)
        manifest = json.loads(manifest_bytes)
        from .dataset import validator
        try:
            validator('owned_candidate_execution_bundle').validate(manifest)
        except Exception as error:
            raise ValueError('owned_candidate_execution_manifest_invalid') from error
        expected_v1 = {'schema_version', 'synthetic', 'mode',
                    'candidate_sha256', 'candidate_execution_sha256',
                    'preview_sha256',
                    'source_run_id', 'source_run_ref', 'source_invocation_sha256',
                    'source_group_sha256', 'parameter_variant_sha256',
                    'invocation_sha256', 'recipe_sha256', 'admission_sha256',
                    'files', 'independent_held_out',
                    'dataset_ingestion_authorized', 'training_ready',
                    'activation_authorized'}
        expected_v11 = expected_v1 | {'review_sha256'}
        expected_v12 = expected_v11 | {'release_sha256', 'selection_sha256'}
        expected_v13 = expected_v12 | {'reuse_admission_sha256'}
        expected_v14 = expected_v13 | {'planning_bundle_sha256'}
        expected_v15 = expected_v13 | {'adapter_admission_sha256'}
        if (canonical(manifest).encode() != manifest_bytes
                or set(manifest) != {'1.0': expected_v1, '1.1': expected_v11,
                                     '1.2': expected_v12, '1.3': expected_v13,
                                     '1.4': expected_v14, '1.5': expected_v15}.get(manifest.get('schema_version'))
                or manifest['schema_version'] not in {'1.0', '1.1', '1.2', '1.3', '1.4', '1.5'}
                or (manifest['schema_version'] in {'1.1', '1.2', '1.3', '1.4', '1.5'}
                    and re.fullmatch(r'[a-f0-9]{64}', manifest.get('review_sha256', '')) is None)
                or (manifest['schema_version'] in {'1.2', '1.4', '1.5'}
                    and any(re.fullmatch(r'[a-f0-9]{64}', manifest.get(key, '')) is None
                            for key in ('release_sha256', 'selection_sha256')))
                or (manifest['schema_version'] in {'1.3', '1.4', '1.5'}
                    and any(re.fullmatch(r'[a-f0-9]{64}', manifest.get(key, '')) is None
                            for key in ('release_sha256', 'selection_sha256',
                                        'reuse_admission_sha256')))
                or (manifest['schema_version'] == '1.4'
                    and re.fullmatch(r'[a-f0-9]{64}',
                                     manifest.get('planning_bundle_sha256', '')) is None)
                or (manifest['schema_version'] == '1.5'
                    and re.fullmatch(r'[a-f0-9]{64}',
                                     manifest.get('adapter_admission_sha256', '')) is None)
                or manifest['synthetic'] is not True
                or manifest['mode'] != 'owned_candidate_development'
                or manifest['candidate_execution_sha256'] != execution_sha256
                or re.fullmatch(r'[a-f0-9]{64}', manifest['preview_sha256'] or '') is None
                or digest({'preview_sha256': manifest['preview_sha256'],
                           'admission_sha256': manifest['admission_sha256'],
                           'source_run_ref': manifest['source_run_ref'],
                           **({'release_sha256': manifest['release_sha256'],
                               'selection_sha256': manifest['selection_sha256']}
                              if manifest['schema_version'] in {'1.2', '1.3', '1.4', '1.5'} else {}),
                           **({'reuse_admission_sha256': manifest['reuse_admission_sha256']}
                              if manifest['schema_version'] in {'1.3', '1.4', '1.5'} else {}),
                           **({'planning_bundle_sha256': manifest['planning_bundle_sha256']}
                              if manifest['schema_version'] == '1.4' else {}),
                           **({'adapter_admission_sha256': manifest['adapter_admission_sha256']}
                              if manifest['schema_version'] == '1.5' else {})}) != execution_sha256
                or manifest['source_run_ref'] != digest({'run_id': manifest['source_run_id']})
                or manifest['independent_held_out'] is not False
                or manifest['dataset_ingestion_authorized'] is not False
                or manifest['training_ready'] is not False
                or manifest['activation_authorized'] is not False):
            raise ValueError('owned_candidate_execution_manifest_invalid')
        expected_names = {
            'candidate.json', 'validation-plan.json', 'case-inputs.json',
            'form-plan.json', 'state-plan.json', 'field-bindings.json',
            'recipe.json', 'invocation.json', 'admission.json', 'form-body.bin',
            'state-after.bin'}
        if manifest['schema_version'] in {'1.3', '1.4', '1.5'}:
            expected_names.add('reuse-admission.json')
        if manifest['schema_version'] == '1.4':
            expected_names.add('planning-bundle.json')
        if manifest['schema_version'] == '1.5':
            expected_names.add('adapter-admission.json')
        if (not isinstance(manifest['files'], dict)
                or set(manifest['files']) != expected_names):
            raise ValueError('owned_candidate_execution_file_inventory_invalid')
        names = set(os.listdir(directory_fd))
        if names not in (expected_names | {'manifest.json'},
                         expected_names | {'manifest.json', 'completion.json'}):
            raise ValueError('owned_candidate_execution_directory_inventory_invalid')
        raw_files = {}
        for filename, checksum in manifest['files'].items():
            if re.fullmatch(r'[a-f0-9]{64}', checksum or '') is None:
                raise ValueError('owned_candidate_execution_file_pin_invalid')
            limit = 131072 if filename == 'planning-bundle.json' else 65536
            content = _read_private_child(directory_fd, filename, limit)
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError('owned_candidate_execution_file_changed')
            raw_files[filename] = content
        values = {}
        for filename, content in raw_files.items():
            if filename.endswith('.json'):
                value = json.loads(content)
                if canonical(value).encode() != content:
                    raise ValueError('owned_candidate_execution_json_not_canonical')
                values[filename[:-5]] = value
            else:
                values[filename[:-4]] = content
        if (values['admission']['invocation_sha256'] != manifest['invocation_sha256']
                or values['admission']['candidate_sha256'] != manifest['candidate_sha256']
                or digest(values['admission']) != manifest['admission_sha256']
                or values['candidate']['source_run_ref'] != manifest['source_run_ref']
                or values['candidate']['source_group_sha256'] != manifest['source_group_sha256']
                or values['candidate']['recipe']['skill_sha256']
                != values['admission']['skill_sha256']
                or values['invocation']['recipe_sha256'] != manifest['recipe_sha256']):
            raise ValueError('owned_candidate_execution_bundle_binding_changed')
        candidate_digest = digest(values['candidate'])
        invocation_digest = digest(values['invocation'])
        recipe_digest = digest(values['recipe'])
        form_plan_digest = digest(values['form-plan'])
        state_plan_digest = digest(values['state-plan'])
        admission = values['admission']
        invocation = values['invocation']
        plan_digest = digest(values['validation-plan'])
        inputs_digest = digest(values['case-inputs'])
        if (candidate_digest != manifest['candidate_sha256']
                or invocation_digest != manifest['invocation_sha256']
                or recipe_digest != manifest['recipe_sha256']
                or form_plan_digest != invocation['form_plan_sha256']
                or state_plan_digest != invocation['state_plan_sha256']
                or plan_digest != invocation['skill_plan_sha256']
                or inputs_digest != invocation['case_inputs_sha256']
                or values['case-inputs']['plan_sha256'] != plan_digest
                or hashlib.sha256(values['form-body']).hexdigest()
                != values['form-plan']['body_sha256']
                or len(values['form-body']) != values['form-plan']['body_bytes']
                or hashlib.sha256(values['state-after']).hexdigest()
                != values['state-plan']['expected_after_sha256']
                or (admission['candidate_sha256'] != manifest['candidate_sha256']
                    or admission['invocation_sha256'] != manifest['invocation_sha256']
                    or admission['parameter_variant_sha256']
                    != manifest['parameter_variant_sha256'])):
            raise ValueError('owned_candidate_execution_bundle_content_mismatch')
        expected_preview = {
            'candidate_sha256': manifest['candidate_sha256'],
            'source_run_ref': manifest['source_run_ref'],
            'source_invocation_sha256': manifest['source_invocation_sha256'],
            'source_group_sha256': manifest['source_group_sha256'],
            'profile_sha256': values['candidate']['profile_sha256'],
            'skill_sha256': invocation['skill_sha256'],
            'case_key': invocation['case_key'],
            'parameter_variant_sha256': manifest['parameter_variant_sha256'],
            'invocation_sha256': manifest['invocation_sha256'],
            'recipe_sha256': manifest['recipe_sha256'],
            'steps': [{'step_key': step['step_key'], 'operation': step['operation']}
                      for step in invocation['steps']],
            'purpose': 'development_variation', 'independent_held_out': False,
            'schema_version': manifest['schema_version'],
            'available': True, 'status': 'preview',
            'form_plan_sha256': form_plan_digest,
            'state_plan_sha256': state_plan_digest, 'report': None,
        }
        if manifest['schema_version'] in {'1.1', '1.2', '1.3', '1.4', '1.5'}:
            expected_preview['review_sha256'] = manifest['review_sha256']
        if manifest['schema_version'] in {'1.2', '1.4', '1.5'}:
            expected_preview['release_sha256'] = manifest['release_sha256']
            expected_preview['selection_sha256'] = manifest['selection_sha256']
        if manifest['schema_version'] in {'1.3', '1.4', '1.5'}:
            expected_preview['release_sha256'] = manifest['release_sha256']
            expected_preview['selection_sha256'] = manifest['selection_sha256']
            expected_preview['reuse_admission_sha256'] = manifest['reuse_admission_sha256']
            from .owned_skill_reuse_admission import validate_owned_skill_reuse_admission
            reuse_admission = values['reuse-admission']
            validate_owned_skill_reuse_admission(reuse_admission)
            reuse_preview = reuse_admission['preview']
            if (digest(reuse_admission) != manifest['reuse_admission_sha256']
                    or reuse_preview['candidate_sha256'] != manifest['candidate_sha256']
                    or reuse_preview['source_run_ref'] != manifest['source_run_ref']
                    or reuse_preview['source_invocation_sha256']
                    != manifest['source_invocation_sha256']
                    or reuse_preview['source_manifest_sha256']
                    != values['candidate']['source_context']['manifest_sha256']
                    or ('source_fingerprint_sha256' in values['candidate']
                        and reuse_preview['source_fingerprint_sha256']
                        != values['candidate']['source_fingerprint_sha256'])
                    or reuse_preview['recipe_sha256']
                    != digest(values['candidate']['recipe'])
                    or reuse_preview['review_sha256'] != manifest['review_sha256']
                    or reuse_preview['release_sha256'] != manifest['release_sha256']
                    or reuse_preview['selection_sha256'] != manifest['selection_sha256']):
                raise ValueError('owned_candidate_execution_reuse_admission_changed')
        if manifest['schema_version'] == '1.4':
            planning_bundle = values['planning-bundle']
            case_key, development_value, steps = _planning_execution_values(
                values['candidate'], values['case-inputs'], values['invocation'],
                values['field-bindings'], values['form-body'])
            assert_planning_execution_binding(
                planning_bundle, manifest=manifest,
                reuse_admission=values['reuse-admission'],
                candidate=values['candidate'], case_key=case_key,
                development_value=development_value, steps=steps)
            expected_preview['planning_bundle_sha256'] = manifest[
                'planning_bundle_sha256']
        if manifest['schema_version'] == '1.5':
            adapter_admission = values['adapter-admission']
            expected_preview['adapter_admission_sha256'] = manifest[
                'adapter_admission_sha256']
            base_preview = dict(expected_preview)
            base_preview['schema_version'] = '1.3'
            base_preview.pop('adapter_admission_sha256', None)
            case_key, development_value, _steps = _planning_execution_values(
                values['candidate'], values['case-inputs'], values['invocation'],
                values['field-bindings'], values['form-body'])
            from .owned_adapter_admission import assert_runtime_execution_binding
            assert_runtime_execution_binding(
                adapter_admission, manifest=manifest,
                reuse_admission=values['reuse-admission'],
                base_preview=base_preview, case_key=case_key,
                development_value=development_value)
            if digest(expected_preview) != manifest['preview_sha256']:
                raise ValueError('owned_candidate_execution_preview_pin_changed')
        if digest(expected_preview) != manifest['preview_sha256']:
            raise ValueError('owned_candidate_execution_preview_pin_changed')
        values['manifest'] = manifest
        values['manifest_sha256'] = hashlib.sha256(manifest_bytes).hexdigest()
        if 'completion.json' in names:
            content = _read_private_child(directory_fd, 'completion.json', 4096)
            completion = json.loads(content)
            try:
                validator('owned_candidate_execution_completion').validate(completion)
            except Exception as error:
                raise ValueError('owned_candidate_execution_completion_invalid') from error
            if (canonical(completion).encode() != content
                    or set(completion) != {'schema_version', 'synthetic', 'mode',
                        'candidate_execution_sha256', 'candidate_sha256',
                        'invocation_sha256', 'job_id', 'run_id', 'run_ref', 'status'}
                    or completion['schema_version'] != '1.0'
                    or completion['synthetic'] is not True
                    or completion['mode'] != 'owned_candidate_development'
                    or completion['candidate_execution_sha256'] != execution_sha256
                    or completion['candidate_sha256'] != manifest['candidate_sha256']
                    or completion['invocation_sha256'] != manifest['invocation_sha256']
                    or completion['run_ref'] != digest({'run_id': completion['run_id']})
                    or completion['status'] != 'succeeded'):
                raise ValueError('owned_candidate_execution_completion_invalid')
            values['completion'] = completion
        return values, values['manifest_sha256']
    finally:
        try:
            current = os.fstat(directory_fd)
            linked = os.stat(execution_root.name, dir_fd=parent_fd,
                             follow_symlinks=False)
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_mtime_ns',
                      'st_ctime_ns')
            if any(getattr(current, field) != getattr(linked, field)
                   for field in fields):
                raise ValueError('owned_candidate_execution_directory_changed')
        finally:
            os.close(directory_fd)
            os.close(parent_fd)


def load_candidate_execution_replay_inventory(owned_root: Path, *,
                                               source_run_ref: str,
                                               source_invocation_sha256: str,
                                               source_manifest_sha256: str,
                                               allow_missing: bool = False) -> dict:
    """Read burned previews, not execution success or permission to reopen a source."""
    if (type(allow_missing) is not bool
            or any(not isinstance(value, str)
                   or re.fullmatch(r'[a-f0-9]{64}', value) is None
                   for value in (source_run_ref, source_invocation_sha256,
                                 source_manifest_sha256))):
        raise ValueError('owned_candidate_replay_source_invalid')
    owned_root = Path(owned_root)
    source_fd = open_existing_workspace(owned_root)
    inventory_fd = None
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_mtime_ns', 'st_ctime_ns')

    def identity(metadata):
        return tuple(getattr(metadata, field) for field in fields)

    def private_directory(metadata):
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ValueError('owned_candidate_replay_directory_invalid')

    try:
        source_metadata = os.fstat(source_fd)
        private_directory(source_metadata)
        try:
            inventory_fd = os.open('candidate-execution-bundles',
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=source_fd)
        except FileNotFoundError:
            if not allow_missing:
                raise ValueError('owned_candidate_replay_inventory_missing') from None
            return {'direct': frozenset(), 'selected': frozenset()}
        inventory_metadata = os.fstat(inventory_fd)
        private_directory(inventory_metadata)
        entries = {}
        with os.scandir(inventory_fd) as iterator:
            for entry in iterator:
                if (len(entries) >= 256
                        or re.fullmatch(r'[a-f0-9]{64}', entry.name) is None):
                    raise ValueError('owned_candidate_replay_inventory_invalid')
                metadata = entry.stat(follow_symlinks=False)
                private_directory(metadata)
                entries[entry.name] = identity(metadata)
        consumed = {'direct': set(), 'selected': set()}
        for execution_sha256 in sorted(entries):
            loaded, _checksum = load_candidate_execution_bundle(
                owned_root / 'candidate-execution-bundles', execution_sha256)
            manifest = loaded['manifest']
            context = loaded['candidate'].get('source_context')
            if (manifest['source_run_ref'] != source_run_ref
                    or manifest['source_invocation_sha256'] != source_invocation_sha256
                    or not isinstance(context, dict)
                    or context.get('manifest_sha256') != source_manifest_sha256
                    or context.get('invocation_sha256') != source_invocation_sha256):
                raise ValueError('owned_candidate_replay_source_changed')
            lane = 'selected' if manifest['schema_version'] in {
                '1.2', '1.3', '1.4', '1.5'} else 'direct'
            preview_sha256 = manifest['preview_sha256']
            if preview_sha256 in consumed['direct'] | consumed['selected']:
                raise ValueError('owned_candidate_replay_duplicate_preview')
            consumed[lane].add(preview_sha256)
        for name, initial_identity in entries.items():
            if identity(os.stat(name, dir_fd=inventory_fd,
                                follow_symlinks=False)) != initial_identity:
                raise ValueError('owned_candidate_replay_inventory_changed')
        if (identity(os.fstat(inventory_fd)) != identity(inventory_metadata)
                or identity(os.stat('candidate-execution-bundles', dir_fd=source_fd,
                                    follow_symlinks=False)) != identity(inventory_metadata)):
            raise ValueError('owned_candidate_replay_inventory_changed')
        return {lane: frozenset(previews) for lane, previews in consumed.items()}
    finally:
        try:
            current_fd = open_existing_workspace(owned_root)
            try:
                if (identity(os.fstat(current_fd)) != identity(source_metadata)
                        or identity(os.fstat(source_fd)) != identity(source_metadata)):
                    raise ValueError('owned_candidate_replay_source_directory_changed')
            finally:
                os.close(current_fd)
        finally:
            if inventory_fd is not None:
                os.close(inventory_fd)
            os.close(source_fd)


def persist_candidate_execution_completion(bundle_directory: Path,
                                           execution_sha256: str,
                                           candidate_sha256: str,
                                           invocation_sha256: str,
                                           job_id: str,
                                           run_id: str) -> None:
    if (not isinstance(run_id, str) or not run_id.startswith('run-')
            or not isinstance(job_id, str) or not job_id.startswith('job-')):
        raise ValueError('owned_candidate_execution_run_invalid')
    manifest_values, _checksum = load_candidate_execution_bundle(
        bundle_directory.parent, execution_sha256)
    manifest = manifest_values['manifest']
    completion = {
        'schema_version': '1.0', 'synthetic': True,
        'mode': 'owned_candidate_development',
        'candidate_execution_sha256': execution_sha256,
        'candidate_sha256': candidate_sha256,
        'invocation_sha256': invocation_sha256,
        'job_id': job_id, 'run_id': run_id, 'run_ref': digest({'run_id': run_id}),
        'status': 'succeeded'}
    from .dataset import validator
    try:
        validator('owned_candidate_execution_completion').validate(completion)
    except Exception as error:
        raise ValueError('owned_candidate_execution_completion_invalid') from error
    if (manifest['candidate_sha256'] != candidate_sha256
            or manifest['invocation_sha256'] != invocation_sha256):
        raise ValueError('owned_candidate_execution_completion_binding_changed')
    directory_fd = open_existing_workspace(bundle_directory)
    try:
        metadata = os.fstat(directory_fd)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('owned_candidate_execution_directory_invalid')
        _write_private_child(directory_fd, 'completion.json',
                             canonical(completion).encode('utf-8'))
    finally:
        os.close(directory_fd)


def audit_persisted_candidate_execution(bundle_directory: Path,
                                        execution_sha256: str, *,
                                        candidate_session, database: Path,
                                        _audited_snapshot=None,
                                        _allow_reviewed_base_report=False):
    from .site_skill import SiteSkillStore
    from .site_skill_case_binding import SiteSkillCaseInputs, SiteSkillFormFieldBinding
    from .site_skill_form_recipe import (SiteSkillFormRecipe,
                                         SiteSkillFormRecipeInvocation)
    from .site_skill_form_recipe_candidate import parse_site_skill_form_recipe_candidate
    from .site_skill_form_recipe_candidate_execution import (
        CandidateExecutionAdmission, audit_candidate_execution)
    from .site_skill_validation import SiteSkillValidationPlan
    from .web_https_form_state_probe import WebHTTPSFormStatePlan
    from .web_https_form_transport import WebHTTPSFormPlan
    from .web_application_binding import WebTaskContract

    loaded, _manifest_sha256 = load_candidate_execution_bundle(
        bundle_directory, execution_sha256)
    manifest = loaded['manifest']
    if manifest['schema_version'] == '1.5' and _audited_snapshot is None:
        from .remote_form_learning_source import audit_snapshot

        with audit_snapshot(database) as audited_snapshot:
            return audit_persisted_candidate_execution(
                bundle_directory, execution_sha256,
                candidate_session=candidate_session, database=database,
                _audited_snapshot=audited_snapshot,
                _allow_reviewed_base_report=True)
    if manifest['schema_version'] == '1.5' and _allow_reviewed_base_report is not True:
        raise ValueError('owned_adapter_execution_requires_runtime_audit')
    if (manifest.get('schema_version') in {'1.1', '1.2', '1.3', '1.4', '1.5'}
            and _allow_reviewed_base_report is not True):
        raise ValueError('reviewed_candidate_execution_requires_review_audit')
    candidate, source = candidate_session.execution_source(
        manifest['source_run_id'], manifest['source_invocation_sha256'],
        manifest['candidate_sha256'])
    if (manifest['source_run_ref'] != candidate['source_run_ref']
            or manifest['source_group_sha256'] != candidate['source_group_sha256']
            or manifest['candidate_execution_sha256'] != execution_sha256):
        raise ValueError('owned_candidate_execution_source_binding_changed')
    parse_site_skill_form_recipe_candidate(candidate)
    plan = SiteSkillValidationPlan.model_validate_json(canonical(
        loaded['validation-plan']))
    inputs = SiteSkillCaseInputs.model_validate_json(canonical(
        loaded['case-inputs']))
    form_plan = WebHTTPSFormPlan.model_validate_json(canonical(loaded['form-plan']))
    state_plan = WebHTTPSFormStatePlan.model_validate_json(canonical(
        loaded['state-plan']))
    task = WebTaskContract.model_validate_json(canonical(source['task'].model_dump(mode='json')))
    bindings = [SiteSkillFormFieldBinding.model_validate_json(canonical(item))
                for item in loaded['field-bindings']]
    recipe = SiteSkillFormRecipe.model_validate_json(canonical(loaded['recipe']))
    invocation = SiteSkillFormRecipeInvocation.model_validate_json(
        canonical(loaded['invocation']))
    admission = CandidateExecutionAdmission.model_validate_json(
        canonical(loaded['admission']))
    skill_store = SiteSkillStore(
        candidate_session.directory / 'candidate-executions'
        / manifest['candidate_sha256'] / 'skills',
        candidate_session.profiles, candidate_session.pages)
    completion = loaded.get('completion')
    if completion is None:
        raise ValueError('owned_candidate_execution_not_completed')
    report = audit_candidate_execution(
        admission, candidate_session.candidate_directory,
        run_id=completion['run_id'], database=database,
        _audited_snapshot=_audited_snapshot,
        profiles=candidate_session.profiles, pages=candidate_session.pages,
        source_parameters=source['source_parameters'], store=skill_store,
        plan=plan, inputs=inputs, case_key=invocation.case_key,
        task=task, form_plan=form_plan, state_plan=state_plan,
        field_bindings=bindings, recipe=recipe,
        invocation=invocation.model_dump(mode='json'),
        owned_source_directory=candidate_session.directory,
        owned_source_manifest_sha256=candidate_session.manifest_sha256)
    if manifest['schema_version'] == '1.5':
        from .owned_adapter_admission import verify_runtime_admission

        execution = {
            'candidate_execution_sha256': manifest['candidate_execution_sha256'],
            'preview_sha256': manifest['preview_sha256'],
            'adapter_admission_sha256': manifest['adapter_admission_sha256'],
            'reuse_admission_sha256': manifest['reuse_admission_sha256'],
            'candidate_sha256': manifest['candidate_sha256'],
            'invocation_sha256': manifest['invocation_sha256'],
            'job_id': completion['job_id'],
            'run_id': completion['run_id'],
        }
        connection, _snapshot_identity = _audited_snapshot
        if not verify_runtime_admission(
                connection, execution, loaded['adapter-admission'],
                loaded['reuse-admission']):
            raise ValueError('owned_adapter_runtime_admission_audit_failed')
    return report
