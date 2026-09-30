"""Read-only, post-admission repeat measurements for one exact HTTPS form plan."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sqlite3
import ssl
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import TypedModel, canonical, digest
from .dataset import validator
from .dataset_audit import audit_snapshot
from .desktop_tasks import Approval
from .remote_form_learning_source import (STAGES, STATE_STAGES,
                                          inspect_remote_form_learning_source)
from .web_application import WebApplicationProfiles
from .web_application_binding import WebTaskAdmissionDraft, verify_web_task_binding
from .web_https_form_transport import WebHTTPSFormPlan, verify_web_https_form_plan
from .web_https_form_state_probe import WebHTTPSFormStatePlan, verify_web_https_form_state_plan


CHECKSUM = re.compile(r'[a-f0-9]{64}\Z')
TERMINAL = {'succeeded', 'failed', 'cancelled'}


class FormStageTiming(TypedModel):
    samples: int = Field(ge=1, le=32)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)

    @model_validator(mode='after')
    def ordered_percentiles(self):
        if self.p95_ms < self.p50_ms:
            raise ValueError('remote_form_repeat_stage_percentiles_invalid')
        return self


class RemoteFormRepeatReport(TypedModel):
    schema_version: Literal['1.2']
    mode: Literal['read_only_post_admission_form_repeats']
    profile_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    plan_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    deployment_snapshot_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    bound_attempts: int = Field(ge=2, le=32)
    transport_verified: int = Field(ge=0, le=32)
    failed_or_cancelled: int = Field(ge=0, le=32)
    elapsed_p50_ms: float = Field(ge=0)
    elapsed_p95_ms: float = Field(ge=0)
    approval_window_count: int = Field(ge=0, le=192)
    approval_window_sum_ms: float = Field(ge=0)
    elapsed_excluding_approval_p50_ms: float = Field(ge=0)
    elapsed_excluding_approval_p95_ms: float = Field(ge=0)
    first_action_samples: int = Field(ge=0, le=32)
    first_action_p50_ms: float | None = Field(default=None, ge=0)
    first_action_p95_ms: float | None = Field(default=None, ge=0)
    model_call_count: int = Field(ge=0, le=8192)
    model_latency_samples: int = Field(ge=0, le=8192)
    model_latency_sum_ms: float = Field(ge=0)
    post_approval_stage_ms: dict[str, FormStageTiming]
    site_outcome_verified: Literal[False]
    real_site_acceptance: Literal[False]

    @model_validator(mode='after')
    def consistent_counts(self):
        if (self.transport_verified + self.failed_or_cancelled != self.bound_attempts
                or self.first_action_samples > self.bound_attempts
                or self.model_latency_samples > self.model_call_count
                or (self.first_action_samples == 0)
                != (self.first_action_p50_ms is None and self.first_action_p95_ms is None)
                or (self.first_action_p50_ms is None) != (self.first_action_p95_ms is None)
                or self.elapsed_p95_ms < self.elapsed_p50_ms
                or self.elapsed_excluding_approval_p95_ms
                < self.elapsed_excluding_approval_p50_ms
                or self.elapsed_excluding_approval_p50_ms > self.elapsed_p50_ms
                or self.elapsed_excluding_approval_p95_ms > self.elapsed_p95_ms
                or self.approval_window_count > self.bound_attempts * (
                    6 if self.mode == 'read_only_post_admission_form_state_repeats' else 4)
                or self.approval_window_count == 0 and self.approval_window_sum_ms != 0
                or (self.first_action_p50_ms is not None
                    and self.first_action_p95_ms < self.first_action_p50_ms)):
            raise ValueError('remote_form_repeat_counts_invalid')
        expected = [tool for tool, _choice in (
            STATE_STAGES if self.mode == 'read_only_post_admission_form_state_repeats'
            else STAGES)] if self.transport_verified else []
        if (set(self.post_approval_stage_ms) != set(expected)
                or any(stage.samples != self.transport_verified
                       for stage in self.post_approval_stage_ms.values())):
            raise ValueError('remote_form_repeat_stage_samples_invalid')
        return self


class RemoteFormStateRepeatReport(RemoteFormRepeatReport):
    mode: Literal['read_only_post_admission_form_state_repeats']
    state_plan_sha256: str = Field(pattern='^[a-f0-9]{64}$')


def _duration(started: str, ended: str) -> float:
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(ended)
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError('remote_form_repeat_naive_time')
        value = (end - start).total_seconds() * 1000
    except (TypeError, OverflowError) as error:
        raise ValueError('remote_form_repeat_invalid_time') from error
    if not math.isfinite(value) or value < 0:
        raise ValueError('remote_form_repeat_invalid_duration')
    return value


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    return ordered[(len(ordered) * percent + 99) // 100 - 1]


def _post_approval_stage_durations(snapshot: sqlite3.Connection, job_id: str,
                                   run_id: str, job_ended: str,
                                   stage_tools: list[str]) -> dict[str, float]:
    actions = snapshot.execute('''SELECT action_id,tool,status,created_at,completed_at FROM actions
        WHERE run_id=? AND tool IN ({})'''.format(','.join('?' for _ in stage_tools)),
        (run_id, *stage_tools)).fetchall()
    approvals = snapshot.execute('''SELECT status,envelope_json,created_at,updated_at
        FROM desktop_approvals WHERE job_id=?''', (job_id,)).fetchall()
    if len(actions) != len(stage_tools) or len(approvals) != len(stage_tools):
        raise ValueError('remote_form_repeat_stage_timing_count_invalid')
    actions_by_id = {row['action_id']: row for row in actions}
    durations = {}
    previous_completion = None
    approval_by_tool = {}
    for approval_row in approvals:
        approval = Approval.model_validate_json(approval_row['envelope_json'])
        tool = approval.action.tool
        action = actions_by_id.get(approval.action.action_id)
        if (tool not in stage_tools or tool in approval_by_tool
                or approval.job_id != job_id or approval.action.run_id != run_id
                or approval_row['status'] != 'consumed' or action is None
                or action['tool'] != tool or action['status'] != 'ok'
                or action['completed_at'] is None):
            raise ValueError('remote_form_repeat_stage_timing_binding_invalid')
        approval_by_tool[tool] = (approval_row, action)
    if set(approval_by_tool) != set(stage_tools):
        raise ValueError('remote_form_repeat_stage_timing_scope_invalid')
    for tool in stage_tools:
        approval_row, action = approval_by_tool[tool]
        if previous_completion is not None:
            _duration(previous_completion, approval_row['created_at'])
        _duration(approval_row['updated_at'], action['created_at'])
        durations[tool] = _duration(approval_row['updated_at'], action['completed_at'])
        _duration(action['completed_at'], job_ended)
        previous_completion = action['completed_at']
    return durations


def inspect_remote_form_repeats(database: Path, *, profiles: Path,
                                selected_profile_sha256: str,
                                selected_plan_sha256: str,
                                selected_state_plan_sha256: str | None = None) -> dict:
    if (not isinstance(selected_profile_sha256, str)
            or CHECKSUM.fullmatch(selected_profile_sha256) is None
            or not isinstance(selected_plan_sha256, str)
            or CHECKSUM.fullmatch(selected_plan_sha256) is None
            or selected_state_plan_sha256 is not None
            and (not isinstance(selected_state_plan_sha256, str)
                 or CHECKSUM.fullmatch(selected_state_plan_sha256) is None)):
        raise ValueError('remote_form_repeat_selection_invalid')
    profile_store = WebApplicationProfiles(profiles)
    profile_store.get(selected_profile_sha256)
    with audit_snapshot(database) as (snapshot, identity):
        rows = snapshot.execute('''SELECT binding.*, state_binding.state_plan_sha256,
                state_binding.state_plan_json,
                state_binding.form_plan_sha256 AS state_form_plan_sha256,
                state_binding.browser_runtime_id AS state_browser_runtime_id,
                cookie_binding.job_id AS cookie_job_id,
                job.kind, job.status AS job_status,
                job.real_model, job.runtime_id AS job_runtime_id, job.created_at AS job_started,
                job.updated_at AS job_ended, run.status AS run_status, run.outcome,
                run.policy_version, run.deployment_snapshot_json,
                run.started_at AS run_started, run.ended_at AS run_ended
            FROM desktop_remote_form_bindings AS binding
            LEFT JOIN desktop_remote_form_state_bindings AS state_binding
                ON state_binding.job_id=binding.job_id AND state_binding.run_id=binding.run_id
            LEFT JOIN desktop_remote_form_cookie_bindings AS cookie_binding
                ON cookie_binding.job_id=binding.job_id AND cookie_binding.run_id=binding.run_id
            JOIN desktop_tasks AS job ON job.job_id=binding.job_id AND job.run_id=binding.run_id
            JOIN runs AS run ON run.run_id=binding.run_id
            WHERE binding.profile_sha256=? AND binding.plan_sha256=?
                AND ((? IS NULL AND state_binding.job_id IS NULL)
                     OR state_binding.state_plan_sha256=?)
            ORDER BY job.created_at, job.job_id''',
            (selected_profile_sha256, selected_plan_sha256,
             selected_state_plan_sha256, selected_state_plan_sha256)).fetchall()
        if not 2 <= len(rows) <= 32:
            raise ValueError('remote_form_repeat_attempt_count_invalid')
        elapsed, elapsed_excluding_approval, first_actions = [], [], []
        successful_runs = []
        stage_tools = [tool for tool, _choice in (
            STATE_STAGES if selected_state_plan_sha256 is not None else STAGES)]
        stage_samples = {tool: [] for tool in stage_tools}
        approval_window_count = 0
        approval_window_sum = 0.0
        model_count = 0
        model_samples = 0
        model_latency = 0.0
        deployment_sha = None
        for row in rows:
            if (row['kind'] != 'browser_remote_form' or row['real_model'] != 1
                    or row['cookie_job_id'] is not None
                    or row['job_status'] not in TERMINAL or row['run_status'] not in TERMINAL
                    or row['job_status'] != row['run_status']
                    or row['policy_version'] != 'browser-remote-form-policy-v1'
                    or row['job_runtime_id'] != row['browser_runtime_id']
                    or row['run_ended'] is None):
                raise ValueError('remote_form_repeat_run_unverified')
            draft = WebTaskAdmissionDraft.model_validate_json(row['draft_json'])
            plan = WebHTTPSFormPlan.model_validate_json(row['plan_json'])
            if (canonical(draft.model_dump(mode='json')) != row['draft_json']
                    or canonical(plan.model_dump(mode='json')) != row['plan_json']
                    or draft.profile_sha256 != selected_profile_sha256
                    or draft.binding_sha256 != row['binding_sha256']
                    or draft.runtime_sha256 != row['runtime_sha256']
                    or draft.runtime.runtime_id != row['browser_runtime_id']
                    or plan.profile_sha256 != selected_profile_sha256
                    or digest(plan.model_dump()) != selected_plan_sha256):
                raise ValueError('remote_form_repeat_binding_changed')
            verify_web_task_binding(profile_store, draft)
            verify_web_https_form_plan(profile_store, draft.task, plan)
            if selected_state_plan_sha256 is not None:
                state_plan = WebHTTPSFormStatePlan.model_validate_json(row['state_plan_json'])
                if (canonical(state_plan.model_dump(mode='json')) != row['state_plan_json']
                        or row['state_plan_sha256'] != selected_state_plan_sha256
                        or row['state_form_plan_sha256'] != selected_plan_sha256
                        or row['state_browser_runtime_id'] != row['browser_runtime_id']
                        or state_plan.form_plan_sha256 != selected_plan_sha256
                        or digest(state_plan.model_dump()) != selected_state_plan_sha256):
                    raise ValueError('remote_form_repeat_state_binding_changed')
                public = not (urlsplit(plan.entry_url).hostname or '').endswith('.invalid')
                verify_web_https_form_state_plan(
                    profile_store, draft.task, plan, state_plan, ssl.create_default_context(),
                    confirm_public_form_plan_sha256=selected_plan_sha256 if public else None,
                    confirm_public_state_plan_sha256=selected_state_plan_sha256 if public else None)
            deployment = json.loads(row['deployment_snapshot_json'])
            if (not isinstance(deployment, dict)
                    or canonical(deployment) != row['deployment_snapshot_json']):
                raise ValueError('remote_form_repeat_deployment_changed')
            current_deployment = digest(deployment)
            if deployment_sha is not None and current_deployment != deployment_sha:
                raise ValueError('remote_form_repeat_deployment_changed')
            deployment_sha = current_deployment
            elapsed.append(_duration(row['job_started'], row['job_ended']))
            _duration(row['run_started'], row['run_ended'])
            approvals = snapshot.execute('''SELECT status,created_at,updated_at
                FROM desktop_approvals WHERE job_id=? ORDER BY created_at,approval_id''',
                (row['job_id'],)).fetchall()
            if len(approvals) > (6 if selected_state_plan_sha256 is not None else 4):
                raise ValueError('remote_form_repeat_approval_count_invalid')
            previous_end = row['job_started']
            approval_wait = 0.0
            for approval in approvals:
                if approval['status'] not in {'consumed', 'rejected', 'expired', 'revoked'}:
                    raise ValueError('remote_form_repeat_approval_not_terminal')
                _duration(previous_end, approval['created_at'])
                approval_wait += _duration(approval['created_at'], approval['updated_at'])
                _duration(approval['updated_at'], row['job_ended'])
                previous_end = approval['updated_at']
            non_approval = elapsed[-1] - approval_wait
            if non_approval < -0.000001:
                raise ValueError('remote_form_repeat_approval_exceeds_elapsed')
            elapsed_excluding_approval.append(max(0.0, non_approval))
            approval_window_count += len(approvals)
            approval_window_sum += approval_wait
            first = snapshot.execute('''SELECT created_at FROM actions WHERE run_id=?
                AND tool='browser.form.open' ORDER BY created_at, action_id LIMIT 1''',
                (row['run_id'],)).fetchone()
            if first is not None:
                first_elapsed = _duration(row['job_started'], first['created_at'])
                if first_elapsed > elapsed[-1]:
                    raise ValueError('remote_form_repeat_first_action_after_terminal')
                first_actions.append(first_elapsed)
            calls = snapshot.execute('''SELECT role, status, latency_ms FROM model_calls
                WHERE run_id=? ORDER BY created_at, call_id LIMIT 257''',
                (row['run_id'],)).fetchall()
            if len(calls) > 256:
                raise ValueError('remote_form_repeat_model_call_limit')
            for call in calls:
                if (call['role'] not in {'system1', 'system2'}
                        or call['status'] not in {'ok', 'error', 'timeout', 'cancelled'}
                        or call['latency_ms'] is not None
                        and (not math.isfinite(call['latency_ms'])
                             or call['latency_ms'] < 0)):
                    raise ValueError('remote_form_repeat_model_call_invalid')
                model_count += 1
                if call['latency_ms'] is not None:
                    model_samples += 1
                    model_latency += call['latency_ms']
                    if not math.isfinite(model_latency):
                        raise ValueError('remote_form_repeat_model_latency_overflow')
            if row['job_status'] == 'succeeded':
                if row['outcome'] != 'passed':
                    raise ValueError('remote_form_repeat_outcome_changed')
                successful_runs.append(row)
            elif row['outcome'] == 'passed':
                raise ValueError('remote_form_repeat_failed_outcome_changed')
        for row in successful_runs:
            source = inspect_remote_form_learning_source(
                database, row['run_id'], profiles=profiles,
                selected_profile_sha256=selected_profile_sha256,
                selected_plan_sha256=selected_plan_sha256,
                _audited_snapshot=(snapshot, identity),
                **({'selected_state_plan_sha256': selected_state_plan_sha256}
                   if selected_state_plan_sha256 is not None else {}))
            if source['snapshot_sha256'] != identity['sha256']:
                raise ValueError('remote_form_repeat_snapshot_changed')
            durations = _post_approval_stage_durations(
                snapshot, row['job_id'], row['run_id'], row['job_ended'], stage_tools)
            for tool, duration in durations.items():
                stage_samples[tool].append(duration)
        report = {'schema_version': '1.2', 'mode': 'read_only_post_admission_form_repeats',
                  'profile_sha256': selected_profile_sha256,
                  'plan_sha256': selected_plan_sha256,
                  'snapshot_sha256': identity['sha256'],
                  'deployment_snapshot_sha256': deployment_sha,
                  'bound_attempts': len(rows), 'transport_verified': len(successful_runs),
                  'failed_or_cancelled': len(rows) - len(successful_runs),
                  'elapsed_p50_ms': _percentile(elapsed, 50),
                  'elapsed_p95_ms': _percentile(elapsed, 95),
                  'approval_window_count': approval_window_count,
                  'approval_window_sum_ms': approval_window_sum,
                  'elapsed_excluding_approval_p50_ms': _percentile(elapsed_excluding_approval, 50),
                  'elapsed_excluding_approval_p95_ms': _percentile(elapsed_excluding_approval, 95),
                  'first_action_samples': len(first_actions),
                  'first_action_p50_ms': _percentile(first_actions, 50) if first_actions else None,
                  'first_action_p95_ms': _percentile(first_actions, 95) if first_actions else None,
                  'model_call_count': model_count, 'model_latency_samples': model_samples,
                  'model_latency_sum_ms': model_latency,
                  'post_approval_stage_ms': {
                      tool: {'samples': len(samples), 'p50_ms': _percentile(samples, 50),
                             'p95_ms': _percentile(samples, 95)}
                      for tool, samples in stage_samples.items() if samples},
                  'site_outcome_verified': False, 'real_site_acceptance': False}
        if selected_state_plan_sha256 is not None:
            report['mode'] = 'read_only_post_admission_form_state_repeats'
            report['state_plan_sha256'] = selected_state_plan_sha256
        state_mode = selected_state_plan_sha256 is not None
        model = RemoteFormStateRepeatReport if state_mode else RemoteFormRepeatReport
        checked = model.model_validate(report).model_dump()
        validator('remote_form_state_repeat' if state_mode else 'remote_form_repeat').validate(checked)
        return checked


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Read-only exact form repeat measurements',
                                     allow_abbrev=False)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--selected-profile-sha256', required=True)
    parser.add_argument('--selected-plan-sha256', required=True)
    parser.add_argument('--selected-state-plan-sha256')
    arguments = parser.parse_args(argv)
    try:
        report = inspect_remote_form_repeats(
            arguments.database, profiles=arguments.profiles,
            selected_profile_sha256=arguments.selected_profile_sha256,
            selected_plan_sha256=arguments.selected_plan_sha256,
            selected_state_plan_sha256=arguments.selected_state_plan_sha256)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError,
            RecursionError):
        parser.exit(1, 'Remote form repeat measurements unavailable: incomplete or unsafe source.\n')
    print(canonical(report))


if __name__ == '__main__':
    main()
