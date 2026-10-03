import {useEffect, useRef, useState} from 'react';
import {api, isOwnedFormRecipeSteps, type OwnedFormInvocation, type OwnedFormRecipeStep, type Snapshot} from './api';
import {candidateHash} from './ownedSkillCandidateApi';
import {useLanguage} from './i18n';
import type {PlanningStatus} from './OwnedSkillPlanning';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope,
  validKnowledgeSearch, type KnowledgeSearch} from './knowledgeApi';

type Skill = {skill_ref: string; description: string; recipe_sha256: string; release_sha256: string;
  selection_sha256: string; parameters: Record<string, {description: string; min_chars: number; max_chars: number}>};
type Catalog = {schema_version: '1.0'; synthetic: true; profile_sha256: string; application_key: string;
  tenant_key: string; account_role: string;
  execution_authorized: false; activation_authorized: false; training_ready: false;
  scope_authorization_verified: false; skills: Skill[]};
type Selection = {catalog: Catalog; catalog_sha256: string};
type KnowledgeReview = {schema_version: '1.0'; kind: 'web_goal_knowledge_review'; goal: string;
  authority: Record<string, unknown>; catalog_sha256: string; deployment_sha256: string; query: string;
  retrieval: KnowledgeSearch; workspace_identity: Record<string, unknown>; store_identity: Record<string, unknown>;
  payload: {context_version: 'aos-owned-skill-knowledge-v1'; context_text: string; context_sha256: string; untrusted: true};
  expires_at: string; confirm_sha256: string};
type KnowledgeSource = {document_sha256: string; content_sha256: string; review_sha256: string; chunk_sha256: string;
  source_id: string; title: string; synthetic: boolean; available: boolean; current: boolean; expired: boolean | null;
  current_review_status: 'accepted' | 'rejected' | 'revoked' | 'pending' | 'unavailable'; current_review_sha256: string | null;
  source_binding_valid: boolean};
type KnowledgeReport = Record<string, unknown> & {bundle_sha256: string; context_sha256: string; model_request_sha256: string;
  historical_binding_verified: true; model_input_bound: true; proposal_recorded: boolean; real_model: boolean;
  model_acknowledgement_recorded: boolean; actual_model_use_verified: boolean; current_review_valid: boolean;
  current_source_valid: boolean; current_authority_valid: boolean; review_expired: boolean; sources: KnowledgeSource[]};
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
const identity = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const nonnegative = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const nullableHash = (value: unknown) => value === null || knowledgeHash(value);
const same = (left: unknown, right: unknown) => knowledgeCanonical(left) === knowledgeCanonical(right);
function canonicalObjectMatches(raw: unknown, value: unknown, maximum: number): raw is string {
  if (typeof raw !== 'string' || !raw.length || raw.length > maximum) return false;
  try {return same(JSON.parse(raw), value);} catch {return false;}
}
const knowledgeReportFalse = ['semantic_relevance_verified', 'downstream_verified', 'training_ready',
  'execution_authorized', 'activation_authorized', 'gpu_release_verified'];
const knowledgeReportBooleans = ['proposal_recorded', 'real_model', 'model_acknowledgement_recorded', 'actual_model_use_verified',
  'current_review_valid', 'current_source_valid', 'current_authority_valid', 'review_expired'];
