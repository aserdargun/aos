import {useEffect, useRef, useState} from 'react';
import {api, type Tasks} from './api';
import {useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';
import {validTaskKnowledgePreview} from './TaskKnowledge';

type ObjectValue = Record<string, unknown>;
const record = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: ObjectValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const identifier = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 500;
const equal = (left: unknown, right: unknown) => knowledgeCanonical(left) === knowledgeCanonical(right);
const flagKeys = ['untrusted', 'execution_authorized', 'training_ready', 'gold', 'manual_approval_required', 'causality_verified', 'scope_authorization_verified'];
const flags = (value: ObjectValue) => value.untrusted === true && value.manual_approval_required === true
  && ['execution_authorized', 'training_ready', 'gold', 'causality_verified', 'scope_authorization_verified'].every(key => value[key] === false);
const markerKeys = ['schema_version', 'intent_sha256', 'admission_sha256', 'job_id', 'context_sha256', 'run_id', 'step_id',
  'state_version', 'snapshot_id', 'state_sha256', 'options', 'options_sha256', 'request_sha256', 'call_id'];
const reportKeys = ['schema_version', 'kind', 'job_id', 'intent_sha256', 'intent', 'admission_sha256', 'admission',
  'historical_binding_verified', 'context_binding_verified', 'model_request_verified', 'knowledge_applied',
  'current_source_valid', 'current_authority_valid', 'prepared_context_count', 'dispatched_context_count',
  'successful_model_call_count', 'context_proofs', 'dispatch_proofs', 'model_proofs', 'outcome', ...flagKeys];

export async function validTaskKnowledgeReport(value: unknown, jobId: string, runtimeId: string): Promise<boolean> {
  if (!record(value) || !exact(value, reportKeys) || !flags(value) || value.schema_version !== '1.0'
    || value.kind !== 'task_knowledge_report' || value.job_id !== jobId || !identifier(jobId)
    || !record(value.intent) || !exact(value.intent, ['schema_version', 'kind', 'preview', 'inference_consent', 'trajectory_storage_consent'])
    || value.intent.schema_version !== '1.0' || value.intent.kind !== 'task_knowledge_intent'
    || value.intent.inference_consent !== true || value.intent.trajectory_storage_consent !== true
    || !record(value.intent.preview) || !validKnowledgeScope(value.intent.preview.scope)
    || typeof value.intent.preview.query !== 'string' || !record(value.intent.preview.target)
    || typeof value.intent.preview.target.task_kind !== 'string'
    || value.intent.preview.target.parent_runtime_id !== runtimeId
    || !await validTaskKnowledgePreview(value.intent.preview, value.intent.preview.scope, value.intent.preview.query,
      value.intent.preview.target.task_kind, undefined, true)
    || !knowledgeHash(value.intent_sha256) || value.intent_sha256 !== await knowledgeDigest(value.intent)
    || !record(value.admission) || !exact(value.admission, ['schema_version', 'kind', 'intent_sha256', 'job_id',
      'preview_sha256', 'context_sha256', 'target', ...flagKeys]) || !flags(value.admission)
    || value.admission.schema_version !== '1.0' || value.admission.kind !== 'task_knowledge_admission'
    || value.admission.job_id !== jobId || value.admission.intent_sha256 !== value.intent_sha256
    || value.admission.preview_sha256 !== value.intent.preview.confirm_sha256
    || value.admission.context_sha256 !== value.intent.preview.context_sha256
    || !equal(value.admission.target, value.intent.preview.target) || !knowledgeHash(value.admission_sha256)
    || value.admission_sha256 !== await knowledgeDigest(value.admission)
    || !['historical_binding_verified', 'context_binding_verified', 'model_request_verified', 'knowledge_applied',
      'current_source_valid', 'current_authority_valid'].every(key => typeof value[key] === 'boolean')
    || !['prepared_context_count', 'dispatched_context_count', 'successful_model_call_count'].every(key => count(value[key]))
    || !Array.isArray(value.context_proofs) || !Array.isArray(value.dispatch_proofs) || !Array.isArray(value.model_proofs)
    || value.prepared_context_count !== value.context_proofs.length || value.dispatched_context_count !== value.dispatch_proofs.length
    || value.successful_model_call_count !== value.model_proofs.length || value.model_proofs.length > value.dispatch_proofs.length
    || value.dispatch_proofs.length > value.context_proofs.length
    || value.context_binding_verified !== (value.context_proofs.length > 0)
    || value.model_request_verified !== (value.model_proofs.length > 0)
    || value.knowledge_applied !== (value.historical_binding_verified && value.context_binding_verified && value.model_request_verified)
    || (value.model_request_verified && !value.historical_binding_verified)
    || !record(value.outcome) || !exact(value.outcome, ['status', 'verification_count', 'scope', 'reason'])
    || !count(value.outcome.verification_count) || !['verified', 'not_verified', 'unsupported'].includes(String(value.outcome.status))
    || !['fixed_task_outcome', 'transport_readback', 'none'].includes(String(value.outcome.scope))
    || !['independent_evidence_verified', 'run_not_settled_success', 'evidence_missing_or_changed', 'task_contract_unsupported'].includes(String(value.outcome.reason))
    || (value.outcome.status === 'verified') !== (value.outcome.reason === 'independent_evidence_verified')
    || (value.outcome.status === 'verified' && (value.outcome.verification_count === 0 || value.outcome.scope === 'none'))) return false;
  const preview = value.intent.preview;
  const contexts = new Map<unknown, ObjectValue>();
  const calls = new Set<unknown>();
  let runId: unknown;
  for (const proof of value.context_proofs) {
    if (!record(proof) || !exact(proof, markerKeys) || proof.schema_version !== '1.0'
      || proof.intent_sha256 !== value.intent_sha256 || proof.admission_sha256 !== value.admission_sha256
      || proof.job_id !== jobId || proof.context_sha256 !== preview.context_sha256
      || !['run_id', 'step_id', 'snapshot_id'].every(key => identifier(proof[key]))
      || !Number.isSafeInteger(proof.state_version) || Number(proof.state_version) < 0
      || !['state_sha256', 'options_sha256', 'request_sha256'].every(key => knowledgeHash(proof[key]))
      || !(proof.call_id === null || identifier(proof.call_id)) || contexts.has(proof.snapshot_id)
      || (proof.call_id !== null && calls.has(proof.call_id)) || (runId !== undefined && runId !== proof.run_id)
      || !Array.isArray(proof.options) || proof.options.length < 2 || proof.options.length > 10
      || !proof.options.every(option => record(option) && exact(option, ['id', 'label']) && typeof option.id === 'string'
        && typeof option.label === 'string') || new Set(proof.options.map(option => option.id)).size !== proof.options.length
      || proof.options_sha256 !== await knowledgeDigest(proof.options)) return false;
    runId = proof.run_id; contexts.set(proof.snapshot_id, proof); if (proof.call_id !== null) calls.add(proof.call_id);
  }
  const dispatches = new Map<unknown, ObjectValue>();
  for (const proof of value.dispatch_proofs) {
    if (!record(proof) || !exact(proof, [...markerKeys, 'actual_request_sha256', 'actual_request'])
      || proof.call_id === null || dispatches.has(proof.call_id) || !contexts.has(proof.snapshot_id)
      || !equal(Object.fromEntries(markerKeys.map(key => [key, proof[key]])), contexts.get(proof.snapshot_id))
      || !record(proof.actual_request) || !exact(proof.actual_request, ['state', 'question', 'options'])
      || typeof proof.actual_request.state !== 'string' || proof.actual_request.question !== 'What is the next action?'
      || !proof.actual_request.state.includes(String(preview.context_text)) || !equal(proof.actual_request.options, proof.options)
      || proof.actual_request_sha256 !== proof.request_sha256 || proof.actual_request_sha256 !== await knowledgeDigest(proof.actual_request)) return false;
    dispatches.set(proof.call_id, proof);
  }
  const modelCalls = new Set<unknown>();
  const decisions = new Set<unknown>();
  for (const proof of value.model_proofs) {
    if (!record(proof) || !exact(proof, [...markerKeys, 'deployment_id', 'decision_id', 'actual_request_sha256', 'response_sha256', 'response_canonical', 'response'])
      || !dispatches.has(proof.call_id) || modelCalls.has(proof.call_id) || !identifier(proof.decision_id) || decisions.has(proof.decision_id)
      || proof.deployment_id !== (preview.target as ObjectValue).system1_deployment_id || String(proof.deployment_id).startsWith('fixture')
      || !equal(Object.fromEntries(markerKeys.map(key => [key, proof[key]])), contexts.get(proof.snapshot_id))
      || proof.actual_request_sha256 !== dispatches.get(proof.call_id)!.actual_request_sha256
      || !record(proof.response) || !exact(proof.response, ['selected_option', 'probabilities'])
      || !record(proof.response.probabilities) || !Array.isArray(proof.options)) return false;
    const optionIds = proof.options.map(option => (option as ObjectValue).id);
    const probabilities = proof.response.probabilities;
    if (!optionIds.includes(proof.response.selected_option) || !exact(probabilities, optionIds as string[])
      || !Object.values(probabilities).every(probability => typeof probability === 'number' && Number.isFinite(probability) && probability >= 0 && probability <= 1)
      || Math.abs(Object.values(probabilities).reduce<number>((sum, probability) => sum + Number(probability), 0) - 1) > 1e-6) return false;
    if (typeof proof.response_canonical !== 'string' || proof.response_canonical.length > 32768
      || !knowledgeHash(proof.response_sha256) || proof.response_sha256 !== await knowledgeTextHash(proof.response_canonical)) return false;
    try {if (!equal(JSON.parse(proof.response_canonical), proof.response)) return false;} catch {return false;}
    modelCalls.add(proof.call_id); decisions.add(proof.decision_id);
  }
  return true;
}

export function TaskKnowledgeReport({tasks, startedJobId, runtimeId}: {tasks: Tasks | null; startedJobId: string; runtimeId: string}) {
  const tr = useLanguage() === 'tr';
  const text = (english: string, turkish: string) => tr ? turkish : english;
  const [jobId, setJobId] = useState(startedJobId);
  const [report, setReport] = useState<ObjectValue | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const serial = useRef(0);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => {if (startedJobId) setJobId(startedJobId);}, [startedJobId]);
  useEffect(() => {serial.current++; abort.current?.abort(); setReport(null); setError(false); setLoading(false);
    return () => {serial.current++; abort.current?.abort();};}, [jobId, runtimeId]);
  async function refresh() {
    const controller = new AbortController(); abort.current?.abort(); abort.current = controller;
    const requestId = ++serial.current; setLoading(true); setReport(null); setError(false);
    try {
      const value = await api<unknown>('/api/tasks/knowledge/report', {schema_version: '1.0', job_id: jobId}, controller.signal);
      if (!await validTaskKnowledgeReport(value, jobId, runtimeId)) throw new Error('invalid');
      if (!controller.signal.aborted && serial.current === requestId) setReport(value as ObjectValue);
    } catch {if (!controller.signal.aborted && serial.current === requestId) setError(true);}
    finally {if (serial.current === requestId) setLoading(false);}
  }
  const jobs = Array.from(new Set([startedJobId, ...(tasks?.jobs.map(job => job.job_id) ?? [])].filter(Boolean)));
  const preview = report ? (report.intent as ObjectValue).preview as ObjectValue : null;
  const hits = preview ? (preview.retrieval as ObjectValue).hits as ObjectValue[] : [];
  return <section data-testid="task-knowledge-audit">
    <h3>{text('Read-only task document audit', 'Salt okunur görev belge denetimi')}</h3>
    <p>{text('This report contains private source and actual model request text. Do not publish or commit it. Refresh is explicit and never starts a task.',
      'Bu rapor özel kaynak ve gerçek model isteği metni içerir. Yayımlamayın veya commit etmeyin. Yenileme açık seçimdir ve görev başlatmaz.')}</p>
    <label>{text('Task to audit (current session)', 'Denetlenecek görev (mevcut oturum)')}<select data-testid="task-knowledge-report-job" value={jobId} onChange={event => setJobId(event.target.value)}>
      <option value="">{text('Select a task', 'Görev seçin')}</option>{jobs.map(id => <option key={id} value={id}>{id}</option>)}
    </select></label>
    <p>{text('Ordinary tasks without a document-context admission cannot produce this report.', 'Belge bağlamı kabulü olmayan sıradan görevler bu raporu üretemez.')}</p>
    <button data-testid="task-knowledge-report-refresh" disabled={!identifier(jobId) || loading} onClick={() => void refresh()}>{text('Refresh read-only report', 'Salt okunur raporu yenile')}</button>
    {error ? <p role="alert" data-testid="task-knowledge-report-error">{text('Audit unavailable or invalid. No verified report is displayed.', 'Denetim kullanılamıyor veya geçersiz. Doğrulanmış rapor gösterilmiyor.')}</p> : null}
    {report && preview ? <div data-testid="task-knowledge-report">
      <p>{text('Task / deployment', 'Görev / deployment')}: {String((preview.target as ObjectValue).task_kind)} · <code>{String((preview.target as ObjectValue).system1_deployment_id)}</code></p>
      <p>{text('Local scope / original query', 'Yerel kapsam / özgün sorgu')}: {Object.values(preview.scope as ObjectValue).map(String).join(' / ')} · {String(preview.query)}</p>
      <p>{text('Server-audited historical binding', 'Sunucuda denetlenmiş tarihsel bağ')}: {String(report.historical_binding_verified)}</p>
      <p>{text('Current source valid', 'Güncel kaynak geçerli')}: {String(report.current_source_valid)} · {text('Current authority valid', 'Güncel yetki geçerli')}: {String(report.current_authority_valid)}</p>
      <p>{text('Prepared context verified', 'Hazırlanmış bağlam doğrulandı')}: {String(report.context_binding_verified)} · {text('Actual model request verified', 'Gerçek model isteği doğrulandı')}: {String(report.model_request_verified)}</p>
      <p data-testid="task-knowledge-report-applied">{report.knowledge_applied ? text('Document context applied to an actual model call', 'Belge bağlamı gerçek model çağrısına uygulandı') : text('Actual model application not verified; fixture preparation is not model use', 'Gerçek model uygulaması doğrulanmadı; fixture hazırlığı model kullanımı değildir')}</p>
      <p data-testid="task-knowledge-report-counts">{text('Prepared / dispatched / successful model calls', 'Hazırlanmış / gönderilmiş / başarılı model çağrıları')}: {String(report.prepared_context_count)} / {String(report.dispatched_context_count)} / {String(report.successful_model_call_count)}</p>
      <p>{text('Independent task outcome', 'Bağımsız görev sonucu')}: {String((report.outcome as ObjectValue).status)} · {String((report.outcome as ObjectValue).scope)} · {String((report.outcome as ObjectValue).reason)} · {String((report.outcome as ObjectValue).verification_count)}</p>
      <p>{text('No verified causality, external scope authorization, gold label or training readiness.', 'Doğrulanmış nedensellik, harici kapsam yetkisi, gold etiketi veya eğitim hazırlığı yoktur.')}</p>
      <p>{text('Intent / admission hashes', 'İzin / kabul hash’leri')}: <code>{String(report.intent_sha256)}</code> · <code>{String(report.admission_sha256)}</code></p>
      <p>{text('Source citations', 'Kaynak atıfları')}: {hits.length}</p>
      {hits.map((hit, index) => <div key={`${hit.document_sha256}:${hit.chunk_index}`} data-testid="task-knowledge-report-citation">
        <p>c{index + 1} · {String(hit.title)} · {String(hit.source_id)} · {text('Revision / chunk', 'Revizyon / parça')}: {String(hit.revision)} / {String(hit.chunk_index)} · {text('Synthetic', 'Sentetik')}: {String(hit.synthetic)}</p>
        <p>{text('Document / content / review / chunk hashes', 'Belge / içerik / inceleme / parça hash’leri')}: <code>{String(hit.document_sha256)}</code> · <code>{String(hit.content_sha256)}</code> · <code>{String(hit.review_sha256)}</code> · <code>{String(hit.chunk_sha256)}</code></p><pre>{String(hit.text)}</pre>
      </div>)}
      <details><summary>{text('Exact context and proof records (private)', 'Exact bağlam ve kanıt kayıtları (özel)')}</summary><pre data-testid="task-knowledge-report-proof">{JSON.stringify(report, null, 2)}</pre></details>
    </div> : null}
  </section>;
}
