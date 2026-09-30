import {isOwnedFormRecipeSteps, type OwnedFormInvocation, type OwnedFormRecipeStep} from './api';

export const candidateOperations = ['open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after'] as const;
type Operation = typeof candidateOperations[number];
export type CandidateAnnotation = {
  schema_version: '1.0'; synthetic: true; skill_key: string; page_key: string;
  page_draft_sha256: string; task_key: string;
  parameter_bindings: {parameter_key: string; form_field_name: string}[];
  preconditions: {precondition_key: string; kind: 'entry_form_available' | 'declared_state_before'}[];
  outcome: {outcome_key: string; parameter_key: string};
  operation_step_keys: {operation: Operation; step_key: string}[];
};
export type CandidateContext = {
  schema_version: '1.0'; available: true; mode: 'owned_synthetic_form_invocation';
  lifecycle: 'audited'; source_run_ref: string; invocation_sha256: string;
  profile_sha256: string; task_sha256: string; page_draft_sha256: string;
  task_key: string; form_fields: string[]; annotation_seed: CandidateAnnotation;
};
export type CandidateResult = {
  schema_version: '1.0'; available: true; status: 'preview' | 'published' | 'reinspected';
  persisted: boolean; candidate_sha256: string;
  summary: {
    schema_version: '1.0'; candidate_schema_version: '1.1'; candidate_sha256: string;
    source_run_ref: string; invocation_sha256: string; profile_sha256: string;
    source_group_sha256: string; source_fingerprint_sha256: string;
    source_parameter_variant_sha256: string; skill_sha256: string; recipe_sha256: string;
    steps: OwnedFormRecipeStep[]; status: 'unreviewed_executable_recipe_candidate';
    reviewed: false; activation_authorized: false; training_ready: false;
    site_outcome_verified: false; skill_executed: false;
  };
};

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export function candidateHash(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
}
function key(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
}
export function validCandidateAnnotation(value: unknown): value is CandidateAnnotation {
  if (!record(value) || value.schema_version !== '1.0' || value.synthetic !== true
      || !['skill_key', 'page_key', 'task_key'].every(name => key(value[name]))
      || !candidateHash(value.page_draft_sha256) || !record(value.outcome)
      || !key(value.outcome.outcome_key) || !key(value.outcome.parameter_key)
      || !Array.isArray(value.parameter_bindings) || !value.parameter_bindings.length
      || value.parameter_bindings.length > 8 || !value.parameter_bindings.every(item => record(item)
        && key(item.parameter_key) && typeof item.form_field_name === 'string'
        && /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(item.form_field_name))
      || new Set(value.parameter_bindings.map(item => item.parameter_key)).size !== value.parameter_bindings.length
      || new Set(value.parameter_bindings.map(item => item.form_field_name)).size !== value.parameter_bindings.length
      || !value.parameter_bindings.some(item => item.parameter_key === (value.outcome as Record<string, unknown>).parameter_key)
      || !Array.isArray(value.preconditions) || value.preconditions.length !== 2
      || !value.preconditions.every(item => record(item) && key(item.precondition_key)
        && ['entry_form_available', 'declared_state_before'].includes(String(item.kind)))
      || new Set(value.preconditions.map(item => item.kind)).size !== 2
      || new Set(value.preconditions.map(item => item.precondition_key)).size !== 2
      || !Array.isArray(value.operation_step_keys) || value.operation_step_keys.length !== 6
      || !value.operation_step_keys.every(item => record(item) && key(item.step_key)
        && candidateOperations.includes(item.operation as Operation))
      || new Set(value.operation_step_keys.map(item => item.operation)).size !== 6
      || new Set(value.operation_step_keys.map(item => item.step_key)).size !== 6) return false;
  return true;
}

export function canonicalAnnotation(value: CandidateAnnotation): CandidateAnnotation {
  const compareKeys = (left: string, right: string) => left < right ? -1 : left > right ? 1 : 0;
  return {...value,
    parameter_bindings: [...value.parameter_bindings].sort((left, right) => compareKeys(left.parameter_key, right.parameter_key)),
    preconditions: [...value.preconditions].sort((left, right) => compareKeys(left.precondition_key, right.precondition_key)),
    operation_step_keys: [...value.operation_step_keys].sort((left, right) => compareKeys(left.operation, right.operation))};
}

export function validCandidateContext(value: unknown, expected: OwnedFormInvocation): value is CandidateContext {
  if (!record(value) || value.schema_version !== '1.0' || value.available !== true
      || value.mode !== 'owned_synthetic_form_invocation' || value.lifecycle !== 'audited'
      || value.source_run_ref !== expected.run_ref || value.invocation_sha256 !== expected.invocation_sha256
      || value.profile_sha256 !== expected.profile_sha256
      || !candidateHash(value.task_sha256) || !candidateHash(value.page_draft_sha256)
      || !key(value.task_key) || !validCandidateAnnotation(value.annotation_seed)
      || value.annotation_seed.page_draft_sha256 !== value.page_draft_sha256
      || value.annotation_seed.task_key !== value.task_key || !Array.isArray(value.form_fields)
      || value.form_fields.length !== value.annotation_seed.parameter_bindings.length
      || !value.form_fields.every(field => typeof field === 'string')
      || new Set(value.form_fields).size !== value.form_fields.length
      || !value.annotation_seed.parameter_bindings.every(binding => value.form_fields instanceof Array
        && value.form_fields.includes(binding.form_field_name))) return false;
  return true;
}

export function validCandidateResult(value: unknown, context: CandidateContext,
                                     status: CandidateResult['status'], expectedHash?: string): value is CandidateResult {
  if (!record(value) || value.schema_version !== '1.0' || value.available !== true
      || value.status !== status || value.persisted !== (status !== 'preview')
      || !candidateHash(value.candidate_sha256) || expectedHash && value.candidate_sha256 !== expectedHash
      || !record(value.summary)) return false;
  const summary = value.summary;
  return summary.schema_version === '1.0' && summary.candidate_schema_version === '1.1'
    && summary.candidate_sha256 === value.candidate_sha256
    && summary.source_run_ref === context.source_run_ref
    && summary.invocation_sha256 === context.invocation_sha256
    && summary.profile_sha256 === context.profile_sha256
    && ['source_group_sha256', 'source_fingerprint_sha256', 'source_parameter_variant_sha256',
      'skill_sha256', 'recipe_sha256'].every(name => candidateHash(summary[name]))
    && isOwnedFormRecipeSteps(summary.steps)
    && summary.steps.every((step, index) => step.operation === candidateOperations[index])
    && summary.status === 'unreviewed_executable_recipe_candidate'
    && ['reviewed', 'activation_authorized', 'training_ready', 'site_outcome_verified', 'skill_executed']
      .every(name => summary[name] === false);
}
