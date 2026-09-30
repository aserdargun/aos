import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks as TaskStatus} from './api';
import {useLanguage} from './i18n';
import {FailureFollowup, type FollowupSource} from './FailureFollowup';
import {FailureGuidance} from './FailureGuidance';
import {HelloGuidanceReuse} from './HelloGuidanceReuse';

type FailureJob = TaskStatus['jobs'][number];
type CorrectionCode = 'refresh_observation' | 'inspect_before_retry' | 'request_clarification' | 'repair_environment';
type Decision = 'accept' | 'reject';
type RoleCounts = {ok: number; error: number; timeout: number; cancelled: number};
type Candidate = {
  schema_version: '1.0'; kind: 'failed_task_improvement'; job_id: string; session_id: string;
  task_kind: string; run_ref: string; source_sha256: string; scope_sha256: string;
  outcome: 'failed' | 'cancelled'; failure_code: string | null; synthetic: boolean;
  model_roles: Record<'system1' | 'system2', {deployment_id: string | null; real_model: boolean; calls: RoleCounts}>;
  approval_counts: {consumed: number; rejected: number; expired: number; revoked: number};
  action_counts: {ok: number; error: number; denied: number; cancelled: number};
  metadata_only: true; retrospective: true; training_ready: false; gold: false;
  execution_authorized: false; failure_attribution_verified: false;
};
type Review = {receipt_sha256: string; decision: Decision; correction_code: CorrectionCode | null; revoked: boolean};
type ReviewOption = {decision: Decision; correction_code: CorrectionCode | null; confirm_sha256: string};
type Envelope = {schema_version: '1.0'; job_id: string; available: true; candidate_sha256: string;
  candidate: Candidate; saved: boolean; review: Review | null; review_options: ReviewOption[]; source_current: boolean};
type FailureTasks = TaskStatus & {failure_improvement_available?: boolean; failure_followup_available?: boolean};
type Props = {tasks: FailureTasks | null; snapshot: Snapshot | null; busy: boolean};

const codes: CorrectionCode[] = ['refresh_observation', 'inspect_before_retry', 'request_clarification', 'repair_environment'];
const failureCodes = ['TOOL_FAILURE', 'MODEL_FAILURE', 'INVALID_OUTPUT', 'TIMEOUT', 'UI_CHANGED', 'ELEMENT_MISSING',
  'NETWORK_FAILURE', 'APP_CRASH', 'RUNTIME_CRASH', 'STUCK', 'UNSAFE_ACTION'];
const taskKinds = ['hello', 'browser_form', 'vision_canvas', 'browser_local_navigation', 'browser_staging_workflow',
  'browser_remote_entry', 'browser_remote_routes', 'browser_remote_static_assets', 'browser_remote_form'];
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const countObject = (value: unknown, fields: string[]) => value !== null && typeof value === 'object'
  && Object.keys(value).length === fields.length && fields.every(field => Number.isSafeInteger((value as Record<string, unknown>)[field])
    && (value as Record<string, number>)[field] >= 0 && (value as Record<string, number>)[field] <= 500);

