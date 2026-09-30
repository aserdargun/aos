import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {useLanguage} from './i18n';
import {OwnedAdapterRuntime} from './OwnedAdapterRuntime';
import {OwnedAdapterEvaluation} from './OwnedAdapterEvaluation';

export type AdaptationStatus = {state: 'idle' | 'training' | 'evaluating' | 'verified' | 'failed' | 'cancelled';
  episode_id?: string; conversion_sha256?: string; authorization_sha256?: string;
  training_ready: false; promotion_authorized: false};
type Preview = {authorization_sha256: string; persisted: false; training_started: false;
  authorization: {episode_id: string; conversion_sha256: string; development_decisions: number;
    split: 'development_only'; scope: 'owned_synthetic_development_adapter'; training_ready: false;
    promotion_authorized: false; budget: {adapter_rank: 4; optimizer_steps: 1; max_tokens: 1536}}};
type Report = {authorization_sha256: string; authorization: Preview['authorization'];
  evaluation: 'resubstitution'; training_ready: false; promotion_authorized: false;
  artifact_sha256: string; replay: {candidate_loaded: true; base_parameters_unchanged: true;
    development_decisions: number; base_development_nll: number; candidate_development_nll: number;
    base_development_accuracy: number; candidate_development_accuracy: number}};
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

