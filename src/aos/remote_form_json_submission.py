"""Bind an explicit JSON readback to one audited form field value."""

import argparse
import hashlib
from pathlib import Path
import sqlite3
import ssl
from typing import Literal

from pydantic import Field, model_validator

from .contracts import REPO_ROOT, TypedModel, canonical, digest
from .local_app import private_read
from .remote_form_json_oracle import (RemoteFormJSONOraclePlan,
                                      RemoteFormJSONOracleReport,
                                      _plan_from_source_form,
                                      _source_and_form,
                                      probe_remote_form_json_oracle)
from .web_application import Checksum
from .web_https_form_transport import (FIELD_NAME, exact_form_fields,
                                       form_body, parse_form_fields_document)


class RemoteFormJSONSubmissionPlan(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    oracle_plan: RemoteFormJSONOraclePlan
    submitted_field_name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    form_body_sha256: Checksum
    form_body_bytes: int = Field(ge=1, le=4096)
    status: Literal['draft'] = 'draft'
    execution_authorized: Literal[False] = False
    collection_authorized: Literal[False] = False


class RemoteFormJSONSubmissionReport(TypedModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['explicit_submitted_value_json_readback'] = 'explicit_submitted_value_json_readback'
    plan_sha256: Checksum
    oracle_plan_sha256: Checksum
    form_body_sha256: Checksum
    submitted_field_name_sha256: Checksum
    submitted_value_sha256: Checksum
    oracle_report: RemoteFormJSONOracleReport
    submitted_value_readback_bound: Literal[True] = True
    account_verified: Literal[False] = False
    site_outcome_verified: Literal[False] = False
    collection_authorized: Literal[False] = False
    training_ready: Literal[False] = False

    @model_validator(mode='after')
    def readback_identity_matches(self):
        if (self.oracle_report.plan_sha256 != self.oracle_plan_sha256
                or self.oracle_report.observed_value_sha256 != self.submitted_value_sha256):
            raise ValueError('json_submission_report_readback_mismatch')
        return self


def plan_remote_form_json_submission(database: Path, run_id: str, *, profiles: Path,
                                     profile_sha256: str, form_plan_sha256: str,
                                     url: str, field_key: str, submitted_field_name: str,
                                     fields: tuple[tuple[str, str], ...],
                                     cookie_sha256: str | None = None
                                     ) -> RemoteFormJSONSubmissionPlan:
    if (not isinstance(fields, tuple) or not 1 <= len(fields) <= 8
            or not isinstance(submitted_field_name, str)
            or FIELD_NAME.fullmatch(submitted_field_name) is None):
        raise ValueError('json_submission_fields_invalid')
    normalized = (exact_form_fields(*fields[0]) if len(fields) == 1 else
                  exact_form_fields(None, None, fields))
    selected = [value for name, value in normalized if name == submitted_field_name]
    if len(selected) != 1:
        raise ValueError('json_submission_selected_field_missing')
    source, form = _source_and_form(database, run_id, profiles, profile_sha256,
                                    form_plan_sha256, cookie_sha256)
    body = form_body(normalized)
    if (hashlib.sha256(body).hexdigest() != form.body_sha256
            or len(body) != form.body_bytes):
        raise ValueError('json_submission_form_body_changed')
    oracle = _plan_from_source_form(
        source, form, profile_sha256=profile_sha256,
        form_plan_sha256=form_plan_sha256, url=url, field_key=field_key,
        expected_value_sha256=digest({'value': selected[0]}),
        cookie_sha256=cookie_sha256)
    return RemoteFormJSONSubmissionPlan(
        oracle_plan=oracle, submitted_field_name=submitted_field_name,
        form_body_sha256=form.body_sha256, form_body_bytes=form.body_bytes)


def probe_remote_form_json_submission(database: Path, run_id: str, *, profiles: Path,
                                      plan: RemoteFormJSONSubmissionPlan,
                                      confirm_plan_sha256: str,
                                      fields: tuple[tuple[str, str], ...],
                                      cookie_header: str | None = None,
                                      tls_context: ssl.SSLContext | None = None
                                      ) -> RemoteFormJSONSubmissionReport:
    plan = RemoteFormJSONSubmissionPlan.model_validate(plan.model_dump())
    plan_sha256 = digest(plan.model_dump())
    if confirm_plan_sha256 != plan_sha256:
        raise ValueError('json_submission_exact_confirmation_required')
    oracle = plan.oracle_plan
    current = plan_remote_form_json_submission(
        database, run_id, profiles=profiles, profile_sha256=oracle.profile_sha256,
        form_plan_sha256=oracle.form_plan_sha256, url=oracle.url,
        field_key=oracle.field_key, submitted_field_name=plan.submitted_field_name,
        fields=fields, cookie_sha256=oracle.cookie_sha256)
    if current != plan:
        raise ValueError('json_submission_source_or_fields_changed')
    readback = probe_remote_form_json_oracle(
        database, run_id, profiles=profiles, plan=oracle,
        confirm_plan_sha256=digest(oracle.model_dump()),
        tls_context=tls_context, cookie_header=cookie_header)
    return RemoteFormJSONSubmissionReport(
        plan_sha256=plan_sha256, oracle_plan_sha256=digest(oracle.model_dump()),
        form_body_sha256=plan.form_body_sha256,
        submitted_field_name_sha256=digest({'field_name': plan.submitted_field_name}),
        submitted_value_sha256=oracle.expected_value_sha256,
        oracle_report=readback)


def _private_file(path: Path, limit: int) -> bytes:
    source = path.absolute()
    if not source.is_relative_to(REPO_ROOT / 'data'):
        raise ValueError('json_submission_private_file_outside_data')
    return private_read(source, limit)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Explicit post-run JSON readback of one submitted form value; no site authority',
        allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--profile-sha256', required=True)
    parser.add_argument('--form-plan-sha256', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--field-key', required=True)
    parser.add_argument('--submitted-field-name', required=True)
    form_source = parser.add_mutually_exclusive_group(required=True)
    form_source.add_argument('--form-value-file', type=Path)
    form_source.add_argument('--form-fields-file', type=Path)
    parser.add_argument('--cookie-sha256')
    parser.add_argument('--cookie-file', type=Path)
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--confirm-plan-sha256')
    arguments = parser.parse_args()
    try:
        if arguments.form_value_file is not None:
            value = _private_file(arguments.form_value_file, 2048).decode('utf-8', 'strict')
            fields = exact_form_fields(arguments.submitted_field_name, value)
        else:
            fields = parse_form_fields_document(_private_file(arguments.form_fields_file, 8192))
        plan = plan_remote_form_json_submission(
            arguments.database, arguments.run_id, profiles=arguments.profiles,
            profile_sha256=arguments.profile_sha256,
            form_plan_sha256=arguments.form_plan_sha256,
            url=arguments.url, field_key=arguments.field_key,
            submitted_field_name=arguments.submitted_field_name,
            fields=fields, cookie_sha256=arguments.cookie_sha256)
        if arguments.probe:
            cookie_header = None
            if plan.oracle_plan.cookie_sha256 is not None:
                if arguments.cookie_file is None:
                    raise ValueError('json_submission_private_cookie_file_required')
                cookie_header = _private_file(arguments.cookie_file, 2048).decode('ascii')
            elif arguments.cookie_file is not None:
                raise ValueError('json_submission_cookie_scope_mismatch')
            report = probe_remote_form_json_submission(
                arguments.database, arguments.run_id, profiles=arguments.profiles,
                plan=plan, confirm_plan_sha256=arguments.confirm_plan_sha256,
                fields=fields, cookie_header=cookie_header)
            print(canonical(report.model_dump()))
        elif arguments.confirm_plan_sha256 is None and arguments.cookie_file is None:
            print(canonical({'plan': plan.model_dump(),
                             'plan_sha256': digest(plan.model_dump())}))
        else:
            raise ValueError('json_submission_confirmation_without_probe')
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, ssl.SSLError):
        parser.exit(1, 'Submitted-value JSON readback unavailable: source, private input, confirmation or HTTPS response failed. No task retried.\n')


if __name__ == '__main__':
    main()
