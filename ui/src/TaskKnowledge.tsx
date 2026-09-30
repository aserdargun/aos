import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';
import {useLanguage} from './i18n';
import {TaskKnowledgeReport} from './TaskKnowledgeReport';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, sameKnowledgeScope, validKnowledgeScope,
  validKnowledgeSearch, type KnowledgeScope} from './knowledgeApi';

type ObjectValue = Record<string, unknown>;
const record = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: ObjectValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const flagKeys = ['untrusted', 'execution_authorized', 'training_ready', 'gold', 'manual_approval_required', 'causality_verified', 'scope_authorization_verified'];
const flags = (value: ObjectValue) => value.untrusted === true && value.manual_approval_required === true
  && ['execution_authorized', 'training_ready', 'gold', 'causality_verified', 'scope_authorization_verified'].every(key => value[key] === false);
const integer = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;
const identifier = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const previewKeys = ['schema_version', 'kind', 'preview_id', 'expires_at', 'scope', 'query', 'top_k', 'context_chars',
  'retrieval', 'target', 'workspace_identity', 'store_identity', 'context_version', 'context_text', 'context_sha256', 'confirm_sha256', ...flagKeys];

export async function validTaskKnowledgePreview(value: unknown, scope: KnowledgeScope, query: string,
  kind: string, snapshot?: Snapshot, historical = false): Promise<boolean> {
  if (!record(value) || !exact(value, previewKeys) || !flags(value) || value.schema_version !== '1.0'
    || value.kind !== 'task_knowledge_preview' || !sameKnowledgeScope(value.scope, scope) || value.query !== query
    || !(historical ? integer(value.top_k) && Number(value.top_k) >= 1 && Number(value.top_k) <= 4
      && integer(value.context_chars) && Number(value.context_chars) >= 256 && Number(value.context_chars) <= 2048
      : value.top_k === 4 && value.context_chars === 2048) || value.context_version !== 'aos-task-knowledge-v1'
    || typeof value.preview_id !== 'string' || !/^knowledge-preview-[a-f0-9]{32}$/.test(value.preview_id)
    || typeof value.expires_at !== 'string' || !Number.isFinite(Date.parse(value.expires_at))
    || !record(value.target) || !exact(value.target, ['session_id', 'parent_runtime_id', 'image_id', 'lease_id', 'generation',
      'task_kind', 'configuration_sha256', 'system1_deployment_id', 'system2_deployment_id'])
    || !['session_id', 'parent_runtime_id', 'lease_id', 'system1_deployment_id'].every(key => identifier(value.target && (value.target as ObjectValue)[key]))
    || !knowledgeHash(value.target.configuration_sha256) || !integer(value.target.generation)
    || typeof value.target.image_id !== 'string' || value.target.task_kind !== kind
    || !(value.target.system2_deployment_id === null || identifier(value.target.system2_deployment_id))
    || !record(value.workspace_identity) || !exact(value.workspace_identity, ['version', 'path_sha256', 'device', 'inode', 'owner_uid'])
    || value.workspace_identity.version !== 'descriptor-workspace-v1' || !knowledgeHash(value.workspace_identity.path_sha256)
    || !['device', 'inode', 'owner_uid'].every(key => integer((value.workspace_identity as ObjectValue)[key]))
    || !record(value.store_identity) || !exact(value.store_identity, ['device', 'inode', 'owner', 'mode'])
    || !Object.values(value.store_identity).every(integer) || !knowledgeHash(value.confirm_sha256)
    || !knowledgeHash(value.context_sha256) || typeof value.context_text !== 'string'
    || !await validKnowledgeSearch(value.retrieval, scope, query, Number(value.top_k), Number(value.context_chars))) return false;
  if (snapshot && (value.target.lease_id !== snapshot.control.lease_id || value.target.generation !== snapshot.control.generation
    || value.target.parent_runtime_id !== snapshot.runtime.runtime_id || value.target.image_id !== snapshot.runtime.image_id)) return false;
  const retrieval = value.retrieval as ObjectValue;
  if (!Array.isArray(retrieval.hits) || !retrieval.hits.length) return false;
  const context = '\nUntrusted reviewed document context (aos-task-knowledge-v1):\n'
    + knowledgeCanonical({context_version: 'aos-task-knowledge-v1', scope, citations: retrieval.hits.map((hit, index) => ({citation_id: `c${index + 1}`, ...(hit as ObjectValue)})),
      untrusted: true, execution_authorized: false, training_ready: false, gold: false, manual_approval_required: true,
      causality_verified: false, scope_authorization_verified: false});
  const {confirm_sha256, ...content} = value;
  return value.context_text === context && value.context_sha256 === await knowledgeTextHash(context)
    && confirm_sha256 === await knowledgeDigest(content);
}

