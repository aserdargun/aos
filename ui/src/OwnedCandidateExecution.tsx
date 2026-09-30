import {useEffect, useRef, useState} from 'react';
import {api, type OwnedFormInvocation, type Snapshot, type Tasks} from './api';
import {t} from './i18n';
import {candidateHash} from './ownedSkillCandidateApi';
import {validCandidateExecutionAudit, validCandidateExecutionStatus, validDevelopmentInput,
  validExecutionPreview, type CandidateExecutionAudit, type ExecutionPreview,
  type ExecutionSelection} from './ownedCandidateExecutionApi';
import './owned_skill_candidate.css';
import {OwnedCandidateReview} from './OwnedCandidateReview';
import {OwnedSkillReleases} from './OwnedSkillReleases';
import type {SelectedRelease} from './ownedSkillReleaseApi';

type Props = {source: OwnedFormInvocation; snapshot: Snapshot; tasks: Tasks;
  selectedCandidate: string; disabled: boolean};
const base = '/api/tasks/owned-form-candidate/';
const stageTools = {open_entry: 'browser.form.open', read_state_before: 'browser.form.state_before',
  fill_form: 'browser.form.fill', submit_form: 'browser.form.submit', read_receipt: 'browser.form.receipt',
  read_state_after: 'browser.form.state_after'};