export function OwnedEpisodeAdaptation({episodeId, conversionSha256, status, snapshot, disabled}: {
  episodeId: string; conversionSha256: string; status: AdaptationStatus; snapshot: Snapshot; disabled: boolean;
}) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const [preview, setPreview] = useState<Preview | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [rights, setRights] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const serial = useRef(0);
  const scope = episodeId + conversionSha256 + snapshot.control.lease_id + ':' + snapshot.control.generation;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1; setPreview(null); setReport(null); setRights(false); setConsent(false); setError(false); setBusy(false);
  }, [scope]);
  useEffect(() => () => {serial.current += 1;}, []);
  const current = status.episode_id === episodeId && status.conversion_sha256 === conversionSha256 ? status : null;
  const state = current?.state ?? 'idle';
  const running = state === 'training' || state === 'evaluating';

  async function operate(operation: 'adaptation-preview' | 'adaptation-start' | 'adaptation-inspect') {
    const requestScope = scopeRef.current;
    const requestSerial = ++serial.current;
    const checksum = operation === 'adaptation-inspect' ? current?.authorization_sha256 : preview?.authorization_sha256;
    setBusy(true); setError(false); setReport(null);
    try {
      const control = snapshot.control;
      const body = operation === 'adaptation-inspect' ? {authorization_sha256: checksum}
        : {conversion_sha256: conversionSha256, lease_id: control.lease_id, generation: control.generation,
          ...(operation === 'adaptation-start' ? {confirm_sha256: checksum,
            rights_redaction_reviewed: rights, experimental_training_authorized: consent} : {})};
      const result = await api('/api/tasks/owned-episode/' + operation, {
        schema_version: '1.0', episode_id: episodeId, ...body}) as Preview | Report | AdaptationStatus;
      if (requestSerial !== serial.current || requestScope !== scopeRef.current) return;
      if (operation === 'adaptation-start') {
        const started = result as AdaptationStatus;
        if (started.state !== 'training' || started.authorization_sha256 !== checksum
            || started.episode_id !== episodeId || started.conversion_sha256 !== conversionSha256
            || started.training_ready !== false || started.promotion_authorized !== false) throw new Error('invalid_start');
        setPreview(null); setConsent(false); setRights(false);
        return;
      }
      const value = result as Preview | Report;
      if (!hash(value.authorization_sha256) || value.authorization?.episode_id !== episodeId
          || value.authorization.conversion_sha256 !== conversionSha256
          || value.authorization.split !== 'development_only' || value.authorization.training_ready !== false
          || value.authorization.promotion_authorized !== false
          || !Number.isInteger(value.authorization.development_decisions)
          || value.authorization.development_decisions < 1 || value.authorization.development_decisions > 32) {
        throw new Error('invalid_authorization');
      }
      if (operation === 'adaptation-preview') {
        const value = result as Preview;
        if (value.persisted !== false || value.training_started !== false
            || value.authorization.scope !== 'owned_synthetic_development_adapter'
            || value.authorization.budget?.adapter_rank !== 4 || value.authorization.budget.optimizer_steps !== 1
            || value.authorization.budget.max_tokens !== 1536) throw new Error('invalid_budget');
        setPreview(value); setConsent(false); setRights(false);
      } else {
        const value = result as Report;
        const metrics = value.replay;
        if (value.authorization_sha256 !== checksum || value.evaluation !== 'resubstitution'
            || value.training_ready !== false || value.promotion_authorized !== false || !hash(value.artifact_sha256)
            || metrics?.candidate_loaded !== true || metrics.base_parameters_unchanged !== true
            || metrics.development_decisions !== value.authorization.development_decisions
            || ![metrics.base_development_nll, metrics.candidate_development_nll,
              metrics.base_development_accuracy, metrics.candidate_development_accuracy].every(Number.isFinite)) {
          throw new Error('invalid_report');
        }
        setReport(value);
      }
    } catch {
      if (requestSerial === serial.current) {setError(true); setPreview(null); setConsent(false); setRights(false);}
    } finally {
      if (requestSerial === serial.current) setBusy(false);
    }
  }

  const label = state === 'training' ? text('Training private experimental adapter', 'Özel deneysel adapter eğitiliyor')
    : state === 'evaluating' ? text('Reloading and comparing in a fresh CUDA process', 'Yeni CUDA sürecinde yüklenip karşılaştırılıyor')
    : state === 'verified' ? text('Experimental adapter verified', 'Deneysel adapter doğrulandı')
    : state === 'failed' ? text('Experiment failed; no promotion', 'Deney başarısız; yükseltme yok')
    : state === 'cancelled' ? text('Experiment cancelled', 'Deney iptal edildi') : text('Not started', 'Başlatılmadı');
  return <section data-testid="episode-adaptation">
    <h4>{text('Experimental System 1 adapter', 'Deneysel Sistem 1 adapter')}</h4>
    <p>{text('Explicit GPU training on this synthetic development cohort only. No held-out quality claim, automatic deployment or promotion.',
      'Yalnız bu sentetik geliştirme grubunda açık izinli GPU eğitimi. Bağımsız test başarısı, otomatik devreye alma veya yükseltme değildir.')}</p>
    <p data-testid="adaptation-status" data-state={state}>{label}</p>
    {running ? <p>{text('Pause stops and drains this experiment. Revoke source review after it stops.',
      'Duraklat bu deneyi durdurup kapatır. Durduktan sonra kaynak incelemesini geri çekebilirsiniz.')}</p> : null}
    <button data-testid="adaptation-preview" disabled={disabled || busy || running} onClick={() => void operate('adaptation-preview')}>
      {text('Preview training budget', 'Eğitim bütçesini önizle')}</button>
    {preview ? <div>
      <p>{preview.authorization.development_decisions} {text('development examples · rank 4 · one optimizer step · 1536 tokens',
        'geliştirme örneği · rank 4 · tek optimizer adımı · 1536 token')}</p>
      <code style={{overflowWrap: 'anywhere'}}>{preview.authorization_sha256}</code>
      <label><input data-testid="adaptation-rights" type="checkbox" checked={rights} disabled={disabled || busy}
        onChange={event => setRights(event.target.checked)}/>{text('I reviewed these inputs and targets for data rights and sensitive content.',
          'Bu girdileri ve hedefleri veri hakları ve hassas içerik açısından inceledim.')}</label>
      <label><input data-testid="adaptation-consent" type="checkbox" checked={consent} disabled={disabled || busy}
        onChange={event => setConsent(event.target.checked)}/>{text('I authorize this exact local experimental training budget, not model deployment.',
          'Modelin devreye alınmasına değil, bu exact yerel deneysel eğitim bütçesine izin veriyorum.')}</label>
      <button data-testid="adaptation-start" disabled={disabled || busy || running || !rights || !consent}
        onClick={() => void operate('adaptation-start')}>{text('Train experimental adapter', 'Deneysel adapter eğit')}</button>
    </div> : null}
    {state === 'verified' ? <button data-testid="adaptation-inspect" disabled={disabled || busy}
      onClick={() => void operate('adaptation-inspect')}>{text('Verify saved experiment', 'Kayıtlı deneyi doğrula')}</button> : null}
    {report ? <div data-testid="adaptation-report">
      <p>{text('Same-cohort comparison; not independent validation.', 'Aynı grupta karşılaştırma; bağımsız doğrulama değildir.')}</p>
      <p>{text('Base / adapter NLL: ', 'Taban / adapter NLL: ')}{report.replay.base_development_nll.toFixed(4)} / {report.replay.candidate_development_nll.toFixed(4)}</p>
      <p>{text('Base / adapter accuracy: ', 'Taban / adapter doğruluğu: ')}{(100 * report.replay.base_development_accuracy).toFixed(1)}% / {(100 * report.replay.candidate_development_accuracy).toFixed(1)}%</p>
      <p>{text('Base weights unchanged. No model promoted.', 'Taban ağırlıkları değişmedi. Model yükseltilmedi.')}</p>
      <OwnedAdapterRuntime episodeId={episodeId} authorizationSha256={report.authorization_sha256}
        snapshot={snapshot} disabled={disabled || busy || running}/>
      <OwnedAdapterEvaluation episodeId={episodeId} authorizationSha256={report.authorization_sha256}
        snapshot={snapshot} disabled={disabled || busy || running}/>
    </div> : null}
    {error ? <p role="alert">{text('Source, authorization or control changed. Inspect again; no automatic retry.',
      'Kaynak, izin veya kontrol değişti. Yeniden inceleyin; otomatik tekrar yok.')}</p> : null}
  </section>;
}
