"""No-network private onboarding for one explicitly scoped HTTPS form task."""

import hashlib
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from .contracts import canonical, digest
from .local_app import private_read
from .web_application import WebApplicationProfiles, canonical_origin
from .web_application_binding import WebTaskContract
from .web_https_form_state_probe import (WebHTTPSFormStatePlan,
                                         plan_web_https_form_state)
from .web_https_form_transport import (ExactHTTPSFormTransport, WebHTTPSFormPlan,
                                       exact_form_fields,
                                       form_body, parse_form_fields_document,
                                       plan_web_https_form)
from .web_task_draft import write_private_content


def preview_form_task(profiles: WebApplicationProfiles, profile_sha256: str,
                      task_key: str, verification_ref: str,
                      state_readback: bool = False) -> tuple[WebTaskContract, str]:
    if type(state_readback) is not bool:
        raise ValueError('form_task_state_readback_invalid')
    profile = profiles.get(profile_sha256)
    origin, local = canonical_origin(profile.entry_url)
    if (profile.environment not in {'staging', 'production'} or local
            or task_key not in profile.task_keys):
        raise ValueError('form_task_outside_profile')
    task = WebTaskContract(
        profile_sha256=profile_sha256, task_key=task_key,
        entry_url=profile.entry_url, allowed_origins=[origin],
        tools=['browser.navigate', 'browser.snapshot', 'browser.fill',
               'browser.click', 'browser.verify'],
        max_pages=2, max_navigations=2,
        max_actions=6 if state_readback else 4, max_seconds=300,
        verification_ref=verification_ref)
    return task, digest(task.model_dump())


def register_form_task(profiles: WebApplicationProfiles, task_root: Path,
                       profile_sha256: str, task_key: str, verification_ref: str,
                       confirm_sha256: str,
                       state_readback: bool = False) -> tuple[str, Path]:
    task, checksum = preview_form_task(profiles, profile_sha256, task_key,
                                       verification_ref, state_readback)
    if confirm_sha256 != checksum:
        raise ValueError('form_task_confirmation_mismatch')
    return checksum, write_private_content(task_root, checksum,
                                           canonical(task.model_dump(mode='json')).encode())


def preview_form_plan(profiles: WebApplicationProfiles, task_root: Path,
                      profile_sha256: str, task_sha256: str, submit_url: str,
                      receipt_url: str, field_name: str | None, value: str | None,
                      *, fields: list[dict] | None = None
                      ) -> tuple[WebHTTPSFormPlan, str]:
    content = private_read(task_root / (task_sha256 + '.json'))
    task = WebTaskContract.model_validate_json(content)
    if (content != canonical(task.model_dump(mode='json')).encode()
            or hashlib.sha256(content).hexdigest() != task_sha256
            or task.profile_sha256 != profile_sha256
            or not {'browser.fill', 'browser.click'}.issubset(task.tools)):
        raise ValueError('form_task_changed')
    ordered_fields = exact_form_fields(field_name, value, fields)
    if any(not content.isprintable() for _name, content in ordered_fields):
        raise ValueError('form_value_invalid')
    body = form_body(ordered_fields)
    plan = plan_web_https_form(
        profiles, task, submit_url=submit_url, receipt_url=receipt_url,
        body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body))
    return plan, digest(plan.model_dump())


def register_form_plan(profiles: WebApplicationProfiles, task_root: Path,
                       plan_root: Path, value_root: Path, profile_sha256: str,
                       task_sha256: str, submit_url: str, receipt_url: str,
                       field_name: str | None, value: str | None, confirm_sha256: str,
                       *, fields: list[dict] | None = None
                       ) -> tuple[str, Path, Path, WebHTTPSFormPlan]:
    plan, checksum = preview_form_plan(
        profiles, task_root, profile_sha256, task_sha256,
        submit_url, receipt_url, field_name, value, fields=fields)
    if confirm_sha256 != checksum:
        raise ValueError('form_plan_confirmation_mismatch')
    if fields is None:
        content = value.encode('utf-8')
        suffix = '.txt'
    else:
        document = {'schema_version': '1.0', 'fields': fields,
                    'body_sha256': plan.body_sha256, 'body_bytes': plan.body_bytes,
                    'execution_authorized': False, 'collection_authorized': False}
        content = canonical(document).encode()
        parse_form_fields_document(content)
        suffix = '.json'
    plan_path = write_private_content(
        plan_root, checksum, canonical(plan.model_dump(mode='json')).encode())
    value_path = write_private_content(
        value_root, hashlib.sha256(content).hexdigest(), content, suffix=suffix)
    return checksum, plan_path, value_path, plan


def preview_form_state_plan(profiles: WebApplicationProfiles, task_root: Path,
                            plan_root: Path, profile_sha256: str,
                            task_sha256: str, form_plan_sha256: str,
                            state_url: str, before_sha256: str, after_sha256: str,
                            marker_id: str | None = None,
                            before_marker_sha256: str | None = None,
                            after_marker_sha256: str | None = None,
                            submitted_field_name: str | None = None
                            ) -> tuple[WebHTTPSFormStatePlan, str]:
    task_content = private_read(task_root / (task_sha256 + '.json'))
    task = WebTaskContract.model_validate_json(task_content)
    form_content = private_read(plan_root / (form_plan_sha256 + '.json'))
    form_plan = WebHTTPSFormPlan.model_validate_json(form_content)
    if (task_content != canonical(task.model_dump(mode='json')).encode()
            or hashlib.sha256(task_content).hexdigest() != task_sha256
            or form_content != canonical(form_plan.model_dump(mode='json')).encode()
            or hashlib.sha256(form_content).hexdigest() != form_plan_sha256
            or task.profile_sha256 != profile_sha256
            or form_plan.profile_sha256 != profile_sha256
            or form_plan.task_sha256 != task_sha256):
        raise ValueError('form_state_source_changed')
    public = not (urlsplit(form_plan.entry_url).hostname or '').endswith('.invalid')
    transport = ExactHTTPSFormTransport(
        profiles, task, form_plan, form_plan_sha256,
        consume_approval=lambda _request_sha256: False,
        tls_context=ssl.create_default_context(),
        confirm_public_plan_sha256=form_plan_sha256 if public else None)
    state_plan = plan_web_https_form_state(
        transport, state_url=state_url,
        expected_before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        marker_id=marker_id,
        expected_before_marker_sha256=before_marker_sha256,
        expected_after_marker_sha256=after_marker_sha256,
        submitted_field_name=submitted_field_name)
    return state_plan, digest(state_plan.model_dump())


def register_form_state_plan(profiles: WebApplicationProfiles, task_root: Path,
                             plan_root: Path, state_root: Path,
                             profile_sha256: str, task_sha256: str,
                             form_plan_sha256: str, state_url: str,
                             before_sha256: str, after_sha256: str,
                             confirm_sha256: str,
                             marker_id: str | None = None,
                             before_marker_sha256: str | None = None,
                             after_marker_sha256: str | None = None,
                             submitted_field_name: str | None = None
                             ) -> tuple[str, Path, WebHTTPSFormStatePlan]:
    state_plan, checksum = preview_form_state_plan(
        profiles, task_root, plan_root, profile_sha256, task_sha256,
        form_plan_sha256, state_url, before_sha256, after_sha256,
        marker_id, before_marker_sha256, after_marker_sha256, submitted_field_name)
    if confirm_sha256 != checksum:
        raise ValueError('form_state_confirmation_mismatch')
    path = write_private_content(
        state_root, checksum, canonical(state_plan.model_dump(mode='json')).encode())
    return checksum, path, state_plan