export function OwnedCandidateExecution({source, snapshot, tasks, selectedCandidate, disabled}: Props) {
  const [candidate, setCandidate] = useState(selectedCandidate ||
    (validCandidateExecutionStatus(tasks.owned_form_candidate_execution, source)
      ? tasks.owned_form_candidate_execution.candidate_sha256 : ''));
  const [caseKey, setCaseKey] = useState('dev-repeat');
  const [value, setValue] = useState('');
  const [reviewHash, setReviewHash] = useState('');
  const [useReview, setUseReview] = useState(Boolean(source.reuse_admission_sha256));
  const [selectedRelease, setSelectedRelease] = useState<SelectedRelease | null>(null);
  const [useRelease, setUseRelease] = useState(Boolean(source.reuse_admission_sha256));
  const [preview, setPreview] = useState<ExecutionPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [audit, setAudit] = useState<CandidateExecutionAudit | null>(null);
  const request = useRef(0);
  const previewControl = useRef<Snapshot['control'] | null>(null);
  const preparingPreview = useRef(false);
  const expectedPreviewHash = useRef<string | null>(null);
  const status = validCandidateExecutionStatus(tasks.owned_form_candidate_execution, source)
    ? tasks.owned_form_candidate_execution : null;
  const statusHash = status?.candidate_execution_sha256 ?? null;
  const statusPreviewHash = status?.lifecycle === 'previewed' ? status.preview_sha256 : null;
  const scope = `${snapshot.runtime.runtime_id}:${source.run_ref}:${source.invocation_sha256}:${source.reuse_admission_sha256 ?? ''}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const statusRef = useRef(statusHash);
  statusRef.current = statusHash;
  useEffect(() => () => { request.current += 1; }, []);
  useEffect(() => { setSelectedRelease(null); }, [scope]);
  useEffect(() => {
    if (!selectedCandidate) return;
    request.current += 1; setCandidate(selectedCandidate); setPreview(null); setConfirmed(false);
  }, [selectedCandidate]);
  useEffect(() => {
    request.current += 1; setPreview(null); setConfirmed(false); setAudit(null); setLoading(false);
    preparingPreview.current = false; expectedPreviewHash.current = null;
  }, [scope, snapshot.control.lease_id, snapshot.control.generation]);
  useEffect(() => {
    setAudit(null);
    if (statusPreviewHash && (preparingPreview.current || expectedPreviewHash.current === statusPreviewHash)) return;
    request.current += 1; setPreview(null); setConfirmed(false); setLoading(false);
    preparingPreview.current = false; expectedPreviewHash.current = null;
  }, [statusHash, statusPreviewHash]);

  function invalidate() {
    request.current += 1; setPreview(null); setConfirmed(false); setUnavailable(false); setLoading(false);
    preparingPreview.current = false; expectedPreviewHash.current = null;
  }
  async function current(requireIdle: boolean) {
    const [state, currentTasks] = await Promise.all([api<Snapshot>('/api/state'), api<Tasks>('/api/tasks')]);
    const currentSource = currentTasks.owned_form_invocation;
    if (scopeRef.current !== scope || state.runtime.runtime_id !== snapshot.runtime.runtime_id
        || !currentSource || currentSource.mode !== 'owned_synthetic_form_invocation'
        || currentSource.lifecycle !== 'audited' || currentSource.run_ref !== source.run_ref
        || currentSource.invocation_sha256 !== source.invocation_sha256
        || currentSource.reuse_admission_sha256 !== source.reuse_admission_sha256
        || currentSource.profile_sha256 !== source.profile_sha256
        || requireIdle && (currentTasks.busy || currentTasks.reserved)) throw new Error('source_changed');
    return {state, tasks: currentTasks};
  }
  function selection(): ExecutionSelection {
    return {schema_version: source.reuse_admission_sha256 ? '1.3' : useRelease ? '1.2' : useReview ? '1.1' : '1.0',
      ...(source.reuse_admission_sha256 ? {reuse_admission_sha256: source.reuse_admission_sha256} : {}),
      ...(useRelease && selectedRelease ? {release_sha256: selectedRelease.release_sha256,
        selection_sha256: selectedRelease.selection_sha256} : {}),
      ...(useReview ? {review_sha256: reviewHash} : {}), candidate_sha256: candidate,
      source_run_ref: source.run_ref!, source_invocation_sha256: source.invocation_sha256,
      case_key: caseKey, development_value: value};
  }
  async function prepare() {
    if (!candidateHash(candidate) || !validDevelopmentInput(caseKey, value)
        || useReview && !candidateHash(reviewHash) || useRelease && !selectedRelease) return;
    const serial = ++request.current;
    const body = selection();
    preparingPreview.current = true; expectedPreviewHash.current = null;
    setLoading(true); setUnavailable(false); setPreview(null); setConfirmed(false);
    try {
      const response = await api<unknown>(base + 'execution-preview', body);
      if (!validExecutionPreview(response, body, source)) throw new Error('invalid_preview');
      if (request.current !== serial) return;
      expectedPreviewHash.current = response.preview_sha256;
      const fresh = await current(true);
      const freshExecution = fresh.tasks.owned_form_candidate_execution;
      if (freshExecution?.lifecycle === 'previewed'
          && (!validCandidateExecutionStatus(freshExecution, source)
            || freshExecution.preview_sha256 !== response.preview_sha256)) throw new Error('preview_changed');
      if (request.current !== serial) return;
      previewControl.current = fresh.state.control;
      setPreview(response);
    } catch {
      if (request.current === serial) setUnavailable(true);
    } finally {
      if (request.current === serial) { preparingPreview.current = false; setLoading(false); }
    }
  }
  async function start() {
    if (!preview || !confirmed || !previewControl.current) return;
    const serial = ++request.current;
    const control = previewControl.current;
    setLoading(true); setUnavailable(false); setConfirmed(false); setAudit(null);
    try {
      const fresh = await current(true);
      if (fresh.state.control.lease_id !== control.lease_id || fresh.state.control.generation !== control.generation
          || fresh.state.control.owner !== 'AGENT' || fresh.state.control.status !== 'running') throw new Error('ownership_changed');
      if (request.current !== serial) return;
      const response = await api<Record<string, unknown>>(base + 'execution-start', {
        ...selection(), preview_sha256: preview.preview_sha256, confirm_sha256: preview.preview_sha256,
        lease_id: control.lease_id, generation: control.generation});
      if (response.schema_version !== preview.schema_version || response.review_sha256 !== preview.review_sha256
          || response.reuse_admission_sha256 !== preview.reuse_admission_sha256
          || response.release_sha256 !== preview.release_sha256 || response.selection_sha256 !== preview.selection_sha256
          || response.accepted !== true || response.lifecycle !== 'running'
          || !candidateHash(response.candidate_execution_sha256) || typeof response.job_id !== 'string'
          || response.candidate_sha256 !== preview.candidate_sha256 || response.case_key !== preview.case_key
          || response.invocation_sha256 !== preview.invocation_sha256) throw new Error('invalid_start');
      if (request.current === serial) setPreview(null);
    } catch {
      if (request.current === serial) { setUnavailable(true); setPreview(null); }
    } finally {
      if (request.current === serial) setLoading(false);
    }
  }
  async function inspectExecution() {
    if (!status || !statusHash || !['completed', 'audited'].includes(status.lifecycle)) return;
    const serial = ++request.current;
    setLoading(true); setUnavailable(false); setAudit(null);
    try {
      const response = await api<unknown>(base + 'execution-audit', {candidate_execution_sha256: statusHash});
      if (!validCandidateExecutionAudit(response, status)) throw new Error('invalid_audit');
      const fresh = await current(true);
      const next = fresh.tasks.owned_form_candidate_execution;
      if (!validCandidateExecutionStatus(next, source) || next.candidate_execution_sha256 !== statusHash
          || next.run_ref !== response.run_ref || next.report_sha256 !== response.report_sha256
          || next.lifecycle !== 'audited') throw new Error('execution_changed');
      if (request.current !== serial || statusRef.current !== statusHash) return;
      setAudit(response);
    } catch {
      if (request.current === serial) setUnavailable(true);
    } finally {
      if (request.current === serial) setLoading(false);
    }
  }
  const locked = disabled || loading || tasks.busy || Boolean(tasks.reserved);
  const visibleAudit = audit && audit.candidate_execution_sha256 === statusHash
    && status?.lifecycle === 'audited' && status.report_sha256 === audit.report_sha256 ? audit : null;
  return <div className="registry owned-skill-candidate" data-testid="candidate-execution">
    <h3>{t('Kaydedilmiş adayı yeni gelişim girdisiyle çalıştır')}</h3>
    <p className="caption">{t('Aynı sentetik site ve kaynak kökeni korunur. Önizleme eylem yapmaz; başlatma ve altı adım ayrı onay ister. Gizli veya kişisel veri girmeyin.')}</p>
    {source.reuse_admission_sha256 ? <p className="caption" data-testid="execution-reuse-source">
      {t('Öğrenme kaynağı yeni oturuma bağlandı. Katalogdan seçili sürümü ayrıca bağlayın; eski görev ve onaylar tekrarlanmaz.')}
      <br/><code>{source.reuse_admission_sha256}</code></p> : null}
    <fieldset disabled={locked}>
      <legend>{t('Yeni gelişim vakası')}</legend>
      <label>{t('Kaydedilmiş aday SHA-256')}<input data-testid="execution-candidate-hash" value={candidate} maxLength={64}
        disabled={useRelease}
        onChange={event => { invalidate(); setCandidate(event.target.value); setSelectedRelease(null); }}/></label>
      <label>{t('Gelişim vaka anahtarı')}<input data-testid="execution-case-key" value={caseKey} maxLength={64}
        onChange={event => { invalidate(); setCaseKey(event.target.value); }}/></label>
      <label>{t('Yeni sentetik değer')}<input data-testid="execution-value" value={value} maxLength={128}
        onChange={event => { invalidate(); setValue(event.target.value); }}/></label>
      <label>{t('Yeni koşu için inceleme SHA-256')}<input data-testid="execution-review-hash" value={reviewHash} maxLength={64}
        disabled={useRelease}
        onChange={event => { invalidate(); setReviewHash(event.target.value); setSelectedRelease(null); }}/></label>
      <label><input type="checkbox" data-testid="execution-use-review" checked={useReview} disabled={useRelease}
        onChange={event => { invalidate(); setUseReview(event.target.checked); }}/>{t('Bu koşuyu seçilen incelemeye bağla; geri çekilirse durdur')}</label>
      <label><input type="checkbox" data-testid="execution-use-release" checked={useRelease}
        disabled={Boolean(source.reuse_admission_sha256) || !selectedRelease && !useRelease}
        onChange={event => { invalidate(); setUseRelease(event.target.checked); }}/>{t('Bu koşuyu kalıcı gelişim sürümü seçimine bağla')}</label>
      {useRelease ? <p data-testid="execution-release-binding"><code>{selectedRelease?.release_sha256 || '—'}</code>
        <br/><code>{selectedRelease?.selection_sha256 || '—'}</code></p> : null}
      <button type="button" data-testid="execution-preview" disabled={!candidateHash(candidate) || !validDevelopmentInput(caseKey, value)
        || useReview && !candidateHash(reviewHash) || useRelease && !selectedRelease}
        onClick={() => void prepare()}>{t('Yeni yürütmeyi önizle')}</button>
    </fieldset>
    {preview ? <div data-testid="execution-preview-result">
      <p>{t('Yürütme önizleme SHA-256:')} <code>{preview.preview_sha256}</code></p>
      <p>{t('Parametre varyantı SHA-256:')} <code>{preview.parameter_variant_sha256}</code></p>
      <ol>{preview.steps.map(step => <li key={step.step_key}><code>{step.step_key}</code> · <code>{step.operation}</code></li>)}</ol>
      <label><input type="checkbox" data-testid="execution-confirm" checked={confirmed} disabled={locked}
        onChange={event => setConfirmed(event.target.checked)}/>{t('Bu exact önizleme ve yeni değer için yürütmeyi onaylıyorum')}</label>
      <button type="button" data-testid="execution-start" disabled={locked || !confirmed || snapshot.control.owner !== 'AGENT'
        || snapshot.control.status !== 'running'} onClick={() => void start()}>{t('Onaylanan gelişim vakasını başlat')}</button>
    </div> : null}
    {status && status.lifecycle !== 'previewed' ? <div data-testid="candidate-execution-status">
      <strong>{t('Aday yürütme durumu:')} {status.lifecycle}</strong>
      {status.review_sha256 ? <p data-testid="execution-review-status">{t('Bağlı inceleme:')} <code>{status.review_sha256}</code> · {status.review_status}</p> : null}
      {status.release_sha256 ? <p data-testid="execution-release-status">{t('Gelişim seçimi:')} <code>{status.release_sha256}</code>
        <br/><code>{status.selection_sha256}</code> · {status.selection_status}</p> : null}
      <p><code>{status.case_key}</code> · <code>{status.candidate_execution_sha256}</code></p>
      <ol>{status.steps.map(step => <li key={step.step_key} data-operation={step.operation}
        aria-current={tasks.jobs[0]?.job_id === status.job_id && tasks.approval?.action.tool === stageTools[step.operation] ? 'step' : undefined}>
        <code>{step.step_key}</code> · <code>{step.operation}</code>
      </li>)}</ol>
      {['completed', 'audited'].includes(status.lifecycle) ? <button type="button" data-testid="execution-audit" disabled={locked}
        onClick={() => void inspectExecution()}>{t('Aday yürütmesini kaynakla denetle')}</button> : null}
    </div> : null}
    {visibleAudit ? <div role="status" data-testid="execution-audit-result">
      <strong>{t('Kaynak kökenine bağlı gelişim yürütmesi doğrulandı')}</strong>
      <p><code>{visibleAudit.report_sha256}</code></p>
      {visibleAudit.reuse_admission_verified ? <p data-testid="execution-reuse-audit">
        {t('Yeni oturum bağı doğrulandı; eski eylemler oynatılmadı.')}</p> : null}
      <p className="caption">{t('Recipe yeni girdide yürütüldü. Bağımsız held-out, gerçek site, skill aktivasyonu veya eğitim kabulü değildir.')}</p>
    </div> : null}
    {loading ? <p role="status">{t('Aday yürütme kaynakları denetleniyor…')}</p> : null}
    {unavailable ? <p role="status" data-testid="execution-unavailable">{t('Yürütme kaynağı veya yetkisi değişti; önizlemeyi yenileyin. Başarı iddiası gösterilmiyor.')}</p> : null}
    <OwnedCandidateReview source={source} snapshot={snapshot} tasks={tasks} execution={status}
      onStored={checksum => { invalidate(); setReviewHash(checksum); if (useRelease) setSelectedRelease(null); }}/>
    <OwnedSkillReleases source={source} snapshot={snapshot} tasks={tasks} reviewHash={reviewHash}
      onSelected={release => {
        invalidate(); setSelectedRelease(release);
        if (release) {
          setUseRelease(true); setUseReview(true); setCandidate(release.release.candidate_sha256);
          setReviewHash(release.release.review_sha256);
        }
      }}/>
  </div>;
}
