import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {useLanguage} from './i18n';
import './owned_adapter_runtime.css';

type Preview = {adapter_admission_sha256: string; preview: {schema_version: '1.5'; preview_sha256: string};
  admission: {adapter_deployment_id: string; automatic_fallback: false; promotion_authorized: false}};
type Execution = {schema_version: '1.5'; candidate_execution_sha256: string; adapter_deployment_id: string};

export function OwnedAdapterRuntime({episodeId, authorizationSha256, snapshot, disabled}: {
  episodeId: string; authorizationSha256: string; snapshot: Snapshot; disabled: boolean;
}) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const [caseKey, setCaseKey] = useState('adapter-canary');
  const [value, setValue] = useState('adapter development message');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [consent, setConsent] = useState(false);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [verified, setVerified] = useState(false);
  const [sourceAvailable, setSourceAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const serial = useRef(0);
  const scope = episodeId + authorizationSha256 + snapshot.control.lease_id + ':' + snapshot.control.generation
    + ':' + caseKey + ':' + value;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1; setPreview(null); setConsent(false); setError(false); setBusy(false);
    setExecution(null); setVerified(false); setSourceAvailable(false);
  }, [scope]);
  useEffect(() => () => {serial.current += 1;}, []);

  async function operate(operation: 'runtime-preview' | 'runtime-start' | 'audit') {
    const requestSerial = ++serial.current;
    const requestScope = scopeRef.current;
    setBusy(true); setError(false);
    try {
      if (operation === 'audit') {
        if (!execution) throw new Error('missing_execution');
        const report = await api<{available: boolean; schema_version: string; adapter_admission_verified: boolean;
          verification_scope: string; current_source_status: string; runtime_reuse_authorized: boolean}>(
          '/api/tasks/owned-episode/runtime-audit', {
            schema_version: '1.0', episode_id: episodeId, authorization_sha256: authorizationSha256,
            candidate_execution_sha256: execution.candidate_execution_sha256});
        if (!report.available || report.schema_version !== '1.5' || !report.adapter_admission_verified
            || report.verification_scope !== 'historical_execution' || report.runtime_reuse_authorized !== false
            || !['available', 'unavailable'].includes(report.current_source_status)) throw new Error('unverified');
        if (serial.current === requestSerial && scopeRef.current === requestScope) {
          setVerified(true); setSourceAvailable(report.current_source_status === 'available');
        }
      } else {
        if (operation === 'runtime-start' && (!preview || !consent)) throw new Error('missing_consent');
        const result = await api<Preview | Execution>('/api/tasks/owned-episode/' + operation, {
          schema_version: '1.0', episode_id: episodeId,
            authorization_sha256: authorizationSha256, case_key: caseKey, development_value: value,
            lease_id: snapshot.control.lease_id, generation: snapshot.control.generation,
            ...(operation === 'runtime-start' ? {confirm_sha256: preview?.preview.preview_sha256,
              experimental_runtime_authorized: true} : {})});
        if (serial.current !== requestSerial || scopeRef.current !== requestScope) return;
        if (operation === 'runtime-preview') {
          const checked = result as Preview;
          if (checked.preview?.schema_version !== '1.5' || !/^[a-f0-9]{64}$/.test(checked.preview.preview_sha256)
              || !/^[a-f0-9]{64}$/.test(checked.adapter_admission_sha256)
              || checked.admission?.automatic_fallback !== false || checked.admission.promotion_authorized !== false) throw new Error('invalid_preview');
          setPreview(checked); setConsent(false);
        } else {
          const checked = result as Execution;
          if (checked.schema_version !== '1.5' || !/^[a-f0-9]{64}$/.test(checked.candidate_execution_sha256)
              || checked.adapter_deployment_id !== preview?.admission.adapter_deployment_id) throw new Error('invalid_start');
          setExecution(checked); setPreview(null); setConsent(false); setVerified(false);
        }
      }
    } catch {
      if (serial.current === requestSerial && scopeRef.current === requestScope) {setError(true); setPreview(null); setConsent(false);}
    } finally {
      if (serial.current === requestSerial) setBusy(false);
    }
  }

  return <section className="owned-adapter-runtime" data-testid="owned-adapter-runtime">
    <h4>{text('Experimental adapter task', 'Deneysel adapter görevi')}</h4>
    <p>{text('A separate one-task runtime permission. Six manual action approvals; no automatic base fallback or promotion.',
      'Tek görev için ayrı çalıştırma izni. Altı manuel eylem onayı; otomatik taban modele geçiş veya yükseltme yok.')}</p>
    <label>{text('Development case', 'Gelişim vakası')}<input data-testid="adapter-runtime-case" value={caseKey}
      maxLength={64} disabled={disabled || busy} onChange={event => setCaseKey(event.target.value)}/></label>
    <label>{text('Synthetic message (no secrets)', 'Sentetik mesaj (gizli bilgi yok)')}<input data-testid="adapter-runtime-value"
      value={value} maxLength={128} disabled={disabled || busy} onChange={event => setValue(event.target.value)}/></label>
    <button data-testid="adapter-runtime-preview" disabled={disabled || busy || !/^[a-z][a-z0-9-]{0,63}$/.test(caseKey)
      || !/^[A-Za-z0-9 _.-]{1,128}$/.test(value) || value !== value.trim()}
      onClick={() => void operate('runtime-preview')}>{text('Preview adapter task', 'Adapter görevini önizle')}</button>
    {preview ? <div>
      <p>{text('Task-specific adapter: ', 'Göreve özel adapter: ')}<code>{preview.admission.adapter_deployment_id.slice(0, 28)}…</code></p>
      <label><input data-testid="adapter-runtime-consent" type="checkbox" checked={consent} disabled={disabled || busy}
        onChange={event => setConsent(event.target.checked)}/>{text('I authorize this exact experimental adapter for this task only.',
          'Bu exact deneysel adapter kullanımına yalnız bu görev için izin veriyorum.')}</label>
      <button data-testid="adapter-runtime-start" disabled={disabled || busy || !consent}
        onClick={() => void operate('runtime-start')}>{text('Start adapter task', 'Adapter görevini başlat')}</button>
    </div> : null}
    {execution ? <div><p>{text('Adapter task started. Approve each action in Tasks.', 'Adapter görevi başlatıldı. Tasks içinde her eylemi onaylayın.')}</p>
      <button data-testid="adapter-runtime-audit" disabled={disabled || busy} onClick={() => void operate('audit')}>
        {text('Audit adapter task', 'Adapter görevini denetle')}</button></div> : null}
    {verified ? <div><p data-testid="adapter-runtime-verified">{text('Historical use verified: adapter identity and six inference proofs.',
      'Geçmiş kullanım doğrulandı: adapter kimliği ve altı çıkarım kanıtı.')}</p>
      <p>{sourceAvailable ? text('Current source is available; a new task still requires separate permission.',
        'Güncel kaynak kullanılabilir; yeni görev yine ayrı izin gerektirir.')
        : text('Current source is unavailable. Historical success does not authorize reuse.',
          'Güncel kaynak kullanılamıyor. Geçmiş başarı yeniden kullanım izni değildir.')}</p></div> : null}
    {error ? <p role="alert">{text('Source, control or admission changed. No automatic retry.',
      'Kaynak, kontrol veya izin değişti. Otomatik tekrar yok.')}</p> : null}
  </section>;
}
