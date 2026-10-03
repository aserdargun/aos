import type {Overview, Resources, Snapshot, Tasks} from './api';

const record = (value: unknown): value is Record<string, unknown> => value !== null
  && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
const approval = (value: unknown) => value === null || (record(value) && text(value.approval_id)
  && text(value.action_sha256) && typeof value.expires_at === 'number' && Number.isFinite(value.expires_at)
  && record(value.action) && text(value.action.tool) && record(value.action.arguments)
  && text(value.action.runtime_id) && text(value.action.owner_lease_id)
  && typeof value.action.state_version === 'number' && Number.isSafeInteger(value.action.state_version)
  && text(value.action.expected_effect));

export function hasCurrentTaskStatus(value: unknown): value is Tasks {
  if (!record(value) || typeof value.available !== 'boolean' || typeof value.busy !== 'boolean'
      || !approval(value.approval) || !Array.isArray(value.jobs)
      || !['paused', 'reserved', 'restart_quiesced'].every(key => value[key] === undefined
        || typeof value[key] === 'boolean')) return false;
  return value.jobs.every(job => record(job) && text(job.job_id) && text(job.kind) && text(job.status)
    && (job.progress === undefined || job.progress === null || (record(job.progress)
      && (job.progress.phase === null || typeof job.progress.phase === 'string')
      && (job.progress.model_calls === undefined || (Array.isArray(job.progress.model_calls)
        && job.progress.model_calls.every(call => record(call) && text(call.role) && text(call.status)
          && typeof call.latency_ms === 'number' && Number.isFinite(call.latency_ms))))
      && (job.progress.elapsed_ms === undefined || job.progress.elapsed_ms === null
        || (typeof job.progress.elapsed_ms === 'number' && Number.isFinite(job.progress.elapsed_ms)
          && job.progress.elapsed_ms >= 0)))));
}

export function hasOverviewTelemetry(value: unknown): value is Overview {
  return record(value) && text(value.sampled_at) && record(value.trajectory)
    && typeof value.trajectory.available === 'boolean' && Array.isArray(value.events)
    && Array.isArray(value.inputs) && ['runs', 'models', 'deployments'].every(key =>
      value.trajectory !== null && record(value.trajectory)
      && (value.trajectory[key] === undefined || (Array.isArray(value.trajectory[key])
        && value.trajectory[key].every(record))));
}

export function hasResourceTelemetry(value: unknown): value is Resources {
  return record(value) && typeof value.available === 'boolean' && text(value.sampled_at);
}

export function hasCurrentSessionStatus(value: unknown): value is Snapshot {
  return record(value) && record(value.control) && record(value.runtime)
    && ['AGENT', 'HUMAN', 'PAUSED'].includes(String(value.control.owner))
    && text(value.control.status) && text(value.control.lease_id)
    && typeof value.control.generation === 'number' && Number.isSafeInteger(value.control.generation)
    && value.control.generation >= 0 && typeof value.runtime.running === 'boolean'
    && text(value.runtime.runtime_id);
}
