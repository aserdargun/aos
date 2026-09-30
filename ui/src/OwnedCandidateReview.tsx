import {useEffect, useRef, useState} from 'react';
import {api, type OwnedFormInvocation, type Snapshot, type Tasks} from './api';
import {t} from './i18n';
import {candidateHash} from './ownedSkillCandidateApi';
import type {CandidateExecutionStatus} from './ownedCandidateExecutionApi';
import {reviewMatchesExecution, validCandidateReview, type CandidateReview, type ReviewSelection} from './ownedCandidateReviewApi';

type Props = {source: OwnedFormInvocation; snapshot: Snapshot; tasks: Tasks;
  execution: CandidateExecutionStatus | null; onStored: (hash: string) => void};
const base = '/api/tasks/owned-form-candidate/review-';

export function OwnedCandidateReview({source, snapshot, tasks, execution, onStored}: Props) {
  const [hash, setHash] = useState('');
  const [review, setReview] = useState<CandidateReview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [revokeConfirmed, setRevokeConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const serial = useRef(0);
  const scope = `${snapshot.runtime.runtime_id}:${source.run_ref}:${source.invocation_sha256}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => () => { serial.current += 1; }, []);
  useEffect(() => {
    serial.current += 1; setConfirmed(false); setLoading(false);
    setReview(previous => previous?.status === 'preview' ? null : previous);
  }, [scope, execution?.candidate_execution_sha256]);
  useEffect(() => {
    if (execution?.review_sha256 && execution.review_status !== 'accepted') {
      setReview(previous => previous && previous.review_sha256 === execution.review_sha256 && previous.status === 'accepted' ? null : previous);
      setRevokeConfirmed(false);
    }
  }, [execution?.review_sha256, execution?.review_status]);

  async function scopeCurrent() {
    const [state, fresh] = await Promise.all([api<Snapshot>('/api/state'), api<Tasks>('/api/tasks')]);
    if (scopeRef.current !== scope || state.runtime.runtime_id !== snapshot.runtime.runtime_id
        || fresh.owned_form_invocation?.run_ref !== source.run_ref
        || fresh.owned_form_invocation?.invocation_sha256 !== source.invocation_sha256
        || fresh.owned_form_invocation?.profile_sha256 !== source.profile_sha256) throw new Error('scope_changed');
  }
  function selection(current: CandidateExecutionStatus): ReviewSelection {
    return {candidate_sha256: current.candidate_sha256, source_run_ref: current.source_run_ref,
      source_invocation_sha256: current.source_invocation_sha256,
      candidate_execution_sha256: current.candidate_execution_sha256!};
  }
  async function operate(operation: 'preview' | 'accept' | 'inspect' | 'revoke') {
    if (operation === 'preview' && (!execution || execution.lifecycle !== 'audited')) return;
    if (operation === 'accept' && (!review || review.status !== 'preview' || !confirmed)) return;
    if (operation === 'revoke' && (!review || review.status !== 'accepted' || !revokeConfirmed)) return;
    if (operation === 'inspect' && !candidateHash(hash)) return;
    const request = ++serial.current;
    const selected = operation === 'preview' ? selection(execution!) : operation === 'accept' ? {
      candidate_sha256: review!.receipt.candidate_sha256, source_run_ref: review!.receipt.source_run_ref,
      source_invocation_sha256: review!.receipt.source_invocation_sha256,
      candidate_execution_sha256: review!.receipt.candidate_execution_sha256,
    } : undefined;
    const checksum = operation === 'inspect' ? hash : review?.review_sha256;
    const body = operation === 'preview' ? selected : operation === 'accept' ? {
      ...selected, review_sha256: checksum, confirm_sha256: checksum,
    } : operation === 'revoke' ? {review_sha256: checksum, confirm_sha256: checksum} : {review_sha256: checksum};
    setLoading(true); setUnavailable(false); setConfirmed(false); setRevokeConfirmed(false);
    try {
      const result = await api<unknown>(base + operation, body);
      if (!validCandidateReview(result, source, selected)
          || operation === 'preview' && (result.status !== 'preview' || !reviewMatchesExecution(result, execution!))
          || operation === 'accept' && result.status !== 'accepted'
          || operation === 'revoke' && result.status !== 'revoked'
          || operation === 'inspect' && result.status === 'preview'
          || operation !== 'preview' && result.review_sha256 !== checksum) throw new Error('review_changed');
      await scopeCurrent();
      if (serial.current !== request) return;
      setReview(result); setHash(result.review_sha256);
      if (result.status === 'accepted') onStored(result.review_sha256);
    } catch {
      if (serial.current === request) { setUnavailable(true); setReview(null); }
    } finally {
      if (serial.current === request) setLoading(false);
    }
  }
  return <section data-testid="candidate-review" className="owned-skill-candidate">
    <h4>{t('Kanıta bağlı insan incelemesi')}</h4>
    <p className="caption">{t('İnceleme yalnız bu sentetik aday ve denetlenmiş gelişim kanıtını kapsar. Aktivasyon, bağımsız held-out veya eğitim izni değildir.')}</p>
    <button data-testid="review-preview" disabled={loading || tasks.busy || Boolean(tasks.reserved) || execution?.lifecycle !== 'audited'}
      onClick={() => void operate('preview')}>{t('Son denetlenmiş yürütmeyi incele')}</button>
    <label>{t('İnceleme kaydı SHA-256')}<input data-testid="review-hash" value={hash} maxLength={64}
      disabled={loading} onChange={event => { serial.current += 1; setHash(event.target.value); setReview(null); setConfirmed(false); setRevokeConfirmed(false); }}/></label>
    <button data-testid="review-inspect" disabled={loading || !candidateHash(hash)} onClick={() => void operate('inspect')}>{t('İnceleme kaydını yeniden denetle')}</button>
    {review ? <div data-testid="review-result">
      <p>{t('İnceleme durumu:')} <strong>{review.status}</strong></p>
      <p><code>{review.review_sha256}</code></p>
      <p>{t('Skill / sonuç:')} <code>{review.summary.skill_key} / {review.summary.expected_outcome_key}</code></p>
      <p>{t('Parametre / form alanı:')} <code>{review.summary.parameter_key} / {review.summary.form_field_name}</code></p>
      <p>{t('Kanıt vakası:')} <code>{review.receipt.case_key} · {review.receipt.execution_run_ref}</code></p>
      <ol>{review.summary.steps.map(step => <li key={step.step_key}><code>{step.step_key} · {step.operation}</code></li>)}</ol>
      {review.status === 'preview' ? <>
        <label><input data-testid="review-confirm" type="checkbox" checked={confirmed} disabled={loading || tasks.busy}
          onChange={event => setConfirmed(event.target.checked)}/>{t('Bu exact aday, semantik eşleme ve gelişim kanıtının incelemesini kabul ediyorum')}</label>
        <button data-testid="review-accept" disabled={!confirmed || loading || tasks.busy || Boolean(tasks.reserved)}
          onClick={() => void operate('accept')}>{t('İncelemeyi kaydet')}</button>
      </> : null}
      {review.status === 'accepted' ? <>
        <p className="caption">{t('Yeni koşuda bu kaydı kullanmak ayrı seçim gerektirir. Görev ve altı eylem onayı değişmez.')}</p>
        <label><input data-testid="review-revoke-confirm" type="checkbox" checked={revokeConfirmed} disabled={loading}
          onChange={event => setRevokeConfirmed(event.target.checked)}/>{t('Bu exact incelemeyi kalıcı olarak geri çekmeyi onaylıyorum')}</label>
        <button data-testid="review-revoke" disabled={loading || !revokeConfirmed} onClick={() => void operate('revoke')}>{t('İncelemeyi geri çek')}</button>
      </> : null}
      {review.status === 'revoked' ? <p>{t('İnceleme geri çekildi. Geçmiş başarı silinmez; bu incelemeye bağlı yeni eylemler engellenir.')}</p> : null}
    </div> : null}
    {loading ? <p role="status">{t('İnceleme kanıtları denetleniyor…')}</p> : null}
    {unavailable ? <p role="alert" data-testid="review-unavailable">{t('İnceleme kaynağı veya onayı geçersiz. Kabul iddiası gösterilmiyor.')}</p> : null}
  </section>;
}
