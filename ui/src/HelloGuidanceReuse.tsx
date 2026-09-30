import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';
import {type FollowupSource} from './FailureFollowup';
import {type Preview as GuidancePreview, type Report as GuidanceReport, validGuidancePreview, validGuidanceReport} from './FailureGuidance';
import {useLanguage} from './i18n';

type Flags = {execution_authorized: false; causality_verified: false; gold: false; training_ready: false};
type Entry = Flags & {schema_version: '1.0'; kind: 'finite_hello_guidance'; entry_version: 1;
  guidance_code: 'refresh_observation' | 'inspect_before_retry'; context_version: 'hello-guidance-v1'; context_sha256: string;
  scope: {session_id: string; parent_runtime_id: string; image_id: string; configuration_sha256: string;
    workspace_sha256: string; system1_deployment_id: string; role: 'system1'};
  source: {source_job_id: string; candidate_sha256: string; receipt_sha256: string; source_sha256: string;
    contract: {semantic_sha256: string; binding_sha256: string}}};
type Publication = Flags & {schema_version: '1.0'; entry: Entry; entry_sha256: string; confirm_sha256: string; manual_approval_required: true};
type Saved = Flags & {schema_version: '1.0'; entry: Entry; entry_sha256: string; saved: boolean; current_valid: boolean; manual_approval_required: true};
type Reuse = Publication & {guidance: GuidancePreview};
type Report = {schema_version: '1.0'; job_id: string; entry_sha256: string; reuse_intent_sha256: string;
  entry_binding_verified: boolean; current_entry_valid: boolean; guidance: GuidanceReport;
  manual_approval_required: true; causality_verified: false; gold: false; training_ready: false};
type Props = {tasks: Tasks | null; snapshot: Snapshot | null; busy: boolean; source: FollowupSource | null};
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const keys = (value: Record<string, unknown>, fields: string[]) => Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const id = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
const flagKeys = ['execution_authorized', 'causality_verified', 'gold', 'training_ready'];
const flags = (value: Record<string, unknown>) => flagKeys.every(field => value[field] === false);
const envelopeKeys = ['schema_version', 'entry', 'entry_sha256', 'manual_approval_required', ...flagKeys];
const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (record(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
};
async function digest(value: unknown) {
  const result = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(value)));
  return Array.from(new Uint8Array(result), byte => byte.toString(16).padStart(2, '0')).join('');
}
function validEntry(value: unknown, snapshot: Snapshot, source?: FollowupSource | null): value is Entry {
  if (!record(value) || !keys(value, ['schema_version', 'kind', 'entry_version', 'guidance_code', 'context_version',
    'context_sha256', 'scope', 'source', ...flagKeys]) || !record(value.scope) || !record(value.source)) return false;
  const scope = value.scope, origin = value.source, contract = origin.contract;
  return value.schema_version === '1.0' && value.kind === 'finite_hello_guidance' && value.entry_version === 1
    && ['refresh_observation', 'inspect_before_retry'].includes(String(value.guidance_code))
    && value.context_version === 'hello-guidance-v1' && sha(value.context_sha256) && flags(value)
    && keys(scope, ['session_id', 'parent_runtime_id', 'image_id', 'configuration_sha256', 'workspace_sha256', 'system1_deployment_id', 'role'])
    && id(scope.session_id) && scope.parent_runtime_id === snapshot.runtime.runtime_id && scope.image_id === snapshot.runtime.image_id
    && sha(scope.configuration_sha256) && sha(scope.workspace_sha256) && id(scope.system1_deployment_id) && scope.role === 'system1'
    && keys(origin, ['source_job_id', 'candidate_sha256', 'receipt_sha256', 'source_sha256', 'contract'])
    && id(origin.source_job_id) && sha(origin.candidate_sha256) && sha(origin.receipt_sha256) && sha(origin.source_sha256)
    && record(contract) && keys(contract, ['semantic_sha256', 'binding_sha256']) && sha(contract.semantic_sha256) && sha(contract.binding_sha256)
    && (!source || origin.source_job_id === source.job_id && origin.candidate_sha256 === source.candidate_sha256
      && origin.receipt_sha256 === source.receipt_sha256 && scope.session_id === source.session_id && value.guidance_code === source.correction_code);
}
async function validEnvelope(value: unknown, fields: string[], snapshot: Snapshot, source?: FollowupSource | null) {
  return record(value) && keys(value, [...envelopeKeys, ...fields]) && value.schema_version === '1.0'
    && flags(value) && value.manual_approval_required === true && validEntry(value.entry, snapshot, source)
    && sha(value.entry_sha256) && value.entry_sha256 === await digest(value.entry);
}
async function validConfirmation(value: Publication) {
  const {confirm_sha256, ...content} = value;
  return sha(confirm_sha256) && confirm_sha256 === await digest(content);
}

