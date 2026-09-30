import {isOwnedFormRecipeSteps, type OwnedFormInvocation, type OwnedFormRecipeStep} from './api';
import {candidateHash, candidateOperations} from './ownedSkillCandidateApi';

export type ExecutionSelection = {
  schema_version: '1.0' | '1.1' | '1.2' | '1.3'; review_sha256?: string;
  reuse_admission_sha256?: string;
  release_sha256?: string; selection_sha256?: string; candidate_sha256: string; source_run_ref: string;
  source_invocation_sha256: string; case_key: string; development_value: string;
};
type ExecutionPins = {
  candidate_sha256: string; source_run_ref: string; source_invocation_sha256: string;
  source_group_sha256: string; profile_sha256: string; skill_sha256: string;
  case_key: string; parameter_variant_sha256: string; recipe_sha256: string;
  steps: OwnedFormRecipeStep[];
};
export type ExecutionPreview = ExecutionPins & {
  schema_version: '1.0' | '1.1' | '1.2' | '1.3'; review_sha256?: string; release_sha256?: string; selection_sha256?: string;
  reuse_admission_sha256?: string;
  available: true; status: 'preview'; preview_sha256: string;
  form_plan_sha256: string; state_plan_sha256: string; invocation_sha256: string;
  purpose: 'development_variation'; independent_held_out: false; report: null;
};
export type CandidateExecutionStatus = ExecutionPins & {
  schema_version: '1.0' | '1.1' | '1.2' | '1.3'; review_sha256?: string; release_sha256?: string; selection_sha256?: string;
  reuse_admission_sha256?: string;
  family_sha256?: string; selection_status?: 'current' | 'superseded' | 'unavailable';
  review_status?: 'accepted' | 'revoked' | 'unavailable'; mode: 'owned_candidate_development';
  lifecycle: 'previewed' | 'running' | 'completed' | 'audited' | 'failed';
  candidate_execution_sha256: string | null; invocation_sha256: string | null;
  preview_sha256?: string;
  job_id: string | null; run_id: string | null; run_ref: string | null; report_sha256: string | null;
};
export type CandidateExecutionAudit = {
  schema_version: '1.0' | '1.1' | '1.2' | '1.3'; review_sha256?: string; release_sha256?: string; selection_sha256?: string;
  reuse_admission_sha256?: string; reuse_admission_verified?: boolean;
  family_sha256?: string; selection_status?: 'current' | 'superseded' | 'unavailable';
  release_admission_verified?: boolean; review_status?: 'accepted' | 'revoked' | 'unavailable';
  review_admission_verified?: boolean; available: true; mode: 'owned_candidate_development'; status: 'verified';
  candidate_execution_sha256: string; candidate_sha256: string; source_run_ref: string;
  source_invocation_sha256: string; source_group_sha256: string; profile_sha256: string;
  skill_sha256: string; case_key: string; parameter_variant_sha256: string;
  run_id: string; run_ref: string; invocation_sha256: string; recipe_sha256: string;
  report_sha256: string; report: Record<string, unknown>;
};

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function caseKey(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z][a-z0-9-]{0,63}$/.test(value);
}
export function validDevelopmentInput(caseValue: string, value: string): boolean {
  return caseKey(caseValue) && /^[A-Za-z0-9 _.-]{1,128}$/.test(value) && value.trim() === value;
}
function pins(value: Record<string, unknown>): boolean {
  return ['candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'source_group_sha256',
    'profile_sha256', 'skill_sha256', 'parameter_variant_sha256', 'recipe_sha256'].every(name => candidateHash(value[name]))
    && caseKey(value.case_key) && isOwnedFormRecipeSteps(value.steps)
    && value.steps.every((step, index) => step.operation === candidateOperations[index]);
}
function reviewVersion(value: Record<string, unknown>, status = false): boolean {
  if (value.schema_version === '1.3' ? !candidateHash(value.reuse_admission_sha256)
    : value.reuse_admission_sha256 !== undefined || value.reuse_admission_verified !== undefined) return false;
  if (value.schema_version === '1.2' || value.schema_version === '1.3') return candidateHash(value.release_sha256)
    && candidateHash(value.selection_sha256) && candidateHash(value.review_sha256)
    && (!status || candidateHash(value.family_sha256)
      && ['current', 'superseded', 'unavailable'].includes(String(value.selection_status)))
    && (!status || ['accepted', 'revoked', 'unavailable'].includes(String(value.review_status)));
  if (value.release_sha256 !== undefined || value.selection_sha256 !== undefined
      || value.release_admission_verified !== undefined || value.family_sha256 !== undefined
      || value.selection_status !== undefined) return false;
  return value.schema_version === '1.0'
    ? value.review_sha256 === undefined && value.review_status === undefined && value.review_admission_verified === undefined
    : value.schema_version === '1.1' && candidateHash(value.review_sha256)
      && (!status || ['accepted', 'revoked', 'unavailable'].includes(String(value.review_status)));
}
export function validExecutionPreview(value: unknown, selection: ExecutionSelection,
                                      source: OwnedFormInvocation): value is ExecutionPreview {
  return record(value) && pins(value) && reviewVersion(value) && value.schema_version === selection.schema_version
    && value.review_sha256 === selection.review_sha256 && value.available === true
    && value.release_sha256 === selection.release_sha256 && value.selection_sha256 === selection.selection_sha256
    && value.reuse_admission_sha256 === selection.reuse_admission_sha256
    && value.reuse_admission_sha256 === source.reuse_admission_sha256
    && value.status === 'preview' && value.purpose === 'development_variation'
    && value.independent_held_out === false && value.report === null
    && ['preview_sha256', 'form_plan_sha256', 'state_plan_sha256', 'invocation_sha256'].every(name => candidateHash(value[name]))
    && value.candidate_sha256 === selection.candidate_sha256 && value.case_key === selection.case_key
    && value.source_run_ref === selection.source_run_ref
    && value.source_invocation_sha256 === selection.source_invocation_sha256
    && value.profile_sha256 === source.profile_sha256;
}
export function validCandidateExecutionStatus(value: unknown, source: OwnedFormInvocation): value is CandidateExecutionStatus {
  if (!record(value) || !pins(value) || !reviewVersion(value, true)
      || value.mode !== 'owned_candidate_development'
      || !['previewed', 'running', 'completed', 'audited', 'failed'].includes(String(value.lifecycle))
      || value.source_run_ref !== source.run_ref || value.source_invocation_sha256 !== source.invocation_sha256
      || value.reuse_admission_sha256 !== source.reuse_admission_sha256
      || value.profile_sha256 !== source.profile_sha256) return false;
  if (value.lifecycle === 'previewed') return value.candidate_execution_sha256 === null
    && candidateHash(value.preview_sha256)
    && value.job_id === null && value.run_id === null && value.run_ref === null && value.report_sha256 === null
    && (value.invocation_sha256 === null || candidateHash(value.invocation_sha256));
  return candidateHash(value.candidate_execution_sha256) && candidateHash(value.invocation_sha256)
    && typeof value.job_id === 'string' && value.job_id.length > 0
    && (value.run_id === null || typeof value.run_id === 'string' && value.run_id.length > 0)
    && (value.run_ref === null || candidateHash(value.run_ref))
    && (!['completed', 'audited'].includes(String(value.lifecycle)) || candidateHash(value.run_ref) && typeof value.run_id === 'string')
    && (value.lifecycle === 'audited' ? candidateHash(value.report_sha256) : value.report_sha256 === null);
}
export function validCandidateExecutionAudit(value: unknown, expected: CandidateExecutionStatus): value is CandidateExecutionAudit {
  if (!record(value) || !reviewVersion(value, true) || value.schema_version !== expected.schema_version
      || value.review_sha256 !== expected.review_sha256
      || value.release_sha256 !== expected.release_sha256 || value.selection_sha256 !== expected.selection_sha256
      || value.reuse_admission_sha256 !== expected.reuse_admission_sha256
      || value.schema_version === '1.3' && value.reuse_admission_verified !== true
      || value.family_sha256 !== expected.family_sha256
      || ['1.2', '1.3'].includes(String(value.schema_version)) && value.release_admission_verified !== true
      || value.schema_version !== '1.0' && value.review_admission_verified !== true || value.available !== true
      || value.mode !== 'owned_candidate_development' || value.status !== 'verified'
      || !candidateHash(value.report_sha256) || !record(value.report)
      || !['candidate_execution_sha256', 'candidate_sha256', 'source_run_ref', 'source_invocation_sha256',
        'source_group_sha256', 'profile_sha256', 'skill_sha256', 'case_key', 'parameter_variant_sha256',
        'run_id', 'run_ref', 'invocation_sha256', 'recipe_sha256'].every(name => value[name] === expected[name as keyof CandidateExecutionStatus])) return false;
  const report = value.report;
  if (report.schema_version !== '1.0' || report.synthetic !== true
      || report.status !== 'source_bound_development_execution'
      || report.source_run_ref !== expected.source_run_ref || report.execution_run_ref !== expected.run_ref
      || report.source_group_sha256 !== expected.source_group_sha256
      || !['source_snapshot_sha256', 'recipe_audit_sha256', 'admission_observation_ref', 'admission_sha256'].every(name => candidateHash(report[name]))
      || !['source_bound', 'executable_recipe_executed', 'source_and_execution_events_disjoint'].every(name => report[name] === true)
      || !['held_out_independence_verified', 'site_outcome_verified', 'skill_validated',
        'dataset_ingestion_authorized', 'training_ready', 'activation_authorized'].every(name => report[name] === false)
      || !record(report.admission)) return false;
  const admission = report.admission;
  return admission.schema_version === '1.0' && admission.synthetic === true
    && ['candidate_sha256', 'source_group_sha256', 'source_run_ref', 'parameter_variant_sha256',
      'invocation_sha256', 'recipe_sha256', 'skill_sha256', 'profile_sha256'].every(name => admission[name] === expected[name as keyof CandidateExecutionStatus])
    && candidateHash(admission.source_parameter_variant_sha256)
    && admission.source_parameter_variant_sha256 !== expected.parameter_variant_sha256
    && admission.purpose === 'development_variation'
    && ['independent_held_out', 'dataset_ingestion_authorized', 'execution_authorized'].every(name => admission[name] === false);
}
