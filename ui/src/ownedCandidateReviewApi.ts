import {isOwnedFormRecipeSteps, type OwnedFormInvocation, type OwnedFormRecipeStep} from './api';
import {candidateHash, candidateOperations} from './ownedSkillCandidateApi';
import type {CandidateExecutionStatus} from './ownedCandidateExecutionApi';

export type ReviewSelection = {candidate_sha256: string; source_run_ref: string;
  source_invocation_sha256: string; candidate_execution_sha256: string};
export type ReviewSummary = {skill_key: string; expected_outcome_key: string;
  parameter_key: string; form_field_name: string; steps: OwnedFormRecipeStep[]};
export type CandidateReview = {
  schema_version: '1.0'; available: true; status: 'preview' | 'accepted' | 'revoked';
  review_sha256: string; revocation_sha256: string | null; summary: ReviewSummary;
  receipt: ReviewSelection & {schema_version: '1.0'; synthetic: true; decision: 'accept';
    purpose: 'development_review'; reviewer: 'local_authenticated_user';
    source_group_sha256: string; source_fingerprint_sha256: string; execution_run_ref: string;
    invocation_sha256: string; recipe_sha256: string; skill_sha256: string; profile_sha256: string;
    case_key: string; parameter_variant_sha256: string; summary: ReviewSummary;
    activation_authorized: false; training_ready: false; independent_held_out: false};
};

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function semanticKey(value: unknown): value is string {
  return typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
}
export function validCandidateReview(value: unknown, source: OwnedFormInvocation,
                                    selection?: ReviewSelection): value is CandidateReview {
  if (!record(value) || value.schema_version !== '1.0' || value.available !== true
      || !['preview', 'accepted', 'revoked'].includes(String(value.status)) || !candidateHash(value.review_sha256)
      || (value.status === 'revoked' ? !candidateHash(value.revocation_sha256) : value.revocation_sha256 !== null)
      || !record(value.receipt) || !record(value.summary)) return false;
  const receipt = value.receipt;
  const summary = value.summary;
  if (receipt.schema_version !== '1.0' || receipt.synthetic !== true || receipt.decision !== 'accept'
      || receipt.purpose !== 'development_review' || receipt.reviewer !== 'local_authenticated_user'
      || receipt.source_run_ref !== source.run_ref || receipt.source_invocation_sha256 !== source.invocation_sha256
      || receipt.profile_sha256 !== source.profile_sha256
      || !['activation_authorized', 'training_ready', 'independent_held_out'].every(key => receipt[key] === false)
      || !['candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'source_group_sha256',
        'source_fingerprint_sha256', 'candidate_execution_sha256', 'execution_run_ref', 'invocation_sha256',
        'recipe_sha256', 'skill_sha256', 'profile_sha256', 'parameter_variant_sha256'].every(key => candidateHash(receipt[key]))
      || !semanticKey(receipt.case_key)
      || selection && !Object.entries(selection).every(([key, entry]) => receipt[key] === entry)
      || !['skill_key', 'expected_outcome_key', 'parameter_key'].every(key => semanticKey(summary[key]))
      || typeof summary.form_field_name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(summary.form_field_name)
      || !isOwnedFormRecipeSteps(summary.steps)
      || !summary.steps.every((step, index) => step.operation === candidateOperations[index])
      || !record(receipt.summary)) return false;
  const receiptSummary = receipt.summary;
  return ['skill_key', 'expected_outcome_key', 'parameter_key', 'form_field_name'].every(key => summary[key] === receiptSummary[key])
    && JSON.stringify(summary.steps) === JSON.stringify(receiptSummary.steps);
}
export function reviewMatchesExecution(review: CandidateReview, execution: CandidateExecutionStatus): boolean {
  return review.receipt.candidate_execution_sha256 === execution.candidate_execution_sha256
    && review.receipt.execution_run_ref === execution.run_ref
    && ['candidate_sha256', 'source_run_ref', 'source_invocation_sha256', 'source_group_sha256',
      'profile_sha256', 'skill_sha256', 'case_key', 'parameter_variant_sha256', 'invocation_sha256', 'recipe_sha256']
      .every(key => review.receipt[key as keyof CandidateReview['receipt']] === execution[key as keyof CandidateExecutionStatus]);
}
