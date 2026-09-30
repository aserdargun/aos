import {useEffect, useRef, useState} from 'react';
import {api, type OwnedFormInvocation, type Snapshot, type Tasks} from './api';
import {t} from './i18n';
import './owned_skill_candidate.css';
import {candidateHash, candidateOperations, canonicalAnnotation, validCandidateAnnotation,
  validCandidateContext, validCandidateResult, type CandidateAnnotation,
  type CandidateContext, type CandidateResult} from './ownedSkillCandidateApi';

type Props = {invocation: OwnedFormInvocation; runtimeId: string; jobId: string; disabled: boolean;
  onStored?: (checksum: string) => void};
const base = '/api/tasks/owned-form-candidate';

export function OwnedSkillCandidate({invocation, runtimeId, jobId, disabled, onStored}: Props) {
  const [context, setContext] = useState<CandidateContext | null>(null);
  const [annotation, setAnnotation] = useState<CandidateAnnotation | null>(null);
  const [result, setResult] = useState<CandidateResult | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [namesConfirmed, setNamesConfirmed] = useState(false);
  const [inspectHash, setInspectHash] = useState('');
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const request = useRef(0);
  useEffect(() => () => { request.current += 1; }, []);

  async function assertCurrent() {
    const [tasks, snapshot] = await Promise.all([api<Tasks>('/api/tasks'), api<Snapshot>('/api/state')]);
    const current = tasks.owned_form_invocation;
    const job = tasks.jobs.find(item => item.job_id === jobId);
    if (tasks.busy || tasks.reserved || !current || current.mode !== 'owned_synthetic_form_invocation'
        || current.lifecycle !== 'audited' || current.run_ref !== invocation.run_ref
        || current.invocation_sha256 !== invocation.invocation_sha256
        || current.profile_sha256 !== invocation.profile_sha256
        || current.skill_sha256 !== invocation.skill_sha256
        || snapshot.runtime.runtime_id !== runtimeId || job?.kind !== 'browser_remote_form'
        || job.status !== 'succeeded') throw new Error('candidate_source_changed');
  }

  function edit(next: CandidateAnnotation) {
    request.current += 1;
    setAnnotation(next); setResult(null); setConfirmed(false); setNamesConfirmed(false);
    setUnavailable(false); setLoading(false);
  }

  async function loadContext() {
    const serial = ++request.current;
    setLoading(true); setUnavailable(false); setContext(null); setAnnotation(null);
    setResult(null); setConfirmed(false); setNamesConfirmed(false);
    try {
      const value = await api<unknown>(base + '/context');
      if (!validCandidateContext(value, invocation)) throw new Error('invalid_candidate_context');
      await assertCurrent();
      if (request.current !== serial) return;
      setContext(value); setAnnotation(value.annotation_seed);
    } catch {
      if (request.current === serial) setUnavailable(true);
    } finally {
      if (request.current === serial) setLoading(false);
    }
  }

  async function perform(action: 'preview' | 'publish' | 'inspect') {
    if (!context || !annotation) return;
    const expectedHash = action === 'inspect' ? inspectHash : action === 'publish' ? result?.candidate_sha256 : undefined;
    if (action === 'publish' && (!confirmed || !result || result.status !== 'preview')) return;
    if (action !== 'inspect' && (!namesConfirmed || !validCandidateAnnotation(annotation))) return;
    if (action === 'inspect' && !candidateHash(expectedHash)) return;
    const serial = ++request.current;
    const body = {schema_version: '1.0', source_run_ref: context.source_run_ref,
      invocation_sha256: context.invocation_sha256,
      ...(action === 'inspect' ? {candidate_sha256: expectedHash} : {annotation: canonicalAnnotation(annotation)}),
      ...(action === 'publish' ? {confirm_sha256: expectedHash} : {})};
    setLoading(true); setUnavailable(false); setResult(null); setConfirmed(false);
    try {
      const value = await api<unknown>(base + '/' + action, body);
      const expectedStatus = action === 'publish' ? 'published' : action === 'inspect' ? 'reinspected' : 'preview';
      if (!validCandidateResult(value, context, expectedStatus, expectedHash)) throw new Error('invalid_candidate_result');
      if (action !== 'inspect' && !value.summary.steps.every(step =>
        annotation.operation_step_keys.some(item => item.operation === step.operation && item.step_key === step.step_key))) {
        throw new Error('candidate_steps_changed');
      }
      await assertCurrent();
      if (request.current !== serial) return;
      setResult(value);
      if (value.persisted) { setInspectHash(value.candidate_sha256); onStored?.(value.candidate_sha256); }
    } catch {
      if (request.current === serial) setUnavailable(true);
    } finally {
      if (request.current === serial) setLoading(false);
    }
  }

  const locked = disabled || loading;
  return <div className="registry owned-skill-candidate" data-testid="owned-skill-candidate">
    <h3>{t('Bu deneyimden skill adayı oluştur')}</h3>
    <p className="caption">{t('Yalnız denetlenmiş v1 sentetik gösterim. Adları ve eşlemeleri siz beyan edersiniz; görev, etkinleştirme veya eğitim başlamaz.')}</p>
    <button type="button" disabled={locked} data-testid="candidate-context" onClick={() => void loadContext()}>
      {t('Aday kaynak bağlamını yükle')}
    </button>
    {context && annotation ? <>
      <p className="caption">{t('Kaynak koşu SHA-256:')} <code>{context.source_run_ref}</code></p>
      <fieldset disabled={locked} data-testid="candidate-annotation">
        <legend>{t('Açık operatör beyanları')}</legend>
        <label>{t('Skill anahtarı')}<input data-testid="candidate-skill-key" value={annotation.skill_key} maxLength={64}
          onChange={event => edit({...annotation, skill_key: event.target.value})}/></label>
        {annotation.parameter_bindings.map((binding, index) => <label key={binding.form_field_name}>
          {t('Alan → parametre')} <code>{binding.form_field_name}</code>
          <input aria-label={t('Parametre anahtarı') + ' ' + binding.form_field_name} maxLength={64} value={binding.parameter_key}
            onChange={event => edit({...annotation,
              parameter_bindings: annotation.parameter_bindings.map((item, position) => position === index ? {...item, parameter_key: event.target.value} : item),
              outcome: annotation.outcome.parameter_key === binding.parameter_key
                ? {...annotation.outcome, parameter_key: event.target.value} : annotation.outcome})}/>
        </label>)}
        {annotation.preconditions.map((condition, index) => <label key={condition.kind}>
          {t('Önkoşul anahtarı')} <code>{condition.kind}</code>
          <input aria-label={t('Önkoşul anahtarı') + ' ' + condition.kind} maxLength={64} value={condition.precondition_key}
            onChange={event => edit({...annotation, preconditions: annotation.preconditions.map((item, position) =>
              position === index ? {...item, precondition_key: event.target.value} : item)})}/>
        </label>)}
        <label>{t('Beklenen sonuç anahtarı')}<input data-testid="candidate-outcome-key" value={annotation.outcome.outcome_key} maxLength={64}
          onChange={event => edit({...annotation, outcome: {...annotation.outcome, outcome_key: event.target.value}})}/></label>
        {candidateOperations.map(operation => <label key={operation}>
          <code>{operation}</code> → {t('Adım anahtarı')}
          <input aria-label={t('Adım anahtarı') + ' ' + operation} maxLength={64}
            value={annotation.operation_step_keys.find(item => item.operation === operation)?.step_key ?? ''}
            onChange={event => edit({...annotation, operation_step_keys: annotation.operation_step_keys.map(item =>
              item.operation === operation ? {...item, step_key: event.target.value} : item)})}/>
        </label>)}
        <label><input type="checkbox" data-testid="candidate-names-confirmed" checked={namesConfirmed}
          onChange={event => { setNamesConfirmed(event.target.checked); setResult(null); setConfirmed(false); }}/>
          {t('Bu semantik adları ve alan eşlemelerini ben beyan ediyorum')}
        </label>
        <button type="button" data-testid="candidate-preview" disabled={!namesConfirmed || !validCandidateAnnotation(annotation)}
          onClick={() => void perform('preview')}>{t('Adayı önizle — kaydetme')}</button>
      </fieldset>
      {result ? <div role="status" data-testid="candidate-result">
        <strong>{t(result.status === 'preview' ? 'Önizleme — henüz kaydedilmedi' : result.status === 'published'
          ? 'İncelenmemiş aday kaydedildi' : 'Kaydedilmiş aday kaynağı yeniden doğrulandı')}</strong>
        <p>{t('Aday SHA-256:')} <code>{result.candidate_sha256}</code></p>
        <p className="caption">{t('Kaynak grubu SHA-256:')} <code>{result.summary.source_group_sha256}</code></p>
        <ol>{result.summary.steps.map(step => <li key={step.step_key}><code>{step.step_key}</code> · <code>{step.operation}</code></li>)}</ol>
        <p className="caption">{t('Bu aday henüz yürütülmedi veya incelenmedi. Gerçek site, bağımsız held-out, etkinleştirme ve eğitim kabulü yoktur.')}</p>
        {result.status === 'preview' ? <>
          <label><input type="checkbox" data-testid="candidate-publish-confirm" checked={confirmed} disabled={locked}
            onChange={event => setConfirmed(event.target.checked)}/>{t('Gösterilen exact aday hash’iyle özel kaydı onaylıyorum')}</label>
          <button type="button" data-testid="candidate-publish" disabled={locked || !confirmed}
            onClick={() => void perform('publish')}>{t('Onaylanan adayı kaydet')}</button>
        </> : null}
      </div> : null}
      <label>{t('Yeniden incelenecek aday SHA-256')}<input data-testid="candidate-inspect-hash" value={inspectHash}
        maxLength={64} disabled={locked} onChange={event => { setInspectHash(event.target.value); setResult(null); setConfirmed(false); }}/></label>
      <button type="button" data-testid="candidate-inspect" disabled={locked || !candidateHash(inspectHash)}
        onClick={() => void perform('inspect')}>{t('Kaydedilmiş adayı yeniden incele')}</button>
    </> : null}
    {loading ? <p role="status">{t('Aday kaynağı denetleniyor…')}</p> : null}
    {unavailable ? <p role="status" data-testid="candidate-unavailable">{t('Aday kaynağı değişti veya yanıt geçersiz; doğrulanmış aday gösterilmiyor.')}</p> : null}
  </div>;
}
