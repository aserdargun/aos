import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {useLanguage} from './i18n';
import './owned_adapter_runtime.css';

type Arm = 'base' | 'adapter';
type Pair = {schema_version: '1.0'; pair_sha256: string; committed: boolean; started: false;
  record: {scope: 'development_only'; independent_held_out: 0; promotion_authorized: false;
    automatic_second_arm: false; cold_worker_each_arm: true}};
type Metrics = {call_count: number; action_count: number; consumed_approval_count: number;
  call_latency_sum_ms: number; approval_window_sum_ms: number};
type Report = {schema_version: '1.0'; pair_sha256: string; verified: boolean;
  arms: Record<Arm, {status: string}>; scope: 'development_only'; independent_held_out: 0;
  promotion_authorized: false; runtime_reuse_authorized: false;
  comparison: {base: Metrics; adapter: Metrics; quality_superiority_verified: false} | null};

export function OwnedAdapterEvaluation({episodeId, authorizationSha256, snapshot, disabled}: {
  episodeId: string; authorizationSha256: string; snapshot: Snapshot; disabled: boolean;
}) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const [caseKey, setCaseKey] = useState('paired-canary');
  const [value, setValue] = useState('paired development message');
  const [pair, setPair] = useState<Pair | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [consent, setConsent] = useState(false);
  const [started, setStarted] = useState<Arm[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const serial = useRef(0);
  const scope = episodeId + authorizationSha256 + snapshot.control.lease_id + ':'
    + snapshot.control.generation + ':' + caseKey + ':' + value;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1; setPair(null); setReport(null); setConsent(false);
    setStarted([]); setBusy(false); setError(false);
  }, [scope]);
  useEffect(() => () => {serial.current += 1;}, []);

  async function operate(operation: 'preview' | 'commit' | 'start' | 'report', arm?: Arm) {
    const requestSerial = ++serial.current;
    const requestScope = scopeRef.current;
    setBusy(true); setError(false);
    try {
      const authority = {lease_id: snapshot.control.lease_id, generation: snapshot.control.generation};
      const selection = {schema_version: '1.0', episode_id: episodeId};
      if (operation === 'preview' || operation === 'commit') {
        if (operation === 'commit' && (!pair || !consent)) throw new Error('consent_required');
        const checked = await api<Pair>('/api/tasks/owned-episode/pair-' + operation, {
          ...selection, ...authority, authorization_sha256: authorizationSha256,
          case_key: caseKey, development_value: value,
          ...(operation === 'commit' ? {confirm_sha256: pair?.pair_sha256,
            experimental_evaluation_authorized: true} : {})});
        if (checked.schema_version !== '1.0' || !/^[a-f0-9]{64}$/.test(checked.pair_sha256)
            || checked.started !== false || checked.committed !== (operation === 'commit')
            || checked.record?.scope !== 'development_only' || checked.record.independent_held_out !== 0
            || checked.record.promotion_authorized !== false || checked.record.automatic_second_arm !== false
            || checked.record.cold_worker_each_arm !== true
            || operation === 'commit' && checked.pair_sha256 !== pair?.pair_sha256) throw new Error('invalid_pair');
        if (serial.current === requestSerial && scopeRef.current === requestScope) {
          setPair(checked); setReport(null); setStarted([]); setConsent(false);
        }
      } else if (operation === 'start') {
        if (!pair?.committed || !consent || !arm) throw new Error('consent_required');
        const result = await api<{schema_version: string; pair_sha256: string; arm: Arm; accepted: boolean}>(
          '/api/tasks/owned-episode/pair-start', {...selection, ...authority, pair_sha256: pair.pair_sha256,
            arm, confirm_sha256: pair.pair_sha256, experimental_runtime_authorized: true});
        if (result.schema_version !== '1.0' || result.pair_sha256 !== pair.pair_sha256
            || result.arm !== arm || result.accepted !== true) throw new Error('invalid_start');
        if (serial.current === requestSerial && scopeRef.current === requestScope) {
          setStarted(previous => [...previous, arm]); setReport(null); setConsent(false);
        }
      } else {
        if (!pair?.committed) throw new Error('missing_pair');
        const checked = await api<Report>('/api/tasks/owned-episode/pair-report',
          {...selection, pair_sha256: pair.pair_sha256});
        if (checked.schema_version !== '1.0' || checked.pair_sha256 !== pair.pair_sha256
            || checked.scope !== 'development_only' || checked.independent_held_out !== 0
            || checked.promotion_authorized !== false || checked.runtime_reuse_authorized !== false
            || typeof checked.verified !== 'boolean' || typeof checked.arms?.base?.status !== 'string'
            || typeof checked.arms?.adapter?.status !== 'string') throw new Error('invalid_report');
        if (checked.verified) {
          if (!checked.comparison || checked.comparison.quality_superiority_verified !== false
              || checked.arms.base.status !== 'verified' || checked.arms.adapter.status !== 'verified') throw new Error('invalid_proof');
          for (const metrics of [checked.comparison.base, checked.comparison.adapter]) {
            if (!metrics || metrics.call_count !== 6 || metrics.action_count !== 7 || metrics.consumed_approval_count !== 6
                || !Number.isFinite(metrics.call_latency_sum_ms) || metrics.call_latency_sum_ms < 0
                || !Number.isFinite(metrics.approval_window_sum_ms) || metrics.approval_window_sum_ms < 0) throw new Error('invalid_metrics');
          }
        } else if (checked.comparison !== null) throw new Error('unverified_metrics');
        if (serial.current === requestSerial && scopeRef.current === requestScope) {setReport(checked); setConsent(false);}
      }
    } catch {
      if (serial.current === requestSerial && scopeRef.current === requestScope) {setError(true); setConsent(false);}
    } finally {
      if (serial.current === requestSerial) setBusy(false);
    }
  }

  const nextArm: Arm | null = !started.includes('base') ? 'base'
    : report?.arms.base.status === 'verified' && !started.includes('adapter') ? 'adapter' : null;
  return <section className="owned-adapter-runtime" data-testid="owned-adapter-evaluation">
    <h4>{text('Paired base–adapter browser evaluation', 'Eşli taban–adapter tarayıcı değerlendirmesi')}</h4>
    <p>{text('Freeze the same new synthetic input and skill before either run. Two separate starts, six manual approvals each, fresh model workers. No automatic second run or promotion.',
      'İki koşudan önce aynı yeni sentetik girdiyi ve skill’i sabitleyin. İki ayrı başlangıç, altışar manuel onay, yeni model süreçleri. Otomatik ikinci koşu veya yükseltme yok.')}</p>
    <label>{text('Paired development case', 'Eşli gelişim vakası')}<input data-testid="pair-case" value={caseKey}
      maxLength={64} disabled={disabled || busy || pair?.committed} onChange={event => setCaseKey(event.target.value)}/></label>
    <label>{text('New synthetic message (no secrets)', 'Yeni sentetik mesaj (gizli bilgi yok)')}<input data-testid="pair-value"
      value={value} maxLength={128} disabled={disabled || busy || pair?.committed} onChange={event => setValue(event.target.value)}/></label>
    <button data-testid="pair-preview" disabled={disabled || busy || pair?.committed
      || !/^[a-z][a-z0-9-]{0,63}$/.test(caseKey) || !/^[A-Za-z0-9 _.-]{1,128}$/.test(value) || value !== value.trim()}
      onClick={() => void operate('preview')}>{text('Preview pair', 'İkili koşuyu önizle')}</button>
    {pair ? <div>
      <p><code>{pair.pair_sha256.slice(0, 24)}…</code></p>
      {(!pair.committed || nextArm) ? <label><input data-testid="pair-consent" type="checkbox" checked={consent}
        disabled={disabled || busy} onChange={event => setConsent(event.target.checked)}/>
        {!pair.committed ? text('I authorize this exact development evaluation; this does not start either run.',
          'Bu exact gelişim değerlendirmesine izin veriyorum; bu seçim iki koşudan hiçbirini başlatmaz.')
          : nextArm === 'base' ? text('I authorize only the base arm now.', 'Şimdi yalnız taban model koluna izin veriyorum.')
          : text('I authorize only the experimental adapter arm now.', 'Şimdi yalnız deneysel adapter koluna izin veriyorum.')}</label> : null}
      {!pair.committed ? <button data-testid="pair-commit" disabled={disabled || busy || !consent}
        onClick={() => void operate('commit')}>{text('Commit pair', 'İkili koşuyu sabitle')}</button>
        : nextArm ? <button data-testid={'pair-start-' + nextArm} disabled={disabled || busy || !consent}
          onClick={() => void operate('start', nextArm)}>{nextArm === 'base'
            ? text('Start base arm', 'Taban model kolunu başlat') : text('Start adapter arm', 'Adapter kolunu başlat')}</button> : null}
      {pair.committed ? <button data-testid="pair-report" disabled={disabled || busy}
        onClick={() => void operate('report')}>{text('Audit paired runs', 'Eşli koşuları denetle')}</button> : null}
      {started.length ? <p>{text('Approve each action in Tasks, then audit. The next arm never starts automatically.',
        'Tasks içinde her eylemi onaylayıp denetleyin. Sonraki kol otomatik başlamaz.')}</p> : null}
    </div> : null}
    {report ? <div data-testid="pair-result">
      <p>{text('Base: ', 'Taban: ')}{report.arms.base.status} · {text('Adapter: ', 'Adapter: ')}{report.arms.adapter.status}</p>
      {report.verified && report.comparison ? <div data-testid="pair-verified">
        <p>{text('Both runs verified on the same development case. One pair; no held-out or quality-superiority claim.',
          'İki koşu aynı gelişim vakasında doğrulandı. Tek çift; held-out veya kalite üstünlüğü iddiası yok.')}</p>
        <p>{text('Decision-call wall time, including startup and source checks (ms), base / adapter: ',
          'Başlangıç ve kaynak denetimi dahil karar çağrısı süresi (ms), taban / adapter: ')}
          {report.comparison.base.call_latency_sum_ms.toFixed(1)} / {report.comparison.adapter.call_latency_sum_ms.toFixed(1)}</p>
        <p>{text('Approval windows, not pure human wait (ms), base / adapter: ',
          'Saf insan beklemesi olmayan onay pencereleri (ms), taban / adapter: ')}
          {report.comparison.base.approval_window_sum_ms.toFixed(1)} / {report.comparison.adapter.approval_window_sum_ms.toFixed(1)}</p>
      </div> : null}
    </div> : null}
    {error ? <p role="alert">{text('Pair, source, control or quota changed. No automatic retry; audit attempted runs.',
      'İkili koşu, kaynak, kontrol veya kota değişti. Otomatik tekrar yok; denenen koşuları denetleyin.')}</p> : null}
  </section>;
}
