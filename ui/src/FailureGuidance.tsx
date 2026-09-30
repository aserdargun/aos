import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';
import {type FollowupSource, type Preview as FollowupPreview, type Report as FollowupReport,
  validPreview, validReport} from './FailureFollowup';
import {useLanguage} from './i18n';

type GuidanceCode = 'refresh_observation' | 'inspect_before_retry';
export type Preview = {schema_version: '1.0'; followup: FollowupPreview; guidance_code: GuidanceCode;
  context_version: 'hello-guidance-v1'; context_sha256: string; confirm_sha256: string;
  manual_approval_required: true; guidance_applied: false; causality_verified: false; gold: false; training_ready: false};
type Started = {schema_version: '1.0'; job_id: string; intent_sha256: string;
  guidance_intent_sha256: string; manual_approval_required: true};
export type Report = {schema_version: '1.0'; job_id: string; guidance_intent_sha256: string; guidance_code: GuidanceCode;
  followup: FollowupReport; context_binding_verified: boolean; model_request_verified: boolean;
  guidance_applied: boolean; causality_verified: false; gold: false; training_ready: false; manual_approval_required: true};
type Props = {tasks: Tasks | null; snapshot: Snapshot | null; busy: boolean; source: FollowupSource | null};

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const id = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
const supported = (value: unknown): value is GuidanceCode => value === 'refresh_observation' || value === 'inspect_before_retry';

export function validGuidancePreview(value: unknown, source: FollowupSource, snapshot: Snapshot): value is Preview {
  return record(value) && exactKeys(value, ['schema_version', 'followup', 'guidance_code', 'context_version',
    'context_sha256', 'confirm_sha256', 'manual_approval_required', 'guidance_applied', 'causality_verified', 'gold', 'training_ready'])
    && value.schema_version === '1.0' && supported(value.guidance_code) && value.guidance_code === source.correction_code
    && source.kind === 'hello' && value.context_version === 'hello-guidance-v1' && sha(value.context_sha256)
    && sha(value.confirm_sha256) && value.manual_approval_required === true && value.guidance_applied === false
    && value.causality_verified === false && value.gold === false && value.training_ready === false
    && validPreview(value.followup, source, snapshot.runtime.runtime_id, snapshot.runtime.image_id,
      snapshot.control.lease_id, snapshot.control.generation);
}

function validGuidanceStart(value: unknown): value is Started {
  return record(value) && exactKeys(value, ['schema_version', 'job_id', 'intent_sha256', 'guidance_intent_sha256', 'manual_approval_required'])
    && value.schema_version === '1.0' && id(value.job_id) && sha(value.intent_sha256)
    && sha(value.guidance_intent_sha256) && value.manual_approval_required === true;
}

export function validGuidanceReport(value: unknown, job: Tasks['jobs'][number]): value is Report {
  return record(value) && exactKeys(value, ['schema_version', 'job_id', 'guidance_intent_sha256', 'guidance_code', 'followup',
    'context_binding_verified', 'model_request_verified', 'guidance_applied', 'causality_verified', 'gold', 'training_ready', 'manual_approval_required'])
    && value.schema_version === '1.0' && value.job_id === job.job_id && job.kind === 'hello'
    && sha(value.guidance_intent_sha256) && value.guidance_intent_sha256 === job.failure_guidance?.guidance_intent_sha256
    && job.failure_guidance?.source_job_id === job.failure_followup?.source_job_id && supported(value.guidance_code)
    && validReport(value.followup, job) && typeof value.context_binding_verified === 'boolean'
    && typeof value.model_request_verified === 'boolean' && typeof value.guidance_applied === 'boolean'
    && (!value.model_request_verified || value.context_binding_verified)
    && value.guidance_applied === (value.context_binding_verified && value.model_request_verified)
    && value.causality_verified === false && value.gold === false && value.training_ready === false
    && value.manual_approval_required === true;
}

