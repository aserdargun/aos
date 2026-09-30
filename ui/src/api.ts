import {t} from './i18n';
import type {CandidateExecutionStatus} from './ownedCandidateExecutionApi';
import type {PlanningStatus} from './OwnedSkillPlanning';
import type {AdaptationStatus} from './OwnedEpisodeAdaptation';

export type Control = 'pause' | 'stop' | 'take-control' | 'return-control' | 'resume' | 'restart';
export interface Snapshot {
  control: {owner: 'AGENT' | 'HUMAN' | 'PAUSED'; status: string; lease_id: string; generation: number};
  runtime: {running: boolean; runtime_id: string; image_id: string | null; container_id: string | null; pointer_tracking?: boolean};
}
export interface Run {
  run_id: string; status: string; started_at: string; model_calls: number; passed: number; failed: number;
}
export interface Overview {
  sampled_at: string;
  events: {event_id: string; kind: string; created_at: string}[];
  inputs: {input_id: string; tool: string; status: string; generation: number; created_at: string}[];
  trajectory: {
    available: boolean;
    runs?: Run[];
    models?: {model_id: string; backend: string; revision: string | null; enabled: number}[];
    deployments?: {deployment_id: string; model_id: string; status: string}[];
  };
}
export interface Resources {
  available: boolean; sampled_at: string; running?: boolean; memory_limit_bytes?: number;
  cpu_limit?: number; pids_limit?: number; network_mode?: string; readonly_rootfs?: boolean;
}
export interface RetentionStatus {
  schema_version: '1.0'; mode: 'managed_remote_metadata_retention_status';
  enabled: boolean; state: 'disabled' | 'pending' | 'ok' | 'incomplete' | 'stale' | 'unavailable';
  last_attempt_at: string | null; session_count: number | null; purged_count: number | null;
  failed_session_count: number | null; failed_consent_count: number | null;
  metadata_only: true; training_ready: false;
}
export interface RemoteFormRepeatReport {
  schema_version: '1.1' | '1.2'; mode: 'read_only_post_admission_form_repeats' | 'read_only_post_admission_form_state_repeats';
  profile_sha256: string; plan_sha256: string; state_plan_sha256?: string; snapshot_sha256: string;
  deployment_snapshot_sha256: string; bound_attempts: number;
  transport_verified: number; failed_or_cancelled: number;
  elapsed_p50_ms: number; elapsed_p95_ms: number;
  approval_window_count: number; approval_window_sum_ms: number;
  elapsed_excluding_approval_p50_ms: number; elapsed_excluding_approval_p95_ms: number;
  first_action_samples: number; first_action_p50_ms: number | null;
  first_action_p95_ms: number | null; model_call_count: number;
  model_latency_samples: number; model_latency_sum_ms: number;
  post_approval_stage_ms?: Record<string, {samples: number; p50_ms: number; p95_ms: number}>;
  site_outcome_verified: false; real_site_acceptance: false;
}
interface OwnedFormInvocationBase {
  lifecycle: 'ready' | 'running' | 'completed' | 'audited' | 'failed';
  profile_sha256: string;
  skill_sha256: string;
  invocation_sha256: string;
  run_ref?: string | null;
  report_sha256?: string | null;
  reuse_admission_sha256?: string;
}
export type OwnedFormRecipeOperation = 'open_entry' | 'read_state_before' | 'fill_form'
  | 'submit_form' | 'read_receipt' | 'read_state_after';
export interface OwnedFormRecipeStep {step_key: string; operation: OwnedFormRecipeOperation}
export type OwnedFormInvocation = OwnedFormInvocationBase & (
  | {mode: 'owned_synthetic_form_invocation'}
  | {mode: 'owned_synthetic_form_recipe'; recipe_sha256: string; steps: OwnedFormRecipeStep[]});