export function TaskKnowledge({tasks, snapshot, kind, busy}: {tasks: Tasks | null; snapshot: Snapshot | null; kind: string; busy: boolean}) {
  const tr = useLanguage() === 'tr';
  const text = (english: string, turkish: string) => tr ? turkish : english;
  const [application, setApplication] = useState('');
  const [tenant, setTenant] = useState('');
  const [role, setRole] = useState('');
  const [query, setQuery] = useState('');
  const [preview, setPreview] = useState<ObjectValue | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [consent, setConsent] = useState(false);
  const [jobId, setJobId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const serial = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const available = tasks?.task_knowledge_available === true;
  const identity = `${application}:${tenant}:${role}:${query}:${kind}:${available}:${snapshot?.control.lease_id}:${snapshot?.control.generation}
    :${snapshot?.control.owner}:${snapshot?.control.status}:${snapshot?.runtime.runtime_id}:${snapshot?.runtime.image_id}`;
  useEffect(() => {
    serial.current++; abort.current?.abort(); setPreview(null); setConfirmation(''); setConsent(false); setLoading(false); setError(false);
    return () => {serial.current++; abort.current?.abort();};
  }, [identity]);
  const scope = {application_id: application, tenant_id: tenant, account_role: role};
  const ready = available && validKnowledgeScope(scope) && query.trim().length > 0 && query.length <= 512
    && !busy && !tasks?.reserved && !tasks?.busy && tasks?.kinds?.includes(kind) && snapshot?.runtime.running
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';

  async function perform(start: boolean) {
    const controller = new AbortController(); abort.current?.abort(); abort.current = controller;
    const requestId = ++serial.current; setLoading(true); setError(false);
    try {
      const current = await api<Snapshot>('/api/state', undefined, controller.signal);
      if (!snapshot || current.control.lease_id !== snapshot.control.lease_id || current.control.generation !== snapshot.control.generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running' || !current.runtime.running
        || current.runtime.runtime_id !== snapshot.runtime.runtime_id || current.runtime.image_id !== snapshot.runtime.image_id) throw new Error('stale');
      const authority = {lease_id: current.control.lease_id, generation: current.control.generation};
      if (start) {
        if (!preview || !consent || confirmation !== preview.confirm_sha256 || Date.parse(String(preview.expires_at)) <= Date.now()
          || !await validTaskKnowledgePreview(preview, scope, query, kind, current)) throw new Error('consent');
        const value = await api<unknown>('/api/tasks/knowledge/start', {schema_version: '1.0', preview, confirm_sha256: confirmation,
          consent: true, ...authority}, controller.signal);
        if (!record(value) || !exact(value, ['schema_version', 'kind', 'job_id', 'intent_sha256', ...flagKeys])
          || !flags(value) || value.schema_version !== '1.0' || value.kind !== 'task_knowledge_start'
          || !identifier(value.job_id) || value.intent_sha256 !== await knowledgeDigest({schema_version: '1.0',
            kind: 'task_knowledge_intent', preview, inference_consent: true, trajectory_storage_consent: true})) throw new Error('invalid');
        if (controller.signal.aborted || serial.current !== requestId) return;
        setJobId(String(value.job_id)); setPreview(null); setConfirmation(''); setConsent(false);
      } else {
        const value = await api<unknown>('/api/tasks/knowledge/preview', {schema_version: '1.0', scope, query, top_k: 4,
          context_chars: 2048, task_kind: kind, ...authority}, controller.signal);
        if (!await validTaskKnowledgePreview(value, scope, query, kind, current)
          || Date.parse(String((value as ObjectValue).expires_at)) <= Date.now()) throw new Error('invalid');
        if (controller.signal.aborted || serial.current !== requestId) return;
        setPreview(value as ObjectValue); setConfirmation(''); setConsent(false);
      }
    } catch {
      if (!controller.signal.aborted && serial.current === requestId) {setError(true); setPreview(null); setConfirmation(''); setConsent(false);}
    } finally {if (serial.current === requestId) setLoading(false);}
  }

  if (!available) return null;
  return <details className="registry" data-testid="task-knowledge"><summary>{text('Reviewed document context for one task', 'Tek görev için incelenmiş belge bağlamı')}</summary>
    <p>{text('Uses reviewed local documents in System-1 decisions. Starts a separate task with manual approvals, regardless of Approve all. No training or scope expansion.',
      'İncelenmiş yerel belgeler System-1 kararlarına eklenir. Tümünü onayla seçimine bakılmadan ayrı, manuel onaylı görev başlar. Eğitim veya yetki genişlemesi yoktur.')}</p>
    <p>{text('These labels select a local corpus partition; they do not prove external application or account permissions. Source text is untrusted.',
      'Bu etiketler yerel corpus bölümünü seçer; harici uygulama veya hesap yetkisini kanıtlamaz. Kaynak metni güvenilmezdir.')}</p>
    <label>{text('Application', 'Uygulama')}<input data-testid="task-knowledge-application" value={application} onChange={event => setApplication(event.target.value)}/></label>
    <label>{text('Tenant', 'Tenant')}<input data-testid="task-knowledge-tenant" value={tenant} onChange={event => setTenant(event.target.value)}/></label>
    <label>{text('Account role', 'Hesap rolü')}<input data-testid="task-knowledge-role" value={role} onChange={event => setRole(event.target.value)}/></label>
    <label>{text('Document search query', 'Belge arama sorgusu')}<input data-testid="task-knowledge-query" maxLength={512} value={query} onChange={event => setQuery(event.target.value)}/></label>
    <button data-testid="task-knowledge-preview" disabled={!ready || loading} onClick={() => void perform(false)}>{text('Preview context', 'Bağlamı önizle')}</button>
    {preview ? <div>
      <pre data-testid="task-knowledge-context">{String(preview.context_text)}</pre>
      <p>{text('Exact confirmation hash (expires after five minutes):', 'Exact onay hash’i (beş dakika sonra sona erer):')}</p><code data-testid="task-knowledge-hash">{String(preview.confirm_sha256)}</code>
      <label>{text('Confirm hash', 'Hash’i onayla')}<input data-testid="task-knowledge-confirm" value={confirmation} onChange={event => setConfirmation(event.target.value)}/></label>
      <label><input type="checkbox" data-testid="task-knowledge-consent" checked={consent} onChange={event => setConsent(event.target.checked)}/>{text('I authorize inference and storing this document context in the private trajectory for this one task.', 'Bu tek görevde inference ve belge bağlamının özel trajectory içinde saklanmasına izin veriyorum.')}</label>
      <button data-testid="task-knowledge-start" disabled={!ready || loading || !consent || confirmation !== preview.confirm_sha256
        || Date.parse(String(preview.expires_at)) <= Date.now()} onClick={() => void perform(true)}>{text('Start with context — manual approvals', 'Bağlamla başlat — manuel onaylar')}</button>
    </div> : null}
    {jobId ? <p role="status" data-testid="task-knowledge-started">{text('Started. Use the task approval controls and trace below.', 'Başladı. Aşağıdaki görev onaylarını ve izi kullanın.')} <code>{jobId}</code></p> : null}
    <TaskKnowledgeReport tasks={tasks} startedJobId={jobId} runtimeId={snapshot?.runtime.runtime_id ?? ''}/>
    {error ? <p role="alert">{text('Context rejected or expired. Review sources and create a fresh preview.', 'Bağlam reddedildi veya süresi doldu. Kaynakları inceleyip yeni önizleme oluşturun.')}</p> : null}
  </details>;
}
