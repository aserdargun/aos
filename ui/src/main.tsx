import {useEffect, useRef, useState, type FormEvent} from 'react';
import {createRoot} from 'react-dom/client';
import {api, ApiError, supportsImageDrafts, supportsStaticAssetQueries, type Control, type ImageDraftCapability, type StaticQueryCapability, type Overview, type RemoteFormRepeatReport, type Resources, type RetentionStatus, type Snapshot, type Trace, type Tasks as TaskStatus, type WebApplicationReport} from './api';
import {Computer, type ComputerHandle} from './Computer';
import {Tasks} from './Tasks';
import {Recovery} from './Recovery';
import {Plan} from './Plan';
import {CompoundPlan} from './CompoundPlan';
import {SessionBinding} from './SessionBinding';
import {FirstUse} from './FirstUse';
import {WorkflowNotice} from './WorkflowNotice';
import {Development} from './Development';
import {hasCurrentSessionStatus, hasCurrentTaskStatus, hasOverviewTelemetry, hasResourceTelemetry} from './controlCenterStatus';
import {ScientistLab} from './ScientistLab';
import {SystemTopology} from './SystemTopology';
import {WebApplicationDrafts} from './WebApplicationDrafts';
import {Knowledge} from './Knowledge';
import {locale, setLanguage, t, useLanguage} from './i18n';
import './style.css';
import './language.css';

const controls: [Control, string][] = [['pause', 'Duraklat'], ['stop', 'Durdur'], ['take-control', 'Kontrolü al'], ['return-control', 'Ajana geri ver'], ['resume', 'Devam et'], ['restart', 'Yeniden başlat']];
const tabs = ['Geliştirme', 'Sistem ve topoloji', 'Bilgisayar', 'Görevler', 'Scientist', 'Plan', 'Çalışmalar', 'Web uygulamaları', 'Bilgi', 'Kurtarma', 'Modeller', 'Kaynaklar'] as const;
type Tab = typeof tabs[number];
const defaultTab: Tab = 'Geliştirme';

