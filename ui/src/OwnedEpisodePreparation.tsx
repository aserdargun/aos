import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {useLanguage} from './i18n';
import {OwnedEpisodeAdaptation, type AdaptationStatus} from './OwnedEpisodeAdaptation';

type PreparationStatus = {state: 'idle' | 'pending' | 'verified' | 'failed' | 'cancelled';
  episode_id?: string; conversion_sha256?: string; training_ready: false};
type Counts = {system1: number; system2: number};
type Readiness = {schema_version: '1.0'; available: true; episode_id: string;
  export_sha256: string; conversion_sha256: string; persisted: boolean; counts: Counts;
  split: 'development_only'; system1: {format: 'decider-example-q-v1'; converter_verified: true;
    tokenizer: 'verified' | 'missing'; tokenizer_start_available: boolean};
  system2: {format: 'aos-owned-plan-messages-v1'; converter_verified: true;
    tokenizer: 'unverified'; trainer: 'unverified'};
  tokenizer_report_sha256: string | null; blockers: string[]; training_ready: false; promotion_authorized: false};
type Props = {episodeId: string; exportSha256: string; status?: PreparationStatus; adaptation?: AdaptationStatus;
  snapshot: Snapshot; disabled: boolean};
const isHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

function isReadiness(value: unknown, episodeId: string, exportSha256: string): value is Readiness {
  if (value === null || typeof value !== 'object') return false;
  const report = value as Record<string, unknown>;
  const system1 = report.system1 as Record<string, unknown> | undefined;
  const system2 = report.system2 as Record<string, unknown> | undefined;
  const counts = report.counts as Record<string, unknown> | undefined;
  return report.schema_version === '1.0' && report.available === true && report.episode_id === episodeId
    && report.export_sha256 === exportSha256 && isHash(report.conversion_sha256)
    && typeof report.persisted === 'boolean' && report.split === 'development_only'
    && counts !== undefined && Number.isInteger(counts.system1) && Number.isInteger(counts.system2)
    && system1?.format === 'decider-example-q-v1' && system1.converter_verified === true
    && ['verified', 'missing'].includes(String(system1.tokenizer))
    && typeof system1.tokenizer_start_available === 'boolean'
    && system2?.format === 'aos-owned-plan-messages-v1' && system2.converter_verified === true
    && system2.tokenizer === 'unverified' && system2.trainer === 'unverified'
    && (report.tokenizer_report_sha256 === null || isHash(report.tokenizer_report_sha256))
    && Array.isArray(report.blockers) && report.blockers.every(item => typeof item === 'string')
    && report.training_ready === false && report.promotion_authorized === false;
}