interface OwnedFormAuditBase {
  synthetic: true;
  invocation_sha256: string; skill_sha256: string; profile_sha256: string; run_ref: string;
  stages: {stage: string}[];
  consumed_approval_count: 6; submit_count: 1;
  transport_readback_verified: true; declared_state_readback_verified: true;
  invocation_persisted_before_actions: true; invocation_consistent_across_states: true;
  invocation_execution_verified: true; site_outcome_verified: false;
  held_out_independence_verified: false; reviewed: false;
  activation_authorized: false; training_ready: false;
}
export type OwnedFormInvocationAuditReport = OwnedFormAuditBase & (
  | {schema_version: '1.0'; status: 'fixed_template_invocation_execution_verified';
     symbolic_skill_steps_executed: false; skill_executed: false}
  | {schema_version: '2.0'; status: 'executable_recipe_execution_verified';
     recipe_sha256: string; symbolic_step_keys: string[]; step_observation_refs: string[];
     executable_recipe_executed: true; symbolic_skill_steps_executed: true;
     skill_executed: true; skill_validated: false});
export type OwnedFormInvocationAuditResponse =
  | {mode?: 'owned_synthetic_form_invocation'; available: true; status: 'verified';
     report: Extract<OwnedFormInvocationAuditReport, {schema_version: '1.0'}>; report_sha256: string}
  | {mode: 'owned_synthetic_form_recipe'; available: true; status: 'verified';
     report: Extract<OwnedFormInvocationAuditReport, {schema_version: '2.0'}>; report_sha256: string}
  | {mode?: 'owned_synthetic_form_invocation'; available: false; status: 'not_ready' | 'unavailable'; report: null; report_sha256: null}
  | {mode: 'owned_synthetic_form_recipe'; available: false; status: 'not_ready' | 'unavailable'; report: null; report_sha256: null};
export function isOwnedFormRecipeSteps(value: unknown): value is OwnedFormRecipeStep[] {
  if (!Array.isArray(value) || value.length !== 6) return false;
  const orders = [
    ['open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after'],
    ['open_entry', 'fill_form', 'read_state_before', 'submit_form', 'read_receipt', 'read_state_after']];
  return value.every(step => step !== null && typeof step === 'object'
      && Object.keys(step).length === 2 && typeof step.step_key === 'string'
      && /^[a-z][a-z0-9_-]{0,63}$/.test(step.step_key) && typeof step.operation === 'string')
    && new Set(value.map(step => step.step_key)).size === 6
    && orders.some(order => value.every((step, index) => step.operation === order[index]));
}
export function isVerifiedOwnedFormInvocationAudit(value: unknown, expected: OwnedFormInvocation): value is {
  available: true; status: 'verified'; report: OwnedFormInvocationAuditReport; report_sha256: string;
} {
  if (value === null || typeof value !== 'object') return false;
  const response = value as Record<string, unknown>;
  const report = response.report;
  if (response.available !== true || response.status !== 'verified'
      || typeof response.report_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(response.report_sha256)
      || report === null || typeof report !== 'object') return false;
  const result = report as Record<string, unknown>;
  const recipe = expected.mode === 'owned_synthetic_form_recipe';
  if (recipe ? response.mode !== expected.mode
      : response.mode !== undefined && response.mode !== expected.mode) return false;
  const stages = recipe ? expected.steps.map(step => step.operation)
    : ['open_entry', 'read_state_before', 'fill_form', 'submit_form', 'read_receipt', 'read_state_after'];
  const versionMatches = recipe
    ? result.schema_version === '2.0' && result.status === 'executable_recipe_execution_verified'
      && result.recipe_sha256 === expected.recipe_sha256
      && result.executable_recipe_executed === true && result.symbolic_skill_steps_executed === true
      && result.skill_executed === true && result.skill_validated === false
      && Array.isArray(result.symbolic_step_keys) && result.symbolic_step_keys.length === 6
      && result.symbolic_step_keys.every((key, index) => key === expected.steps[index].step_key)
      && Array.isArray(result.step_observation_refs) && result.step_observation_refs.length === 6
      && result.step_observation_refs.every(ref => typeof ref === 'string' && /^[a-f0-9]{64}$/.test(ref))
      && new Set(result.step_observation_refs).size === 6
    : result.schema_version === '1.0' && result.status === 'fixed_template_invocation_execution_verified'
      && result.symbolic_skill_steps_executed === false && result.skill_executed === false;
  return versionMatches && result.synthetic === true
    && result.invocation_sha256 === expected.invocation_sha256
    && result.skill_sha256 === expected.skill_sha256
    && result.profile_sha256 === expected.profile_sha256
    && typeof expected.run_ref === 'string' && result.run_ref === expected.run_ref
    && Array.isArray(result.stages) && result.stages.length === stages.length
    && result.stages.every((stage, index) => stage !== null && typeof stage === 'object'
      && (stage as Record<string, unknown>).stage === stages[index])
    && result.consumed_approval_count === 6 && result.submit_count === 1
    && result.transport_readback_verified === true && result.declared_state_readback_verified === true
    && result.invocation_persisted_before_actions === true && result.invocation_consistent_across_states === true
    && result.invocation_execution_verified === true && result.site_outcome_verified === false
    && result.held_out_independence_verified === false && result.reviewed === false
    && result.activation_authorized === false && result.training_ready === false;
}
export type PointerTelemetry =
  | {available: true; sampled_at: string; x: number; y: number; width: number; height: number}
  | {available: false; sampled_at: string; reason: 'desktop_stopped' | 'runtime_unavailable' | 'pointer_unavailable' | 'runtime_changed'};