export function FailureGuidance({tasks, snapshot, busy, source}: Props) {
  const tr = useLanguage() === 'tr';
  const text = (en: string, translated: string) => tr ? translated : en;
  const available = tasks?.failure_guidance_available === true;
  const ready = Boolean(snapshot && tasks && !busy && !tasks.busy && !tasks.reserved && !tasks.approval
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running');
  const eligible = Boolean(source?.eligible && source.kind === 'hello' && supported(source.correction_code));
  const scope = `${source?.job_id}:${source?.candidate_sha256}:${source?.receipt_sha256}:${source?.eligible}:${source?.correction_code}
    :${snapshot?.runtime.runtime_id}:${snapshot?.runtime.image_id}:${snapshot?.control.lease_id}:${snapshot?.control.generation}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const serial = useRef(0);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [consent, setConsent] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [started, setStarted] = useState<Started | null>(null);
  const [reportJobId, setReportJobId] = useState('');
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState(false);
  const jobs = (tasks?.jobs ?? []).filter(job => job.kind === 'hello' && job.failure_guidance && job.failure_followup
    && sha(job.failure_guidance.guidance_intent_sha256) && sha(job.failure_followup.intent_sha256)
    && id(job.failure_guidance.source_job_id) && job.failure_guidance.source_job_id === job.failure_followup.source_job_id);
  const selectedJob = jobs.find(job => job.job_id === reportJobId) ?? null;
  const reportScope = `${reportJobId}:${selectedJob?.failure_guidance?.guidance_intent_sha256}:${selectedJob?.failure_followup?.intent_sha256}`;

  useEffect(() => {
    serial.current += 1;
    setPreview(null); setConsent(false); setConfirmation(''); setLoading(false); setError(false); setStarted(null);
  }, [scope]);
  useEffect(() => {serial.current += 1; setReport(null); setReportError(false); setLoading(false);}, [reportScope]);
  useEffect(() => () => {serial.current += 1;}, []);

  async function freshControl() {
    if (!snapshot || !source || !eligible || !ready || scopeRef.current !== scope) throw new Error('stale');
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id
        || current.runtime.image_id !== snapshot.runtime.image_id || current.control.lease_id !== snapshot.control.lease_id
        || current.control.generation !== snapshot.control.generation || current.control.owner !== 'AGENT'
        || current.control.status !== 'running') throw new Error('stale');
    return current;
  }

  async function createPreview() {
    if (!available || !source || !eligible || !ready || loading) return;
    const requestSerial = ++serial.current;
    setLoading(true); setError(false); setPreview(null); setConsent(false); setConfirmation(''); setStarted(null);
    try {
      const current = await freshControl();
      const result = await api<unknown>('/api/tasks/failure-guidance/preview', {schema_version: '1.0',
        source_job_id: source.job_id, candidate_sha256: source.candidate_sha256, receipt_sha256: source.receipt_sha256});
      if (serial.current !== requestSerial || scopeRef.current !== scope) return;
      if (!validGuidancePreview(result, source, current)) throw new Error('invalid');
      setPreview(result);
    } catch {if (serial.current === requestSerial) setError(true);}
    finally {if (serial.current === requestSerial) setLoading(false);}
  }

  async function startGuidedTask() {
    if (!preview || !available || !eligible || !ready || loading || !consent || confirmation !== preview.confirm_sha256) return;
    const accepted = preview;
    const requestSerial = ++serial.current;
    setLoading(true); setError(false); setPreview(null); setConsent(false); setConfirmation(''); setStarted(null);
    try {
      const current = await freshControl();
      const target = accepted.followup.target;
      if (current.runtime.runtime_id !== target.parent_runtime_id || current.runtime.image_id !== target.image_id
          || current.control.lease_id !== target.lease_id || current.control.generation !== target.generation) throw new Error('stale');
      const result = await api<unknown>('/api/tasks/failure-guidance/start', {schema_version: '1.0', preview: accepted,
        confirm_sha256: accepted.confirm_sha256, consent: true, lease_id: current.control.lease_id, generation: current.control.generation});
      if (serial.current !== requestSerial || scopeRef.current !== scope) return;
      if (!validGuidanceStart(result)) throw new Error('invalid');
      setStarted(result); setReportJobId(result.job_id);
    } catch {if (serial.current === requestSerial) setError(true);}
    finally {if (serial.current === requestSerial) setLoading(false);}
  }

  async function inspect() {
    if (!available || !selectedJob || loading) return;
    const requestSerial = ++serial.current;
    setLoading(true); setReportError(false); setReport(null);
    try {
      const result = await api<unknown>('/api/tasks/failure-guidance/inspect', {schema_version: '1.0', job_id: selectedJob.job_id});
      if (serial.current !== requestSerial) return;
      if (!validGuidanceReport(result, selectedJob)) throw new Error('invalid');
      setReport(result);
    } catch {if (serial.current === requestSerial) setReportError(true);}
    finally {if (serial.current === requestSerial) setLoading(false);}
  }

  if (!available || !eligible && jobs.length === 0) return null;
  const yesNo = (value: boolean) => value ? text('Yes', 'Evet') : text('No', 'Hayır');
  const codeLabel = (code: GuidanceCode) => code === 'refresh_observation'
    ? text('Use the fresh observation', 'Taze gözlemi kullan') : text('Inspect the current result before retrying', 'Yeniden denemeden önce mevcut sonucu incele');
  return <section className="registry" data-testid="failure-guidance-panel">
    <h3>{text('Use reviewed guidance', 'İncelenmiş yönlendirmeyi kullan')}</h3>
    <p className="caption">{text('For a fresh Hello task, add the reviewed instruction to the decision model’s current observation. Each action still needs manual approval.',
      'Yeni Hello görevinde incelenmiş talimatı karar modelinin güncel gözlemine ekleyin. Her eylem yine elle onay ister.')}</p>
    {eligible ? <div>
      <button data-testid="failure-guidance-preview" disabled={!ready || loading} onClick={() => void createPreview()}>
        {text('Preview guidance use', 'Yönlendirme kullanımını önizle')}</button>
      {preview ? <div data-testid="failure-guidance-preview-result">
        <p>{codeLabel(preview.guidance_code)}</p>
        <p>{text('Confirmation SHA-256', 'Onay SHA-256')}: <code style={{overflowWrap: 'anywhere'}}>{preview.confirm_sha256}</code></p>
        <label><input type="checkbox" data-testid="failure-guidance-consent" checked={consent} disabled={loading}
          onChange={event => setConsent(event.target.checked)}/>{text('I consent to use this guidance in a new task.', 'Bu yönlendirmeyi yeni görevde kullanmaya izin veriyorum.')}</label>
        <label htmlFor="failure-guidance-confirm">{text('Enter the exact preview hash', 'Önizleme hash değerini tam girin')}</label>
        <input id="failure-guidance-confirm" data-testid="failure-guidance-confirm" value={confirmation} maxLength={64}
          autoComplete="off" spellCheck={false} disabled={loading} onChange={event => setConfirmation(event.target.value.trim().toLowerCase())}/>
        <button data-testid="failure-guidance-start" disabled={!ready || loading || !consent || confirmation !== preview.confirm_sha256}
          onClick={() => void startGuidedTask()}>{text('Start task with guidance', 'Yönlendirmeyle görev başlat')}</button>
      </div> : null}
      {started ? <p role="status" data-testid="failure-guidance-started">{text('Guided task started; open its manual approval.', 'Yönlendirmeli görev başladı; elle onayını açın.')} <code>{started.job_id}</code></p> : null}
    </div> : null}
    {error ? <p role="alert">{text('Guidance preview or start is unavailable or changed. Request a fresh preview.', 'Yönlendirme önizlemesi veya başlangıcı kullanılamıyor ya da değişti. Taze önizleme isteyin.')}</p> : null}
    {jobs.length ? <div>
      <label htmlFor="failure-guidance-job">{text('Guided tasks', 'Yönlendirmeli görevler')}</label>
      <select id="failure-guidance-job" data-testid="failure-guidance-job" value={reportJobId} onChange={event => setReportJobId(event.target.value)}>
        <option value="">{text('Select a task', 'Görev seçin')}</option>
        {jobs.map(job => <option key={job.job_id} value={job.job_id}>{job.status} · {job.job_id.slice(0, 12)}</option>)}
      </select>
      <button data-testid="failure-guidance-inspect" disabled={!selectedJob || loading} onClick={() => void inspect()}>
        {text('Inspect guidance evidence', 'Yönlendirme kanıtını incele')}</button>
      {reportError ? <p role="alert">{text('The guidance evidence does not match this task.', 'Yönlendirme kanıtı bu görevle eşleşmiyor.')}</p> : null}
      {report ? <div data-testid="failure-guidance-report">
        <p>{codeLabel(report.guidance_code)}</p>
        <p>{text('Context binding verified', 'Bağlam bağı doğrulandı')}: {yesNo(report.context_binding_verified)}</p>
        <p>{text('Model request verified', 'Model isteği doğrulandı')}: {yesNo(report.model_request_verified)}</p>
        <p>{text('Guidance applied', 'Yönlendirme uygulandı')}: {yesNo(report.guidance_applied)}</p>
        <p>{text('Current review valid', 'Güncel inceleme geçerli')}: {yesNo(report.followup.current_review_valid)}</p>
        <p>{text('Task outcome verified', 'Görev sonucu doğrulandı')}: {yesNo(report.followup.outcome.status === 'verified')}</p>
        <p className="caption">{text('This evidence shows model-input use and the task result separately. A causal improvement or training label still needs independent evaluation.',
          'Bu kanıt model girdisindeki kullanımı ve görev sonucunu ayrı gösterir. Nedensel iyileşme veya eğitim etiketi hâlâ bağımsız değerlendirme ister.')}</p>
      </div> : null}
    </div> : null}
  </section>;
}
