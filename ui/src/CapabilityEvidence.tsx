import {useEffect, useState} from 'react';
import {ApiError, api} from './api';
import {locale, t} from './i18n';

const caseNames = ['contracts', 'ui', 'transport', 'real_tasks', 'real_mcp', 'real_takeover', 'real_reuse', 'real_learning'] as const;
type CaseName = typeof caseNames[number];
type CaseStatus = 'passed' | 'partial' | 'not_verified' | 'failed' | 'infrastructure_error' | 'not_run' | 'unavailable';
type Counts = {passed: number; skipped: number; failed: number; error: number;
  expected_failure: number; unexpected_success: number; unknown: number};
type CaseReport = {case: CaseName; status: CaseStatus; started_at: string | null;
  counts: Counts | null; seconds: number | null};
type CapabilityReport = {schema_version: '1.0'; historical_only: true; real_site_acceptance: false;
  approval_driver: 'test harness'; available: boolean; cases: CaseReport[]};
type ViewState = 'loading' | 'ready' | 'empty' | 'unavailable' | 'old_backend' | 'malformed';

const countNames = ['passed', 'skipped', 'failed', 'error', 'expected_failure', 'unexpected_success', 'unknown'] as const;
const statusNames: CaseStatus[] = ['passed', 'partial', 'not_verified', 'failed', 'infrastructure_error', 'not_run', 'unavailable'];
const scopes: Record<CaseName, string> = {
  contracts: 'Sözleşme, politika, kalıcılık ve ret testleri; gerçek model değil.',
  ui: 'Gerçek Chromium/Docker arayüzü; sentetik karar motoru ve fixture yanıtları.',
  transport: 'Gerçek headless/görünür Chromium DOM, görsel ve MCP sınırları; fixture karar motorları.',
  real_tasks: 'Gerçek Decider/Bonsai; sentetik dosya, headless/görünür form ve SAVE, pause ve model yeniden kullanımı.',
  real_mcp: 'Gerçek Decider ve görünür Ubuntu Chromium/Playwright MCP; sentetik gezinme, profil pini ve sayfa kanıtı.',
  real_takeover: 'Gerçek GPU işçisi: kontrol devri, taze onay, idle süresi ve Bonsai öncesi bırakma.',
  real_reuse: 'Gerçek Decider ile yeni izole oturumda seçilmiş sentetik skill kullanımı.',
  real_learning: 'Gerçek S1/S2 capture, review/export, tokenizer, CUDA adapter ve sentetik eşli kıyas.'
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function validTimestamp(value: unknown): value is string | null {
  if (value === null) return true;
  return typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value)
    && Number.isFinite(Date.parse(value));
}

function validCounts(value: unknown): value is Counts | null {
  return value === null || (isRecord(value) && hasKeys(value, countNames)
    && countNames.every(key => Number.isInteger(value[key]) && Number(value[key]) >= 0));
}

export function validCapabilityReport(value: unknown): value is CapabilityReport {
  if (!isRecord(value) || !hasKeys(value, ['schema_version', 'historical_only', 'real_site_acceptance',
      'approval_driver', 'available', 'cases']) || value.schema_version !== '1.0'
      || value.historical_only !== true || value.real_site_acceptance !== false
      || value.approval_driver !== 'test harness' || typeof value.available !== 'boolean'
      || !Array.isArray(value.cases) || value.cases.length !== caseNames.length) return false;
  const seen = new Set<string>();
  for (const item of value.cases) {
    if (!isRecord(item) || !hasKeys(item, ['case', 'status', 'started_at', 'counts', 'seconds'])
        || typeof item.case !== 'string' || !caseNames.includes(item.case as CaseName) || seen.has(item.case)
        || typeof item.status !== 'string' || !statusNames.includes(item.status as CaseStatus)
        || !validTimestamp(item.started_at) || !validCounts(item.counts)
        || !(item.seconds === null || (typeof item.seconds === 'number' && Number.isFinite(item.seconds) && item.seconds >= 0))) return false;
    const counts = item.counts;
    if (item.status === 'passed' && (!counts || counts.passed < 1
        || countNames.some(name => name !== 'passed' && counts[name] !== 0))) return false;
    if (item.counts !== null && item.status !== 'not_run' && item.status !== 'unavailable'
        && item.status !== 'infrastructure_error' && item.started_at === null) return false;
    if (['not_run', 'unavailable', 'infrastructure_error'].includes(item.status)
        && (item.counts !== null || item.seconds !== null)) return false;
    seen.add(item.case);
  }
  return caseNames.every(name => seen.has(name));
}