async function validKnowledgeGoalReport(value: unknown, checksum: string): Promise<boolean> {
  if (!object(value) || !exact(value, ['schema_version', 'kind', 'bundle_sha256', 'intent_sha256', 'planning_id', 'stage',
    'review_sha256', 'context_sha256', 'model_request_sha256', 'model_response_sha256', 'deployment_sha256',
    'acknowledgement_sha256', 'acknowledgement', 'acknowledgement_canonical', 'bundle', 'bundle_canonical',
    'model_request_canonical', 'model_response_canonical', 'deployment_canonical', 'current_authority_check',
    'historical_binding_verified', 'model_input_bound', 'sources', 'errors', ...knowledgeReportFalse, ...knowledgeReportBooleans])
    || value.schema_version !== '1.0' || value.kind !== 'web_goal_knowledge_report' || value.bundle_sha256 !== checksum
    || value.historical_binding_verified !== true || value.model_input_bound !== true
    || !knowledgeReportFalse.every(key => value[key] === false) || !knowledgeReportBooleans.every(key => typeof value[key] === 'boolean')
    || !['intent', 'proposal'].includes(String(value.stage)) || typeof value.planning_id !== 'string'
    || !/^planning-[a-f0-9]{32}$/.test(value.planning_id) || !nullableHash(value.intent_sha256) || !nullableHash(value.model_response_sha256)
    || !nullableHash(value.acknowledgement_sha256) || !['review_sha256', 'context_sha256', 'model_request_sha256', 'deployment_sha256'].every(key => knowledgeHash(value[key]))
    || !object(value.bundle) || !canonicalObjectMatches(value.bundle_canonical, value.bundle, 131072)
    || await knowledgeTextHash(value.bundle_canonical) !== checksum || value.bundle.stage !== value.stage
    || value.bundle.planning_id !== value.planning_id || value.bundle.intent_sha256 !== value.intent_sha256
    || !object(value.bundle.knowledge) || !object(value.bundle.knowledge.review) || !object(value.bundle.deployment)
    || !object(value.bundle.model_request) || !object(value.bundle.request) || !object(value.bundle.catalog)
    || value.bundle.knowledge.inference_consent !== true || value.bundle.knowledge.storage_consent !== true
    || value.bundle.deployment.real_model !== value.real_model || value.proposal_recorded !== (value.stage === 'proposal')
    || value.current_authority_check !== 'current_planning_admission'
    || !canonicalObjectMatches(value.model_request_canonical, value.bundle.model_request, 131072)
    || !value.bundle_canonical.includes('"model_request":' + value.model_request_canonical + ',')
    || value.model_request_sha256 !== await knowledgeTextHash(value.model_request_canonical)
    || !canonicalObjectMatches(value.deployment_canonical, value.bundle.deployment, 131072)
    || !value.bundle_canonical.includes('"deployment":' + value.deployment_canonical + ',')
    || value.deployment_sha256 !== await knowledgeTextHash(value.deployment_canonical)
    || !Array.isArray(value.sources) || value.sources.length < 1 || value.sources.length > 8 || !Array.isArray(value.errors)
    || value.errors.length > 3 || !value.errors.every(error => ['current_source_invalid', 'current_authority_invalid', 'review_expired'].includes(String(error)))) return false;
  const review = value.bundle.knowledge.review;
  if (!object(review.retrieval) || !validKnowledgeScope(review.retrieval.scope) || typeof review.query !== 'string'
    || typeof review.goal !== 'string' || !object(review.payload) || review.payload.untrusted !== true
    || review.payload.context_version !== 'aos-owned-skill-knowledge-v1' || !Array.isArray(review.retrieval.hits)
    || !await validKnowledgeSearch(review.retrieval, review.retrieval.scope, review.query, 4, 2048)
    || review.retrieval.hits.length !== value.sources.length || review.confirm_sha256 !== value.review_sha256
    || review.payload.context_sha256 !== value.context_sha256 || review.deployment_sha256 !== value.deployment_sha256
    || review.goal !== value.bundle.request.goal || !same(review.authority, value.bundle.authority)
    || review.catalog_sha256 !== await knowledgeDigest(value.bundle.catalog)) return false;
  const {confirm_sha256, ...reviewBody} = review;
  const context = knowledgeCanonical({context_version: review.payload.context_version, scope: review.retrieval.scope, query: review.query,
    citations: review.retrieval.hits.map((hit, index) => ({...(hit as Record<string, unknown>), citation_id: `c${index + 1}`})),
    untrusted: true, execution_authorized: false, training_ready: false, gold: false, manual_approval_required: true,
    causality_verified: false, scope_authorization_verified: false});
  const messages = value.bundle.model_request.messages;
  if (confirm_sha256 !== await knowledgeDigest(reviewBody) || review.payload.context_text !== context
    || value.context_sha256 !== await knowledgeTextHash(context) || !Array.isArray(messages) || messages.length !== 2
    || !object(messages[1]) || messages[1].role !== 'user' || messages[1].content !== knowledgeCanonical({goal: review.goal,
      catalog: value.bundle.catalog, reviewed_documents: review.payload})
    || value.current_review_valid !== (value.current_source_valid && value.current_authority_valid && !value.review_expired)) return false;
  for (const [index, source] of value.sources.entries()) {
    const hit = review.retrieval.hits[index];
    if (!object(source) || !exact(source, ['document_sha256', 'content_sha256', 'review_sha256', 'chunk_sha256', 'source_id', 'title',
      'synthetic', 'available', 'current', 'expired', 'current_review_status', 'current_review_sha256', 'source_binding_valid'])
      || !object(hit) || !['document_sha256', 'content_sha256', 'review_sha256', 'chunk_sha256', 'source_id', 'title', 'synthetic'].every(key => source[key] === hit[key])
      || !['available', 'current', 'source_binding_valid'].every(key => typeof source[key] === 'boolean')
      || !(source.expired === null || typeof source.expired === 'boolean') || !nullableHash(source.current_review_sha256)
      || !['accepted', 'rejected', 'revoked', 'pending', 'unavailable'].includes(String(source.current_review_status))
      || (value.current_source_valid && source.source_binding_valid !== true)
      || (source.source_binding_valid && (source.available !== true || source.current !== true || source.expired !== false
        || source.current_review_status !== 'accepted' || source.current_review_sha256 !== source.review_sha256))) return false;
  }
  if (value.stage === 'intent' ? value.intent_sha256 !== null || value.model_response_sha256 !== null
      || value.model_response_canonical !== null || value.bundle.model_response !== null
    : !knowledgeHash(value.intent_sha256) || !object(value.bundle.model_response)
      || !canonicalObjectMatches(value.model_response_canonical, value.bundle.model_response, 131072)
      || !value.bundle_canonical.includes('"model_response":' + value.model_response_canonical + ',')
      || value.model_response_sha256 !== await knowledgeTextHash(value.model_response_canonical)) return false;
  if (value.model_acknowledgement_recorded) {
    const expected = {schema_version: '1.0', kind: 'web_goal_knowledge_acknowledgement', planning_id: value.planning_id,
      bundle_sha256: checksum, intent_sha256: value.intent_sha256, review_sha256: value.review_sha256,
      context_sha256: value.context_sha256, model_request_sha256: value.model_request_sha256,
      model_response_sha256: value.model_response_sha256, deployment_sha256: value.deployment_sha256,
      real_model: value.real_model, acknowledgement_code_path: 'bonsai-web-goal-native-last-request-response-v1'};
    if (value.stage !== 'proposal' || !same(value.acknowledgement, expected)
      || !canonicalObjectMatches(value.acknowledgement_canonical, expected, 8192)
      || value.acknowledgement_sha256 !== await knowledgeTextHash(value.acknowledgement_canonical)) return false;
  } else if (value.acknowledgement !== null || value.acknowledgement_canonical !== null || value.acknowledgement_sha256 !== null) return false;
  return value.actual_model_use_verified === (value.model_acknowledgement_recorded && value.real_model);
}
const authorityIds = ['manager_session', 'desktop_session_id', 'runtime_id', 'lease_id'];
const authorityHashes = ['reuse_admission_sha256', 'source_manifest_sha256', 'source_run_ref', 'source_invocation_sha256',
  'source_fingerprint_sha256', 'family_sha256', 'release_sha256', 'selection_sha256', 'review_sha256', 'candidate_sha256', 'recipe_sha256'];