function App() {
  const language = useLanguage();
  const [authenticated, setAuthenticated] = useState(false);
  const [ready, setReady] = useState(false);
  const [localLogin, setLocalLogin] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [resources, setResources] = useState<Resources | null>(null);
  const [retention, setRetention] = useState<RetentionStatus | null | 'unavailable'>(null);
  const [formRepeats, setFormRepeats] = useState<RemoteFormRepeatReport | null | 'not-configured' | 'unavailable'>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [tasks, setTasks] = useState<TaskStatus | null>(null);
  const [webApplications, setWebApplications] = useState<WebApplicationReport[] | null | 'unavailable'>(null);
  const [imageDraftCapability, setImageDraftCapability] = useState<ImageDraftCapability>('checking');
  const [staticQueryCapability, setStaticQueryCapability] = useState<StaticQueryCapability>('checking');
  const [tab, setTab] = useState<Tab>(defaultTab);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [runtimeObservedAt, setRuntimeObservedAt] = useState<number | null>(null);
  const [runtimeError, setRuntimeError] = useState(false);
  const [runtimeRefreshing, setRuntimeRefreshing] = useState(false);
  const computer = useRef<ComputerHandle>(null);
  const actionLock = useRef(false);
  const epoch = useRef(0);
  const visibleTasks = tab === 'Görevler' && (tasks?.browser_display === 'desktop' || tasks?.vision_display === 'desktop');

  useEffect(() => {
    if (tab !== 'Scientist') return;
    const panel = document.querySelector<HTMLDetailsElement>('[data-testid="scientist-lab"]');
    if (panel) panel.open = true;
  }, [tab]);

  function reset() {
    epoch.current += 1;
    setTab(defaultTab);
    setImageDraftCapability('checking');
    setStaticQueryCapability('checking');
    setRuntimeObservedAt(null); setRuntimeError(false); setRuntimeRefreshing(false);
    setAuthenticated(false); setSnapshot(null); setOverview(null); setResources(null); setRetention(null); setFormRepeats(null); setTrace(null); setTasks(null); setWebApplications(null);
  }
  function failed(failure: unknown) {
    if (failure instanceof ApiError && failure.status === 401) reset();
    setError(failure instanceof Error ? failure.message : t("İşlem tamamlanamadı."));
  }
  useEffect(() => {
    const abort = new AbortController();
    api<{authenticated: boolean; local_auto_login?: boolean}>('/api/session', undefined, abort.signal)
      .then(async result => {
        if (abort.signal.aborted) return;
        setLocalLogin(result.local_auto_login === true);
        if (!result.authenticated && result.local_auto_login === true) {
          const entered = await api<{authenticated: boolean}>('/api/login/local', {}, abort.signal);
          if (entered.authenticated !== true) throw new Error(t('Yerel oturum açılamadı. Tekrar deneyin.'));
          if (abort.signal.aborted) return;
          setAuthenticated(true);
        } else setAuthenticated(result.authenticated);
        setReady(true);
      })
      .catch(failure => { if (!abort.signal.aborted) { failed(failure); setReady(true); } });
    return () => abort.abort();
  }, []);
  useEffect(() => {
    if (!authenticated || busy) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let nextInspection = 0;
    async function refresh() {
      let delay = 3000;
      setRuntimeRefreshing(true);
      try {
        let taskStatus: TaskStatus;
        if (performance.now() >= nextInspection) {
          const [essential, telemetry] = await Promise.all([
            Promise.all([api<unknown>('/api/state', undefined, abort.signal),
              api<unknown>('/api/tasks', undefined, abort.signal)]),
            Promise.allSettled([api<Overview>('/api/overview', undefined, abort.signal),
              api<Resources>('/api/resources', undefined, abort.signal)])
          ]);
          const [next, currentTasks] = essential;
          if (!hasCurrentSessionStatus(next) || !hasCurrentTaskStatus(currentTasks)) {
            throw new Error(t('Oturum durumu doğrulanamadı; güncel kabul edilmedi.'));
          }
          const [records, limits] = telemetry;
          const unauthorized = telemetry.find(result => result.status === 'rejected'
            && result.reason instanceof ApiError && result.reason.status === 401);
          if (unauthorized?.status === 'rejected') throw unauthorized.reason;
          taskStatus = currentTasks;
          nextInspection = performance.now() + 3000;
          if (!abort.signal.aborted) {
            setSnapshot(next);
            setOverview(records.status === 'fulfilled' && hasOverviewTelemetry(records.value) ? records.value : null);
            setResources(limits.status === 'fulfilled' && hasResourceTelemetry(limits.value) ? limits.value : null);
          }
        } else {
          const currentTasks = await api<unknown>('/api/tasks', undefined, abort.signal);
          if (!hasCurrentTaskStatus(currentTasks)) throw new Error(t('Oturum durumu doğrulanamadı; güncel kabul edilmedi.'));
          taskStatus = currentTasks;
        }
        if (!abort.signal.aborted) { setTasks(taskStatus); setRuntimeObservedAt(Date.now()); setRuntimeError(false); }
        if (taskStatus.busy || taskStatus.decider_preparation?.state === 'preparing') delay = 500;
      } catch (failure) { if (!abort.signal.aborted) { setRuntimeError(true); failed(failure); } }
      finally { if (!abort.signal.aborted) setRuntimeRefreshing(false); }
      if (!abort.signal.aborted) timer = setTimeout(refresh, delay);
    }
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [authenticated, busy, reload]);
  useEffect(() => {
    if (!authenticated || tab !== 'Geliştirme') return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refreshRetention() {
      try {
        const status = await api<RetentionStatus>('/api/retention', undefined, abort.signal);
        if (!abort.signal.aborted) setRetention(status);
      } catch (failure) {
        if (abort.signal.aborted) return;
        if (!(failure instanceof ApiError && failure.status === 404)) failed(failure);
        setRetention('unavailable');
        return;
      }
      if (!abort.signal.aborted) timer = setTimeout(refreshRetention, 30000);
    }
    void refreshRetention();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [authenticated, tab, reload]);
  useEffect(() => {
    if (!authenticated || tab !== 'Geliştirme') return;
    if (tasks === null) return;
    if (!tasks.remote_form) {
      setFormRepeats('not-configured');
      return;
    }
    const abort = new AbortController();
    setFormRepeats(null);
    api<RemoteFormRepeatReport>('/api/remote-form/repeats', undefined, abort.signal)
      .then(report => {
        if (abort.signal.aborted) return;
        const stageTools = report.mode === 'read_only_post_admission_form_state_repeats'
          ? ['browser.form.open', 'browser.form.state_before', 'browser.form.fill',
             'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after']
          : ['browser.form.open', 'browser.form.fill', 'browser.form.submit', 'browser.form.receipt'];
        const stages = report.post_approval_stage_ms;
        const expectedStages = report.transport_verified === 0 ? [] : stageTools;
        const validStages = report.schema_version === '1.1'
          || (report.schema_version === '1.2' && stages !== null && typeof stages === 'object'
            && !Array.isArray(stages)
            && Object.keys(stages).sort().join('|') === [...expectedStages].sort().join('|')
            && expectedStages.every(tool => {
              const sample = stages[tool];
              return sample !== null && typeof sample === 'object'
                && Number.isInteger(sample.samples) && sample.samples === report.transport_verified
                && typeof sample.p50_ms === 'number' && Number.isFinite(sample.p50_ms)
                && typeof sample.p95_ms === 'number' && Number.isFinite(sample.p95_ms)
                && sample.p50_ms >= 0 && sample.p95_ms >= sample.p50_ms;
            }));
        const valid = ['1.1', '1.2'].includes(report.schema_version)
          && validStages
          && Number.isInteger(report.approval_window_count)
          && report.approval_window_count >= 0
          && [report.approval_window_sum_ms, report.elapsed_excluding_approval_p50_ms,
              report.elapsed_excluding_approval_p95_ms].every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0);
        setFormRepeats(valid ? report : 'unavailable');
      })
      .catch(failure => {
        if (abort.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 404) setFormRepeats('not-configured');
        else if (failure instanceof ApiError && [409, 429, 503].includes(failure.status)) setFormRepeats('unavailable');
        else failed(failure);
      });
    return () => abort.abort();
  }, [authenticated, tab, reload, tasks === null, tasks?.remote_form?.plan_sha256, tasks?.jobs?.[0]?.job_id, tasks?.jobs?.[0]?.status]);
  useEffect(() => {
    if (!authenticated || (tab !== 'Web uygulamaları' && tab !== 'Geliştirme')) return;
    const abort = new AbortController();
    api<WebApplicationReport[]>('/api/web-applications', undefined, abort.signal)
      .then(result => { if (!abort.signal.aborted) setWebApplications(result); })
      .catch(failure => {
        if (abort.signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 404) setWebApplications('unavailable');
        else failed(failure);
      });
    return () => abort.abort();
  }, [authenticated, tab, reload]);
  useEffect(() => {
    if (!authenticated) return;
    const abort = new AbortController();
    setImageDraftCapability('checking');
    setStaticQueryCapability('checking');
    api<unknown>('/api/web-applications/capabilities', undefined, abort.signal)
      .then(result => { if (!abort.signal.aborted) {
        setImageDraftCapability(supportsImageDrafts(result) ? 'supported' : 'unconfirmed');
        setStaticQueryCapability(supportsStaticAssetQueries(result) ? 'supported' : 'unconfirmed');
      }})
      .catch(() => { if (!abort.signal.aborted) {
        setImageDraftCapability('unconfirmed'); setStaticQueryCapability('unconfirmed');
      }});
    return () => abort.abort();
  }, [authenticated, reload]);

  async function action(operation: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(true); setError('');
    try { await operation(); } catch (failure) { failed(failure); }
    finally { actionLock.current = false; setBusy(false); }
  }
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const token = new FormData(form).get('token');
    await action(async () => {
      await api('/api/login', {token}); form.reset(); setTab(defaultTab); setAuthenticated(true);
    });
  }
  async function openLocalSession() {
    await action(async () => {
      const result = await api<{authenticated: boolean}>('/api/login/local', {});
      if (result.authenticated !== true) throw new Error(t('Yerel oturum açılamadı. Tekrar deneyin.'));
      setTab(defaultTab); setAuthenticated(true);
    });
  }
  async function control(command: Control) {
    await action(async () => {
      await computer.current?.disconnect();
      await api('/api/control', {command});
      setSnapshot(await api<Snapshot>('/api/state'));
    });
  }
  async function showTrace(runId: string) {
    const requestedEpoch = ++epoch.current;
    setTrace(null);
    try {
      const result = await api<Trace>(`/api/runs/${encodeURIComponent(runId)}`);
      if (epoch.current === requestedEpoch) setTrace(result);
    } catch (failure) { if (epoch.current === requestedEpoch) failed(failure); }
  }

  return <div className={'app' + (authenticated && tab === 'Geliştirme' ? ' control-center-app' : '')}>
    <header className="topbar"><div className="brand">AOS<span>LOCAL COMPUTER</span></div><span className="environment">{t("Yerel · ağsız çalışma alanı")}</span><div className="language-switch" role="group" aria-label={t('Dil')}><button type="button" lang="en" aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>English</button><button type="button" lang="tr" aria-pressed={language === 'tr'} onClick={() => setLanguage('tr')}>Türkçe</button></div><span className={`owner ${snapshot?.control.owner.toLowerCase() ?? ''}`} data-testid="owner">{authenticated ? snapshot?.control.owner ?? t("Bağlanıyor") : t("Oturum kapalı")}</span></header>
    {!authenticated ? <main className="login"><span className="eyebrow">{t("KONTROL MERKEZİ / 01")}</span><h1>{t("Bilgisayarınız.")}<br/>{t("Sınırları belli.")}</h1><p>{t("İzole masaüstünü görüntüleyin, kontrolü devralın ve doğrulanmış çalışma kayıtlarını inceleyin.")}</p>{!ready ? <p role="status">{t('Yerel oturuma bağlanıyor…')}</p> : localLogin ? <button className="primary" disabled={busy} onClick={() => void openLocalSession()}>{t('Yerel oturumu aç')}</button> : <form onSubmit={login}><label htmlFor="token">{t("Yerel oturum anahtarı")}</label><input id="token" name="token" type="password" autoComplete="off" required/><p className="caption">{t("Backend terminalinde belirtilen 0600 izinli dosyadan alınır. Tarayıcı depolamasına kaydedilmez.")}</p><button className="primary" disabled={busy}>{t("Giriş yap")}</button></form>}</main> : <>
      <div className="controlbar" aria-label={t("Kalıcı kontrol çubuğu")}>{controls.map(([command, label]) => <button key={command} className={command === 'stop' ? 'danger' : ''} disabled={busy || !snapshot || (snapshot.control.status === 'stopped' && command !== 'restart')} onClick={() => void control(command)}>{t(label)}</button>)}<button className="logout" disabled={busy} onClick={() => void action(async () => { await computer.current?.disconnect(); await api('/api/logout', {}); reset(); })}>{t("Çıkış")}</button></div>
      <div className="workspace"><aside className="sidebar"><p className="eyebrow">{t("ÇALIŞMA ALANI")}</p><nav aria-label={t("Paneller")}>{tabs.map(name => <button key={name} aria-current={tab === name ? 'page' : undefined} onClick={() => setTab(name)}>{t(name)}</button>)}</nav><div className="scope"><span className="dot"/>{t("Sınırlı kabul ortamı")}<p>{t("Yalnız sabit görevler onayla çalışır. Genel komut veya otomatik eğitim yoktur.")}</p></div></aside><main className="content">
        <div className={'page-heading' + (tab === 'Geliştirme' ? ' control-center-page-heading' : '')}><div><p className="eyebrow">AOS / {t(tab).toLocaleUpperCase(locale())}</p><h1>{tab === 'Bilgisayar' ? t("İzole çalışma alanı") : t(tab)}</h1></div><button disabled={busy} onClick={() => { setReload(value => value + 1); setError(''); }}>{t("Yenile")}</button></div>
        {!snapshot ? <p role="status">{t("Runtime bilgisi alınıyor…")}</p> : null}
        <WorkflowNotice snapshot={snapshot} tasks={tasks} showApproval={() => { setTab('Görevler'); window.scrollTo(0, 0); }}/>
        {tab === 'Bilgisayar' ? <FirstUse tasks={tasks} navigate={setTab}/> : null}
        <div className={visibleTasks ? 'desktop-task-layout' : undefined}>
          {snapshot && (tab === 'Bilgisayar' || visibleTasks) ? <Computer key={reload} snapshot={snapshot} ref={computer}/> : null}
          {tab === 'Görevler' ? <Tasks tasks={tasks} snapshot={snapshot} busy={busy} action={action} showTrace={runId => { setTab('Çalışmalar'); void showTrace(runId); }}/> : null}
        </div>
        {tab === 'Kurtarma' ? <><Recovery onError={failed}/>{snapshot ? <SessionBinding key={`${snapshot.runtime.runtime_id}:${snapshot.control.generation}`} onError={failed}/> : null}</> : null}
        {tab === 'Plan' ? <><Plan onError={failed}/><CompoundPlan key={`${snapshot?.runtime.runtime_id}:${snapshot?.control.generation}`} onError={failed} snapshot={snapshot} tasks={tasks} busy={busy} action={action}/></> : null}
        {tab === 'Geliştirme' ? <Development tasks={tasks} overview={overview} retention={retention} formRepeats={formRepeats} webApplications={webApplications} imageDraftCapability={imageDraftCapability} staticQueryCapability={staticQueryCapability} onNavigate={setTab} refreshKey={reload}
          snapshot={snapshot} runtimeObservedAt={runtimeObservedAt} runtimeError={runtimeError} refreshing={busy || runtimeRefreshing}
          onRefresh={() => { setReload(value => value + 1); setError(''); }}/>: null}
        {tab === 'Scientist' ? <ScientistLab/> : null}
        {tab === 'Sistem ve topoloji' ? <SystemTopology onNavigate={setTab}/> : null}
        {tab === 'Web uygulamaları' ? <WebApplicationDrafts inventory={webApplications} managerScope={tasks?.manager_scope} imageDraftCapability={imageDraftCapability} staticQueryCapability={staticQueryCapability} onRegistered={() => setReload(value => value + 1)} onError={failed}/> : null}
        {tab === 'Bilgi' ? <Knowledge tasks={tasks} snapshot={snapshot} busy={busy} refreshKey={reload}/> : null}
        {tab === 'Modeller' && overview?.trajectory.available && !overview.trajectory.models?.length && !overview.trajectory.deployments?.length ? <p>{t("Henüz model/deployment kaydı yok.")}</p> : null}
        {snapshot && tab === 'Bilgisayar' ? <section className="panel"><div className="panel-heading"><h2>{t("Kontrol izi")}</h2><button disabled={busy || snapshot.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onClick={() => void action(async () => {
          const queued = await api<{input_id: string}>('/api/input', {lease_id: snapshot.control.lease_id, generation: snapshot.control.generation});
          await api(`/api/input/${queued.input_id}/execute`, {});
        })}>{t("Sentetik giriş testi")}</button></div><p className="caption">{t("Bu düğme model çağırmaz; sabit X11 girişini bağımsız okuma ile doğrular.")}</p><div className="event-list">{overview?.inputs.map(input => <div key={input.input_id}><code>{input.tool}</code><span>{input.status}</span><small>generation {input.generation}</small></div>)}{overview?.events.map(event => <div key={event.event_id}><span>{event.kind}</span><time>{new Date(event.created_at).toLocaleString(locale())}</time></div>)}{overview && !overview.inputs.length && !overview.events.length ? <p>{t("Bu oturumda henüz kontrol olayı yok.")}</p> : null}</div></section> : null}
        {tab === 'Çalışmalar' ? <section className="panel"><h2>{t("Kalıcı çalışma kayıtları")}</h2><p className="caption">{t("En yeni 30 kayıt · mevcut SQLite veritabanı · çalıştırma veya yeniden oynatma yok")}</p>{overview?.trajectory.available ? <div className="table-wrap"><table><thead><tr><th>{t("Çalışma")}</th><th>{t("Durum")}</th><th>{t("Model çağrısı")}</th><th>Verification</th></tr></thead><tbody>{overview.trajectory.runs?.map(run => <tr key={run.run_id}><td><button className="text-button" onClick={() => void showTrace(run.run_id)}>{run.run_id}</button></td><td>{run.status}</td><td>{run.model_calls}</td><td>{run.passed} {t("geçti")} / {run.failed} {t("hata")}</td></tr>)}</tbody></table>{!overview.trajectory.runs?.length ? <p>{t("Henüz kayıt yok.")}</p> : null}</div> : <p>{t("Trajectory veritabanı mevcut değil veya okunamıyor. Sonuç üretilmedi.")}</p>}{trace ? <div className="trace" data-testid="trace"><h3>{trace.run.run_id}</h3><p>{t("Durum:")} {trace.run.status}</p><h4>{t("Kararlar")}</h4>{trace.decisions.map(item => <p key={item.decision_id}>{item.selected_option} · {(item.confidence * 100).toFixed(2)}% · {item.policy_result}</p>)}<h4>{t("Gerçek eylemler")}</h4>{trace.actions.map(item => <p key={item.action_id}>{item.tool} · {item.status} {item.error_code}</p>)}<h4>{t("Bağımsız doğrulama")}</h4>{trace.verifications.map(item => <p key={item.verification_id}>{item.method} · {item.result}</p>)}<h4>{t("Model çağrıları")}</h4>{trace.model_calls.map(item => <p key={item.call_id}>{item.role} · {item.status} · {item.latency_ms === null ? t("Ölçülmedi") : `${item.latency_ms.toFixed(0)} ms`}</p>)}</div> : null}</section> : null}
        {tab === 'Modeller' ? <section className="panel" data-testid="model-registry"><h2>{t("Model ve deployment kayıtları")}</h2><p className="caption">{t("Registry, genel kullanım ve promotion durumunu gösterir; çalışan servis, yüklenmiş model veya GPU sağlığı değildir. EXPERIMENTAL pilot, açık oturum ayarıyla genel kullanıma etkinleştirilmemiş bir kaydı kullanabilir. Otomatik promotion yapılmaz.")}</p><div data-testid="model-session-configuration"><h3>{t("Bu oturumun görev yapılandırması")}</h3>{tasks ? <><p>{t("Görev motoru:")} {!tasks.available ? t("Bu oturumda görev motoru kapalı") : tasks.real_model ? t("Yerel Decider ile gerçek çıkarım seçili") : t("Sentetik test motoru seçili")}</p><p>{t("Görsel gözlemci:")} {!tasks.kinds?.includes('vision_canvas') ? t("Bu oturumda görsel görev açık değil") : tasks.real_supervisor ? t("Yerel Bonsai ile gerçek çıkarım seçili") : t("Sentetik gözlemci seçili")}</p></> : <p>{t("Oturum yapılandırması bekleniyor…")}</p>}<p className="caption">{t("Bunlar yapılandırma seçimleridir; modelin şu anda bellekte yüklü veya servisinin sağlıklı olduğunu kanıtlamaz.")}</p></div>{overview?.trajectory.available ? <>{overview.trajectory.models?.map(model => <article className="registry" key={model.model_id}><h3>{model.model_id}</h3><p>{model.backend} · {model.enabled ? t("Registry: etkin") : t("Registry: genel kullanım için etkin değil")}</p><code>{model.revision ?? t("Revision kaydı yok")}</code></article>)}{overview.trajectory.deployments?.map(deployment => <article className="registry" key={deployment.deployment_id}><span className="badge">{deployment.status}</span><p><code>{deployment.deployment_id}</code></p></article>)}</> : <p>{t("Registry verisi okunamıyor.")}</p>}</section> : null}
        {tab === 'Kaynaklar' ? <section className="panel"><h2>{t("Runtime sınırları")}</h2><p className="caption">{t("Docker inspect ölçümü · bunlar kullanım/peak değerleri değil yapılandırılmış limitlerdir.")}</p>{resources?.available ? <div className="metrics"><article><span>{t("RAM sınırı")}</span><strong>{((resources.memory_limit_bytes ?? 0) / 1024 ** 3).toFixed(1)} GiB</strong></article><article><span>{t("CPU sınırı")}</span><strong>{resources.cpu_limit}</strong></article><article><span>{t("PID / thread sınırı")}</span><strong>{resources.pids_limit}</strong></article><article><span>{t("Ağ")}</span><strong>{resources.network_mode}</strong></article></div> : <p>{t("Masaüstü durduruldu veya kaynak bilgisi alınamadı.")}</p>}<p>Root filesystem: {resources?.available ? resources.readonly_rootfs ? t("salt okunur") : t("yazılabilir") : t("bilinmiyor")}</p><p>{t("GPU kullanımı: bu dilimde ölçülmüyor.")}</p><dl><dt>Runtime</dt><dd><code>{snapshot?.runtime.runtime_id ?? '—'}</code></dd><dt>Image</dt><dd><code>{snapshot?.runtime.image_id ?? '—'}</code></dd></dl></section> : null}
        <footer>{t("Son örnek:")} {overview?.sampled_at ? new Date(overview.sampled_at).toLocaleString(locale()) : t('Henüz alınmadı')} · {t("Eğitim ve otomatik promotion kapalı")}</footer>
      </main></div></>}
    {error ? <div className="error" role="alert">{t(error)}</div> : null}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