export function OwnedEpisodePreparation({episodeId, exportSha256, status, adaptation, snapshot, disabled}: Props) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const tokenizerLabel = (value: 'verified' | 'missing' | 'unverified') => value === 'verified'
    ? text('Verified', 'Doğrulandı') : value === 'missing'
      ? text('Missing', 'Eksik') : text('Unverified', 'Doğrulanmadı');
  const blockerLabel = (code: string) => ({
    synthetic_development_only: text('Synthetic development data only; not independent validation.',
      'Yalnız sentetik geliştirme verisi; bağımsız doğrulama değildir.'),
    independent_validation_test_missing: text('Independent validation and test examples are missing.',
      'Bağımsız doğrulama ve test örnekleri eksik.'),
    rights_redaction_review_missing: text('Preparation alone does not certify data rights or redaction.',
      'Hazırlık tek başına veri haklarını veya hassas veri ayıklamasını doğrulamaz.'),
    training_authorization_missing: text('Preparation alone does not authorize training.',
      'Hazırlık tek başına eğitim izni vermez.'),
    system1_forward_loss_missing: text('This preparation report does not verify System 1 training or evaluation.',
      'Bu hazırlık raporu Sistem 1 eğitimini veya değerlendirmesini doğrulamaz.'),
    system2_tokenizer_trainer_unverified: text('System 2 tokenizer and trainer compatibility are unverified.',
      'Sistem 2 tokenizer ve trainer uyumluluğu doğrulanmadı.'),
    promotion_unavailable: text('Model promotion is unavailable.',
      'Model yükseltme kullanılamıyor.'),
    system1_tokenizer_missing: text('System 1 tokenizer check has not been run.',
      'Sistem 1 tokenizer denetimi çalıştırılmadı.'),
    system1_tokenizer_prerequisites_missing: text('System 1 tokenizer prerequisites are unavailable.',
      'Sistem 1 tokenizer önkoşulları kullanılamıyor.'),
  } as Record<string, string>)[code] ?? text('An additional preparation requirement remains.',
    'Ek bir hazırlık gereksinimi karşılanmadı.');
  const [report, setReport] = useState<Readiness | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [taskState, setTaskState] = useState(status?.state ?? 'idle');
  const serial = useRef(0);
  const scope = episodeId + ':' + exportSha256 + ':' + snapshot.runtime.runtime_id + ':'
    + snapshot.control.lease_id + ':' + snapshot.control.generation;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1;
    setReport(null); setConfirmed(false); setBusy(false); setError(false); setTaskState('idle');
  }, [scope]);
  useEffect(() => {
    if (status?.episode_id === episodeId) setTaskState(status.state);
  }, [episodeId, status?.episode_id, status?.state, status?.conversion_sha256]);
  useEffect(() => () => {serial.current += 1;}, []);

  async function freshControl() {
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id
        || current.control.lease_id !== snapshot.control.lease_id
        || current.control.generation !== snapshot.control.generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') {
      throw new Error('episode_preparation_control_changed');
    }
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }

  async function operate(operation: 'conversion-preview' | 'convert' | 'tokenizer-start' | 'readiness') {
    const current = ++serial.current;
    setBusy(true); setError(false);
    try {
      const mutating = operation === 'convert' || operation === 'tokenizer-start';
      const control = mutating ? await freshControl() : {};
      const statusConversionSha = status?.episode_id === episodeId ? status.conversion_sha256 : undefined;
      const conversionSha = report?.conversion_sha256 ?? statusConversionSha;
      if (operation !== 'conversion-preview' && !isHash(conversionSha)) throw new Error('missing_conversion_pin');
      if (operation === 'convert' && (!report || report.persisted || !confirmed
          || report.conversion_sha256 !== conversionSha)) throw new Error('conversion_confirmation_required');
      if (operation === 'tokenizer-start' && (!report?.persisted
          || report.conversion_sha256 !== conversionSha || !report.system1.tokenizer_start_available)) {
        throw new Error('tokenizer_unavailable');
      }
      const body: Record<string, unknown> = {schema_version: '1.0', episode_id: episodeId, ...control};
      if (operation === 'conversion-preview' || operation === 'convert') body.export_sha256 = exportSha256;
      if (operation === 'tokenizer-start' || operation === 'readiness') body.conversion_sha256 = conversionSha;
      if (operation === 'convert' || operation === 'tokenizer-start') body.confirm_sha256 = conversionSha;
      const result = await api<unknown>('/api/tasks/owned-episode/' + operation, body);
      if (current !== serial.current || scopeRef.current !== scope) return;
      if (operation === 'tokenizer-start') {
        if (result === null || typeof result !== 'object') throw new Error('invalid_tokenizer_response');
        const response = result as Record<string, unknown>;
        if (response.state !== 'pending' || response.episode_id !== episodeId
            || response.conversion_sha256 !== conversionSha || response.training_ready !== false) {
          throw new Error('invalid_tokenizer_response');
        }
        setTaskState('pending');
        return;
      }
      if (!isReadiness(result, episodeId, exportSha256)) throw new Error('invalid_preparation_response');
      if (operation === 'conversion-preview' && result.persisted !== false) throw new Error('preview_must_not_persist');
      if (operation !== 'conversion-preview' && result.persisted !== true) throw new Error('persisted_conversion_required');
      if (operation !== 'conversion-preview' && result.conversion_sha256 !== conversionSha) {
        throw new Error('conversion_pin_changed');
      }
      setReport(result);
      setConfirmed(false);
    } catch {
      if (current === serial.current) {setError(true); setReport(null); setConfirmed(false);}
    } finally {
      if (current === serial.current) setBusy(false);
    }
  }

  const state = taskState;
  const statusLabel = state === 'pending' ? text('Tokenizer check pending', 'Token denetimi bekliyor')
    : state === 'verified' ? text('Tokenizer checked', 'Token denetlendi')
    : state === 'failed' ? text('Tokenizer check failed', 'Token denetimi başarısız')
    : state === 'cancelled' ? text('Tokenizer check cancelled', 'Token denetimi iptal edildi')
    : text('Not started', 'Başlatılmadı');
  const locked = disabled || busy;

  return <section className="registry" data-testid="episode-preparation">
    <h4>{text('Development conversion and tokenizer preparation', 'Geliştirme dönüşümü ve tokenizer hazırlığı')}</h4>
    <p className="caption">{text('Conversion is an explicit local development artifact. A separate isolated CPU tokenizer probe does not train a model or authorize promotion.',
      'Dönüşüm açıkça oluşturulan yerel geliştirme çıktısıdır. Ayrı yalıtılmış CPU tokenizer denetimi model eğitmez veya yükseltmeye izin vermez.')}</p>
    <p role="status" data-testid="preparation-status" data-state={state}>{statusLabel}</p>
    <button data-testid="preparation-preview" disabled={locked} onClick={() => void operate('conversion-preview')}>
      {text('Preview conversion', 'Dönüşümü önizle')}</button>
    {!report && status?.episode_id === episodeId && state === 'verified' && isHash(status.conversion_sha256) ? <button
      data-testid="preparation-readiness" disabled={locked} onClick={() => void operate('readiness')}>
      {text('Refresh readiness', 'Hazırlık durumunu yenile')}</button> : null}
    {report && !report.persisted ? <div>
      <p>{text('System 1: ', 'Sistem 1: ')}{report.system1.format} · {text('verified converter', 'doğrulanmış dönüştürücü')}</p>
      <p>{text('System 2: ', 'Sistem 2: ')}{report.system2.format} · {text('trainer/tokenizer unverified', 'trainer/tokenizer doğrulanmadı')}</p>
      <p className="caption"><code style={{overflowWrap: 'anywhere', wordBreak: 'break-all'}}>{report.conversion_sha256}</code></p>
      <label><input data-testid="preparation-confirm" type="checkbox" checked={confirmed} disabled={locked}
        onChange={event => setConfirmed(event.target.checked)}/>
        {text('Confirm this exact local conversion.', 'Bu exact yerel dönüşümü onayla.')}</label>
      <button data-testid="preparation-publish" disabled={locked || !confirmed} onClick={() => void operate('convert')}>
        {text('Save conversion locally', 'Dönüşümü yerel kaydet')}</button>
    </div> : null}
    {report?.persisted ? <div>
      <p>{text('System 1 converter: ', 'Sistem 1 dönüştürücü: ')}{tokenizerLabel('verified')}; {text('tokenizer: ', 'tokenizer: ')}{tokenizerLabel(report.system1.tokenizer)}</p>
      <p>{text('System 2 tokenizer and trainer: ', 'Sistem 2 tokenizer ve trainer: ')}
        {tokenizerLabel(report.system2.tokenizer)}</p>
      <button data-testid="preparation-tokenizer" disabled={locked || state === 'pending'
        || !report.system1.tokenizer_start_available}
        onClick={() => void operate('tokenizer-start')}>{text('Run isolated CPU tokenizer check', 'Yalıtılmış CPU tokenizer denetimini çalıştır')}</button>
      <button data-testid="preparation-readiness" disabled={locked} onClick={() => void operate('readiness')}>
        {text('Refresh readiness', 'Hazırlık durumunu yenile')}</button>
      {report.blockers.length ? <ul>{report.blockers.map(blocker => <li key={blocker} data-code={blocker}>
        {blockerLabel(blocker)}</li>)}</ul> : null}
      <p>{text('Training ready: no · Promotion authorized: no', 'Eğitime hazır: hayır · Yükseltme yetkisi: hayır')}</p>
      {report.system1.tokenizer === 'verified' && adaptation ? <OwnedEpisodeAdaptation
        episodeId={episodeId} conversionSha256={report.conversion_sha256} status={adaptation}
        snapshot={snapshot} disabled={locked}/> : null}
    </div> : null}
    {error ? <p role="alert">{text('Source or control changed. Inspect and preview again; no automatic retry occurred.',
      'Kaynak veya kontrol değişti. Yeniden inceleyip önizleyin; otomatik tekrar yapılmadı.')}</p> : null}
  </section>;
}
