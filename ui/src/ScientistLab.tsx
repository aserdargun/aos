import {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api';
import {t} from './i18n';
import {ScientistCpuStudy} from './ScientistCpuStudy';

interface Approval {
  action_id: string;
  state: string;
  envelope_sha256: string;
  expires_at: number;
  rejection_reason?: string | null;
  envelope: {action: {tool: string}; task: {request: {suite: string; budget: {experiments: number; wall_seconds: number; model_tokens: number}}}};
}
interface SavedReadback {
  readback_id: string;
  report_sha256: string;
  record_sha256: string;
  created_at: string;
  status: string;
}
interface Inventory {
  configured: boolean;
  joint_runtime_admitted: boolean;
  allowed_suites?: string[];
  program_version?: string;
  request_limits?: unknown;
  supported_context_fields?: unknown;
  cpu_study_supported?: unknown;
  inference?: {configured: boolean; admission_blocked: boolean; unresolved_count: number;
    resolved_count?: number;
    unresolved_lab_effect_count?: number;
    other_session_count: number; local_cleanup_pending: boolean; truncated: boolean;
    evidence_controls?: {available: boolean; supported: boolean; pending_count: number | null;
      current_session_pending_count: number | null; other_session_pending_count: number | null; metadata_only: boolean};
    intents: {request_id: string; state: string; profile_id: string; deployment_digest: string;
      resolution_recorded?: boolean;
      request_sha256: string; owner: string; generation: number; worker_unit: string | null;
      admission_identity?: {available: boolean; present: boolean; record_sha256: string | null; binding_sha256: string | null}}[]};
  jobs: {run_id: string; lab_run_id: string | null; actions: Approval[];
    readbacks?: {supported: boolean; available: boolean; items: SavedReadback[]; truncated: boolean}}[];
}

interface ExperienceRecord {
  experiment_id: string;
  experiment_sha256: string;
  trajectory_sha256: string;
}

function emptyExperienceRecord(): ExperienceRecord {
  return {experiment_id: '', experiment_sha256: '', trajectory_sha256: ''};
}

function advisoryText(value: string, maximum: number): string | null {
  const trimmed = value.trim();
  return !/[\u0000-\u001f\u007f]/.test(value) && Array.from(trimmed).length >= 1
    && Array.from(trimmed).length <= maximum ? trimmed : null;
}

function requestedInteger(value: string, minimum: number, maximum: number): number | null {
  if (!/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null;
}

function cpuRequestLimits(value: unknown, suites: string[], program: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const limits = value as Record<string, unknown>;
  if (Object.keys(limits).sort().join(',') !== 'max_experiments,max_wall_seconds,model_tokens,profile,source,suite,track'
      || limits.profile !== 'scientist-cpu-mode-grid.v1' || limits.source !== 'reviewed_configuration'
      || limits.track !== 'mode' || limits.model_tokens !== 0 || program !== 'mode-grid.v1'
      || suites.length !== 1 || limits.suite !== suites[0]
      || typeof limits.max_experiments !== 'number' || !Number.isSafeInteger(limits.max_experiments)
      || limits.max_experiments < 1 || limits.max_experiments > 35
      || typeof limits.max_wall_seconds !== 'number' || !Number.isSafeInteger(limits.max_wall_seconds)
      || limits.max_wall_seconds < 1 || limits.max_wall_seconds > 14400) return null;
  return {maxExperiments: limits.max_experiments, maxWallSeconds: limits.max_wall_seconds};
}

export function ScientistLab() {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [suite, setSuite] = useState('');
  const [track, setTrack] = useState('');
  const [experiments, setExperiments] = useState('');
  const [wallSeconds, setWallSeconds] = useState('');
  const [modelTokens, setModelTokens] = useState('');
  const [fieldIntentEnabled, setFieldIntentEnabled] = useState(false);
  const [assetId, setAssetId] = useState('');
  const [goalKind, setGoalKind] = useState('');
  const [objective, setObjective] = useState('');
  const [experienceEnabled, setExperienceEnabled] = useState(false);
  const [sourceRunId, setSourceRunId] = useState('');
  const [sourceReportSha, setSourceReportSha] = useState('');
  const [experienceRecords, setExperienceRecords] = useState<ExperienceRecord[]>(() => [emptyExperienceRecord()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<unknown>(null);
  const [resultOperation, setResultOperation] = useState('');
  const [reportToSave, setReportToSave] = useState<{run_id: string; report_sha256: string} | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null);
  const [inventoryError, setInventoryError] = useState('');
  const mounted = useRef(true);
  const effectBusy = useRef(false);
  const panelOpen = useRef(false);
  const inventoryRequest = useRef<{abort: AbortController; promise: Promise<void>} | null>(null);
  const refresh = useCallback(() => {
    if (inventoryRequest.current) return inventoryRequest.current.promise;
    const abort = new AbortController();
    const promise = api<Inventory>('/api/scientist/jobs', undefined, abort.signal).then(value => {
      if (!mounted.current || abort.signal.aborted) return;
      setInventory(value);
      setRefreshedAt(new Date().toISOString());
      setInventoryError('');
    }).catch(exception => {
      if (mounted.current && !abort.signal.aborted) setInventoryError(String(exception));
    }).finally(() => {
      if (inventoryRequest.current?.abort === abort) inventoryRequest.current = null;
    });
    inventoryRequest.current = {abort, promise};
    return promise;
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      inventoryRequest.current?.abort.abort();
    };
  }, [refresh]);
  useEffect(() => {
    if (!expanded || busy) return;
    const timer = window.setInterval(() => {
      if (!effectBusy.current && !inventoryRequest.current) void refresh();
    }, 5000);
    return () => {
      window.clearInterval(timer);
      inventoryRequest.current?.abort.abort();
    };
  }, [expanded, busy, refresh]);
  const perform = async (operation: string, body: unknown) => {
    if (effectBusy.current) return;
    effectBusy.current = true;
    setBusy(true);
    setError('');
    setResult(null);
    setResultOperation('');
    setReportToSave(null);
    try {
      const previous = inventoryRequest.current;
      previous?.abort.abort();
      await previous?.promise;
      if (!mounted.current) return;
      const value = await api('/api/scientist/' + operation, body);
      if (mounted.current) {
        setResult(value);
        setResultOperation(operation);
        if (operation === 'report' && value && typeof value === 'object' && body && typeof body === 'object') {
          const report = value as {run_id?: unknown; report_sha256?: unknown; report?: {run_id?: unknown}};
          const request = body as {run_id?: unknown};
          if (typeof request.run_id === 'string' && typeof report.run_id === 'string'
              && typeof report.report_sha256 === 'string' && /^[a-f0-9]{64}$/.test(report.report_sha256)
              && report.report?.run_id === report.run_id) {
            setReportToSave({run_id: request.run_id, report_sha256: report.report_sha256});
          }
        }
      }
    } catch (exception) {
      if (mounted.current) setError(String(exception));
    } finally {
      if (mounted.current && panelOpen.current) await refresh();
      effectBusy.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const allowedSuites = Array.isArray(inventory?.allowed_suites)
    && inventory.allowed_suites.length > 0 && inventory.allowed_suites.length <= 20
    && inventory.allowed_suites.every(value => typeof value === 'string' && value.length <= 128
      && /^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/.test(value))
    && new Set(inventory.allowed_suites).size === inventory.allowed_suites.length
    ? inventory.allowed_suites : [];
  const selected = suite || allowedSuites[0] || '';
  const contextFields = inventory?.supported_context_fields;
  const contextSupported = Array.isArray(contextFields) && contextFields.length === 2
    && contextFields.includes('field_intent') && contextFields.includes('prior_experience');
  const hasRequestLimits = inventory !== null && Object.hasOwn(inventory, 'request_limits');
  const cpuLimits = cpuRequestLimits(inventory?.request_limits, allowedSuites, inventory?.program_version);
  const invalidRequestLimits = hasRequestLimits && cpuLimits === null;
  const maximumExperiments = cpuLimits?.maxExperiments ?? 35;
  const maximumWallSeconds = cpuLimits?.maxWallSeconds ?? 14400;
  const maximumModelTokens = cpuLimits ? 0 : 350000;
  const requestedExperiments = requestedInteger(experiments, 1, maximumExperiments);
  const requestedWallSeconds = requestedInteger(wallSeconds, 1, maximumWallSeconds);
  const requestedModelTokens = requestedInteger(modelTokens, 0, maximumModelTokens);
  const requestedAssetId = advisoryText(assetId, 128);
  const requestedObjective = advisoryText(objective, 600);
  const fieldIntent = track === 'mode' && requestedAssetId !== null && requestedObjective !== null
    && (goalKind === 'digital_twin' || goalKind === 'predictive_maintenance')
    ? {asset_id: requestedAssetId, goal_kind: goalKind, objective: requestedObjective} : null;
  const priorExperience = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(sourceRunId)
    && /^[a-f0-9]{64}$/.test(sourceReportSha) && experienceRecords.length >= 1 && experienceRecords.length <= 8
    && new Set(experienceRecords.map(record => record.experiment_id)).size === experienceRecords.length
    && experienceRecords.every(record => /^exp_[a-f0-9]{32}$/.test(record.experiment_id)
      && /^[a-f0-9]{64}$/.test(record.experiment_sha256) && /^[a-f0-9]{64}$/.test(record.trajectory_sha256))
    ? {source_run_id: sourceRunId, source_report_sha256: sourceReportSha, records: experienceRecords} : null;
  const changeTrack = (value: string) => {
    setTrack(value);
    setFieldIntentEnabled(false);
    setAssetId('');
    setGoalKind('');
    setObjective('');
    setExperienceEnabled(false);
    setSourceRunId('');
    setSourceReportSha('');
    setExperienceRecords([emptyExperienceRecord()]);
  };
  const updateExperienceRecord = (index: number, field: keyof ExperienceRecord, value: string) => {
    setExperienceRecords(records => records.map((record, position) => position === index ? {...record, [field]: value} : record));
  };
  const proposal = inventory?.configured === true && !inventoryError && allowedSuites.includes(selected)
    && (contextSupported || (!fieldIntentEnabled && !experienceEnabled))
    && !invalidRequestLimits && (!cpuLimits || track === 'mode')
    && typeof inventory.program_version === 'string' && inventory.program_version.length > 0
    && inventory.program_version.length <= 64 && (track === 'anomaly' || track === 'mode')
    && requestedExperiments !== null && requestedWallSeconds !== null && requestedModelTokens !== null
    && (!fieldIntentEnabled || fieldIntent !== null) && (!experienceEnabled || priorExperience !== null)
    ? {suite: selected, track, program_version: inventory.program_version,
      budget: {experiments: requestedExperiments, wall_seconds: requestedWallSeconds, model_tokens: requestedModelTokens},
      ...(fieldIntentEnabled && fieldIntent ? {field_intent: fieldIntent} : {}),
      ...(experienceEnabled && priorExperience ? {prior_experience: priorExperience} : {})}
    : null;
  return <details data-testid="scientist-lab" style={{overflowWrap: 'anywhere'}} onToggle={event => {
    if (event.target !== event.currentTarget) return;
    panelOpen.current = event.currentTarget.open;
    setExpanded(event.currentTarget.open);
    if (!event.currentTarget.open) inventoryRequest.current?.abort.abort();
    else if (!effectBusy.current) void refresh();
  }}>
    <summary>{t('Scientist deneyleri')}</summary>
    <p>{t('GPU kabulü henüz doğrulanmadı. Durdurma yanıtı GPU bırakımı değildir; belirsiz işlemi tekrar başlatmayın.')}</p>
    <p>{t('Envanter açık panelde 5 saniyede bir salt okunur yenilenir.')}</p>
    <p data-testid="scientist-inventory-refresh">{refreshedAt
      ? <>{t('Son envanter yenilemesi')}: <time dateTime={refreshedAt}>{refreshedAt}</time></>
      : t('Envanter henüz yenilenmedi.')}</p>
    {inventoryError ? <p role="alert">{t('Envanter yenilenemedi; gösterilen bilgiler güncel olmayabilir.')} {inventoryError}</p> : null}
    {inventory?.inference ? <section data-testid="scientist-inference">
      <h3>{t('GPU çıkarım devri')}</h3>
      <p>{inventory.inference.configured ? t('Broker görev bağlantısı yapılandırıldı.') : t('Broker görev bağlantısı yapılandırılmadı.')}</p>
      <p>{t('Çözümlenmemiş çıkarım sayısı')}: {inventory.inference.unresolved_count}
        {' · '}{t('Önceki oturumlar')}: {inventory.inference.other_session_count}</p>
      {Number.isSafeInteger(inventory.inference.resolved_count)
        && (inventory.inference.resolved_count ?? -1) >= 0
        ? <p>{t('Geçmiş çözüm kaydı sayısı')}: {inventory.inference.resolved_count}</p> : null}
      <p>{t('Belirsiz Lab işlemleri')}: {inventory.inference.unresolved_lab_effect_count || 0}</p>
      {inventory.inference.evidence_controls ? <section data-testid="scientist-control-inventory">
        <p>{t('Kontrol günlüğü yalnız bilgi amaçlıdır; yanıt kaydı GPU bırakımı veya görev çözümü değildir.')}</p>
        {!inventory.inference.evidence_controls.available ? <p role="alert">{t('Kontrol günlüğü okunamıyor; sıfır bekleyen işlem varsayılmaz.')}</p>
          : !inventory.inference.evidence_controls.supported ? <p>{t('Bu veritabanında kontrol günlüğü yok; otomatik yükseltme yapılmaz.')}</p>
            : Number.isSafeInteger(inventory.inference.evidence_controls.pending_count)
              && (inventory.inference.evidence_controls.pending_count ?? -1) >= 0
              ? <p>{t('Yanıtı kaydedilmemiş kontrol istekleri')}: {inventory.inference.evidence_controls.pending_count}</p>
              : <p role="alert">{t('Kontrol günlüğü okunamıyor; sıfır bekleyen işlem varsayılmaz.')}</p>}
      </section> : null}
      {inventory.inference.admission_blocked ? <p role="alert">{t('Yeni görev kabulü kapalı. Güvenilir GPU bırakımı doğrulaması gerekir; intent sıfırlanamaz.')}</p> : null}
      {inventory.inference.local_cleanup_pending ? <p>{t('Yerel socket kapanışı bekleniyor; bu GPU bırakımı değildir.')}</p> : null}
      <p>{t('Kabul kimliği geçmişi yalnız bilgi amaçlıdır; yeni yetki veya GPU doğrulaması değildir.')}</p>
      {inventory.inference.intents.map(intent => <details key={intent.request_id}>
        <summary>{intent.profile_id} · {intent.state}</summary>
        {intent.resolution_recorded === true
          ? <p>{t('Bu istek için çözüm kaydı var; geçmiş kayıt yeni GPU yetkisi değildir.')}</p> : null}
        <p>{intent.request_id} · {intent.owner} · {intent.generation}</p>
        <p>{t('İstek hash değeri')}: <code>{intent.request_sha256}</code></p>
        <p>{t('Deployment hash değeri')}: <code>{intent.deployment_digest}</code></p>
        {intent.admission_identity?.available === true && intent.admission_identity.present === false
          && intent.admission_identity.record_sha256 === null && intent.admission_identity.binding_sha256 === null
          ? <p>{t('Bu intent için kayıtlı kabul kimliği yok; otomatik benimseme yapılmaz.')}</p>
          : intent.admission_identity?.available === true && intent.admission_identity.present === true
            && typeof intent.admission_identity.record_sha256 === 'string'
            && typeof intent.admission_identity.binding_sha256 === 'string'
            && /^[a-f0-9]{64}$/.test(intent.admission_identity.record_sha256 ?? '')
            && /^[a-f0-9]{64}$/.test(intent.admission_identity.binding_sha256 ?? '')
            ? <><p>{t('Kabul kimliği kayıt hash değeri')}: <code>{intent.admission_identity.record_sha256}</code></p>
              <p>{t('Kabul kimliği bağ hash değeri')}: <code>{intent.admission_identity.binding_sha256}</code></p></>
            : <p role="alert">{t('Kabul kimliği geçmişi kullanılamıyor; güvenli kabul varsayılmaz.')}</p>}
        {intent.worker_unit ? <p>{intent.worker_unit}</p> : null}
      </details>)}
      {inventory.inference.truncated ? <p>{t('Son 20 çıkarım gösteriliyor.')}</p> : null}
    </section> : null}
    {!inventory?.configured ? <p>{t('Scientist bağlantısı yapılandırılmadı; gerçek deney başlatma kapalı.')}</p> : <>
      {invalidRequestLimits ? <p role="alert">{t('Deney kapsamı bilgisi geçersiz veya desteklenmiyor; yeni öneri oluşturma kapalı.')}</p> : null}
      {cpuLimits ? <section data-testid="scientist-cpu-limits">
        <h3>{t('Yalnız CPU · sentetik mode-grid')}</h3>
        <p>{t('İncelenmiş yapılandırma sınırları; canlı yetki veya GPU kabulü değildir. Her işlemde sunucu tekrar doğrular.')}</p>
        <p>{t('En fazla deney')}: {cpuLimits.maxExperiments} · {t('En fazla saniye')}: {cpuLimits.maxWallSeconds} · {t('Model token')}: 0</p>
      </section> : null}
      {cpuLimits && inventory.cpu_study_supported === true ? <ScientistCpuStudy
        key={JSON.stringify(inventory.request_limits)} suite={selected} disabled={busy || !!inventoryError}/> : null}
      <form data-testid="scientist-proposal-form" onSubmit={event => {
        event.preventDefault();
        if (proposal && !busy) void perform('propose', proposal);
      }}>
        <fieldset disabled={busy} style={{minWidth: 0, display: 'grid', gap: 12}}>
          <legend>{t('İstenen deney kapsamı')}</legend>
          <label htmlFor="scientist-request-suite">{t('İzinli deney paketi')}</label>
          <select id="scientist-request-suite" value={selected} onChange={event => setSuite(event.target.value)}>
            {allowedSuites.map(value => <option key={value} value={value}>{value}</option>)}
          </select>
          <label htmlFor="scientist-request-track">{t('İstenen deney dalı')}</label>
          <select id="scientist-request-track" value={track} required onChange={event => changeTrack(event.target.value)}>
            <option value="">{t('Deney dalını seçin')}</option>
            {!cpuLimits && !invalidRequestLimits ? <option value="anomaly">anomaly</option> : null}
            <option value="mode">mode</option>
          </select>
          <p>{t('Sunucunun bildirdiği program sürümü')}: <code>{typeof inventory.program_version === 'string'
            && inventory.program_version.length > 0 && inventory.program_version.length <= 64
            ? inventory.program_version : t('Kullanılamıyor')}</code></p>
          {!hasRequestLimits ? <p>{t('Sunucu izinli paketleri bildirir; dal iznini ve etkin bütçe sınırlarını bildirmez. İstenen kapsam sunucuda ayrıca denetlenir.')}</p> : null}
          <label style={{display: 'grid'}}>{t('İstenen deney sayısı')} <input type="number" min="1" max={maximumExperiments} step="1" required
            value={experiments} onChange={event => setExperiments(event.target.value)}/></label>
          <label style={{display: 'grid'}}>{t('İstenen süre (saniye)')} <input type="number" min="1" max={maximumWallSeconds} step="1" required
            value={wallSeconds} onChange={event => setWallSeconds(event.target.value)}/></label>
          <label style={{display: 'grid'}}>{t('İstenen model token sınırı')} <input type="number" min="0" max={maximumModelTokens} step="1" required
            value={modelTokens} onChange={event => setModelTokens(event.target.value)}/></label>
          {!hasRequestLimits ? <p>{t('Protokol aralığı: 1–35 deney, 1–14400 saniye, 0–350000 model token. Bunlar etkin sunucu bütçesi değildir.')}</p> : null}
          <p>{t('Bütçeyi açıkça girin. Öneri çalıştırma izni vermez; exact kapsam için ayrı insan onayı gerekir.')}</p>
          {!contextSupported ? <p>{t('Bu backend isteğe bağlı amaç/geçmiş aktarımı desteği bildirmiyor; bu alanlar gönderilmez.')}</p> : null}
          {contextSupported && track === 'mode' ? <section data-testid="scientist-field-intent">
            <label><input type="checkbox" checked={fieldIntentEnabled}
              onChange={event => setFieldIntentEnabled(event.target.checked)}/>{' '}{t('İsteğe bağlı saha amacını ekle')}</label>
            <p>{t('Saha amacı kullanıcı beyanıdır; veri, algoritma veya puanlama yetkisi vermez.')}</p>
            {fieldIntentEnabled ? <div style={{display: 'grid', gap: 12}}>
              <label style={{display: 'grid'}}>{t('Varlık referansı')}<input value={assetId} maxLength={128}
                onChange={event => setAssetId(event.target.value)}/></label>
              <div style={{display: 'grid'}}><label htmlFor="scientist-field-goal-kind">{t('Saha amacı türü')}</label>
                <select id="scientist-field-goal-kind" value={goalKind}
                onChange={event => setGoalKind(event.target.value)}>
                <option value="">{t('Saha amacı türünü seçin')}</option>
                <option value="digital_twin">{t('Dijital ikiz')}</option>
                <option value="predictive_maintenance">{t('Öngörülü bakım')}</option>
              </select></div>
              <div style={{display: 'grid'}}><label htmlFor="scientist-field-objective">{t('Saha hedefi')}</label>
                <textarea id="scientist-field-objective" value={objective} maxLength={600} rows={3}
                  onChange={event => setObjective(event.target.value)}/></div>
              {!fieldIntent ? <p role="alert">{t('Varlık ve hedefi boş olmayan düz metinle girin; kontrol karakterleri kabul edilmez.')}</p> : null}
            </div> : null}
          </section> : null}
          {contextSupported ? <details data-testid="scientist-prior-experience">
            <summary>{t('İsteğe bağlı önceki deneyim referansları · gelişmiş')}</summary>
            <label><input type="checkbox" checked={experienceEnabled}
              onChange={event => setExperienceEnabled(event.target.checked)}/>{' '}{t('Açıkça seçilmiş deneyim referanslarını ekle')}</label>
            <p>{t('Referansları elle seçin; otomatik geçmiş araması yoktur. Sunucu sahipliği, terminal raporu ve uygun deney kayıtlarını doğrular.')}</p>
            <p>{t('Geçmiş bağlam yalnız danışma amaçlıdır; grid algoritmasını uyarlamaz veya öğrenilmiş iyileşme kanıtlamaz.')}</p>
            {experienceEnabled ? <div style={{display: 'grid', gap: 12}}>
              <label style={{display: 'grid'}}>{t('Kaynak koşu kimliği (UUID)')}<input value={sourceRunId} maxLength={36}
                onChange={event => setSourceRunId(event.target.value)}/></label>
              <label style={{display: 'grid'}}>{t('Kaynak rapor SHA-256')}<input value={sourceReportSha} maxLength={64}
                onChange={event => setSourceReportSha(event.target.value)}/></label>
              {experienceRecords.map((record, index) => <fieldset key={index} style={{minWidth: 0, display: 'grid', gap: 8}}>
                <legend>{t('Seçilmiş deney kaydı')} {index + 1}</legend>
                <label style={{display: 'grid'}}>{t('Deney kimliği')}<input value={record.experiment_id} maxLength={36}
                  onChange={event => updateExperienceRecord(index, 'experiment_id', event.target.value)}/></label>
                <label style={{display: 'grid'}}>{t('Deney SHA-256')}<input value={record.experiment_sha256} maxLength={64}
                  onChange={event => updateExperienceRecord(index, 'experiment_sha256', event.target.value)}/></label>
                <label style={{display: 'grid'}}>{t('Trajectory SHA-256')}<input value={record.trajectory_sha256} maxLength={64}
                  onChange={event => updateExperienceRecord(index, 'trajectory_sha256', event.target.value)}/></label>
                <button type="button" disabled={experienceRecords.length <= 1}
                  onClick={() => setExperienceRecords(records => records.filter((_record, position) => position !== index))}>
                  {t('Bu referansı kaldır')}</button>
              </fieldset>)}
              <button type="button" disabled={experienceRecords.length >= 8}
                onClick={() => setExperienceRecords(records => [...records, emptyExperienceRecord()])}>{t('Deney referansı ekle')}</button>
              {!priorExperience ? <p role="alert">{t('Küçük harfli UUID ve SHA-256 ile 1–8 benzersiz deney referansı gerekir.')}</p> : null}
            </div> : null}
          </details> : null}
          <p>{t('Deney dalı değişirse isteğe bağlı bağlam temizlenir; göndermek için yeniden açıkça seçin.')}</p>
          {proposal ? <section data-testid="scientist-proposal-preview">
            <h4>{t('İstenen önerinin önizlemesi')}</h4>
            <pre style={{whiteSpace: 'pre-wrap'}}>{JSON.stringify(proposal, null, 2)}</pre>
          </section> : <p>{t('İzinli paket, deney dalı ve aralık içindeki tam sayı bütçesi gerekir.')}</p>}
          <button type="submit" disabled={!proposal}>{t('Deney önerisi oluştur')}</button>
        </fieldset>
      </form>
      {inventory.jobs.map(job => <div key={job.run_id}>
        <strong>{job.run_id}</strong> <span>{job.lab_run_id || t('Uzak koşu henüz bağlanmadı')}</span>
        {job.actions.map(approval => <div key={approval.action_id}>
          <p>{approval.envelope.action.tool} · {approval.state} · {approval.envelope.task.request.suite}</p>
          {approval.rejection_reason ? <p>{t(approval.rejection_reason === 'stale_controller'
            ? 'Eski onay kontrol devri nedeniyle iptal edildi.' : approval.rejection_reason === 'approval_expired'
              ? 'Süresi dolan onay iptal edildi; yeni onay gerekir.' : 'İşlem insan tarafından reddedildi.')}</p> : null}
          <code style={{display: 'block'}}>{approval.envelope_sha256}</code>
          <details><summary>{t('Exact işlem kapsamı')}</summary><pre style={{whiteSpace: 'pre-wrap'}}>{JSON.stringify(approval.envelope, null, 2)}</pre></details>
          {approval.state === 'pending' ? <>
            <button disabled={busy || approval.expires_at <= Date.now() / 1000} onClick={() => void perform('approve', {
              action_id: approval.action_id, envelope_sha256: approval.envelope_sha256, accept: true})}>{t('Bu exact işlemi onayla')}</button>
            <button disabled={busy || approval.expires_at <= Date.now() / 1000} onClick={() => void perform('approve', {
              action_id: approval.action_id, envelope_sha256: approval.envelope_sha256, accept: false})}>{t('Reddet')}</button>
          </> : null}
          {approval.state === 'approved' ? <button disabled={busy || approval.expires_at <= Date.now() / 1000}
            onClick={() => void perform('execute', {action_id: approval.action_id})}>{t('Onaylı işlemi uygula')}</button> : null}
          {approval.state === 'intent' ? <p role="alert">{t('Sonuç belirsiz; otomatik yeniden deneme kapalı.')}</p> : null}
        </div>)}
        {job.lab_run_id ? <>
          <button disabled={busy} onClick={() => void perform('status', {run_id: job.run_id})}>{t('Durumu oku')}</button>
          <button disabled={busy} onClick={() => void perform('report', {run_id: job.run_id})}>{t('Raporu bağımsız doğrula')}</button>
          <button disabled={busy} onClick={() => void perform('stop', {run_id: job.run_id})}>{t('Durdurma onayı iste')}</button>
        </> : null}
        {job.readbacks ? <section data-testid="scientist-saved-reports">
          <h4>{t('Kaydedilmiş doğrulanmış raporlar')}</h4>
          <p>{t('Rapor içeriği yalnız ayrı kayıt isteğiyle saklanır. Geçmiş kayıt güncel durum, yeni yetki veya GPU bırakımı kanıtı değildir.')}</p>
          {!job.readbacks.supported ? <p>{t('Bu veritabanında rapor geçmişi desteklenmiyor; otomatik benimseme yapılmaz.')}</p>
            : !job.readbacks.available ? <p role="alert">{t('Rapor geçmişi okunamıyor; boş veya güvenilir olduğu varsayılmaz.')}</p>
              : <>
                {!job.readbacks.items.length ? <p>{t('Henüz ayrı kayıt isteğiyle saklanmış rapor yok.')}</p> : null}
                {job.readbacks.items.map(saved => <div key={saved.readback_id}>
                  <p>{t('Rapor durumu')}: {saved.status} · <time dateTime={saved.created_at}>{saved.created_at}</time></p>
                  <p>{t('Rapor hash değeri')}: <code>{saved.report_sha256}</code></p>
                  <p>{t('Geçmiş kayıt hash değeri')}: <code>{saved.record_sha256}</code></p>
                  <button disabled={busy} onClick={() => void perform('read_saved_report', {
                    run_id: job.run_id, readback_id: saved.readback_id})}>{t('Kaydedilmiş raporu oku')}</button>
                </div>)}
                {job.readbacks.truncated ? <p>{t('Son 20 kaydedilmiş rapor gösteriliyor.')}</p> : null}
              </>}
        </section> : null}
      </div>)}
    </>}
    <button disabled={busy} onClick={() => void refresh()}>{t('Yenile')}</button>
    {error ? <p role="alert">{error}</p> : null}
    {reportToSave ? <section data-testid="scientist-report-retention">
      <p>{t('Bu doğrulanmış raporu yerel özel geçmişe kaydetmek ayrı bir istektir; normal rapor okuması içeriği saklamaz.')}</p>
      <code>{reportToSave.report_sha256}</code>
      <button disabled={busy} onClick={() => void perform('save_report', {
        run_id: reportToSave.run_id, expected_report_sha256: reportToSave.report_sha256})}>{t('Bu exact raporu özel geçmişe kaydet')}</button>
    </section> : null}
    {resultOperation === 'read_saved_report' ? <p data-testid="scientist-historical-report">
      {t('Kaydedilmiş tarihsel rapor; uzaktan yeni rapor alınmadı. Güncel durum veya GPU bırakımı değildir.')}</p> : null}
    {resultOperation === 'save_report' ? <p>{t('Doğrulanmış rapor özel geçmişe kaydedildi; görev başlatılmadı ve GPU yetkisi verilmedi.')}</p> : null}
    {result ? <pre style={{whiteSpace: 'pre-wrap'}}>{JSON.stringify(result, null, 2)}</pre> : null}
  </details>;
}