function caseScope(name: CaseName): string {
  return t(scopes[name]);
}

function statusLabel(status: CaseStatus): string {
  const labels: Record<CaseStatus, string> = {
    passed: 'Geçti', partial: 'Kısmi', not_verified: 'Doğrulanmadı', failed: 'Başarısız',
    infrastructure_error: 'Altyapı hatası', not_run: 'Tamamlanmış kanıt yok', unavailable: 'Kullanılamıyor'
  };
  return t(labels[status]);
}

export function CapabilityEvidence({refreshKey = 0}: {refreshKey?: number}) {
  const [state, setState] = useState<ViewState>('loading');
  const [report, setReport] = useState<CapabilityReport | null>(null);
  const [manualRefresh, setManualRefresh] = useState(0);

  useEffect(() => {
    const abort = new AbortController();
    setState('loading');
    api<unknown>('/api/capability-checks', undefined, abort.signal).then(value => {
      if (abort.signal.aborted) return;
      if (!validCapabilityReport(value)) {
        setReport(null); setState('malformed'); return;
      }
      setReport(value);
      setState(!value.available ? 'unavailable'
        : value.cases.every(item => item.status === 'not_run') ? 'empty' : 'ready');
    }).catch(failure => {
      if (abort.signal.aborted) return;
      setReport(null);
      setState(failure instanceof ApiError && failure.status === 404 ? 'old_backend' : 'unavailable');
    });
    return () => abort.abort();
  }, [refreshKey, manualRefresh]);

  const timeText = (value: string | null) => value ?? '—';
  const secondsText = (value: number | null) => value === null ? '—'
    : `${new Intl.NumberFormat(locale(), {maximumFractionDigits: 2}).format(value)} ${t('s')}`;

  return <section className="development-card" data-testid="capability-evidence" data-state={state}>
    <div className="development-heading"><h3>{t('Tarihsel yetenek test havuzu')}</h3>
      <button type="button" data-testid="capability-evidence-refresh" onClick={() => setManualRefresh(value => value + 1)}>
        {t('Kanıtı yenile')}
      </button>
    </div>
    <p className="caption">{t('Yalnız geçmiş test sonuçlarıdır; canlı servis sağlığı veya gerçek-site kabulü değildir. Onaylar test harness tarafından sürülür.')}</p>
    <p data-testid="capability-evidence-state">{t(state === 'loading' ? 'Test kanıtı yükleniyor…'
      : state === 'old_backend' ? 'Bu backend test kanıtı API’sini sunmuyor.'
      : state === 'malformed' ? 'Test kanıtı yanıtı geçersiz; sonuç gösterilmiyor.'
      : state === 'unavailable' ? 'Test kanıtı kaynağı kullanılamıyor; başarı varsayılmıyor.'
      : state === 'empty' ? 'Henüz kaydedilmiş yetenek testi yok.'
      : 'Test sonuçları yalnız seçilen profillerin tarihsel kanıtıdır.')}</p>
    {report?.available ? <p className="caption" data-testid="capability-evidence-driver">
      {t('Tarihsel rapor')} · {t('Onay sürücüsü: test harness')} · {t('Gerçek site kabulü: hayır')}
    </p> : null}
    {report ? <div className="development-grid capability-evidence-list">{report.cases.map(item => <article className="registry"
      key={item.case} data-testid={`capability-check-case-${item.case}`} data-status={item.status}>
      <div className="development-heading"><h4><code>{item.case}</code> · {statusLabel(item.status)}</h4>
        <span>{secondsText(item.seconds)}</span></div>
      <p>{caseScope(item.case)}</p>
      <p>{t('Başlangıç zamanı:')} <time dateTime={item.started_at ?? undefined}>{timeText(item.started_at)}</time></p>
      {item.counts ? <p data-testid={`capability-check-counts-${item.case}`}>
        {countNames.map((name, index) => <span key={name}>{index ? ' · ' : ''}{t(({passed: 'Geçti', skipped: 'Atlandı', failed: 'Başarısız', error: 'Hata', expected_failure: 'Beklenen hata', unexpected_success: 'Beklenmeyen başarı', unknown: 'Bilinmiyor'} as const)[name])}: {item.counts?.[name]}</span>)}
      </p> : <p>{t('Ayrıntılı test sayıları yok.')}</p>}
    </article>)}</div> : null}
  </section>;
}
