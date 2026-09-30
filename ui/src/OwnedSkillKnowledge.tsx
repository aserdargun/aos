import {useEffect, useRef, useState} from 'react';
import {api, isOwnedFormRecipeSteps, type OwnedFormInvocation, type Snapshot, type Tasks} from './api';
import {useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope, validKnowledgeSearch, type KnowledgeScope} from './knowledgeApi';
import type {PlanningStatus} from './OwnedSkillPlanning';

type ObjectValue = Record<string, unknown>;
const record = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: ObjectValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const equal = (left: unknown, right: unknown) => knowledgeCanonical(left) === knowledgeCanonical(right);
const canonicalMatches = (raw: unknown, value: unknown) => {
  if (typeof raw !== 'string' || raw.length > 131072) return false;
  try {return equal(JSON.parse(raw), value);} catch {return false;}
};
const integer = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;
const identifier = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const flagKeys = ['untrusted', 'execution_authorized', 'training_ready', 'gold', 'manual_approval_required', 'causality_verified', 'scope_authorization_verified'];
const flags = (value: ObjectValue) => value.untrusted === true && value.manual_approval_required === true
  && ['execution_authorized', 'training_ready', 'gold', 'causality_verified', 'scope_authorization_verified'].every(key => value[key] === false);
const authorityIds = ['manager_session', 'desktop_session_id', 'runtime_id', 'lease_id'];
const authorityHashes = ['reuse_admission_sha256', 'source_manifest_sha256', 'source_run_ref', 'source_invocation_sha256',
  'source_fingerprint_sha256', 'family_sha256', 'release_sha256', 'selection_sha256', 'review_sha256', 'candidate_sha256', 'recipe_sha256'];
const previewKeys = ['schema_version', 'kind', 'preview_id', 'expires_at', 'goal', 'case_key', 'scope', 'query', 'top_k', 'context_chars',
  'retrieval', 'authority', 'evidence', 'deployment', 'model_pins', 'workspace_identity', 'store_identity', 'context_version', 'context_text', 'context_sha256', 'confirm_sha256', ...flagKeys];
const payloadSchemaHash = '136c4bea556199ce86d2d88dec39194063209728a1f95b8906a308db47953595';