function validCandidate(value: unknown, job: FailureJob): value is Candidate {
  if (value === null || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  const roles = item.model_roles as Record<string, unknown> | null;
  if (Object.keys(item).length !== 20 || item.schema_version !== '1.0'
      || item.kind !== 'failed_task_improvement' || item.job_id !== job.job_id
      || item.task_kind !== job.kind || !taskKinds.includes(String(item.task_kind))
      || !['failed', 'cancelled'].includes(String(item.outcome))
      || item.outcome !== job.status || typeof item.session_id !== 'string'
      || !/^[A-Za-z0-9_-]{1,100}$/.test(item.session_id)
      || typeof item.job_id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(item.job_id)
      || !hash(item.run_ref) || !hash(item.source_sha256) || !hash(item.scope_sha256)
      || !(item.failure_code === null || failureCodes.includes(String(item.failure_code)))
      || typeof item.synthetic !== 'boolean' || !countObject(item.approval_counts, ['consumed', 'rejected', 'expired', 'revoked'])
      || !countObject(item.action_counts, ['ok', 'error', 'denied', 'cancelled'])
      || item.metadata_only !== true || item.retrospective !== true || item.training_ready !== false
      || item.gold !== false || item.execution_authorized !== false || item.failure_attribution_verified !== false
      || roles === null || typeof roles !== 'object' || Object.keys(roles).length !== 2
      || !['system1', 'system2'].every(role => {
        const model = roles[role] as Record<string, unknown> | null;
        return model !== null && typeof model === 'object' && Object.keys(model).length === 3
          && (model.deployment_id === null || typeof model.deployment_id === 'string'
            && /^[A-Za-z0-9_-]{1,100}$/.test(model.deployment_id))
          && typeof model.real_model === 'boolean'
          && countObject(model.calls, ['ok', 'error', 'timeout', 'cancelled']);
      })) return false;
  return true;
}

function validEnvelope(value: unknown, job: FailureJob): value is Envelope {
  if (value === null || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  if (Object.keys(item).length !== 9 || item.schema_version !== '1.0' || item.job_id !== job.job_id
      || item.available !== true || !hash(item.candidate_sha256) || !validCandidate(item.candidate, job)
      || typeof item.saved !== 'boolean' || typeof item.source_current !== 'boolean'
      || !Array.isArray(item.review_options) || item.review_options.length !== 5) return false;
  if (item.review !== null) {
    const review = item.review as Record<string, unknown>;
    if (review === null || typeof review !== 'object' || Object.keys(review).length !== 4
        || !hash(review.receipt_sha256) || !['accept', 'reject'].includes(String(review.decision))
        || !(review.correction_code === null || codes.includes(review.correction_code as CorrectionCode))
        || typeof review.revoked !== 'boolean') return false;
    if ((review.decision === 'accept') !== (review.correction_code !== null)) return false;
  }
  const options = item.review_options as unknown[];
  const keys = new Set<string>();
  if (!options.every(option => {
    if (option === null || typeof option !== 'object') return false;
    const choice = option as Record<string, unknown>;
    if (Object.keys(choice).length !== 3 || !['accept', 'reject'].includes(String(choice.decision))
        || !(choice.correction_code === null || codes.includes(choice.correction_code as CorrectionCode))
        || !hash(choice.confirm_sha256)) return false;
    const key = `${choice.decision}:${choice.correction_code ?? ''}`;
    if (keys.has(key)) return false;
    keys.add(key);
    return true;
  })) return false;
  if (!codes.every(code => keys.has(`accept:${code}`)) || !keys.has('reject:')) return false;
  return true;
}

export function FailureImprovement({tasks, snapshot, busy}: Props) {
  const language = useLanguage();
  const tr = language === 'tr';
  const text = (en: string, trText: string) => tr ? trText : en;
  const jobs = (tasks?.jobs ?? []).filter(job => job.status === 'failed' || job.status === 'cancelled');
  const [jobId, setJobId] = useState('');
  const selectedJob = jobs.find(job => job.job_id === jobId) ?? null;
  const [envelope, setEnvelope] = useState<Envelope | null>(null);
  const [consent, setConsent] = useState(false);
  const [correctionCode, setCorrectionCode] = useState<CorrectionCode>(codes[0]);
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [sourceStale, setSourceStale] = useState(false);
  const serial = useRef(0);
  const runtimeId = snapshot?.runtime.runtime_id ?? '';
  const leaseId = snapshot?.control.lease_id ?? '';
  const generation = snapshot?.control.generation ?? -1;
  const scope = `${jobId}:${selectedJob?.runtime_id ?? ''}:${selectedJob?.status ?? ''}:${runtimeId}:${leaseId}:${generation}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;

  useEffect(() => {
    setJobId(current => jobs.some(job => job.job_id === current) ? current : jobs[0]?.job_id ?? '');
  }, [jobs.map(job => job.job_id).join(':')]);
  useEffect(() => {
    serial.current += 1;
    setEnvelope(null); setConsent(false); setReviewConfirmed(false); setLoading(false); setError(false); setSourceStale(false);
  }, [scope]);
  useEffect(() => () => { serial.current += 1; }, []);

  const controlReady = Boolean(snapshot && tasks && !busy && !tasks.busy && !tasks.reserved && !tasks.approval
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running');
  const enabled = tasks?.failure_improvement_available === true;
  if (jobs.length === 0) return <><FailureFollowup tasks={tasks as (TaskStatus & {
    failure_followup_available?: boolean;
    jobs: (TaskStatus['jobs'][number] & {failure_followup?: {intent_sha256: string; source_job_id: string} | null})[]
  }) | null} snapshot={snapshot} busy={busy} source={null}/>
    <FailureGuidance tasks={tasks} snapshot={snapshot} busy={busy} source={null}/>
    <HelloGuidanceReuse tasks={tasks} snapshot={snapshot} busy={busy} source={null}/></>;

  async function freshControl() {
    if (!snapshot || !selectedJob || scopeRef.current !== scope || !controlReady) throw new Error('stale');
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== runtimeId
        || current.control.lease_id !== leaseId || current.control.generation !== generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') throw new Error('stale');
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }

  async function invoke(operation: 'preview' | 'save' | 'review' | 'revoke', extras: Record<string, unknown> = {}) {
    if (!selectedJob) return;
    const requestSerial = ++serial.current;
    setLoading(true); setError(false);
    try {
      const control = operation === 'preview' ? {} : await freshControl();
      const result = await api<unknown>(`/api/tasks/failure-improvement/${operation}`, {
        schema_version: '1.0', job_id: selectedJob.job_id, ...extras, ...control
      });
      if (serial.current !== requestSerial || scopeRef.current !== scope) return;
      if (!validEnvelope(result, selectedJob)) throw new Error('invalid_envelope');
      if (operation !== 'preview') {
        const expectedCandidate = extras.candidate_sha256 ?? extras.confirm_sha256;
        if (result.candidate_sha256 !== expectedCandidate) throw new Error('candidate_pin_changed');
        if (operation === 'save' && (!result.saved || result.review !== null)) throw new Error('save_not_recorded');
        if (operation === 'review' && (!result.saved || result.review === null
            || result.review.decision !== extras.decision
            || result.review.correction_code !== extras.correction_code
            || result.review.receipt_sha256 !== extras.confirm_sha256 || result.review.revoked)) {
          throw new Error('review_not_recorded');
        }
        if (operation === 'revoke' && (!result.saved || result.review === null
            || result.review.receipt_sha256 !== extras.receipt_sha256 || !result.review.revoked)) {
          throw new Error('revocation_not_recorded');
        }
      }
      setEnvelope(result); setSourceStale(!result.source_current); setConsent(false); setReviewConfirmed(false);
    } catch {
      if (serial.current === requestSerial) {
        setError(true); setSourceStale(true);
      }
    } finally {
      if (serial.current === requestSerial) setLoading(false);
    }
  }

  const review = envelope?.review ?? null;
  const envelopeMatchesJob = Boolean(selectedJob && envelope?.job_id === selectedJob.job_id);
  const canSave = Boolean(enabled && controlReady && !loading && envelopeMatchesJob && envelope && !envelope.saved
    && envelope.source_current && !sourceStale && consent);
  const canReview = Boolean(enabled && controlReady && !loading && envelopeMatchesJob && envelope?.saved
    && envelope.source_current && !sourceStale && !review && reviewConfirmed);
  const canRevoke = Boolean(enabled && controlReady && !loading && envelopeMatchesJob && envelope?.saved
    && review && !review.revoked);
  const currentEnvelope = selectedJob && envelope?.job_id === selectedJob.job_id ? envelope : null;
  const followupSource: FollowupSource | null = selectedJob && currentEnvelope?.saved && currentEnvelope.review ? {
    job_id: selectedJob.job_id,
    session_id: currentEnvelope.candidate.session_id,
    kind: selectedJob.kind,
    candidate_sha256: currentEnvelope.candidate_sha256,
    receipt_sha256: currentEnvelope.review.receipt_sha256,
    correction_code: currentEnvelope.review.correction_code,
    eligible: currentEnvelope.source_current && !sourceStale && currentEnvelope.review.decision === 'accept'
      && !currentEnvelope.review.revoked && currentEnvelope.review.correction_code !== null
  } : null;
  const codeLabel = (code: CorrectionCode) => ({
    refresh_observation: text('Refresh the observation', 'Gözlemi yenile'),
    inspect_before_retry: text('Inspect before retrying', 'Yeniden denemeden önce incele'),
    request_clarification: text('Request clarification', 'Açıklama iste'),
    repair_environment: text('Repair the environment', 'Ortamı düzelt')
  })[code];

  return <section className="registry" data-testid="failure-improvement-panel">
    <h3>{text('Failed task · private diagnostic review', 'Başarısız görev · özel tanı incelemesi')}</h3>
    <p className="caption">{text('Metadata only: no prompts, model outputs, goals, or raw error details are shown. Guidance is not a corrected target, retry, execution permission, or training label.',
      'Yalnız metadata: istemler, model çıktıları, hedefler veya ham hata ayrıntıları gösterilmez. Yönlendirme düzeltilmiş hedef, yeniden deneme, yürütme izni veya eğitim etiketi değildir.')}</p>
    <label htmlFor="failure-job-select">{text('Failed or cancelled task', 'Başarısız veya iptal edilmiş görev')}</label>
    <select id="failure-job-select" data-testid="failure-job-select" value={selectedJob?.job_id ?? ''}
      disabled={loading} onChange={event => setJobId(event.target.value)}>
      {jobs.map(job => <option key={job.job_id} value={job.job_id}>{job.kind} · {job.status} · {job.job_id.slice(0, 12)}</option>)}
    </select>
    {!enabled ? <p role="status">{text('This backend does not provide failure-improvement review.', 'Bu arka uç başarısızlık incelemesini sunmuyor.')}</p> : <>
      <button data-testid="failure-preview" disabled={loading || !selectedJob} onClick={() => void invoke('preview')}>
        {text('Inspect diagnostic metadata', 'Tanı metadata bilgisini incele')}</button>
      {currentEnvelope ? <div data-testid="failure-improvement-result">
        <p role="status">{currentEnvelope.source_current && !sourceStale
          ? text('Source revalidated for this view.', 'Kaynak bu görünüm için yeniden doğrulandı.')
          : text('Source is unavailable or changed; review actions are disabled except revocation.', 'Kaynak kullanılamıyor veya değişti; geri alma dışında inceleme işlemleri kapalıdır.')}</p>
        <dl>
          <dt>{text('Outcome', 'Sonuç')}</dt><dd>{currentEnvelope.candidate.outcome}</dd>
          <dt>{text('Failure code', 'Hata kodu')}</dt><dd>{currentEnvelope.candidate.failure_code ?? '—'}</dd>
          <dt>{text('Task kind', 'Görev türü')}</dt><dd>{currentEnvelope.candidate.task_kind}</dd>
          <dt>{text('System 1 calls · ok / error / timeout / cancelled', 'Sistem 1 çağrıları · başarılı / hata / zaman aşımı / iptal')}</dt>
          <dd>{Object.values(currentEnvelope.candidate.model_roles.system1.calls).join(' / ')}</dd>
          <dt>{text('System 2 calls · ok / error / timeout / cancelled', 'Sistem 2 çağrıları · başarılı / hata / zaman aşımı / iptal')}</dt>
          <dd>{Object.values(currentEnvelope.candidate.model_roles.system2.calls).join(' / ')}</dd>
          <dt>{text('Approvals · consumed / rejected / expired / revoked', 'Onaylar · kullanıldı / reddedildi / süresi doldu / geri alındı')}</dt>
          <dd>{Object.values(currentEnvelope.candidate.approval_counts).join(' / ')}</dd>
          <dt>{text('Actions · ok / error / denied / cancelled', 'Eylemler · başarılı / hata / reddedildi / iptal')}</dt>
          <dd>{Object.values(currentEnvelope.candidate.action_counts).join(' / ')}</dd>
          <dt>{text('Diagnostic record SHA-256', 'Tanı kaydı SHA-256')}</dt><dd><code style={{overflowWrap: 'anywhere'}}>{currentEnvelope.candidate_sha256}</code></dd>
        </dl>
        <p className="caption">{text('Correction codes are bounded human triage guidance only; none proves an outcome. The record remains retrospective, non-gold, and not training-ready.',
          'Düzeltme kodları yalnız sınırlı insan yönlendirmesidir; hiçbiri sonucu kanıtlamaz. Kayıt geçmişe dönük, gold olmayan ve eğitime hazır olmayan durumdadır.')}</p>
        {!currentEnvelope.saved ? <div>
          <label><input data-testid="failure-consent" type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)}/>
            {text('I explicitly consent to save this metadata-only diagnostic record locally.', 'Bu yalnız-metadata tanı kaydının yerel olarak saklanmasına açıkça izin veriyorum.')}</label>
          <button data-testid="failure-save" disabled={!canSave} onClick={() => void invoke('save', {
            confirm_sha256: currentEnvelope.candidate_sha256, consent: true
          })}>{text('Save diagnostic record', 'Tanı kaydını sakla')}</button>
        </div> : <>
          {currentEnvelope.review ? <p role="status" data-testid="failure-review-status">{text('Review:', 'İnceleme:')} {currentEnvelope.review.decision}
            {currentEnvelope.review.correction_code ? ` · ${codeLabel(currentEnvelope.review.correction_code)}` : ''}
            {currentEnvelope.review.revoked ? text(' · revoked', ' · geri alındı') : ''}
            {' · '}<code style={{overflowWrap: 'anywhere'}}>{currentEnvelope.review.receipt_sha256}</code></p> : null}
          {!currentEnvelope.review ? <div>
            <label htmlFor="failure-correction-code">{text('Human triage guidance (not a target label)', 'İnsan yönlendirmesi (hedef etiketi değildir)')}</label>
            <select id="failure-correction-code" data-testid="failure-correction-code" value={correctionCode}
              onChange={event => {setCorrectionCode(event.target.value as CorrectionCode); setReviewConfirmed(false);}}>
              {codes.map(code => <option key={code} value={code}>{codeLabel(code)}</option>)}
            </select>
            <label><input data-testid="failure-review-confirm" type="checkbox" checked={reviewConfirmed}
              onChange={event => setReviewConfirmed(event.target.checked)}/>
              {text('I reviewed this diagnostic guidance; it does not certify task success.', 'Bu tanı yönlendirmesini inceledim; görev başarısını onaylamaz.')}</label>
            {currentEnvelope.review_options.filter(option => option.decision === 'accept' && option.correction_code === correctionCode).map(option =>
              <button key={option.confirm_sha256} data-testid="failure-review-accept" disabled={!canReview}
                onClick={() => void invoke('review', {candidate_sha256: currentEnvelope.candidate_sha256,
                  decision: option.decision, correction_code: option.correction_code, confirm_sha256: option.confirm_sha256})}>
                {text('Record guidance', 'Yönlendirmeyi kaydet')} · {codeLabel(correctionCode)}</button>)}
            {currentEnvelope.review_options.filter(option => option.decision === 'reject').map(option =>
              <button key={option.confirm_sha256} data-testid="failure-review-reject" disabled={!canReview}
                onClick={() => void invoke('review', {candidate_sha256: currentEnvelope.candidate_sha256,
                  decision: option.decision, correction_code: null, confirm_sha256: option.confirm_sha256})}>
                {text('Reject this diagnostic record', 'Bu tanı kaydını reddet')}</button>)}
          </div> : null}
          {currentEnvelope.review && !currentEnvelope.review.revoked ? <button data-testid="failure-revoke" disabled={!canRevoke}
            onClick={() => void invoke('revoke', {candidate_sha256: currentEnvelope.candidate_sha256,
              receipt_sha256: currentEnvelope.review!.receipt_sha256})}>{text('Revoke review', 'İncelemeyi geri al')}</button> : null}
        </>}
        <p className="caption" data-testid="failure-no-training">{text('Export and training are not available for this failed-task record.',
          'Bu başarısız görev kaydı için dışa aktarım ve eğitim kullanılamaz.')}</p>
      </div> : null}
    </>}
    <FailureFollowup tasks={tasks as (TaskStatus & {
      failure_followup_available?: boolean;
      jobs: (TaskStatus['jobs'][number] & {failure_followup?: {intent_sha256: string; source_job_id: string} | null})[]
    }) | null} snapshot={snapshot} busy={busy} source={followupSource}/>
    <FailureGuidance tasks={tasks} snapshot={snapshot} busy={busy} source={followupSource}/>
    <HelloGuidanceReuse tasks={tasks} snapshot={snapshot} busy={busy} source={followupSource}/>
    {error ? <p role="alert">{text('Diagnostic source changed or is unavailable. No action was authorized.',
      'Tanı kaynağı değişti veya kullanılamıyor. Hiçbir eylem yetkilendirilmedi.')}</p> : null}
  </section>;
}