async function validKnowledgeGoalReview(value: unknown, selection: Selection, source: OwnedFormInvocation,
  snapshot: Snapshot, goal: string, query: string): Promise<boolean> {
  const catalog = selection.catalog;
  const scope = {application_id: catalog.application_key, tenant_id: catalog.tenant_key, account_role: catalog.account_role};
  if (!object(value) || !exact(value, ['schema_version', 'kind', 'goal', 'authority', 'catalog_sha256', 'deployment_sha256',
    'query', 'retrieval', 'workspace_identity', 'store_identity', 'payload', 'expires_at', 'confirm_sha256'])
    || value.schema_version !== '1.0' || value.kind !== 'web_goal_knowledge_review' || value.goal !== goal || value.query !== query
    || value.catalog_sha256 !== selection.catalog_sha256 || !knowledgeHash(value.deployment_sha256) || !knowledgeHash(value.confirm_sha256)
    || !object(value.authority) || !exact(value.authority, [...authorityIds, ...authorityHashes, 'generation'])
    || !authorityIds.every(key => identity((value.authority as Record<string, unknown>)[key]))
    || !authorityHashes.every(key => knowledgeHash((value.authority as Record<string, unknown>)[key]))
    || value.authority.runtime_id !== snapshot.runtime.runtime_id || value.authority.lease_id !== snapshot.control.lease_id
    || value.authority.generation !== snapshot.control.generation || !nonnegative(value.authority.generation)
    || value.authority.reuse_admission_sha256 !== source.reuse_admission_sha256 || value.authority.source_run_ref !== source.run_ref
    || value.authority.source_invocation_sha256 !== source.invocation_sha256
    || value.authority.release_sha256 !== catalog.skills[0].release_sha256 || value.authority.selection_sha256 !== catalog.skills[0].selection_sha256
    || value.authority.recipe_sha256 !== catalog.skills[0].recipe_sha256 || !validKnowledgeScope(scope)
    || !await validKnowledgeSearch(value.retrieval, scope, query, 4, 2048)
    || !object(value.retrieval) || !Array.isArray(value.retrieval.hits) || !value.retrieval.hits.length
    || !object(value.workspace_identity) || !exact(value.workspace_identity, ['version', 'path_sha256', 'device', 'inode', 'owner_uid'])
    || value.workspace_identity.version !== 'descriptor-workspace-v1' || !knowledgeHash(value.workspace_identity.path_sha256)
    || !['device', 'inode', 'owner_uid'].every(key => nonnegative((value.workspace_identity as Record<string, unknown>)[key]))
    || Number(value.workspace_identity.inode) < 1 || !object(value.store_identity)
    || !exact(value.store_identity, ['device', 'inode', 'owner', 'mode']) || !Object.values(value.store_identity).every(nonnegative)
    || !object(value.payload) || !exact(value.payload, ['context_version', 'context_text', 'context_sha256', 'untrusted'])
    || value.payload.context_version !== 'aos-owned-skill-knowledge-v1' || value.payload.untrusted !== true
    || typeof value.payload.context_text !== 'string' || value.payload.context_text.length > 8192 || !knowledgeHash(value.payload.context_sha256)
    || typeof value.expires_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value.expires_at)
    || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now()) return false;
  const context = knowledgeCanonical({context_version: value.payload.context_version, scope, query,
    citations: value.retrieval.hits.map((hit, index) => ({...(hit as Record<string, unknown>), citation_id: `c${index + 1}`})),
    untrusted: true, execution_authorized: false, training_ready: false, gold: false, manual_approval_required: true,
    causality_verified: false, scope_authorization_verified: false});
  const {confirm_sha256, ...content} = value;
  return value.payload.context_text === context && value.payload.context_sha256 === await knowledgeTextHash(context)
    && confirm_sha256 === await knowledgeDigest(content);
}
type Result = {schema_version: '1.0'; proposal_bundle_sha256: string; job_id: string;
  job_status: string;
  status: 'pending_verification' | 'verified' | 'not_verified'; independently_verified: boolean;
  receipt_sha256: string; report_sha256: string | null; verification_scope: 'owned_synthetic_recipe';
  execution_authorized: false; training_ready: false; gpu_release_verified: false};
type Preview = {schema_version: '1.3'; available: true; status: 'preview'; preview_sha256: string;
  profile_sha256: string; source_run_ref: string; reuse_admission_sha256: string;
  release_sha256: string; selection_sha256: string; recipe_sha256: string; steps: OwnedFormRecipeStep[];
  proposal_bundle_sha256?: string; proposal?: {catalog_sha256: string; parameters: Record<string, string>}};
type Props = {source: OwnedFormInvocation; snapshot: Snapshot; disabled: boolean; planning?: PlanningStatus};
const base = '/api/tasks/owned-web-goal/';