export async function validOwnedSkillKnowledgePreview(value: unknown, scope: KnowledgeScope, query: string, goal: string,
  runtimeId: string, reuseAdmission: string, snapshot?: Snapshot): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'preview', 'preview_canonical', 'preview_body_canonical', 'model_pins_canonical'])
    || value.schema_version !== '1.1' || !canonicalMatches(value.preview_canonical, value.preview)) return false;
  const transport = value;
  value = transport.preview;
  if (!record(value) || !exact(value, previewKeys) || !flags(value) || value.schema_version !== '1.0'
    || value.kind !== 'owned_skill_knowledge_preview' || typeof value.preview_id !== 'string'
    || !/^owned-skill-knowledge-preview-[a-f0-9]{32}$/.test(value.preview_id)
    || typeof value.expires_at !== 'string' || !Number.isFinite(Date.parse(value.expires_at))
    || value.goal !== goal || value.query !== query || !equal(value.scope, scope) || !validKnowledgeScope(value.scope)
    || !goal.length || goal.length > 256 || !query.length || Array.from(query).length > 512
    || value.case_key !== 'plan-' + (await knowledgeDigest({goal})).slice(0, 32)
    || !(snapshot ? value.top_k === 4 && value.context_chars === 2048 : integer(value.top_k) && Number(value.top_k) >= 1
      && Number(value.top_k) <= 4 && integer(value.context_chars) && Number(value.context_chars) >= 256 && Number(value.context_chars) <= 2048)
    || !await validKnowledgeSearch(value.retrieval, scope, query, Number(value.top_k), Number(value.context_chars))
    || !record(value.retrieval) || !Array.isArray(value.retrieval.hits) || !value.retrieval.hits.length
    || !record(value.authority) || !exact(value.authority, [...authorityIds, ...authorityHashes, 'generation'])
    || !authorityIds.every(key => identifier((value.authority as ObjectValue)[key]))
    || !authorityHashes.every(key => knowledgeHash((value.authority as ObjectValue)[key]))
    || !integer(value.authority.generation) || value.authority.runtime_id !== runtimeId
    || value.authority.reuse_admission_sha256 !== reuseAdmission
    || !Array.isArray(value.evidence) || value.evidence.length !== 1 || !record(value.evidence[0])
    || !exact(value.evidence[0], ['id', 'skill_key', 'parameter_key', 'form_field_name', 'ordered_steps', 'requested_case_key', 'requested_value'])
    || value.evidence[0].id !== 'admitted-skill' || value.evidence[0].requested_case_key !== value.case_key
    || !isOwnedFormRecipeSteps(value.evidence[0].ordered_steps)
    || !['skill_key', 'parameter_key'].every(key => typeof (value.evidence as ObjectValue[])[0][key] === 'string'
      && /^[a-z][a-z0-9_-]{0,63}$/.test(String((value.evidence as ObjectValue[])[0][key])))
    || typeof value.evidence[0].form_field_name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(value.evidence[0].form_field_name)
    || typeof value.evidence[0].requested_value !== 'string'
    || !/^[A-Za-z0-9 _.-]{1,128}$/.test(value.evidence[0].requested_value) || value.evidence[0].requested_value.trim() !== value.evidence[0].requested_value
    || !["Save message \"" + value.evidence[0].requested_value + '"', 'Mesaj alanına "' + value.evidence[0].requested_value + '" kaydet'].includes(goal)
    || !record(value.deployment) || !exact(value.deployment, ['deployment_id', 'kind', 'real_model', 'pins'])
    || typeof value.deployment.real_model !== 'boolean' || !record(value.model_pins) || !equal(value.deployment.pins, value.model_pins)
    || value.model_pins.owned_skill_knowledge_context_protocol !== 'aos-owned-skill-knowledge-v1'
    || value.model_pins.owned_skill_knowledge_context_schema_sha256 !== payloadSchemaHash
    || !knowledgeHash(value.model_pins.owned_skill_plan_schema_sha256)
    || value.deployment.kind !== (value.deployment.real_model ? 'bonsai_native_owned_skill_planner' : 'fixture_owned_skill_planner')
    || !canonicalMatches(transport.model_pins_canonical, value.model_pins)
    || value.deployment.deployment_id !== (value.deployment.real_model ? 'bonsai-' : 'fixture-') + await knowledgeTextHash(String(transport.model_pins_canonical))
    || !record(value.workspace_identity) || !exact(value.workspace_identity, ['version', 'path_sha256', 'device', 'inode', 'owner_uid'])
    || value.workspace_identity.version !== 'descriptor-workspace-v1' || !knowledgeHash(value.workspace_identity.path_sha256)
    || !['device', 'inode', 'owner_uid'].every(key => integer((value.workspace_identity as ObjectValue)[key]))
    || !record(value.store_identity) || !exact(value.store_identity, ['device', 'inode', 'owner', 'mode'])
    || !Object.values(value.store_identity).every(integer) || value.context_version !== 'aos-owned-skill-knowledge-v1'
    || !knowledgeHash(value.context_sha256) || !knowledgeHash(value.confirm_sha256) || typeof value.context_text !== 'string') return false;
  if (snapshot && (value.authority.lease_id !== snapshot.control.lease_id || value.authority.generation !== snapshot.control.generation)) return false;
  const context = knowledgeCanonical({context_version: value.context_version, scope, query,
    citations: value.retrieval.hits.map((hit, index) => ({...(hit as ObjectValue), citation_id: `c${index + 1}`})),
    untrusted: true, execution_authorized: false, training_ready: false, gold: false, manual_approval_required: true,
    causality_verified: false, scope_authorization_verified: false});
  const {confirm_sha256, ...content} = value;
  const body = String(transport.preview_body_canonical);
  const insertion = body.indexOf('"context_chars":');
  const expectedCanonical = body.slice(0, insertion) + '"confirm_sha256":' + knowledgeCanonical(confirm_sha256) + ',' + body.slice(insertion);
  return value.context_text === context && value.context_sha256 === await knowledgeTextHash(context)
    && canonicalMatches(transport.preview_body_canonical, content)
    && body.startsWith('{') && insertion > 0 && transport.preview_canonical === expectedCanonical
    && body.includes('"model_pins":' + String(transport.model_pins_canonical) + ',')
    && body.includes('"pins":' + String(transport.model_pins_canonical) + ',"real_model":' + String(value.deployment.real_model) + '}')
    && confirm_sha256 === await knowledgeTextHash(body);
}

