import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks as TaskStatus} from './api';
import {useLanguage} from './i18n';

type CorrectionCode = 'refresh_observation' | 'inspect_before_retry' | 'request_clarification' | 'repair_environment';
type FollowupLink = {intent_sha256: string; source_job_id: string};
type FollowupJob = TaskStatus['jobs'][number] & {failure_followup?: FollowupLink | null};
type FollowupTasks = TaskStatus & {failure_followup_available?: boolean; jobs: FollowupJob[]};
export type FollowupSource = {job_id: string; session_id: string; kind: string; candidate_sha256: string; receipt_sha256: string;
  correction_code: CorrectionCode | null; eligible: boolean};
type Target = {session_id: string; parent_runtime_id: string; image_id: string; lease_id: string;
  generation: number; task_kind: string; configuration_sha256: string; system1_deployment_id: string;
  system2_deployment_id: string | null};
export type Preview = {schema_version: '1.0'; attempt_id: string; source_job_id: string; candidate_sha256: string;
  receipt_sha256: string; source_sha256: string; target: Target;
  source_contract: {semantic_sha256: string; binding_sha256: string}; correction_code: CorrectionCode;
  manual_approval_required: true; guidance_applied: false; causality_verified: false;
  gold: false; training_ready: false; confirm_sha256: string};
export type Report = {schema_version: '1.0'; job_id: string; source_job_id: string; intent_sha256: string;
  historical_binding_verified: boolean; current_source_valid: boolean; current_review_valid: boolean;
  outcome: {status: 'verified' | 'not_verified' | 'unsupported'; verification_count: number;
    scope: 'fixed_task_outcome' | 'transport_readback' | 'none';
    reason: 'independent_evidence_verified' | 'run_not_settled_success' | 'evidence_missing_or_changed' | 'task_contract_unsupported'};
  manual_approval_required: true; guidance_applied: false; causality_verified: false;
  gold: false; training_ready: false};
type StartResult = {schema_version: '1.0'; job_id: string; intent_sha256: string; manual_approval_required: true};
type Props = {tasks: FollowupTasks | null; snapshot: Snapshot | null; busy: boolean; source: FollowupSource | null};

const codes: CorrectionCode[] = ['refresh_observation', 'inspect_before_retry', 'request_clarification', 'repair_environment'];
const sha = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value: Record<string, unknown>, expected: string[]) => Object.keys(value).length === expected.length
  && expected.every(key => Object.hasOwn(value, key));
const id = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);

export function validPreview(value: unknown, source: FollowupSource, runtimeId: string, imageId: string | null,
  leaseId: string, generation: number): value is Preview {
  if (!record(value) || !exactKeys(value, ['schema_version', 'attempt_id', 'source_job_id', 'candidate_sha256',
    'receipt_sha256', 'source_sha256', 'target', 'source_contract', 'correction_code',
    'manual_approval_required', 'guidance_applied', 'causality_verified', 'gold', 'training_ready', 'confirm_sha256'])) return false;
  const target = value.target;
  const contract = value.source_contract;
  if (!record(target) || !exactKeys(target, ['session_id', 'parent_runtime_id', 'image_id', 'lease_id', 'generation',
    'task_kind', 'configuration_sha256', 'system1_deployment_id', 'system2_deployment_id'])
      || !record(contract) || !exactKeys(contract, ['semantic_sha256', 'binding_sha256'])) return false;
  return value.schema_version === '1.0' && typeof value.attempt_id === 'string'
    && /^followup-[a-f0-9]{32}$/.test(value.attempt_id) && value.source_job_id === source.job_id
    && value.candidate_sha256 === source.candidate_sha256 && value.receipt_sha256 === source.receipt_sha256
    && sha(value.source_sha256) && codes.includes(value.correction_code as CorrectionCode)
    && source.correction_code !== null && value.correction_code === source.correction_code && value.manual_approval_required === true
    && value.guidance_applied === false && value.causality_verified === false && value.gold === false
    && value.training_ready === false && sha(value.confirm_sha256)
    && target.session_id === source.session_id && target.parent_runtime_id === runtimeId && target.image_id === imageId
    && target.lease_id === leaseId && target.generation === generation && target.task_kind === source.kind
    && sha(target.configuration_sha256) && id(target.system1_deployment_id)
    && (target.system2_deployment_id === null || id(target.system2_deployment_id))
    && sha(contract.semantic_sha256) && sha(contract.binding_sha256);
}