export function OwnedWebGoalCatalog({source, snapshot, disabled, planning}: Props) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const [selection, setSelection] = useState<Selection | null>(null);
  const [message, setMessage] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [goal, setGoal] = useState('');
  const [consent, setConsent] = useState(false);
  const [planningId, setPlanningId] = useState<string | null>(null);
  const [confirmedExecution, setConfirmedExecution] = useState(false);
  const [startedJob, setStartedJob] = useState<string | null>(null);
  const [startedBundle, setStartedBundle] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [recoveryJob, setRecoveryJob] = useState('');
  const [confirmRecovery, setConfirmRecovery] = useState(false);
  const [startUncertain, setStartUncertain] = useState(false);
  const [operationLabel, setOperationLabel] = useState<string | null>(null);
  const [knowledgeQuery, setKnowledgeQuery] = useState('');
  const [knowledgeReview, setKnowledgeReview] = useState<KnowledgeReview | null>(null);
  const [documentInferenceConsent, setDocumentInferenceConsent] = useState(false);
  const [documentStorageConsent, setDocumentStorageConsent] = useState(false);
  const [knowledgeReport, setKnowledgeReport] = useState<KnowledgeReport | null>(null);
  const [knowledgeReportError, setKnowledgeReportError] = useState(false);
  const activeRequest = useRef<number | null>(null);
  const serial = useRef(0);
  const scope = JSON.stringify([snapshot.runtime.runtime_id, snapshot.control.lease_id, snapshot.control.generation,
    snapshot.control.owner, snapshot.control.status, source.profile_sha256, source.run_ref, source.reuse_admission_sha256]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const reportBundle = candidateHash(planning?.bundle_sha256) && ['ready', 'bound', 'consumed', 'needs_human'].includes(planning!.status)
    ? planning!.bundle_sha256! : null;
  const reportBundleRef = useRef(reportBundle);
  reportBundleRef.current = reportBundle;
  useEffect(() => {
    serial.current += 1; setSelection(null); setMessage(''); setPreview(null); setError(false); setLoading(false);
    setGoal(''); setConsent(false); setPlanningId(null);
    setConfirmedExecution(false); setStartedJob(null); setStartedBundle(null); setResult(null);
    setRecoveryJob(''); setConfirmRecovery(false);
    setStartUncertain(false); setOperationLabel(null); activeRequest.current = null;
    setKnowledgeQuery(''); setKnowledgeReview(null); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);
    setKnowledgeReport(null); setKnowledgeReportError(false);
  }, [scope]);
  useEffect(() => {
    if (planning?.status === 'consumed' && typeof planning.job_id === 'string'
        && /^[A-Za-z0-9_-]{1,128}$/.test(planning.job_id) && candidateHash(planning.bundle_sha256)) {
      setStartedJob(planning.job_id); setStartedBundle(planning.bundle_sha256); setStartUncertain(false);
    }
  }, [planning?.status, planning?.job_id, planning?.bundle_sha256, scope]);
  useEffect(() => {setKnowledgeReport(null); setKnowledgeReportError(false);}, [reportBundle]);
  useEffect(() => () => {serial.current += 1;}, []);
  if (!source.reuse_admission_sha256) return null;
  const locked = disabled || loading || startUncertain || Boolean(planning?.execution_review_pending)
    || planning?.status === 'pending' || snapshot.control.owner !== 'AGENT' || snapshot.control.status !== 'running';
  const validMessage = /^[A-Za-z0-9 _.-]{1,128}$/.test(message) && message === message.trim();
  const ownPlanning = planningId !== null && planning?.planning_id === planningId;
  const proposalWaiting = planningId !== null && (!ownPlanning || planning?.status === 'pending');

  function beginRequest(label: string) {
    if (activeRequest.current !== null) return null;
    const request = ++serial.current;
    activeRequest.current = request;
    setOperationLabel(label); setLoading(true); setError(false);
    return request;
  }
  function currentRequest(request: number) {
    return request === serial.current && scope === scopeRef.current;
  }
  function finishRequest(request: number) {
    if (activeRequest.current === request) activeRequest.current = null;
    if (currentRequest(request)) {setLoading(false); setOperationLabel(null);}
  }
  async function fresh() {
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id
        || current.control.lease_id !== snapshot.control.lease_id || current.control.generation !== snapshot.control.generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') throw new Error('control_changed');
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }
  async function operate(operation: 'catalog' | 'preview' | 'propose' | 'preview-planned' | 'start-planned' | 'preview-knowledge' | 'propose-knowledge') {
    if (locked) return;
    const request = beginRequest(operation);
    if (request === null) return;
    let startDispatched = false;
    setConfirmedExecution(false);
    setResult(null);
    setKnowledgeReport(null); setKnowledgeReportError(false);
    if (operation !== 'start-planned') {setPreview(null); setStartedJob(null); setStartedBundle(null);}
    try {
      const control = await fresh();
      if (!currentRequest(request)) return;
      if (operation === 'catalog') {
        setKnowledgeReview(null); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);
        setSelection(null);
        const result = await api<Selection>(base + operation, {schema_version: '1.0', ...control});
        const catalog = result.catalog;
        const skill = catalog?.skills?.[0];
        if (!candidateHash(result.catalog_sha256) || catalog?.schema_version !== '1.0' || catalog.synthetic !== true
            || catalog.profile_sha256 !== source.profile_sha256 || catalog.execution_authorized !== false
            || catalog.activation_authorized !== false || catalog.training_ready !== false || catalog.scope_authorization_verified !== false
            || typeof catalog.application_key !== 'string' || !Array.isArray(catalog.skills) || catalog.skills.length !== 1
            || !validKnowledgeScope({application_id: catalog.application_key, tenant_id: catalog.tenant_key, account_role: catalog.account_role})
            || typeof skill.skill_ref !== 'string' || typeof skill.description !== 'string'
            || !candidateHash(skill.release_sha256) || !candidateHash(skill.selection_sha256) || !candidateHash(skill.recipe_sha256)
            || !skill.parameters || Object.keys(skill.parameters).length !== 1) throw new Error('invalid_catalog');
        await fresh();
        if (currentRequest(request)) setSelection(result);
      } else if (operation === 'start-planned') {
        if (!preview?.proposal || !confirmedExecution || !candidateHash(preview.proposal_bundle_sha256)
            || planning?.planning_id !== planningId || planning.status !== 'ready') throw new Error('execution_not_reviewed');
        const checksum = preview.proposal_bundle_sha256;
        startDispatched = true;
        const result = await api<{accepted: boolean; job_id: string; proposal_bundle_sha256: string}>(base + operation, {
          schema_version: '1.0', ...control, bundle_sha256: checksum, confirm_bundle_sha256: checksum,
          preview_sha256: preview.preview_sha256, confirm_preview_sha256: preview.preview_sha256});
        if (result.accepted !== true || result.proposal_bundle_sha256 !== checksum || typeof result.job_id !== 'string'
            || !/^[A-Za-z0-9_-]{1,128}$/.test(result.job_id)) throw new Error('start_unproven');
        await fresh();
        if (currentRequest(request)) {setStartedJob(result.job_id); setStartedBundle(checksum); setPreview(null);}
      } else if (operation === 'preview-knowledge') {
        if (!selection || !goal.trim() || !knowledgeQuery.trim() || !planning?.knowledge_available) throw new Error('knowledge_not_ready');
        setKnowledgeReview(null); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);
        const result = await api<{schema_version: string; review: KnowledgeReview; execution_authorized: false; training_ready: false}>(base + operation, {
          schema_version: '1.0', goal, scope: {application_id: selection.catalog.application_key,
            tenant_id: selection.catalog.tenant_key, account_role: selection.catalog.account_role},
          query: knowledgeQuery, top_k: 4, context_chars: 2048, confirm_catalog_sha256: selection.catalog_sha256, ...control});
        if (!object(result) || !exact(result, ['schema_version', 'review', 'execution_authorized', 'training_ready'])
            || result.schema_version !== '1.0' || result.execution_authorized !== false || result.training_ready !== false
            || !await validKnowledgeGoalReview(result.review, selection, source, snapshot, goal, knowledgeQuery)) throw new Error('invalid_knowledge');
        await fresh();
        if (currentRequest(request)) setKnowledgeReview(result.review);
      } else if (operation === 'propose' || operation === 'propose-knowledge') {
        if (!selection || !goal.trim() || !planning?.available || (operation === 'propose' && !consent)) throw new Error('planning_not_ready');
        if (operation === 'propose-knowledge' && (!planning.knowledge_available || !documentInferenceConsent || !documentStorageConsent
            || !knowledgeReview || !await validKnowledgeGoalReview(knowledgeReview, selection, source, snapshot, goal, knowledgeQuery))) throw new Error('knowledge_not_reviewed');
        await fresh();
        if (!currentRequest(request)) return;
        setConsent(false); setPlanningId(null);
        setDocumentInferenceConsent(false); setDocumentStorageConsent(false);
        const payload = operation === 'propose' ? {goal, inference_consent: true, confirm_catalog_sha256: selection.catalog_sha256}
          : {review: knowledgeReview, confirm_knowledge_sha256: knowledgeReview!.confirm_sha256, inference_consent: true, storage_consent: true};
        const result = await api<PlanningStatus>(base + operation, {schema_version: '1.0', ...control, ...payload});
        if (result.execution_authorized !== false || result.status !== 'pending'
            || !result.planning_id?.match(/^planning-[a-f0-9]{32}$/)) throw new Error('invalid_planning');
        await fresh();
        if (currentRequest(request)) setPlanningId(result.planning_id);
      } else {
        if (!selection || operation === 'preview' && !validMessage) throw new Error('preview_not_ready');
        const skill = selection.catalog.skills[0];
        const parameter = Object.keys(skill.parameters)[0];
        const proposal = {schema_version: '1.0', catalog_sha256: selection.catalog_sha256,
          decision: 'propose_skill', skill_ref: skill.skill_ref, parameters: {[parameter]: message}, reason_code: 'skill_match',
          execution_authorized: false, activation_authorized: false, training_ready: false, scope_authorization_verified: false};
        if (operation === 'preview-planned' && (planning?.planning_id !== planningId || planning.status !== 'ready'
            || !candidateHash(planning.bundle_sha256))) throw new Error('planning_changed');
        const payload = operation === 'preview' ? {proposal, confirm_catalog_sha256: selection.catalog_sha256}
          : {bundle_sha256: planning!.bundle_sha256, confirm_bundle_sha256: planning!.bundle_sha256};
        const result = await api<Preview>(base + operation, {schema_version: '1.0', ...control, ...payload});
        if (result.schema_version !== '1.3' || result.available !== true || result.status !== 'preview'
            || result.profile_sha256 !== source.profile_sha256 || result.source_run_ref !== source.run_ref
            || result.reuse_admission_sha256 !== source.reuse_admission_sha256 || !candidateHash(result.preview_sha256)
            || result.release_sha256 !== skill.release_sha256 || result.selection_sha256 !== skill.selection_sha256
            || result.recipe_sha256 !== skill.recipe_sha256 || !isOwnedFormRecipeSteps(result.steps)) throw new Error('invalid_preview');
        if (operation === 'preview-planned' && (result.proposal_bundle_sha256 !== planning!.bundle_sha256
            || result.proposal?.catalog_sha256 !== selection.catalog_sha256)) throw new Error('planning_changed');
        await fresh();
        if (currentRequest(request)) setPreview(result);
      }
    } catch {
      if (currentRequest(request)) {
        setError(true); setSelection(null); setPreview(null);
        setKnowledgeReview(null); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);
        if (startDispatched) setStartUncertain(true);
      }
    } finally {
      finishRequest(request);
    }
  }
  async function recoverStart() {
    if (snapshot.control.owner !== 'AGENT' || snapshot.control.status !== 'running') return;
    const request = beginRequest('recover-start');
    if (request === null) return;
    const bundle = planning?.bundle_sha256;
    const job = recoveryJob.trim();
    const confirmed = confirmRecovery;
    setConfirmRecovery(false);
    try {
      if (!confirmed || !planning?.execution_review_pending || !candidateHash(bundle)
          || !/^[A-Za-z0-9_-]{1,128}$/.test(job)) throw new Error('recovery_not_confirmed');
      const control = await fresh();
      if (!currentRequest(request)) return;
      const response = await api<{schema_version: string; job_id: string; proposal_bundle_sha256: string;
        acknowledgement_recovered: boolean; execution_authorized: false; independently_verified: false;
        gpu_release_verified: false}>(base + 'recover-start', {
          schema_version: '1.0', bundle_sha256: bundle, job_id: job, confirm_job_id: job, ...control});
      if (response.schema_version !== '1.0' || response.job_id !== job || response.proposal_bundle_sha256 !== bundle
          || response.acknowledgement_recovered !== true || response.execution_authorized !== false
          || response.independently_verified !== false || response.gpu_release_verified !== false) throw new Error('invalid_recovery');
      await fresh();
      if (currentRequest(request)) {
        setStartedJob(job); setStartedBundle(bundle); setResult(null); setStartUncertain(false);
      }
    } catch {
      if (currentRequest(request)) setError(true);
    } finally {
      finishRequest(request);
    }
  }
  async function readResult() {
    const request = beginRequest('report');
    if (request === null) return;
    setResult(null);
    try {
      if (!startedJob || !candidateHash(startedBundle)) throw new Error('no_bound_job');
      const response = await api<Result>(base + 'report', {schema_version: '1.0', bundle_sha256: startedBundle});
      if (response.schema_version !== '1.0' || response.proposal_bundle_sha256 !== startedBundle || response.job_id !== startedJob
          || response.verification_scope !== 'owned_synthetic_recipe' || response.execution_authorized !== false
          || response.training_ready !== false || response.gpu_release_verified !== false || !candidateHash(response.receipt_sha256)
          || !['pending_verification', 'verified', 'not_verified'].includes(response.status)
          || (response.status === 'verified' ? response.job_status !== 'succeeded'
            || response.independently_verified !== true || !candidateHash(response.report_sha256)
            : response.independently_verified !== false || response.report_sha256 !== null)) throw new Error('invalid_result');
      if (currentRequest(request)) setResult(response);
    } catch {
      if (currentRequest(request)) setError(true);
    } finally {
      finishRequest(request);
    }
  }
  async function readKnowledgeReport() {
    const checksum = reportBundle;
    if (!candidateHash(checksum)) return;
    const request = beginRequest('report-knowledge');
    if (request === null) return;
    setKnowledgeReport(null); setKnowledgeReportError(false);
    try {
      const response = await api<KnowledgeReport>(base + 'report-knowledge', {schema_version: '1.0', bundle_sha256: checksum});
      if (!await validKnowledgeGoalReport(response, checksum)) throw new Error('invalid_knowledge_report');
      if (currentRequest(request) && reportBundleRef.current === checksum) setKnowledgeReport(response);
    } catch {
      if (currentRequest(request) && reportBundleRef.current === checksum) setKnowledgeReportError(true);
    } finally {
      finishRequest(request);
    }
  }
  return <section className="registry" data-testid="owned-web-goal-catalog">
    <h3>{text('Supported web skill', 'Desteklenen web skill')}</h3>
    <p className="caption">{text('Inspect the selected owned synthetic skill. Previews never start a task or training.',
      'Seçili sahipli sentetik skill’i inceleyin. Önizlemeler görev veya eğitim başlatmaz.')}</p>
    {operationLabel ? <p role="status" aria-live="polite">{operationLabel === 'start-planned'
      ? text('Submitting the reviewed task once. Waiting for its acknowledgement…', 'İncelenen görev bir kez gönderiliyor. Teyidi bekleniyor…')
      : operationLabel === 'propose' || operationLabel === 'propose-knowledge' ? text('Submitting the scoped proposal request…', 'Kapsamlı öneri isteği gönderiliyor…')
        : operationLabel === 'recover-start' ? text('Reading back the existing task. No task is being restarted…', 'Mevcut görev kaydı okunuyor. Görev yeniden başlatılmıyor…')
          : operationLabel === 'report' ? text('Checking the bound task and independent result evidence…', 'Bağlı görev ve bağımsız sonuç kanıtı denetleniyor…')
            : operationLabel === 'report-knowledge' ? text('Reading the saved document request binding and acknowledgement. No inference or execution…',
              'Kayıtlı belge istek bağı ve teyidi okunuyor. Çıkarım veya yürütme yok…')
            : text('Checking current authority and selected skill evidence…', 'Güncel yetki ve seçili skill kanıtı denetleniyor…')}</p> : null}
    {proposalWaiting ? <p role="status" aria-live="polite" data-testid="web-goal-planning-progress">
      {text('Proposal accepted. Local Bonsai is preparing a bounded proposal; no task has started. Progress arrives with the task status refresh.',
        'Öneri isteği kabul edildi. Yerel Bonsai sınırlı öneriyi hazırlıyor; görev başlamadı. İlerleme görev durumu yenilenirken gelir.')}</p> : null}
    {ownPlanning && planning?.status === 'ready' ? <p role="status">{text('Proposal ready. Read its exact parameters before approving a task.',
      'Öneri hazır. Görevi onaylamadan önce tam parametrelerini okuyun.')}</p> : null}
    {ownPlanning && planning?.status === 'needs_human' ? <p role="status">{text('The model could not propose a supported skill. Review the goal and catalog; nothing was executed.',
      'Model desteklenen bir skill öneremedi. Hedefi ve kataloğu inceleyin; hiçbir işlem yürütülmedi.')}</p> : null}
    {ownPlanning && (planning?.status === 'failed' || planning?.status === 'cancelled') ? <p role="status">{text('Proposal preparation failed or was cancelled. Nothing was automatically retried.',
      'Öneri hazırlığı başarısız oldu veya iptal edildi. Otomatik tekrar yapılmadı.')}</p> : null}
    <button disabled={locked} onClick={() => void operate('catalog')} data-testid="web-goal-load">{text('Inspect current catalog', 'Güncel kataloğu incele')}</button>
    {selection ? <div>
      <p>{selection.catalog.application_key}: {selection.catalog.skills[0].description}</p>
      <code style={{overflowWrap: 'anywhere'}}>{selection.catalog_sha256}</code>
      {planning?.available ? <div>
        <label>{text('Your goal', 'Hedefiniz')}<textarea value={goal} maxLength={4096} disabled={locked}
          data-testid="web-goal-free-text" style={{display: 'block', width: '100%', boxSizing: 'border-box'}}
          onChange={event => {setGoal(event.target.value); setPreview(null); setConsent(false); setPlanningId(null);
            setKnowledgeReview(null); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);}}/></label>
        <label><input type="checkbox" checked={consent} disabled={locked} data-testid="web-goal-inference-consent"
          onChange={event => setConsent(event.target.checked)}/>{text('Run local Bonsai for this goal and store its private proposal. No execution or training.',
            'Bu hedef için yerel Bonsai çalıştır ve özel önerisini sakla. Yürütme veya eğitim yok.')}</label>
        <button disabled={locked || !consent || !goal.trim()} data-testid="web-goal-generate" onClick={() => void operate('propose')}>
          {text('Generate scoped proposal', 'Kapsamlı öneri üret')}</button>
        {planning.knowledge_available ? <div data-testid="web-goal-knowledge">
          <h4>{text('Optional reviewed document context', 'İsteğe bağlı incelenmiş belge bağlamı')}</h4>
          <p className="caption">{text('Search only already accepted documents in this application, tenant and role. Excerpts are untrusted context, not instructions or execution authority.',
            'Yalnız bu uygulama, tenant ve rol kapsamında önceden kabul edilmiş belgeleri arayın. Alıntılar güvenilmeyen bağlamdır; talimat veya yürütme yetkisi değildir.')}</p>
          <label>{text('Document query', 'Belge sorgusu')}<input value={knowledgeQuery} maxLength={512} disabled={locked}
            data-testid="web-goal-knowledge-query" onChange={event => {setKnowledgeQuery(event.target.value); setKnowledgeReview(null);
              setPreview(null); setConsent(false); setDocumentInferenceConsent(false); setDocumentStorageConsent(false);}}/></label>
          <button data-testid="web-goal-knowledge-preview" disabled={locked || !goal.trim() || !knowledgeQuery.trim()}
            onClick={() => void operate('preview-knowledge')}>{text('Inspect document citations without inference', 'Çıkarım yapmadan belge atıflarını incele')}</button>
          {knowledgeReview ? <div data-testid="web-goal-knowledge-review">
            <p>{text('Review each excerpt and its source hash before consenting. This review expires at ',
              'Onay vermeden önce her alıntıyı ve kaynak hash’ini inceleyin. Bu incelemenin son geçerlilik zamanı: ')}{knowledgeReview.expires_at}</p>
            <ol>{knowledgeReview.retrieval.hits.map((hit, index) => <li key={`${hit.document_sha256}:${hit.chunk_index}`}>
              <strong>{`c${index + 1}`}: {hit.title}</strong> <span>{hit.source_id} · {text('revision', 'revizyon')} {hit.revision}</span>
              <blockquote style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{hit.text}</blockquote>
              <code style={{overflowWrap: 'anywhere'}}>{hit.document_sha256}</code>
              <p className="caption" style={{overflowWrap: 'anywhere'}}>{text('Chunk hash: ', 'Parça hash’i: ')}{hit.chunk_sha256}</p>
            </li>)}</ol>
            <label><input type="checkbox" checked={documentInferenceConsent} disabled={locked} data-testid="web-goal-document-inference-consent"
              onChange={event => setDocumentInferenceConsent(event.target.checked)}/>{text('I reviewed these exact excerpts. Use them as untrusted context in local Bonsai inference.',
                'Tam olarak bu alıntıları inceledim. Yerel Bonsai çıkarımında güvenilmeyen bağlam olarak kullan.')}</label>
            <label><input type="checkbox" checked={documentStorageConsent} disabled={locked} data-testid="web-goal-document-storage-consent"
              onChange={event => setDocumentStorageConsent(event.target.checked)}/>{text('Store these exact excerpts and the proposal privately. This does not authorize training.',
                'Tam olarak bu alıntıları ve öneriyi özel olarak sakla. Bu eğitim yetkisi vermez.')}</label>
            <button data-testid="web-goal-knowledge-generate" disabled={locked || !documentInferenceConsent || !documentStorageConsent
              || Date.parse(knowledgeReview.expires_at) <= Date.now()} onClick={() => void operate('propose-knowledge')}>
              {text('Generate proposal with reviewed documents', 'İncelenmiş belgelerle öneri üret')}</button>
          </div> : null}
        </div> : null}
        <button data-testid="web-goal-preview-planned" disabled={locked || planningId !== planning.planning_id || planning.status !== 'ready'
          || !candidateHash(planning.bundle_sha256)} onClick={() => void operate('preview-planned')}>
          {text('Read proposal and preview parameters', 'Öneriyi oku ve parametreleri önizle')}</button>
      </div> : null}
      <label style={{display: 'block'}}>{text('Exact message (supported ASCII text)', 'Tam mesaj (desteklenen ASCII metin)')}
        <input value={message} maxLength={128} disabled={locked} data-testid="web-goal-message"
          onChange={event => {setMessage(event.target.value); setPreview(null);}}/></label>
      <button disabled={locked || !validMessage} data-testid="web-goal-preview" onClick={() => void operate('preview')}>
        {text('Preview manual parameters only', 'Yalnız manuel parametreleri önizle')}</button>
      {message && !validMessage ? <p className="caption">{text('Use 1–128 ASCII letters, digits, spaces, dots, underscores or hyphens, without leading or trailing spaces.',
        'Başında veya sonunda boşluk olmadan 1–128 ASCII harf, rakam, boşluk, nokta, alt çizgi veya tire kullanın.')}</p> : null}
    </div> : null}
    {preview ? <div data-testid="web-goal-preview-result"><ol>{preview.steps.map(step => <li key={step.step_key}>{step.operation}</li>)}</ol>
      {preview.proposal ? <pre style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{JSON.stringify(preview.proposal.parameters, null, 2)}</pre> : null}
      <code style={{overflowWrap: 'anywhere'}}>{preview.preview_sha256}</code>
      <p>{text('Preview only. Execution still needs separate fresh approval.', 'Yalnız önizleme. Yürütme için ayrı güncel onay gerekir.')}</p></div> : null}
    {preview?.proposal && !startedJob ? <div>
      <label><input type="checkbox" checked={confirmedExecution} disabled={locked} data-testid="web-goal-execution-confirm"
        onChange={event => setConfirmedExecution(event.target.checked)}/>{text('I reviewed these exact parameters. Start with fresh manual action approvals.',
          'Tam olarak bu parametreleri inceledim. Güncel manuel eylem onaylarıyla başlat.')}</label>
      <button disabled={locked || !confirmedExecution} data-testid="web-goal-start" onClick={() => void operate('start-planned')}>
        {text('Start reviewed task', 'İncelenen görevi başlat')}</button>
    </div> : null}
    {startedJob ? <p role="status">{text('Task started; follow the task timeline and independent result verification. ',
      'Görev başladı; görev zaman çizelgesini ve bağımsız sonuç doğrulamasını izleyin. ')}<code>{startedJob}</code></p> : null}
    {startedJob && startedBundle ? <button disabled={loading} data-testid="web-goal-read-result" onClick={() => void readResult()}>
      {text('Verify bound task result', 'Bağlı görev sonucunu doğrula')}</button> : null}
    {result ? <p role="status" data-testid="web-goal-result">{result.status === 'verified'
      ? text('Independent owned recipe verification passed. Not real-site acceptance or training approval.',
        'Bağımsız sahipli recipe doğrulaması geçti. Gerçek site kabulü veya eğitim onayı değildir.')
      : result.status === 'pending_verification' ? text('The bound task has not completed independent verification. Follow the task timeline, then check the result again.',
        'Bağlı görevin bağımsız doğrulaması tamamlanmadı. Görev zaman çizelgesini izleyin, ardından sonucu tekrar denetleyin.')
        : text('Result is not verified. No automatic retry.', 'Sonuç doğrulanmadı. Otomatik tekrar yok.')}</p> : null}
    {reportBundle && planning?.knowledge_available ? <button disabled={loading} data-testid="web-goal-read-knowledge-report"
      onClick={() => void readKnowledgeReport()}>{text('Inspect saved proposal document binding', 'Kayıtlı önerinin belge bağını incele')}</button> : null}
    {knowledgeReport ? <div data-testid="web-goal-knowledge-report" role="status">
      <h4>{text('Saved document-binding readback', 'Kayıtlı belge bağı readback’i')}</h4>
      <p>{text('Historical reviewed context and model request binding verified. This is not semantic relevance or task success.',
        'Tarihsel incelenmiş bağlam ve model isteği bağı doğrulandı. Bu semantik uygunluk veya görev başarısı değildir.')}</p>
      <p>{knowledgeReport.actual_model_use_verified
        ? text('A matching durable native-model acknowledgement is recorded for this context and proposal.',
          'Bu bağlam ve öneri için eşleşen kalıcı native-model teyidi kayıtlı.')
        : knowledgeReport.model_acknowledgement_recorded
          ? text('An acknowledgement is recorded, but the deployment is not a real model. Actual native-model use is unverified.',
            'Bir teyit kayıtlı, fakat deployment gerçek model değil. Gerçek native-model kullanımı doğrulanmadı.')
          : text('No matching durable model acknowledgement. A saved proposal alone does not prove document use by the model.',
            'Eşleşen kalıcı model teyidi yok. Kayıtlı öneri tek başına modelin belgeyi kullandığını kanıtlamaz.')}</p>
      <dl><dt>{text('Current document sources', 'Güncel belge kaynakları')}</dt><dd>{knowledgeReport.current_source_valid
        ? text('Valid at readback', 'Readback sırasında geçerli') : text('Not currently valid', 'Şu anda geçerli değil')}</dd>
        <dt>{text('Current fresh planning eligibility', 'Güncel yeni planlama uygunluğu')}</dt><dd>{knowledgeReport.current_authority_valid
          ? text('Available at readback', 'Readback sırasında kullanılabilir') : text('Unavailable; this is not a running-task ownership failure.',
            'Kullanılamıyor; bu çalışan görevin sahiplik hatası değildir.')}</dd>
        <dt>{text('Document review', 'Belge incelemesi')}</dt><dd>{knowledgeReport.review_expired
          ? text('Expired', 'Süresi dolmuş') : text('Not expired at readback', 'Readback sırasında süresi dolmamış')}</dd></dl>
      <ul>{knowledgeReport.sources.map((source, index) => <li key={`${source.document_sha256}:${index}`}>
        {source.title} · {source.source_id} · {source.source_binding_valid ? text('current binding valid', 'güncel bağ geçerli')
          : text('current binding invalid', 'güncel bağ geçersiz')}
        <p className="caption" style={{overflowWrap: 'anywhere'}}>{text('Document: ', 'Belge: ')}{source.document_sha256}</p>
      </li>)}</ul>
      <p className="caption" style={{overflowWrap: 'anywhere'}}>{text('Context hash: ', 'Bağlam hash’i: ')}{knowledgeReport.context_sha256}</p>
      <p className="caption" style={{overflowWrap: 'anywhere'}}>{text('Model request hash: ', 'Model isteği hash’i: ')}{knowledgeReport.model_request_sha256}</p>
      <p>{text('Read-only snapshot. Relevance, downstream success, training approval and GPU release are not verified. No model, task or retry was started.',
        'Salt okunur anlık görüntü. Uygunluk, sonraki görev başarısı, eğitim onayı ve GPU bırakımı doğrulanmadı. Model, görev veya tekrar başlatılmadı.')}</p>
    </div> : null}
    {knowledgeReportError ? <p role="alert">{text('Saved document-binding evidence could not be verified. No model or task was retried.',
      'Kayıtlı belge bağı kanıtı doğrulanamadı. Model veya görev tekrarlanmadı.')}</p> : null}
    {startUncertain || planning?.execution_review_pending ? <p role="alert" data-testid="web-goal-start-uncertain">{text('Start acknowledgement is unresolved. New model/task admission is blocked. Inspect the task timeline; do not repeat the start.',
      'Başlangıç teyidi belirsiz. Yeni model/görev kabulü engellendi. Görev zaman çizelgesini inceleyin; başlangıcı tekrar etmeyin.')}</p> : null}
    {planning?.execution_review_pending && planning.status === 'bound' && candidateHash(planning.bundle_sha256) ? <div>
      <label>{text('Existing task ID from the timeline', 'Zaman çizelgesindeki mevcut görev kimliği')}
        <input value={recoveryJob} maxLength={128} disabled={loading} data-testid="web-goal-recovery-job" onChange={event => {
          setRecoveryJob(event.target.value); setConfirmRecovery(false);
        }} /></label>
      <label><input type="checkbox" checked={confirmRecovery} disabled={loading} data-testid="web-goal-recovery-confirm" onChange={event => setConfirmRecovery(event.target.checked)} />
        {text('Confirm readback of this existing task only. Do not start it again.',
          'Yalnız bu mevcut görevin kaydını doğrula. Görevi tekrar başlatma.')}</label>
      <button data-testid="web-goal-recover-start" disabled={loading || !confirmRecovery || !/^[A-Za-z0-9_-]{1,128}$/.test(recoveryJob.trim())
          || snapshot.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onClick={() => void recoverStart()}>
        {text('Recover existing task acknowledgement', 'Mevcut görev teyidini kurtar')}</button>
    </div> : null}
    {error && !startUncertain ? <p role="alert">{text('The request could not be verified against current source and control. Inspect the task timeline; no automatic retry.',
      'İstek güncel kaynak ve kontrolle doğrulanamadı. Görev zaman çizelgesini inceleyin; otomatik tekrar yok.')}</p> : null}
  </section>;
}