export function HelloGuidanceReuse({tasks, snapshot, busy, source}: Props) {
  const tr = useLanguage() === 'tr';
  const text = (en: string, translated: string) => tr ? translated : en;
  const available = tasks?.hello_guidance_reuse_available === true;
  const eligible = Boolean(source?.eligible && source.kind === 'hello' && ['refresh_observation', 'inspect_before_retry'].includes(source.correction_code ?? ''));
  const ready = Boolean(snapshot && tasks && !busy && !tasks.busy && !tasks.reserved && !tasks.approval
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running');
  const scope = `${available}:${source?.job_id}:${source?.candidate_sha256}:${source?.receipt_sha256}:${source?.eligible}:${source?.correction_code}
    :${snapshot?.runtime.runtime_id}:${snapshot?.runtime.image_id}:${snapshot?.control.lease_id}:${snapshot?.control.generation}:${snapshot?.control.owner}:${snapshot?.control.status}`;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const serial = useRef(0);
  const [publication, setPublication] = useState<Publication | null>(null);
  const [reuse, setReuse] = useState<Reuse | null>(null);
  const [entry, setEntry] = useState<Saved | null>(null);
  const [entryHash, setEntryHash] = useState('');
  const [consent, setConsent] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [started, setStarted] = useState('');
  const [jobId, setJobId] = useState('');
  const [report, setReport] = useState<Report | null>(null);
  const jobs = (tasks?.jobs ?? []).filter(job => job.kind === 'hello' && job.hello_guidance_reuse && job.failure_guidance && job.failure_followup
    && sha(job.hello_guidance_reuse.entry_sha256) && sha(job.hello_guidance_reuse.reuse_intent_sha256)
    && job.hello_guidance_reuse.source_job_id === job.failure_guidance.source_job_id
    && job.failure_guidance.source_job_id === job.failure_followup.source_job_id);
  const selectedJob = jobs.find(job => job.job_id === jobId);
  const startedJob = tasks?.jobs.find(job => job.job_id === started);
  const reportScope = `${jobId}:${selectedJob?.hello_guidance_reuse?.reuse_intent_sha256}:${selectedJob?.failure_guidance?.guidance_intent_sha256}:${selectedJob?.failure_followup?.intent_sha256}`;
  function reset() {setPublication(null); setReuse(null); setConsent(false); setConfirmation('');}
  useEffect(() => {serial.current += 1; reset(); setLoading(false); setError(false); setReport(null);}, [scope]);
  useEffect(() => {serial.current += 1; setReport(null); setLoading(false);}, [reportScope]);
  useEffect(() => () => {serial.current += 1;}, []);
  async function control() {
    if (!snapshot || !ready || scopeRef.current !== scope) throw new Error('stale');
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id || current.runtime.image_id !== snapshot.runtime.image_id
      || current.control.lease_id !== snapshot.control.lease_id || current.control.generation !== snapshot.control.generation
      || current.control.owner !== 'AGENT' || current.control.status !== 'running') throw new Error('stale');
    return current;
  }
  async function invoke(operation: 'publish-preview' | 'publish' | 'inspect' | 'reuse-preview' | 'start' | 'report') {
    if (!available || loading || !snapshot) return;
    const accepted = operation === 'publish' ? publication : reuse;
    if (['publish-preview', 'publish', 'reuse-preview', 'start'].includes(operation) && !ready) return;
    if (operation === 'publish-preview' && (!eligible || !source)) return;
    if (['publish', 'start'].includes(operation) && (!accepted || !consent || confirmation !== accepted.confirm_sha256)) return;
    if (['inspect', 'reuse-preview'].includes(operation) && !sha(entryHash)) return;
    if (operation === 'report' && !selectedJob) return;
    const request = ++serial.current;
    const requireCurrent = () => {if (serial.current !== request || scopeRef.current !== scope) throw new Error('stale');};
    setLoading(true); setError(false); reset();
    if (operation === 'report') setReport(null);
    if (operation === 'inspect') setEntry(null);
    try {
      const current = ['inspect', 'report'].includes(operation) ? snapshot : await control();
      let body: Record<string, unknown> = {schema_version: '1.0'};
      if (operation === 'publish-preview') body = {...body, source_job_id: source!.job_id, candidate_sha256: source!.candidate_sha256, receipt_sha256: source!.receipt_sha256};
      else if (operation === 'publish' || operation === 'start') body = {...body, preview: accepted, confirm_sha256: accepted!.confirm_sha256,
        consent: true, lease_id: current.control.lease_id, generation: current.control.generation};
      else if (operation === 'report') body.job_id = jobId;
      else body.entry_sha256 = entryHash;
      const value = await api<unknown>(`/api/tasks/hello-guidance-reuse/${operation}`, body);
      if (serial.current !== request || scopeRef.current !== scope) return;
      if (operation === 'publish-preview') {
        if (!await validEnvelope(value, ['confirm_sha256'], current, source) || !await validConfirmation(value as Publication)) throw new Error('invalid');
        requireCurrent();
        setPublication(value as Publication);
      } else if (operation === 'publish' || operation === 'inspect') {
        if (!await validEnvelope(value, ['saved', 'current_valid'], current, operation === 'publish' ? source : null)) throw new Error('invalid');
        const saved = value as Saved;
        if (saved.saved !== true || typeof saved.current_valid !== 'boolean' || saved.entry_sha256 !== (operation === 'publish' ? accepted!.entry_sha256 : entryHash)
          || operation === 'publish' && canonical(saved.entry) !== canonical(accepted!.entry)) throw new Error('invalid');
        requireCurrent();
        setEntry(saved); setEntryHash(saved.entry_sha256);
      } else if (operation === 'reuse-preview') {
        if (!await validEnvelope(value, ['confirm_sha256', 'guidance'], current) || !await validConfirmation(value as Reuse)) throw new Error('invalid');
        const preview = value as Reuse, origin = preview.entry.source, pinned = preview.entry.scope;
        const selection: FollowupSource = {job_id: origin.source_job_id, candidate_sha256: origin.candidate_sha256, receipt_sha256: origin.receipt_sha256,
          session_id: pinned.session_id, kind: 'hello', eligible: true, correction_code: preview.entry.guidance_code};
        if (preview.entry_sha256 !== entryHash || !validGuidancePreview(preview.guidance, selection, current)
          || preview.guidance.context_sha256 !== preview.entry.context_sha256 || preview.guidance.followup.source_sha256 !== origin.source_sha256
          || canonical(preview.guidance.followup.source_contract) !== canonical(origin.contract)
          || preview.guidance.followup.target.configuration_sha256 !== pinned.configuration_sha256
          || preview.guidance.followup.target.system1_deployment_id !== pinned.system1_deployment_id
          || preview.guidance.followup.target.system2_deployment_id !== null) throw new Error('invalid');
        requireCurrent();
        setReuse(preview);
      } else if (operation === 'start') {
        if (!record(value) || !keys(value, ['schema_version', 'job_id', 'intent_sha256', 'guidance_intent_sha256', 'manual_approval_required', 'entry_sha256', 'reuse_intent_sha256'])
          || value.schema_version !== '1.0' || !id(value.job_id) || !sha(value.intent_sha256) || !sha(value.guidance_intent_sha256)
          || !sha(value.reuse_intent_sha256) || value.entry_sha256 !== accepted!.entry_sha256 || value.manual_approval_required !== true) throw new Error('invalid');
        const preview = accepted as Reuse;
        const intent = (content: unknown) => digest({schema_version: '1.0', preview: content, consent: true});
        if (value.reuse_intent_sha256 !== await intent(preview) || value.guidance_intent_sha256 !== await intent(preview.guidance)
          || value.intent_sha256 !== await intent(preview.guidance.followup)) throw new Error('invalid');
        requireCurrent();
        setStarted(value.job_id as string);
      } else {
        if (!record(value) || !keys(value, ['schema_version', 'job_id', 'entry_sha256', 'reuse_intent_sha256', 'entry_binding_verified', 'current_entry_valid',
          'guidance', 'manual_approval_required', 'causality_verified', 'gold', 'training_ready']) || value.schema_version !== '1.0'
          || value.job_id !== selectedJob!.job_id || value.entry_sha256 !== selectedJob!.hello_guidance_reuse!.entry_sha256
          || value.reuse_intent_sha256 !== selectedJob!.hello_guidance_reuse!.reuse_intent_sha256 || typeof value.entry_binding_verified !== 'boolean'
          || typeof value.current_entry_valid !== 'boolean' || value.manual_approval_required !== true || value.causality_verified !== false
          || value.gold !== false || value.training_ready !== false || !validGuidanceReport(value.guidance, selectedJob!)) throw new Error('invalid');
        setReport(value as Report);
      }
    } catch {if (serial.current === request) {setError(true); reset();}}
    finally {if (serial.current === request) setLoading(false);}
  }
  if (!available) return null;
  const pending = publication ?? reuse;
  const yesNo = (value: boolean) => value ? text('Yes', 'Evet') : text('No', 'Hayır');
  return <section className="registry" data-testid="hello-reuse-panel">
    <h3>{text('Reusable Hello guidance', 'Yeniden kullanılabilir Hello yönlendirmesi')}</h3>
    <p className="caption">{text('Publish a reviewed finite instruction for this session and workspace. Reuse requires separate execution consent and fresh manual action approval. This is not general retrieval or training.',
      'İncelenmiş sonlu talimatı bu oturum ve çalışma alanı için kaydedin. Tekrar kullanım ayrı yürütme izni ve taze elle eylem onayı ister. Bu genel bilgi getirme veya eğitim değildir.')}</p>
    {eligible ? <button data-testid="hello-reuse-publish-preview" disabled={!ready || loading} onClick={() => void invoke('publish-preview')}>
      {text('Preview publication', 'Kaydı önizle')}</button> : null}
    {pending ? <div data-testid="hello-reuse-preview-result">
      <p>{pending.entry.guidance_code === 'refresh_observation' ? text('Use the fresh observation', 'Taze gözlemi kullan') : text('Inspect before retrying', 'Yeniden denemeden önce incele')}</p>
      <p>{text('Confirmation SHA-256', 'Onay SHA-256')}: <code style={{overflowWrap: 'anywhere'}}>{pending.confirm_sha256}</code></p>
      <label><input data-testid="hello-reuse-consent" type="checkbox" checked={consent} disabled={loading} onChange={event => setConsent(event.target.checked)}/>
        {publication ? text('I consent to save this entry.', 'Bu kaydı saklamaya izin veriyorum.') : text('I consent to execute a new task with this entry.', 'Bu kayıtla yeni görev yürütmeye izin veriyorum.')}</label>
      <label htmlFor="hello-reuse-confirm">{text('Enter the exact preview hash', 'Önizleme hash değerini tam girin')}</label>
      <input id="hello-reuse-confirm" data-testid="hello-reuse-confirm" value={confirmation} maxLength={64} autoComplete="off" spellCheck={false}
        onChange={event => setConfirmation(event.target.value.trim().toLowerCase())}/>
      <button data-testid={publication ? 'hello-reuse-publish' : 'hello-reuse-start'} disabled={!ready || loading || !consent || confirmation !== pending.confirm_sha256}
        onClick={() => void invoke(publication ? 'publish' : 'start')}>{publication ? text('Publish entry', 'Kaydı sakla') : text('Start reuse task', 'Tekrar kullanım görevini başlat')}</button>
    </div> : null}
    <label htmlFor="hello-reuse-entry">{text('Entry SHA-256', 'Kayıt SHA-256')}</label>
    <input id="hello-reuse-entry" data-testid="hello-reuse-entry" value={entryHash} maxLength={64} autoComplete="off" spellCheck={false}
      onChange={event => {serial.current += 1; setLoading(false); setEntryHash(event.target.value.trim().toLowerCase()); setEntry(null); reset();}}/>
    <button data-testid="hello-reuse-inspect" disabled={loading || !sha(entryHash)} onClick={() => void invoke('inspect')}>{text('Inspect saved entry', 'Saklanan kaydı incele')}</button>
    {entry ? <p data-testid="hello-reuse-entry-result">{text('Saved entry; current source and scope valid', 'Kayıt saklandı; güncel kaynak ve kapsam geçerli')}: {yesNo(entry.current_valid)}</p> : null}
    <button data-testid="hello-reuse-preview" disabled={!ready || loading || !sha(entryHash)} onClick={() => void invoke('reuse-preview')}>{text('Preview reuse', 'Tekrar kullanımı önizle')}</button>
    {started && !['succeeded', 'failed', 'cancelled'].includes(startedJob?.status ?? '') ? <p role="status" data-testid="hello-reuse-started">{text('Reuse task started; open its fresh manual approval.', 'Tekrar kullanım görevi başladı; taze elle onayını açın.')} <code>{started}</code></p> : null}
    {error ? <p role="alert">{text('The response or scope changed. Request a fresh preview or inspect again.', 'Yanıt veya kapsam değişti. Taze önizleme isteyin ya da yeniden inceleyin.')}</p> : null}
    {jobs.length ? <div>
      <label htmlFor="hello-reuse-job">{text('Reuse task history', 'Tekrar kullanım görev geçmişi')}</label>
      <select id="hello-reuse-job" data-testid="hello-reuse-job" value={jobId} onChange={event => setJobId(event.target.value)}>
        <option value="">{text('Select a task', 'Görev seçin')}</option>
        {jobs.map(job => <option key={job.job_id} value={job.job_id}>{job.status} · {job.job_id.slice(0, 12)}</option>)}
      </select>
      <button data-testid="hello-reuse-report" disabled={loading || !selectedJob} onClick={() => void invoke('report')}>{text('Inspect reuse evidence', 'Tekrar kullanım kanıtını incele')}</button>
      {report ? <div data-testid="hello-reuse-report-result">
        <p>{text('Historical entry binding verified', 'Geçmiş kayıt bağı doğrulandı')}: {yesNo(report.entry_binding_verified)}</p>
        <p>{text('Current entry valid', 'Güncel kayıt geçerli')}: {yesNo(report.current_entry_valid)}</p>
        <p>{text('Context binding verified', 'Bağlam bağı doğrulandı')}: {yesNo(report.guidance.context_binding_verified)}</p>
        <p>{text('Model request verified', 'Model isteği doğrulandı')}: {yesNo(report.guidance.model_request_verified)}</p>
        <p>{text('Guidance applied', 'Yönlendirme uygulandı')}: {yesNo(report.guidance.guidance_applied)}</p>
        <p>{text('Task outcome verified', 'Görev sonucu doğrulandı')}: {yesNo(report.guidance.followup.outcome.status === 'verified')}</p>
        <p className="caption">{text('Historical binding, current validity, model-input evidence and outcome are separate. Causality, gold labels and training remain unverified.',
          'Geçmiş bağ, güncel geçerlilik, model girdisi kanıtı ve sonuç ayrıdır. Nedensellik, gold etiketler ve eğitim doğrulanmış değildir.')}</p>
      </div> : null}
    </div> : null}
  </section>;
}