export async function validOwnedSkillKnowledgeReport(value: unknown, checksum: string, runtimeId: string, reuseAdmission: string): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'report', 'preview_canonical', 'preview_body_canonical',
    'model_pins_canonical', 'intent_canonical', 'model_request_canonical']) || value.schema_version !== '1.1') return false;
  const transport = value;
  value = transport.report;
  if (!record(value) || !exact(value, ['schema_version', 'kind', 'planning_bundle_sha256', 'bundle', 'bundle_canonical',
    'historical_binding_verified', 'current_source_valid', 'current_authority_valid', 'dispatch_recorded', 'model_request_verified',
    'knowledge_applied', 'real_model', 'downstream_verified', 'semantic_relevance_verified', ...flagKeys])
    || !flags(value) || value.schema_version !== '1.0' || value.kind !== 'owned_skill_knowledge_report'
    || !knowledgeHash(checksum) || value.planning_bundle_sha256 !== checksum || typeof value.bundle_canonical !== 'string'
    || value.bundle_canonical.length > 131072 || await knowledgeTextHash(value.bundle_canonical) !== checksum
    || value.historical_binding_verified !== true || value.dispatch_recorded !== true || value.downstream_verified !== false
    || value.semantic_relevance_verified !== false || !['current_source_valid', 'current_authority_valid', 'model_request_verified',
      'knowledge_applied', 'real_model'].every(key => typeof value[key] === 'boolean')
    || value.model_request_verified !== value.real_model || value.knowledge_applied !== value.real_model || !record(value.bundle)) return false;
  try {if (!equal(JSON.parse(value.bundle_canonical), value.bundle)) return false;} catch {return false;}
  const bundle = value.bundle;
  if (!exact(bundle, ['schema_version', 'synthetic', 'purpose', 'planning_id', 'request', 'authority', 'evidence', 'deployment', 'model_pins',
    'model_request', 'model_response', 'metrics', 'real_model', 'execution_authorized', 'activation_authorized', 'training_ready', 'downstream_verified', 'knowledge'])
    || bundle.schema_version !== '1.2' || bundle.purpose !== 'owned_selected_skill_planning'
    || typeof bundle.planning_id !== 'string' || !/^planning-[a-f0-9]{32}$/.test(bundle.planning_id)
    || !['execution_authorized', 'activation_authorized', 'training_ready', 'downstream_verified'].every(key => bundle[key] === false)
    || bundle.real_model !== value.real_model || !record(bundle.knowledge)
    || !exact(bundle.knowledge, ['intent_sha256', 'intent', 'dispatch']) || !record(bundle.knowledge.intent)
    || !exact(bundle.knowledge.intent, ['schema_version', 'preview', 'inference_consent', 'storage_consent'])
    || bundle.knowledge.intent.schema_version !== '1.0' || bundle.knowledge.intent.inference_consent !== true
    || bundle.knowledge.intent.storage_consent !== true || !canonicalMatches(transport.intent_canonical, bundle.knowledge.intent)
    || transport.intent_canonical !== '{"inference_consent":true,"preview":' + String(transport.preview_canonical) + ',"schema_version":"1.0","storage_consent":true}'
    || !value.bundle_canonical.includes('"intent":' + String(transport.intent_canonical) + ',')
    || bundle.knowledge.intent_sha256 !== await knowledgeTextHash(String(transport.intent_canonical))
    || !record(bundle.knowledge.intent.preview)) return false;
  const preview = bundle.knowledge.intent.preview;
  if (!validKnowledgeScope(preview.scope) || typeof preview.goal !== 'string' || typeof preview.query !== 'string'
    || !await validOwnedSkillKnowledgePreview({schema_version: '1.1', preview, preview_canonical: transport.preview_canonical,
      preview_body_canonical: transport.preview_body_canonical, model_pins_canonical: transport.model_pins_canonical},
      preview.scope, preview.query, preview.goal, runtimeId, reuseAdmission)
    || !record(bundle.request) || !exact(bundle.request, ['schema_version', 'goal', 'case_key', 'lease_id', 'generation'])
    || bundle.request.schema_version !== '1.0' || bundle.request.goal !== preview.goal || bundle.request.case_key !== preview.case_key
    || !record(preview.authority) || bundle.request.lease_id !== preview.authority.lease_id || bundle.request.generation !== preview.authority.generation
    || !equal(bundle.authority, preview.authority) || !equal(bundle.evidence, preview.evidence)
    || !equal(bundle.deployment, preview.deployment) || !equal(bundle.model_pins, preview.model_pins)
    || !record(bundle.deployment) || bundle.deployment.real_model !== value.real_model
    || !record(preview.retrieval) || !Array.isArray(preview.retrieval.hits)
    || bundle.synthetic !== preview.retrieval.hits.every(hit => record(hit) && hit.synthetic === true)
    || !record(bundle.knowledge.dispatch) || !exact(bundle.knowledge.dispatch, ['schema_version', 'planning_id', 'intent_sha256',
      'preview_sha256', 'context_sha256', 'request_sha256', 'deployment_id'])) return false;
  const dispatch = bundle.knowledge.dispatch;
  if (dispatch.schema_version !== '1.0' || dispatch.planning_id !== bundle.planning_id || dispatch.intent_sha256 !== bundle.knowledge.intent_sha256
    || dispatch.preview_sha256 !== preview.confirm_sha256 || dispatch.context_sha256 !== preview.context_sha256
    || dispatch.deployment_id !== bundle.deployment.deployment_id || !record(bundle.model_request)
    || !canonicalMatches(transport.model_request_canonical, bundle.model_request)
    || !value.bundle_canonical.includes('"model_request":' + String(transport.model_request_canonical) + ',')
    || dispatch.request_sha256 !== await knowledgeTextHash(String(transport.model_request_canonical)) || bundle.model_request.model !== bundle.deployment.deployment_id
    || !Array.isArray(bundle.model_request.messages) || bundle.model_request.messages.length !== 2
    || !record(bundle.model_request.messages[1]) || bundle.model_request.messages[1].role !== 'user') return false;
  const expectedUser = knowledgeCanonical({goal: preview.goal, admitted_skill: (preview.evidence as ObjectValue[])[0],
    reviewed_documents: {context_version: preview.context_version, context_sha256: preview.context_sha256, context_text: preview.context_text, untrusted: true}});
  if (bundle.model_request.messages[1].content !== expectedUser || !record(bundle.model_response)
    || !['execution_authorized', 'activation_authorized', 'training_ready'].every(key => (bundle.model_response as ObjectValue)[key] === false)) return false;
  const response = bundle.model_response;
  const evidence = (preview.evidence as ObjectValue[])[0];
  return exact(response, ['schema_version', 'decision', 'evidence_refs', 'case_key', 'parameter_value', 'steps', 'reason_code', 'execution_authorized', 'activation_authorized', 'training_ready'])
    && response.schema_version === '1.0' && equal(response.evidence_refs, ['admitted-skill'])
    && (response.decision === 'invoke_selected_skill' ? response.reason_code === 'skill_match' && response.case_key === preview.case_key
      && response.parameter_value === evidence.requested_value && equal(response.steps, evidence.ordered_steps)
      : response.decision === 'needs_human' && response.case_key === null && response.parameter_value === null
        && equal(response.steps, []) && ['skill_not_applicable', 'input_ambiguous', 'evidence_insufficient', 'safety_unclear'].includes(String(response.reason_code)));
}