export interface WebApplicationReport {
  schema_version: '1.0'; application_key: string; revision: number; profile_sha256: string;
  status: 'draft'; requested_learning_roles: ('system1' | 'system2')[];
  execution_authorized: false; collection_authorized: false; training_ready: false;
  promotion_authorized: false; blockers: string[];
}
export type ImageDraftCapability = 'checking' | 'supported' | 'unconfirmed';
export function supportsImageDrafts(value: unknown): boolean {
  if (value === null || typeof value !== 'object') return false;
  const response = value as Record<string, unknown>;
  const contentTypes = response.static_asset_content_types;
  return (response.schema_version === '1.0' || response.schema_version === '1.1')
    && response.mode === 'web_application_draft_capabilities'
    && Array.isArray(contentTypes)
    && ['application/javascript', 'text/javascript', 'text/css',
      'image/png', 'image/jpeg', 'image/webp', 'image/gif'].every(type => contentTypes.includes(type));
}
export type StaticQueryCapability = ImageDraftCapability;
export function supportsStaticAssetQueries(value: unknown): boolean {
  return supportsImageDrafts(value) && (value as Record<string, unknown>).schema_version === '1.1'
    && (value as Record<string, unknown>).static_asset_canonical_query === true;
}
export interface HTTPSPreflightReport {
  schema_version: '1.0' | '1.1' | '1.2' | '1.3'; mode: 'explicit_host_https_entry_probe';
  status: 'https_entry_reached'; profile_sha256: string; origin_sha256: string;
  request_sha256: string; response_sha256: string; response_bytes: number;
  entry_html_signals?: null | {
    form_count: number; script_count: number; stylesheet_count: number; image_count: number;
    post_form_count?: number; cross_origin_form_action_count?: number;
    unclassified_form_action_count?: number;
    meta_refresh_count?: number; cross_origin_meta_refresh_count?: number;
    unclassified_meta_refresh_count?: number;
    password_input_count: number; cross_origin_resource_count: number;
    unclassified_resource_count: number; static_html_only: true; browser_or_account_verified: false;
  };
  tls_hostname_verified: true; browser_connected: false;
  execution_authorized: false; collection_authorized: false; training_ready: false;
}
export interface RemoteLearningConsentReport {
  consent_sha256: string; registered: boolean; run_ref: string;
  roles: ('system1' | 'system2')[]; expires_at: string;
  metadata_only: true; external_rights_verified: false; training_authorized: false;
}
export interface Tasks {
  knowledge_available?: boolean;
  knowledge_answer_available?: boolean;
  task_knowledge_available?: boolean;
  failure_improvement_available?: boolean;
  failure_followup_available?: boolean;
  failure_guidance_available?: boolean;
  hello_guidance_reuse_available?: boolean;
  available: boolean; busy: boolean; paused?: boolean; reserved?: boolean; restart_quiesced?: boolean; real_model?: boolean;
  supports_approve_all?: boolean; auto_approval?: null | {job_id: string; kind: string};
  supports_learning_metadata?: boolean;
  supports_remote_learning_metadata?: boolean;
  owned_form_invocation?: OwnedFormInvocation | null;
  owned_form_candidate_execution?: CandidateExecutionStatus | null;
  owned_skill_planning?: PlanningStatus;
  owned_episode_learning?: {available: boolean; state?: 'disabled' | 'collecting' | 'reviewable' | 'failed' | 'cancelled' | 'needs_human';
    episode_id?: string; counts?: {system1: number; system2: number}; training_ready: false;
    adaptation?: AdaptationStatus;
    preparation?: {state: 'idle' | 'pending' | 'verified' | 'failed' | 'cancelled';
      episode_id?: string; conversion_sha256?: string; training_ready: false}};
  decider_preparation?: {enabled: boolean; state: 'inactive' | 'preparing' | 'ready' | 'ready_gpu'};
  kinds?: string[]; real_supervisor?: boolean; browser_display?: 'desktop' | 'headless'; vision_display?: 'desktop' | 'headless';
  browser_transport?: 'playwright_mcp' | 'cdp' | 'playwright';
  navigation_transport?: 'playwright_mcp' | 'cdp' | 'playwright';
  staging_transport?: 'playwright_mcp' | null;
  remote_entry?: null | {profile_sha256: string; entry_url: string; task_key: string; mode: 'one_shot_read_only'};
  remote_routes?: null | {plan_sha256: string; route_count: number; review_sha256?: string | null; mode: 'ordered_read_only'};
  remote_static_assets?: null | {plan_sha256: string; asset_count: number; assets: string[];
    data_resources?: string[]; review_sha256?: string | null;
    mode: 'one_shot_static_bundle' | 'one_shot_readonly_data_bundle'};
  remote_form?: null | {plan_sha256: string; entry_url: string; submit_url: string;
    receipt_url: string; field_name: string | null; field_names?: string[]; body_sha256: string;
    state_plan_sha256?: string | null; state_url?: string | null;
    cookie_sha256?: string | null;
    mode: 'synthetic_one_post' | 'public_explicit_one_post'};
  sequence?: null | {sequence_id: string; status: 'running' | 'succeeded' | 'failed' | 'cancelled';
    plan: {kinds: string[]}; completed: number; job_ids: string[]; reason: string; automatic_replay_allowed: false};
  jobs: {job_id: string; run_id: string | null; kind: string; status: string; real_model: number; runtime_id: string | null;
    failure_followup?: {intent_sha256: string; source_job_id: string} | null;
    failure_guidance?: {guidance_intent_sha256: string; source_job_id: string} | null;
    hello_guidance_reuse?: {reuse_intent_sha256: string; entry_sha256: string; source_job_id: string} | null;
    route_knowledge?: {status: 'matched' | 'stale'; route_index: number;
      stale_reason?: 'source_fingerprint_changed' | 'source_link_sample_changed' | 'target_fingerprint_changed' | null};
    json_knowledge?: {status: 'matched' | 'stale'; page_key: string | null; draft_revision: number | null};
    learning_metadata?: {enabled: boolean; state: 'disabled' | 'pending' | 'collecting' | 'paused' | 'synced' | 'recovery_required' | 'failed' | 'failed_unpersisted';
      run_ref?: string | null; phase?: string; duration_ms?: number; new_entries?: number; total_entries?: number | null;
      entries_by_role?: {system1: number; system2: number} | null; reason?: string | null};
    remote_learning_metadata?: {enabled: boolean; state: 'disabled' | 'collecting' | 'synced' | 'recovery_required' | 'failed' | 'failed_unpersisted';
      run_ref?: string | null; consent_sha256?: string; phase?: string; duration_ms?: number;
      new_entries?: number; total_entries?: number | null;
      entries_by_role?: {system1: number; system2: number} | null; reason?: string | null};
    progress?: {phase: string | null; state_version: number | null; elapsed_ms: number | null;
      failure_code?: 'TOOL_FAILURE' | 'MODEL_FAILURE' | 'INVALID_OUTPUT' | 'TIMEOUT' | 'UI_CHANGED' |
        'ELEMENT_MISSING' | 'NETWORK_FAILURE' | 'APP_CRASH' | 'RUNTIME_CRASH' | 'STUCK' | 'UNSAFE_ACTION' | null;
      model_calls: {role: 'system1' | 'system2'; status: 'ok' | 'error' | 'timeout' | 'cancelled'; latency_ms: number}[];
      model_call_totals?: null | {system1: {ok: number; total: number}; system2: {ok: number; total: number}};
      model_call_latency?: null | {system1: {samples: number; p50_ms: number | null; p95_ms: number | null};
        system2: {samples: number; p50_ms: number | null; p95_ms: number | null}}}}[];
  approval: null | {
    approval_id: string; action_sha256: string; expires_at: number;
    action: {tool: string; arguments: Record<string, string | number>; runtime_id: string; owner_lease_id: string; state_version: number; expected_effect: string};
  };
}
export interface NavigationGraph {
  status: 'candidate_observed_route_graph'; route_count: number;
  stable_pages: {route_index: number}[];
  observed_edges: {source_route_index: number; target_route_index: number}[];
  changed_route_indices: number[]; incomplete_link_sample_indices: number[];
  reviewed: false; task_retrieval_authorized: false; execution_authorized: false;
}
export interface PageDraftSeed {
  status: 'unreviewed_private_draft'; profile_sha256: string; page_key: string;
  route_index: number; draft_sha256: string; semantic_page_key_verified: false;
  reviewed: false; execution_authorized: false; collection_authorized: false;
}
export interface PageDraftRegistration {
  status: 'registered_unreviewed_draft'; profile_sha256: string;
  page_key: string; knowledge_sha256: string; source_bound: true;
  semantic_page_key_verified: false; reviewed: false;
  task_retrieval_authorized: false; execution_authorized: false;
}
export interface RouteKnowledgeCandidatePreview {
  mode: 'remote_route_metadata_candidate_preview'; candidate_sha256: string;
  candidate: {status: 'candidate_remote_profile_bound'; page_key: string;
    draft_revision: number; route_index: number; knowledge_sha256: string;
    profile_sha256: string; plan_sha256: string; reviewed: false;
    task_retrieval_authorized: false; execution_authorized: false};
}
export interface RouteKnowledgeReviewReceipt {
  status: 'metadata_review_recorded'; review_sha256: string;
  candidate_sha256: string; metadata_reviewed: true;
  task_retrieval_authorized: false; execution_authorized: false;
}
export interface JsonKnowledgeCandidatePreview {
  mode: 'remote_json_metadata_candidate_preview'; candidate_sha256: string;
  candidate: {status: 'candidate_remote_profile_bound'; page_key: string;
    draft_revision: number; asset_count: number; data_count: number;
    knowledge_sha256: string; reviewed: false; task_retrieval_authorized: false};
}
export interface JsonPageDraftSeed {
  mode: 'audited_remote_json_page_draft_seed'; status: 'unreviewed_private_draft';
  page_key: string; draft_sha256: string; page_fingerprint_sha256: string;
  asset_count: number; data_count: number; reviewed: false;
  task_retrieval_authorized: false;
}
export interface JsonPageDraftRegistration {
  mode: 'audited_remote_json_page_draft_registration';
  status: 'registered_unreviewed_draft'; page_key: string;
  knowledge_sha256: string; reviewed: false; task_retrieval_authorized: false;
}
export interface JsonKnowledgeReviewReceipt {
  status: 'metadata_review_recorded'; review_sha256: string;
  candidate_sha256: string; metadata_reviewed: true;
  task_retrieval_authorized: false;
}
export interface JsonKnowledgeRecheck {
  status: 'reviewed_metadata_matched'; review_sha256: string;
  page_key: string; draft_revision: number; landmark_keys: string[];
  current_readback_bound: true; task_retrieval_authorized: false;
}
export interface RemoteSkillSource {
  status: 'unreviewed_route_source_match'; skill_sha256: string;
  profile_sha256: string; page_draft_sha256: string; route_index: number;
  model_role: 'system1'; source_event_ids: string[];
  profile_bound: true; page_readback_bound: true;
  transport_readback_verified: true; site_outcome_verified: false;
  account_verified: false; parameter_values_bound: false;
  reviewed: false; execution_authorized: false; collection_authorized: false;
  activation_authorized: false; training_ready: false;
}
export interface SiteSkillDraftReport {
  schema_version: '1.0'; skill_sha256: string; profile_sha256: string;
  skill_key: string; model_role: 'system1' | 'system2'; revision: number; status: 'draft';
  execution_authorized: false; collection_authorized: false;
  training_ready: false; activation_authorized: false;
}
export interface GoalPreview {
  schema_version: '1.0'; normalizer_version: 'bounded-tr-v1'; input_sha256: string;
  status: 'recognized' | 'unsupported' | 'negated';
  task_kind: 'hello' | 'browser_form' | 'vision_canvas' | null;
  normalized_goal: string | null; scope: string | null;
  execution_authorized: false; requires_action_approval: true; reason: string;
}
export interface PlanPreview {
  mode: 'read_only_fixed_plan'; intent: GoalPreview; plan_sha256: string | null;
  execution_authorized: false; requires_action_approval: true;
  plan: null | {
    catalog_version: 'fixed-plan-v1'; task_kind: 'hello' | 'browser_form' | 'vision_canvas';
    normalized_goal: string; scope: string; runtime: 'isolated_workspace' | 'separate_networkless_browser';
    preconditions: string[];
    steps: {order: number; phase: 'observe' | 'decide' | 'act' | 'verify'; description: string;
      approval: 'fresh_action' | 'covered_readback' | 'not_applicable'}[];
    verification_method: string; expected_json: string;
    execution_authorized: false; live_state_verified: false;
  };
}
export interface RecoveryInventory {
  mode: 'read_only_recovery_inventory'; snapshot_sha256: string; captured_at: string;
  execution_authorized: false; resume_authorized: false; automatic_replay_allowed: false; live_state_verified: false;
  requires_inspection: boolean; job_limit: number; jobs_truncated: boolean;
  totals: {
    jobs: number; jobs_requiring_inspection: number; runs: number; unsettled_runs: number; unsettled_runs_without_jobs: number;
    action_intent: number; action_running: number; action_uncertain: number; approval_pending: number; approval_approved: number;
    input_queued: number; input_running: number; input_uncertain: number; sessions: number; sessions_not_stopped: number;
  };
  jobs: {job_ref: string; session_ref: string; run_ref: string | null; kind: string; status: string; run_status: string | null;
    reasons: string[]; pending: {action_intent: number; action_running: number; action_uncertain: number;
      approval_pending: number; approval_approved: number}}[];
}
export interface Trace {
  run: {run_id: string; status: string};
  decisions: {decision_id: string; selected_option: string; confidence: number; policy_result: string}[];
  actions: {action_id: string; tool: string; status: string; error_code: string | null}[];
  verifications: {verification_id: string; method: string; result: string}[];
  model_calls: {call_id: string; role: string; status: string; latency_ms: number | null}[];
}
export class ApiError extends Error {
  constructor(public status: number) { super(`${t('İstek reddedildi')} (${status}). ${t('Oturumu ve runtime durumunu kontrol edin.')}`); }
}
export async function api<Result>(path: string, body?: unknown, signal?: AbortSignal): Promise<Result> {
  const response = await fetch(path, {
    signal, credentials: 'same-origin',
    ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json();
}