export function validReport(value: unknown, job: FollowupJob): value is Report {
  if (!record(value) || !exactKeys(value, ['schema_version', 'job_id', 'source_job_id', 'intent_sha256',
    'historical_binding_verified', 'current_source_valid', 'current_review_valid', 'outcome',
    'manual_approval_required', 'guidance_applied', 'causality_verified', 'gold', 'training_ready'])) return false;
  const link = job.failure_followup;
  const outcome = value.outcome;
  if (!link || !record(outcome) || !exactKeys(outcome, ['status', 'verification_count', 'scope', 'reason'])) return false;
  return value.schema_version === '1.0' && value.job_id === job.job_id
    && value.source_job_id === link.source_job_id && sha(value.intent_sha256) && value.intent_sha256 === link.intent_sha256
    && typeof value.historical_binding_verified === 'boolean' && typeof value.current_source_valid === 'boolean'
    && typeof value.current_review_valid === 'boolean' && ['verified', 'not_verified', 'unsupported'].includes(String(outcome.status))
    && Number.isSafeInteger(outcome.verification_count) && (outcome.verification_count as number) >= 0
    && (outcome.verification_count as number) <= 500
    && ['fixed_task_outcome', 'transport_readback', 'none'].includes(String(outcome.scope))
    && ['independent_evidence_verified', 'run_not_settled_success', 'evidence_missing_or_changed', 'task_contract_unsupported'].includes(String(outcome.reason))
    && value.manual_approval_required === true && value.guidance_applied === false && value.causality_verified === false
    && value.gold === false && value.training_ready === false;
}

function validStart(value: unknown): value is StartResult {
  return record(value) && exactKeys(value, ['schema_version', 'job_id', 'intent_sha256', 'manual_approval_required'])
    && value.schema_version === '1.0' && id(value.job_id) && sha(value.intent_sha256)
    && value.manual_approval_required === true;
}