export function OwnedSkillKnowledge({goal, source, snapshot, tasks, disabled, collectLearning, onStarted}: {
  goal: string; source: OwnedFormInvocation; snapshot: Snapshot; tasks: Tasks; disabled: boolean; collectLearning: boolean;
  onStarted: (planningId: string) => void;
}) {
  const tr = useLanguage() === 'tr';
  const text = (english: string, turkish: string) => tr ? turkish : english;
  const [application, setApplication] = useState(''); const [tenant, setTenant] = useState(''); const [role, setRole] = useState('');
  const [query, setQuery] = useState(''); const [preview, setPreview] = useState<ObjectValue | null>(null);
  const [previewTransport, setPreviewTransport] = useState<ObjectValue | null>(null);
  const [confirmation, setConfirmation] = useState(''); const [inference, setInference] = useState(false); const [storage, setStorage] = useState(false);
  const [planningId, setPlanningId] = useState(''); const [checksum, setChecksum] = useState('');
  const [report, setReport] = useState<ObjectValue | null>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState(false);
  const serial = useRef(0); const abort = useRef<AbortController | null>(null);
  const scope = {application_id: application, tenant_id: tenant, account_role: role};
  const reuseAdmission = source.reuse_admission_sha256 ?? '';
  const identity = `${goal}:${application}:${tenant}:${role}:${query}:${snapshot.runtime.runtime_id}:${snapshot.control.lease_id}:${snapshot.control.generation}:${snapshot.control.owner}:${snapshot.control.status}:${reuseAdmission}:${collectLearning}`;
  useEffect(() => {serial.current++; abort.current?.abort(); setPreview(null); setPreviewTransport(null); setConfirmation(''); setInference(false); setStorage(false); setLoading(false); setError(false);
    return () => {serial.current++; abort.current?.abort();};}, [identity]);
  useEffect(() => {if (planningId && tasks.owned_skill_planning?.planning_id === planningId && knowledgeHash(tasks.owned_skill_planning.bundle_sha256))
    setChecksum(tasks.owned_skill_planning.bundle_sha256);}, [planningId, tasks.owned_skill_planning?.planning_id, tasks.owned_skill_planning?.bundle_sha256]);
  useEffect(() => {setReport(null);}, [checksum, snapshot.runtime.runtime_id, reuseAdmission]);
  const ready = !disabled && !loading && !tasks.busy && !tasks.reserved && snapshot.runtime.running
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running' && tasks.owned_skill_planning?.status !== 'bound';
  async function perform(operation: 'preview' | 'start' | 'report') {
    const controller = new AbortController(); abort.current?.abort(); abort.current = controller;
    const requestId = ++serial.current; setLoading(true); setError(false);
    try {
      if (operation === 'report') {
        setReport(null);
        const value = await api<unknown>('/api/tasks/owned-skill-knowledge/report', {schema_version: '1.0', planning_bundle_sha256: checksum}, controller.signal);
        if (!await validOwnedSkillKnowledgeReport(value, checksum, snapshot.runtime.runtime_id, reuseAdmission)) throw new Error('invalid');
        if (!controller.signal.aborted && requestId === serial.current) setReport((value as ObjectValue).report as ObjectValue);
      } else {
        const current = await api<Snapshot>('/api/state', undefined, controller.signal);
        if (current.runtime.runtime_id !== snapshot.runtime.runtime_id || current.control.lease_id !== snapshot.control.lease_id
          || current.control.generation !== snapshot.control.generation || current.control.owner !== 'AGENT' || current.control.status !== 'running' || !current.runtime.running) throw new Error('stale');
        const authority = {lease_id: current.control.lease_id, generation: current.control.generation};
        if (operation === 'preview') {
          const value = await api<unknown>('/api/tasks/owned-skill-knowledge/preview', {schema_version: '1.0', goal, scope, query, top_k: 4, context_chars: 2048, ...authority}, controller.signal);
          if (!await validOwnedSkillKnowledgePreview(value, scope, query, goal, current.runtime.runtime_id, reuseAdmission, current)
            || Date.parse(String(((value as ObjectValue).preview as ObjectValue).expires_at)) <= Date.now()) throw new Error('invalid');
          if (!controller.signal.aborted && requestId === serial.current) {setPreview((value as ObjectValue).preview as ObjectValue); setPreviewTransport(value as ObjectValue); setConfirmation(''); setInference(false); setStorage(false);}
        } else {
          if (!preview || collectLearning || !inference || !storage || confirmation !== preview.confirm_sha256
            || Date.parse(String(preview.expires_at)) <= Date.now()
            || !previewTransport || !await validOwnedSkillKnowledgePreview(previewTransport, scope, query, goal, current.runtime.runtime_id, reuseAdmission, current)) throw new Error('consent');
          const value = await api<PlanningStatus>('/api/tasks/owned-skill-knowledge/start', {schema_version: '1.1', preview_canonical: previewTransport.preview_canonical,
            confirm_sha256: confirmation, inference_consent: true, storage_consent: true, ...authority}, controller.signal);
          if (!record(value) || value.available !== true || value.execution_authorized !== false || typeof value.planning_id !== 'string'
            || !/^planning-[a-f0-9]{32}$/.test(value.planning_id) || value.status !== 'pending') throw new Error('invalid');
          if (!controller.signal.aborted && requestId === serial.current) {setPlanningId(value.planning_id); setChecksum(''); setReport(null);
            setPreview(null); setPreviewTransport(null); setConfirmation(''); setInference(false); setStorage(false); onStarted(value.planning_id);}
        }
      }
    } catch {if (!controller.signal.aborted && requestId === serial.current) {setError(true); setPreview(null); setPreviewTransport(null); setConfirmation(''); setInference(false); setStorage(false); setReport(null);}}
    finally {if (requestId === serial.current) setLoading(false);}
  }
  const reportPreview = report ? (((report.bundle as ObjectValue).knowledge as ObjectValue).intent as ObjectValue).preview as ObjectValue : null;
  return <details data-testid="planning-knowledge" className="registry"><summary>{text('Optional reviewed document context for S2 planning', 'S2 planlama için isteğe bağlı incelenmiş belge bağlamı')}</summary>
    <p>{text('Sources are untrusted background. Local scope labels do not prove external account permissions. The admitted goal and recipe stay pinned; proposals do not execute.', 'Kaynaklar güvenilmez arka plandır. Yerel kapsam etiketleri harici hesap yetkisini kanıtlamaz. Kabul edilmiş hedef ve recipe pinli kalır; öneriler yürütme yapmaz.')}</p>
    {(['application', 'tenant', 'role', 'query'] as const).map(field => <label key={field}>{field === 'application' ? text('Application', 'Uygulama') : field === 'tenant' ? 'Tenant' : field === 'role' ? text('Account role', 'Hesap rolü') : text('Document query', 'Belge sorgusu')}
      <input data-testid={`planning-knowledge-${field}`} value={{application, tenant, role, query}[field]} maxLength={field === 'query' ? 512 : 100}
        onChange={event => ({application: setApplication, tenant: setTenant, role: setRole, query: setQuery}[field])(event.target.value)}/></label>)}
    <button data-testid="planning-knowledge-preview" disabled={!ready || !validKnowledgeScope(scope) || !query.trim()} onClick={() => void perform('preview')}>{text('Preview S2 document context', 'S2 belge bağlamını önizle')}</button>
    {preview ? <div><pre data-testid="planning-knowledge-context">{String(preview.context_text)}</pre><code data-testid="planning-knowledge-hash">{String(preview.confirm_sha256)}</code>
      <label>{text('Exact confirmation hash (five-minute expiry)', 'Exact onay hash’i (beş dakika geçerli)')}<input data-testid="planning-knowledge-confirm" value={confirmation} onChange={event => setConfirmation(event.target.value)}/></label>
      <label><input type="checkbox" data-testid="planning-knowledge-inference" checked={inference} onChange={event => setInference(event.target.checked)}/>{text('I authorize one S2 inference using this document context.', 'Bu belge bağlamıyla tek S2 inference çağrısına izin veriyorum.')}</label>
      <label><input type="checkbox" data-testid="planning-knowledge-storage" checked={storage} onChange={event => setStorage(event.target.checked)}/>{text('I consent to storing source context and the planning request/response privately.', 'Kaynak bağlamının ve planlama isteği/yanıtının özel saklanmasına izin veriyorum.')}</label>
      <button data-testid="planning-knowledge-start" disabled={!ready || collectLearning || !inference || !storage || confirmation !== preview.confirm_sha256 || Date.parse(String(preview.expires_at)) <= Date.now()} onClick={() => void perform('start')}>{text('Generate proposal with document context', 'Belge bağlamıyla öneri üret')}</button></div> : null}
    {collectLearning ? <p>{text('Turn off episode collection before using this separate document-context consent.', 'Bu ayrı belge bağlamı iznini kullanmadan önce episode toplamayı kapatın.')}</p> : null}
    <h4>{text('Read-only private S2 proof', 'Salt okunur özel S2 kanıtı')}</h4><p>{text('Contains private source and actual model request/response text. Do not publish or commit it. Refresh never starts inference or execution.', 'Özel kaynak ve gerçek model isteği/yanıtı metni içerir. Yayımlamayın veya commit etmeyin. Yenileme inference veya yürütme başlatmaz.')}</p>
    <label>{text('Planning bundle SHA-256', 'Planlama bundle SHA-256')}<input data-testid="planning-knowledge-report-hash" value={checksum} onChange={event => setChecksum(event.target.value)}/></label>
    <button data-testid="planning-knowledge-report-refresh" disabled={loading || !knowledgeHash(checksum)} onClick={() => void perform('report')}>{text('Refresh read-only S2 report', 'Salt okunur S2 raporunu yenile')}</button>
    {report && reportPreview ? <div data-testid="planning-knowledge-report"><p>{text('Server-audited historical binding', 'Sunucuda denetlenmiş tarihsel bağ')}: {String(report.historical_binding_verified)}</p>
      <p>{text('Current source / authority valid', 'Güncel kaynak / yetki geçerli')}: {String(report.current_source_valid)} / {String(report.current_authority_valid)}</p>
      <p>{text('Dispatch recorded / actual model request verified', 'Dispatch kaydedildi / gerçek model isteği doğrulandı')}: {String(report.dispatch_recorded)} / {String(report.model_request_verified)}</p>
      <p data-testid="planning-knowledge-applied">{report.knowledge_applied ? text('Context applied to an actual S2 model call', 'Bağlam gerçek S2 model çağrısına uygulandı') : text('Fixture preparation; actual model use is not verified', 'Fixture hazırlığı; gerçek model kullanımı doğrulanmadı')}</p>
      <p>{text('No downstream execution, semantic relevance, causality, gold or training claim.', 'Downstream yürütme, anlamsal uygunluk, nedensellik, gold veya eğitim iddiası yoktur.')}</p>
      {((reportPreview.retrieval as ObjectValue).hits as ObjectValue[]).map((hit, index) => <div key={index} data-testid="planning-knowledge-citation"><p>c{index + 1} · {String(hit.title)} · {String(hit.source_id)}</p><code>{String(hit.document_sha256)} · {String(hit.review_sha256)} · {String(hit.chunk_sha256)}</code><pre>{String(hit.text)}</pre></div>)}
      <details><summary>{text('Exact private request, response and proof', 'Exact özel istek, yanıt ve kanıt')}</summary><pre>{JSON.stringify(report, null, 2)}</pre></details></div> : null}
    {error ? <p role="alert" data-testid="planning-knowledge-error">{text('Context or proof rejected. Review current sources and consent again; no automatic retry.', 'Bağlam veya kanıt reddedildi. Güncel kaynakları inceleyip yeniden izin verin; otomatik tekrar yoktur.')}</p> : null}
  </details>;
}
