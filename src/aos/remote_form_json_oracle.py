"""Explicit post-run JSON readback for one audited HTTPS form transport."""

import argparse
import hashlib
import json
import math
import re
import sqlite3
import ssl
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .dataset_audit import audit_snapshot
from .local_app import private_read
from .remote_form_learning_source import inspect_remote_form_learning_source
from .web_application import Checksum, WebApplicationProfile, WebApplicationProfiles, canonical_origin
from .web_https_form_transport import WebHTTPSFormPlan
from .web_https_preflight import _fetch_response, validate_https_cookie_header


class RemoteFormJSONOraclePlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    profile_sha256: Checksum
    form_plan_sha256: Checksum
    run_ref: Checksum
    source_snapshot_sha256: Checksum
    url: str = Field(min_length=1, max_length=2048)
    field_key: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    expected_value_sha256: Checksum
    cookie_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    method: Literal['GET'] = 'GET'
    status: Literal['draft'] = 'draft'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False

    @field_validator('url')
    @classmethod
    def canonical_url(cls, value):
        WebApplicationProfile.entry_is_canonical(value)
        return value

    @field_validator('execution_authorized', 'collection_authorized', mode='before')
    @classmethod
    def cannot_authorize(cls, value):
        if value is not False:
            raise ValueError('json_oracle_plan_cannot_authorize')
        return value


class RemoteFormJSONOracleReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['explicit_post_run_json_readback'] = 'explicit_post_run_json_readback'
    plan_sha256: Checksum
    profile_sha256: Checksum
    form_plan_sha256: Checksum
    run_ref: Checksum
    source_snapshot_sha256: Checksum
    request_sha256: Checksum
    response_sha256: Checksum
    response_bytes: int = Field(ge=1, le=65536)
    observed_value_sha256: Checksum
    cookie_sha256: Checksum | None = Field(default=None, exclude_if=lambda value: value is None)
    declared_value_matched: Literal[True] = True
    tls_hostname_verified: Literal[True] = True
    browser_connected: Literal[False] = False
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False


def _source_and_form(database: Path, run_id: str, profiles: Path,
                     profile_sha256: str, form_plan_sha256: str,
                     cookie_sha256: str | None) -> tuple[dict, WebHTTPSFormPlan]:
    source = inspect_remote_form_learning_source(
        database, run_id, profiles=profiles,
        selected_profile_sha256=profile_sha256,
        selected_plan_sha256=form_plan_sha256,
        selected_cookie_sha256=cookie_sha256)
    with audit_snapshot(database) as (snapshot, identity):
        if identity['sha256'] != source['snapshot_sha256']:
            raise ValueError('json_oracle_source_changed')
        row = snapshot.execute('SELECT plan_json FROM desktop_remote_form_bindings WHERE run_id=?',
                               (run_id,)).fetchone()
        if row is None:
            raise ValueError('json_oracle_form_binding_missing')
        form = WebHTTPSFormPlan.model_validate_json(row['plan_json'])
        if canonical(form.model_dump()) != row['plan_json'] or digest(form.model_dump()) != form_plan_sha256:
            raise ValueError('json_oracle_form_binding_changed')
    return source, form


def plan_remote_form_json_oracle(database: Path, run_id: str, *, profiles: Path,
                                 profile_sha256: str, form_plan_sha256: str,
                                 url: str, field_key: str,
                                 expected_value_sha256: str,
                                 cookie_sha256: str | None = None) -> RemoteFormJSONOraclePlan:
    source, form = _source_and_form(database, run_id, profiles, profile_sha256,
                                  form_plan_sha256, cookie_sha256)
    return _plan_from_source_form(
        source, form, profile_sha256=profile_sha256,
        form_plan_sha256=form_plan_sha256, url=url, field_key=field_key,
        expected_value_sha256=expected_value_sha256, cookie_sha256=cookie_sha256)