export function FailureFollowup({tasks, snapshot, busy, source}: Props) {
  const tr = useLanguage() === 'tr';
  const text = (en: string, translated: string) => tr ? translated : en;
  const available = tasks?.failure_followup_available === true;
  const controlReady = Boolean(snapshot && tasks && !busy && !tasks.busy && !tasks.reserved && !tasks.approval
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running');
  const runtimeId = snapshot?.runtime.runtime_id ?? '';
  const imageId = snapshot?.runtime.image_id ?? null;
  const leaseId = snapshot?.control.lease_id ?? '';
  const generation = snapshot?.control.generation ?? -1;
  const scope = `${source?.job_id ?? ''}:${source?.candidate_sha256 ?? ''}:${source?.receipt_sha256 ?? ''}:${runtimeId}:${leaseId}:${generation}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const serial = useRef(0);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [consent, setConsent] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [started, setStarted] = useState<StartResult | null>(null);
  const [reportJobId, setReportJobId] = useState('');
  const [report, setReport] = useState<Report | null>(null);
  const [reportError, setReportError] = useState(false);

  useEffect(() => {
    serial.current += 1;
    setPreview(null); setConsent(false); setConfirmation(''); setLoading(false); setError(false); setStarted(null);
    setReport(null); setReportJobId(''); setReportError(false);
  }, [scope]);
  useEffect(() => () => { serial.current += 1; }, []);

  const linkedJobs = (tasks?.jobs ?? []).filter(job => job.failure_followup != null
    && id(job.failure_followup.source_job_id) && sha(job.failure_followup.intent_sha256));
  const selectedReportJob = linkedJobs.find(job => job.job_id === reportJobId) ?? null;
  const reportScope = `${reportJobId}:${selectedReportJob?.failure_followup?.source_job_id ?? ''}:${selectedReportJob?.failure_followup?.intent_sha256 ?? ''}`;
  useEffect(() => {
    serial.current += 1;
    setReport(null); setReportError(false); setLoading(false);
  }, [reportScope]);
  const canPreview = Boolean(available && source?.eligible && !loading && controlReady);

  async function freshControl(target?: Target) {
    if (!controlReady || !source || !source.eligible || scopeRef.current !== scope) throw new Error('stale');
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== runtimeId || current.runtime.image_id !== imageId
        || current.control.lease_id !== leaseId || current.control.generation !== generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') throw new Error('stale');
    if (target && (target.session_id !== source.session_id || current.control.lease_id !== target.lease_id
        || current.control.generation !== target.generation || current.runtime.runtime_id !== target.parent_runtime_id
        || current.runtime.image_id !== target.image_id)) throw new Error('target_changed');
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }

  async function createPreview() {
    if (!source || !source.eligible || !canPreview) return;
    const request = ++serial.current;
    setLoading(true); setError(false); setPreview(null); setConsent(false); setConfirmation(''); setStarted(null);
    try {
      await freshControl();
      const result = await api<unknown>('/api/tasks/failure-followup/preview', {
        schema_version: '1.0', source_job_id: source.job_id, candidate_sha256: source.candidate_sha256,
        receipt_sha256: source.receipt_sha256
      });
      if (request !== serial.current || scopeRef.current !== scope) return;
      if (!validPreview(result, source, runtimeId, imageId, leaseId, generation)) throw new Error('invalid_preview');
      setPreview(result);
    } catch {
      if (request === serial.current) setError(true);
    } finally {
      if (request === serial.current) setLoading(false);
    }
  }

  async function startFollowup() {
    if (!preview || !source?.eligible || !available || !controlReady || !consent || confirmation !== preview.confirm_sha256 || loading) return;
    const request = ++serial.current;
    const accepted = preview;
    setPreview(null); setConsent(false); setConfirmation(''); setLoading(true); setError(false); setStarted(null);
    try {
      const control = await freshControl(accepted.target);
      const result = await api<unknown>('/api/tasks/failure-followup/start', {
        schema_version: '1.0', preview: accepted, confirm_sha256: accepted.confirm_sha256,
        consent: true, ...control
      });
      if (request !== serial.current || scopeRef.current !== scope) return;
      if (!validStart(result)) throw new Error('invalid_start');
      setStarted(result); setReportJobId(result.job_id); setReport(null);
    } catch {
      if (request === serial.current) setError(true);
    } finally {
      if (request === serial.current) setLoading(false);
    }
  }

  async function inspect(job: FollowupJob) {
    const request = ++serial.current;
    setLoading(true); setReportError(false); setReport(null);
    try {
      const result = await api<unknown>('/api/tasks/failure-followup/inspect', {schema_version: '1.0', job_id: job.job_id});
      if (request !== serial.current) return;
      if (!validReport(result, job)) throw new Error('invalid_report');
      setReport(result); setReportJobId(job.job_id);
    } catch {
      if (request === serial.current) setReportError(true);
    } finally {
      if (request === serial.current) setLoading(false);
    }
  }

  const reasons = (reason: Report['outcome']['reason']) => ({
    independent_evidence_verified: text('Independent evidence verified the fixed task result.', 'Bağımsız kanıt sabit görev sonucunu doğruladı.'),
    run_not_settled_success: text('The new run did not settle successfully.', 'Yeni görev başarıyla sonuçlanmadı.'),
    evidence_missing_or_changed: text('Required evidence is missing or changed.', 'Gerekli kanıt eksik veya değişmiş.'),
    task_contract_unsupported: text('This task contract is not supported for outcome verification.', 'Bu görev sözleşmesi sonuç doğrulaması için desteklenmiyor.')
  })[reason];
  const corrections = {
    refresh_observation: text('Refresh the observation', 'Gözlemi yenile'),
    inspect_before_retry: text('Inspect before retrying', 'Yeniden denemeden önce incele'),
    request_clarification: text('Request clarification', 'Açıklama iste'),
    repair_environment: text('Repair the environment', 'Ortamı düzelt')
  };
  const outcomeStatus = (status: Report['outcome']['status']) => ({
    verified: text('Verified', 'Doğrulandı'),
    not_verified: text('Not verified', 'Doğrulanmadı'),
    unsupported: text('Unsupported', 'Desteklenmiyor')
  })[status];
  const yesNo = (value: boolean) => value ? text('Yes', 'Evet') : text('No', 'Hayır');
  if (!linkedJobs.length && !(available && source)) return null;

  return <section className="registry" data-testid="failure-followup-panel">
    <h3>{text('Explicit follow-up task', 'Açık takip görevi')}</h3>
    <p className="caption">{text('A reviewed correction code is not applied automatically. A new task needs its own manual approvals; the report does not show that the guidance caused an outcome or authorizes training.',
      'İncelenmiş düzeltme kodu otomatik uygulanmaz. Yeni görev kendi elle onaylarını ister; rapor yönlendirmenin sonuca neden olduğunu göstermez ve eğitime izin vermez.')}</p>
    {available && source?.eligible ? <div>
      <p>{text('Source review', 'Kaynak incelemesi')}: {source.correction_code ? corrections[source.correction_code] : '—'}</p>
      <button data-testid="failure-followup-preview" disabled={!canPreview} onClick={() => void createPreview()}>
        {text('Preview a new task', 'Yeni görevi önizle')}</button>
      {preview ? <div data-testid="failure-followup-preview-result">
        <p>{text('A fresh task is proposed; no work has started.', 'Yeni görev önerildi; henüz çalışma başlamadı.')}</p>
        <p>{text('Task kind', 'Görev türü')}: {preview.target.task_kind}</p>
        <p>{text('Attempt', 'Deneme')}: <code style={{overflowWrap: 'anywhere'}}>{preview.attempt_id}</code></p>
        <p>{text('Confirmation SHA-256', 'Onay SHA-256')}: <code style={{overflowWrap: 'anywhere'}}>{preview.confirm_sha256}</code></p>
        <p className="caption">{text('The prior guidance is not automatically inserted into the new task. Review the task normally and approve each action manually.',
          'Önceki yönlendirme yeni göreve otomatik eklenmez. Görevi normal şekilde inceleyin ve her eylemi ayrı elle onaylayın.')}</p>
        <label><input data-testid="failure-followup-consent" type="checkbox" checked={consent} disabled={loading}
          onChange={event => setConsent(event.target.checked)}/>{text('I explicitly opt in to start this new task.', 'Bu yeni görevi başlatmayı açıkça seçiyorum.')}</label>
        <label htmlFor="failure-followup-confirm">{text('Enter the exact preview hash', 'Önizleme hash değerini tam girin')}</label>
        <input id="failure-followup-confirm" data-testid="failure-followup-confirm" value={confirmation}
          maxLength={64} autoComplete="off" spellCheck={false} disabled={loading}
          onChange={event => setConfirmation(event.target.value.trim().toLowerCase())}/>
        <button data-testid="failure-followup-start" disabled={!available || !controlReady || loading || !consent
          || confirmation !== preview.confirm_sha256} onClick={() => void startFollowup()}>
          {text('Start new task', 'Yeni görevi başlat')}</button>
      </div> : null}
      {started ? <p role="status" data-testid="failure-followup-started">{text('New task started; manual approvals are required.', 'Yeni görev başladı; elle onay gerekir.')} · <code>{started.job_id}</code></p> : null}
    </div> : null}
    {!available ? <p role="status">{text('This backend does not support failure follow-up.', 'Bu arka uç başarısızlık takibini desteklemiyor.')}</p> : null}
    {error ? <p role="alert">{text('The preview or start response was unavailable or changed. No automatic retry occurred.', 'Önizleme veya başlatma yanıtı kullanılamadı ya da değişti. Otomatik tekrar yapılmadı.')}</p> : null}
    {available && linkedJobs.length ? <div>
      <label htmlFor="failure-followup-job">{text('Previously linked follow-up tasks', 'Önceden bağlı takip görevleri')}</label>
      <select id="failure-followup-job" data-testid="failure-followup-job" value={reportJobId}
        onChange={event => {setReportJobId(event.target.value); setReport(null); setReportError(false);}}>
        <option value="">{text('Select a task', 'Görev seçin')}</option>
        {linkedJobs.map(job => <option key={job.job_id} value={job.job_id}>{job.kind} · {job.status} · {job.job_id.slice(0, 12)}</option>)}
      </select>
      <button data-testid="failure-followup-inspect" disabled={loading || !selectedReportJob}
        onClick={() => selectedReportJob && void inspect(selectedReportJob)}>{text('Inspect outcome report', 'Sonuç raporunu incele')}</button>
      {reportError ? <p role="alert">{text('The linked report is unavailable or does not match this task.', 'Bağlı rapor kullanılamıyor veya bu görevle eşleşmiyor.')}</p> : null}
      {report && report.job_id === reportJobId ? <div data-testid="failure-followup-report">
        <p>{text('Outcome verification', 'Sonuç doğrulaması')}: {outcomeStatus(report.outcome.status)} · {report.outcome.verification_count}</p>
        <p>{reasons(report.outcome.reason)}</p>
        <p>{text('Historical binding verified', 'Geçmiş bağ doğrulandı')}: {yesNo(report.historical_binding_verified)}</p>
        <p>{text('Current source valid', 'Güncel kaynak geçerli')}: {yesNo(report.current_source_valid)} · {text('Current review valid', 'Güncel inceleme geçerli')}: {yesNo(report.current_review_valid)}</p>
        <p className="caption">{text('Outcome verification is not proof that the reviewed guidance caused the outcome. No training or automatic promotion is enabled.',
          'Sonuç doğrulaması, incelenmiş yönlendirmenin sonuca neden olduğunun kanıtı değildir. Eğitim veya otomatik yükseltme etkin değildir.')}</p>
      </div> : null}
    </div> : null}
  </section>;
}