def _plan_from_source_form(source: dict, form: WebHTTPSFormPlan, *,
                           profile_sha256: str, form_plan_sha256: str,
                           url: str, field_key: str,
                           expected_value_sha256: str,
                           cookie_sha256: str | None) -> RemoteFormJSONOraclePlan:
    plan = RemoteFormJSONOraclePlan(
        profile_sha256=profile_sha256, form_plan_sha256=form_plan_sha256,
        run_ref=source['run_ref'], source_snapshot_sha256=source['snapshot_sha256'],
        url=url, field_key=field_key, expected_value_sha256=expected_value_sha256,
        cookie_sha256=cookie_sha256)
    origin = canonical_origin(form.entry_url)[0]
    if (canonical_origin(plan.url)[0] != origin
            or plan.url in (form.entry_url, form.submit_url, form.receipt_url)
            or source['transport_readback_verified'] is not True):
        raise ValueError('json_oracle_outside_audited_form_scope')
    return plan


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('json_oracle_duplicate_field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('json_oracle_nonfinite_number')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('json_oracle_nonfinite_number')
    return number


def observed_json_value_sha256(body: bytes, field_key: str) -> str:
    if type(body) is not bytes or not 1 <= len(body) <= 65536 or not isinstance(field_key, str) or re.fullmatch(
            r'[A-Za-z_][A-Za-z0-9_]{0,63}', field_key) is None:
        raise ValueError('json_oracle_response_input_invalid')
    try:
        payload = json.loads(body.decode('utf-8', 'strict'), object_pairs_hook=_unique_pairs,
                             parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeError, ValueError, TypeError):
        raise ValueError('json_oracle_response_invalid') from None
    if (not isinstance(payload, dict) or field_key not in payload
            or type(payload[field_key]) not in (str, int, float, bool, type(None))):
        raise ValueError('json_oracle_field_missing_or_unsupported')
    return digest({'value': payload[field_key]})


def probe_remote_form_json_oracle(database: Path, run_id: str, *, profiles: Path,
                                  plan: RemoteFormJSONOraclePlan, confirm_plan_sha256: str,
                                  tls_context: ssl.SSLContext | None = None,
                                  cookie_header: str | None = None) -> RemoteFormJSONOracleReport:
    plan = RemoteFormJSONOraclePlan.model_validate(plan.model_dump())
    plan_sha256 = digest(plan.model_dump())
    if confirm_plan_sha256 != plan_sha256:
        raise ValueError('json_oracle_exact_confirmation_required')
    if (plan.cookie_sha256 is None) != (cookie_header is None):
        raise ValueError('json_oracle_cookie_scope_mismatch')
    if cookie_header is not None and hashlib.sha256(
            validate_https_cookie_header(cookie_header).encode('ascii')).hexdigest() != plan.cookie_sha256:
        raise ValueError('json_oracle_cookie_hash_mismatch')
    current = plan_remote_form_json_oracle(
        database, run_id, profiles=profiles, profile_sha256=plan.profile_sha256,
        form_plan_sha256=plan.form_plan_sha256, url=plan.url,
        field_key=plan.field_key, expected_value_sha256=plan.expected_value_sha256,
        cookie_sha256=plan.cookie_sha256)
    if current != plan:
        raise ValueError('json_oracle_source_or_plan_changed')
    _origin, request_url, body = _fetch_response(
        WebApplicationProfiles(profiles), plan.profile_sha256, plan.profile_sha256,
        tls_context=tls_context, target_url=plan.url,
        content_type='application/json', max_response_bytes=65536,
        cookie_header=cookie_header)
    observed_value_sha256 = observed_json_value_sha256(body, plan.field_key)
    if observed_value_sha256 != plan.expected_value_sha256:
        raise ValueError('json_oracle_declared_value_differs')
    if plan_remote_form_json_oracle(
            database, run_id, profiles=profiles, profile_sha256=plan.profile_sha256,
            form_plan_sha256=plan.form_plan_sha256, url=plan.url,
            field_key=plan.field_key, expected_value_sha256=plan.expected_value_sha256,
            cookie_sha256=plan.cookie_sha256) != plan:
        raise ValueError('json_oracle_source_changed_after_get')
    return RemoteFormJSONOracleReport(
        plan_sha256=plan_sha256, profile_sha256=plan.profile_sha256,
        form_plan_sha256=plan.form_plan_sha256, run_ref=plan.run_ref,
        source_snapshot_sha256=plan.source_snapshot_sha256,
        request_sha256=digest({'method': 'GET', 'url': request_url, **(
            {'cookie_sha256': plan.cookie_sha256} if plan.cookie_sha256 is not None else {})}),
        response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body),
        observed_value_sha256=observed_value_sha256,
        cookie_sha256=plan.cookie_sha256)


def main() -> None:
    parser = argparse.ArgumentParser(description='Explicit post-run HTTPS JSON readback; no site outcome authority',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--profile-sha256', required=True)
    parser.add_argument('--form-plan-sha256', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--field-key', required=True)
    parser.add_argument('--expected-value-sha256', required=True)
    parser.add_argument('--cookie-sha256')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--confirm-plan-sha256')
    parser.add_argument('--probe', action='store_true')
    arguments = parser.parse_args()
    try:
        plan = plan_remote_form_json_oracle(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            profile_sha256=arguments.profile_sha256,
            form_plan_sha256=arguments.form_plan_sha256,
            url=arguments.url, field_key=arguments.field_key,
            expected_value_sha256=arguments.expected_value_sha256,
            cookie_sha256=arguments.cookie_sha256)
        if arguments.probe:
            cookie_header = None
            if plan.cookie_sha256 is not None:
                if arguments.cookie_file is None:
                    raise ValueError('json_oracle_private_cookie_file_required')
                source = arguments.cookie_file.absolute()
                if not source.is_relative_to(REPO_ROOT / 'data'):
                    raise ValueError('json_oracle_cookie_file_outside_private_data')
                cookie_header = private_read(source, 2048).decode('ascii')
            elif arguments.cookie_file is not None:
                raise ValueError('json_oracle_cookie_scope_mismatch')
            report = probe_remote_form_json_oracle(
                arguments.database, arguments.run_id, profiles=arguments.profiles,
                plan=plan, confirm_plan_sha256=arguments.confirm_plan_sha256,
                cookie_header=cookie_header)
            print(canonical(report.model_dump()))
        elif arguments.confirm_plan_sha256 is None and arguments.cookie_file is None:
            print(canonical({'plan': plan.model_dump(), 'plan_sha256': digest(plan.model_dump())}))
        else:
            raise ValueError('json_oracle_confirmation_without_probe')
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, ssl.SSLError):
        parser.exit(1, 'JSON readback unavailable: source, scope, confirmation or HTTPS response failed. No task retried.\n')


if __name__ == '__main__':
    main()
