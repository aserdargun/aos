import {t, locale} from './i18n';
import {api, isOwnedFormRecipeSteps, isVerifiedOwnedFormInvocationAudit, type OwnedFormInvocation, type OwnedFormInvocationAuditReport, type RemoteLearningConsentReport, type Snapshot, type Tasks as TaskStatus} from './api';
import {useEffect, useRef, useState} from 'react';
import {GoalPreview} from './GoalPreview';
import {Sequences} from './Sequences';
import {TaskProgress} from './TaskProgress';
import {RemoteNavigationGraph} from './RemoteNavigationGraph';
import {RemoteJsonKnowledge} from './RemoteJsonKnowledge';
import {RemoteSkillSource} from './RemoteSkillSource';
import {OwnedSkillCandidate} from './OwnedSkillCandidate';
import {OwnedCandidateExecution} from './OwnedCandidateExecution';
import {OwnedSkillPlanning} from './OwnedSkillPlanning';
import {OwnedWebGoalCatalog} from './OwnedWebGoalCatalog';
import {ParameterWebGoal} from './ParameterWebGoal';
import {OwnedParameterProject, ownedParameterProjectCanStart, ownedParameterProjectAcceptedIntent} from './OwnedParameterProject';
import {OwnedParameterSkill} from './OwnedParameterSkill';
import {OwnedParameterSkillReview} from './OwnedParameterSkillReview';
import {OwnedParameterSkillRelease} from './OwnedParameterSkillRelease';
import {OwnedParameterSkillReuse} from './OwnedParameterSkillReuse';
import {FailureImprovement} from './FailureImprovement';
import {TaskKnowledge} from './TaskKnowledge';
import {ScientistLab} from './ScientistLab';

const catalog: Record<string, {label: string; scope: string; button: string}> = {
  hello: {label: 'Hello dosyası', scope: '/workspace/hello.txt oluşturulur; exact içerik ayrı okunur. Farklı içerik üzerine yazılmaz.', button: 'Hello görevi başlat'},
  browser_form: {label: 'Yerel browser formu', scope: 'Sabit Message alanına Hello from the local agent. yazılır; Save locally bir kez uygulanır. Fill ve submit ayrı onay ister.', button: 'Browser görevi başlat'},
  browser_local_navigation: {label: 'Yerel iki sayfa gezintisi', scope: 'Yalnız ağsız sentetik Start ve Details sayfaları açılır. Her iki geçiş ayrı elle onay ve bağımsız doğrulama ister.', button: 'Gezinme görevini başlat'},
  browser_staging_workflow: {label: 'Yerel dört adımlı uygulama akışı', scope: 'Ağsız sentetik uygulamada App → Draft → sabit Message → makbuz akışı; dört ayrı onay ve bağımsız doğrulama gerekir. Gerçek site değildir.', button: 'Uygulama akışını başlat'},
  browser_remote_entry: {label: 'Uzak HTTPS giriş denemesi', scope: 'Yalnız kayıtlı exact HTTPS giriş URL’si bir kez okunur. Giriş yapma, form, alt kaynak, veri toplama ve site görevi kapalıdır; ayrı onay gerekir.', button: 'Giriş denemesini başlat'},
  browser_remote_routes: {label: 'Sıralı HTTPS okuma', scope: 'Yalnız kayıtlı exact HTTPS rotaları sıralı okunur. Her GET ayrı onay ve içeriksiz readback ister; veri toplama varsayılan kapalıdır, yalnız exact yerel izinle metadata açılabilir.', button: 'Rota okumasını başlat'},
  browser_remote_static_assets: {label: 'HTTPS statik JS/CSS/görsel paketi', scope: 'Yalnız kayıtlı HTTPS girişi ve exact JS/CSS/görsel varlıkları tek elle onayla yüklenir. Diğer ağ istekleri engellenir; uygulama sonucu doğrulanmaz.', button: 'Statik paket görevini başlat'},
  browser_remote_form: {label: 'HTTPS formu', scope: 'Yalnız kayıtlı exact HTTPS formu için giriş, doldurma, tek POST ve makbuz ayrı elle onaylanır. Hesap ve uygulama sonucu doğrulanmaz.', button: 'HTTPS form görevini başlat'},
  vision_canvas: {label: 'Görsel SAVE görevi', scope: 'Görsel gözlemci sahneyi çıkarır; karar motoru symbolic SAVE hedefini seçer. Tek onaylı click ve bağımsız outcome doğrulaması.', button: 'Vision görevi başlat'}
};

interface Props {
  tasks: TaskStatus | null;
  snapshot: Snapshot | null;
  busy: boolean;
  action: (operation: () => Promise<void>) => Promise<void>;
  showTrace: (runId: string) => void;
}

function visibleProgress(tasks: TaskStatus | null, kind: string): string {
  const latest = tasks?.jobs[0];
  const tool = tasks?.approval?.action.tool;
  if (tasks?.paused) return t('Görev duraklatıldı; ilerlemek için Devam et gerekir.');
  if (kind === 'browser_form' && tool === 'browser.fill') return t('1 / 2 · Alanı doldurma onayınız bekleniyor. Henüz yazılmadı.');
  if (kind === 'browser_form' && tool === 'browser.submit') return t('2 / 2 · Alan dolduruldu. Kaydetme onayınız bekleniyor.');
  if (kind === 'browser_local_navigation' && tool === 'browser.fixture.open') return t('1 / 2 · Start sayfasını açma onayınız bekleniyor.');
  if (kind === 'browser_local_navigation' && tool === 'browser.fixture.follow') return t('2 / 2 · Details bağlantısını izleme onayınız bekleniyor.');
  if (kind === 'browser_staging_workflow' && tool === 'browser.staging.open') return t('1 / 4 · App sayfasını açma onayınız bekleniyor.');
  if (kind === 'browser_staging_workflow' && tool === 'browser.staging.follow') return t('2 / 4 · Draft bağlantısını izleme onayınız bekleniyor.');
  if (kind === 'browser_staging_workflow' && tool === 'browser.staging.fill') return t('3 / 4 · Sabit mesajı doldurma onayınız bekleniyor.');
  if (kind === 'browser_staging_workflow' && tool === 'browser.staging.submit') return t('4 / 4 · Kaydetme onayınız bekleniyor. POST henüz yapılmadı.');
  if (kind === 'browser_remote_entry' && tool === 'browser.remote.open') return t('1 / 1 · Exact HTTPS giriş isteği onayınızı bekliyor; henüz ağ isteği yapılmadı.');
  if (kind === 'browser_remote_routes' && tool === 'browser.remote.route') return `${Number(tasks?.approval?.action.arguments.route_index ?? 0) + 1} / ${tasks?.remote_routes?.route_count ?? '?'} · ${t('Exact HTTPS rota isteği onayınızı bekliyor; sonraki GET henüz yapılmadı.')}`;
  if (kind === 'browser_remote_static_assets' && tool === 'browser.static.open') return tasks?.remote_static_assets?.mode === 'one_shot_readonly_data_bundle'
    ? t('1 / 1 · Exact HTTPS giriş, JS/CSS/görsel ve JSON GET paketi onayınızı bekliyor; henüz ağ isteği yapılmadı.')
    : t('1 / 1 · Exact HTTPS giriş ve JS/CSS/görsel paketi onayınızı bekliyor; henüz ağ isteği yapılmadı.');
  if (kind === 'browser_remote_form' && tool?.startsWith('browser.form.')) {
    const stages = tasks?.remote_form?.state_plan_sha256
      ? ['browser.form.open', 'browser.form.state_before', 'browser.form.fill', 'browser.form.submit', 'browser.form.receipt', 'browser.form.state_after']
      : ['browser.form.open', 'browser.form.fill', 'browser.form.submit', 'browser.form.receipt'];
    return `${stages.indexOf(tool) + 1} / ${stages.length} · ${t('HTTPS form adımı ayrı onayınızı bekliyor; sonraki ağ veya form eylemi henüz yapılmadı.')}`;
  }
  if (kind === 'vision_canvas' && tool === 'vision.click') return t('1 / 1 · SAVE hedefi seçildi; tıklama onayınız bekleniyor. Henüz kaydedilmedi.');
  if (latest?.kind !== kind) return t('Görevi başlattığınızda gerçek tarayıcı adımları burada görünür.');
  if (tasks?.busy) return latest.progress?.phase ? 'Görev çalışıyor; kaydedilen evre ve süreler ilerleme bölümünde gösteriliyor.' : 'Görev çalışıyor; gözlem, model kararı veya bağımsız doğrulama bekleniyor. Ayrıntılı evre henüz ölçülmüyor.';
  if (latest.status === 'succeeded') return kind === 'vision_canvas' ? 'Görsel SAVE görevi tamamlandı; kaydedilen sonuç bağımsız doğrulandı.' : kind === 'browser_local_navigation' ? 'Details sayfası bağımsız doğrulandı.' : kind === 'browser_staging_workflow' ? 'Makbuz ve tek POST bağımsız doğrulandı.' : kind === 'browser_remote_entry' ? 'Tek HTTPS giriş ve içeriksiz tarayıcı parmak izi doğrulandı; site görevi yapılmadı.' : kind === 'browser_remote_routes' ? 'Sıralı HTTPS GET ve içeriksiz tarayıcı parmak izleri doğrulandı; uygulama sonucu doğrulanmadı.' : kind === 'browser_remote_static_assets' ? tasks?.remote_static_assets?.mode === 'one_shot_readonly_data_bundle' ? 'Exact giriş, JS/CSS/görsel ve JSON GET taşıması doğrulandı; uygulama sonucu doğrulanmadı.' : 'Exact giriş ve JS/CSS/görsel paketinin taşıması ve içeriksiz readback doğrulandı; uygulama sonucu doğrulanmadı.' : kind === 'browser_remote_form' ? tasks?.remote_form?.state_plan_sha256 ? 'Form taşıması ve ilan edilen HTML durum değişimi gözlendi; uygulama sonucu doğrulanmadı.' : 'Form taşıması ve makbuz readback doğrulandı; uygulama sonucu doğrulanmadı.' : 'Browser görevi tamamlandı; kaydedilen içerik bağımsız doğrulandı.';
  if (['failed', 'cancelled'].includes(latest.status)) return t('Görev tamamlanmadı; başarısız oldu veya iptal edildi. Mevcutsa İzi aç bölümünü inceleyin.');
  return t('Görevi başlattığınızda gerçek tarayıcı adımları burada görünür.');
}

function validOwnedFormInvocation(value: unknown): value is OwnedFormInvocation {
  if (value === null || typeof value !== 'object') return false;
  const invocation = value as Record<string, unknown>;
  const lifecycle = invocation.lifecycle;
  const hasRun = typeof invocation.run_ref === 'string' && /^[a-f0-9]{64}$/.test(invocation.run_ref);
  const hasReport = typeof invocation.report_sha256 === 'string' && /^[a-f0-9]{64}$/.test(invocation.report_sha256);
  const modeValid = invocation.mode === 'owned_synthetic_form_invocation'
    ? invocation.recipe_sha256 === undefined && invocation.steps === undefined
    : invocation.mode === 'owned_synthetic_form_recipe'
      && typeof invocation.recipe_sha256 === 'string' && /^[a-f0-9]{64}$/.test(invocation.recipe_sha256)
      && isOwnedFormRecipeSteps(invocation.steps);
  return modeValid
    && (invocation.reuse_admission_sha256 === undefined
      || invocation.mode === 'owned_synthetic_form_invocation'
      && typeof invocation.reuse_admission_sha256 === 'string'
      && /^[a-f0-9]{64}$/.test(invocation.reuse_admission_sha256))
    && ['ready', 'running', 'completed', 'audited', 'failed'].includes(String(lifecycle))
    && typeof invocation.profile_sha256 === 'string' && /^[a-f0-9]{64}$/.test(invocation.profile_sha256)
    && typeof invocation.skill_sha256 === 'string' && /^[a-f0-9]{64}$/.test(invocation.skill_sha256)
    && typeof invocation.invocation_sha256 === 'string' && /^[a-f0-9]{64}$/.test(invocation.invocation_sha256)
    && (invocation.run_ref === undefined || invocation.run_ref === null
      || typeof invocation.run_ref === 'string' && /^[a-f0-9]{64}$/.test(invocation.run_ref))
    && (invocation.report_sha256 === undefined || invocation.report_sha256 === null
      || hasReport)
    && (lifecycle !== 'ready' || (!hasRun && !hasReport))
    && (!['completed', 'audited'].includes(String(lifecycle)) || hasRun)
    && (lifecycle !== 'audited' || hasReport);
}

function ownedInvocationIdentity(runtimeId: string, invocation: OwnedFormInvocation): string {
  const recipe = invocation.mode === 'owned_synthetic_form_recipe'
    ? `${invocation.recipe_sha256}:${JSON.stringify(invocation.steps)}` : '';
  return `${runtimeId}:${invocation.mode}:${recipe}:${invocation.profile_sha256}:${invocation.skill_sha256}:${invocation.invocation_sha256}:${invocation.run_ref ?? ''}`;
}

const ownedRecipeTools = {
  open_entry: 'browser.form.open', read_state_before: 'browser.form.state_before',
  fill_form: 'browser.form.fill', submit_form: 'browser.form.submit',
  read_receipt: 'browser.form.receipt', read_state_after: 'browser.form.state_after'
};

export function Tasks({tasks, snapshot, busy, action, showTrace}: Props) {
  const [storedCandidate, setStoredCandidate] = useState('');
  const [kind, setKind] = useState(tasks?.browser_display === 'desktop' ? 'browser_form' : 'hello');
  const advertisedKinds = (tasks?.kinds ?? []).filter(value => Object.hasOwn(catalog, value));
  useEffect(() => {
    if (tasks && !advertisedKinds.includes(kind)) setKind(advertisedKinds[0] ?? 'hello');
  }, [tasks, kind, advertisedKinds]);
  const remote = kind === 'browser_remote_entry' || kind === 'browser_remote_routes' || kind === 'browser_remote_static_assets' || kind === 'browser_remote_form';
  const dataBundle = tasks?.remote_static_assets?.mode === 'one_shot_readonly_data_bundle';
  const staticBundle = dataBundle ? {
    label: 'HTTPS JS/CSS/görsel ve JSON GET paketi',
    scope: 'Yalnız kayıtlı HTTPS girişi, exact JS/CSS/görsel ve salt okunur JSON GET URL’leri tek elle onayla yüklenir. Diğer ağ istekleri engellenir; uygulama sonucu doğrulanmaz.',
    button: 'JSON paket görevini başlat'
  } : catalog.browser_remote_static_assets;
  const taskCatalog = (taskKind: string) => taskKind === 'browser_remote_static_assets' ? staticBundle : catalog[taskKind];
  const publicForm = tasks?.remote_form?.mode === 'public_explicit_one_post';
  const formState = Boolean(tasks?.remote_form?.state_plan_sha256);
  const latest = tasks?.jobs[0];
  const ownedInvocationSupported = Boolean(tasks && Object.hasOwn(tasks, 'owned_form_invocation'));
  const ownedInvocationValue = tasks?.owned_form_invocation;
  const ownedInvocation = validOwnedFormInvocation(ownedInvocationValue) ? ownedInvocationValue : null;
  const ownedRecipe = ownedInvocation?.mode === 'owned_synthetic_form_recipe' ? ownedInvocation : null;
  const ownedInvocationSignature = ownedInvocation
    ? ownedInvocationIdentity(snapshot?.runtime.runtime_id ?? '', ownedInvocation)
    : '';
  const [ownedInvocationOptIn, setOwnedInvocationOptIn] = useState(false);
  const [ownedAuditState, setOwnedAuditState] = useState<'idle' | 'loading' | 'not_ready' | 'unavailable'>('idle');
  const [ownedAuditResult, setOwnedAuditResult] = useState<{signature: string; report: OwnedFormInvocationAuditReport; reportSha256: string} | null>(null);
  const ownedInvocationSignatureRef = useRef(ownedInvocationSignature);
  useEffect(() => { setStoredCandidate(''); }, [snapshot?.runtime.runtime_id, ownedInvocation?.run_ref]);
  ownedInvocationSignatureRef.current = ownedInvocationSignature;
  useEffect(() => {
    setOwnedInvocationOptIn(false);
    setOwnedAuditState('idle');
    setOwnedAuditResult(null);
  }, [ownedInvocationSignature, latest?.job_id, kind]);
  const navigation = kind === 'browser_local_navigation' || kind === 'browser_staging_workflow' || remote;
  const approvalDefault = Boolean(!navigation && tasks?.supports_approve_all && snapshot?.control.owner === 'AGENT' && snapshot.control.status === 'running');
  const [approveAll, setApproveAll] = useState(approvalDefault);
  const [learningMetadata, setLearningMetadata] = useState(false);
  const [remoteConsentSha256, setRemoteConsentSha256] = useState('');
  const [consentSystem1, setConsentSystem1] = useState(false);
  const [consentSystem2, setConsentSystem2] = useState(false);
  const [attestDataRights, setAttestDataRights] = useState(false);
  const [consentPreview, setConsentPreview] = useState<RemoteLearningConsentReport | null>(null);
  const [consentConfirmation, setConsentConfirmation] = useState('');
  useEffect(() => setApproveAll(approvalDefault), [kind, approvalDefault, snapshot?.control.generation]);
  const approval = tasks?.approval;
  const visibleBrowser = tasks?.browser_display === 'desktop';
  const visibleVision = tasks?.vision_display === 'desktop';
  const visibleDesktop = visibleBrowser || visibleVision;
  const learning = latest?.learning_metadata;
  const remoteLearning = latest?.remote_learning_metadata;
  const formLearning = latest?.kind === 'browser_remote_form' && Boolean(tasks?.remote_form
    && !tasks.remote_form.cookie_sha256);
  useEffect(() => {
    setRemoteConsentSha256('');
    setConsentSystem1(false);
    setConsentSystem2(false);
    setAttestDataRights(false);
    setConsentPreview(null);
    setConsentConfirmation('');
  }, [latest?.job_id]);
  useEffect(() => {
    setConsentPreview(null);
    setConsentConfirmation('');
  }, [consentSystem1, consentSystem2, attestDataRights]);
  const canPrepareConsent = Boolean(!busy && latest?.status === 'waiting_approval'
    && (tasks?.approval?.action.tool === 'browser.remote.route'
      || tasks?.approval?.action.tool === 'browser.static.open'
      || formLearning && tasks?.approval?.action.tool === 'browser.form.open')
    && !remoteLearning?.enabled);
  const progressKind = (tasks?.busy || tasks?.reserved || approval) && latest ? latest.kind : kind;
  const progressVisible = progressKind === 'browser_form' || progressKind === 'browser_local_navigation' || progressKind === 'browser_staging_workflow' || progressKind === 'browser_remote_entry' || progressKind === 'browser_remote_routes' || progressKind === 'browser_remote_static_assets' || progressKind === 'browser_remote_form' ? visibleBrowser : progressKind === 'vision_canvas' && visibleVision;
  const ownedAuditEligible = Boolean(ownedInvocation && ownedInvocation.run_ref
    && ['completed', 'audited'].includes(ownedInvocation.lifecycle)
    && latest?.kind === 'browser_remote_form' && latest.status === 'succeeded');
  const visibleOwnedAudit = ownedAuditEligible && ownedAuditResult?.signature === ownedInvocationSignature
    && (ownedInvocation?.report_sha256 == null || ownedInvocation.report_sha256 === ownedAuditResult.reportSha256)
    ? ownedAuditResult : null;
  return <section className="panel">
    <h2>{t("Onaylı sınırlı görevler")}</h2>
    {visibleDesktop ? <div className="visible-task-status" data-testid="visible-task-status" aria-live="polite">
      <h3>{progressVisible ? progressKind === 'vision_canvas' ? t('Görsel SAVE görevini bu bilgisayarda izleyin') : t('Browser görevini bu bilgisayarda izleyin') : t('Canlı görev ekranı')}</h3>
      {progressVisible ? <>
        <p>{progressKind === 'vision_canvas' ? t('SAVE sahnesi aynı canlı izole masaüstünde açılır. Görsel gözlem ve model kararından sonra tek onay SAVE düğmesine tıklar.') : progressKind === 'browser_local_navigation' ? t('Start ve Details sayfaları aynı görünür Chromium’da açılır; iki geçiş ayrı elle onaylanır ve doğrulanır.') : progressKind === 'browser_staging_workflow' ? t('App, Draft ve makbuz aynı görünür Chromium’da açılır; dört eylem ayrı elle onaylanır ve doğrulanır.') : progressKind === 'browser_remote_entry' ? t('Onaydan önce ağ isteği yoktur; exact profil girişi aynı görünür Chromium’da tek kez açılır ve içeriksiz parmak izi okunur.') : progressKind === 'browser_remote_routes' ? t('Exact HTTPS rotaları aynı görünür Chromium’da sırayla açılır; her GET ayrı onay ve içeriksiz readback ister.') : progressKind === 'browser_remote_static_assets' ? dataBundle ? t('Exact HTTPS giriş, JS/CSS/görsel ve JSON GET paketi aynı görünür Chromium’da tek elle onaydan sonra açılır; içeriksiz readback yapılır.') : t('Exact HTTPS giriş ve kayıtlı JS/CSS/görsel paketi aynı görünür Chromium’da tek elle onaydan sonra açılır; içeriksiz readback yapılır.') : progressKind === 'browser_remote_form' ? formState ? t('Form görünür Chromium’da çalışır; önce ve sonra ayrı onaylı host HTTPS durum GET’i yapılır.') : t('HTTPS formu aynı görünür Chromium’da giriş, doldurma, tek POST ve makbuz için ayrı onaylarla çalışır.') : t('Form, yanınızdaki canlı izole masaüstünde açılır. İlk onay Message alanını doldurur; ikinci onay Save locally düğmesini uygular.')}</p>
        <p>{t(visibleProgress(tasks, progressKind))}</p>
        <p className="caption">{t("Başarılı sonuç ekranda kalır; sonraki görev, kontrol devri veya çıkış tarayıcıyı kapatır. Doğrulama kaydı İzi aç bölümündedir.")}</p>
      </> : <p>{progressKind === 'hello' ? t('Hello bir dosya görevidir; masaüstünde tarayıcı adımı göstermez.') : t('Bu görev ayrı headless tarayıcıda çalışır; canlı masaüstünde görünmez.')}</p>}
      <p className="caption">{visibleBrowser ? t('Browser formu bu ekranda çalışır.') : t('Browser formu ayrı headless tarayıcıda çalışır.')} {visibleVision ? t('Görsel SAVE de aynı ekranda çalışır.') : t('Vision görevi ayrı headless tarayıcıda çalışır.')} {t("Hello dosya görevidir.")}</p>
    </div> : null}
    <TaskProgress tasks={tasks}/>
    <FailureImprovement tasks={tasks} snapshot={snapshot} busy={busy}/>
    {learning?.enabled ? <div className="registry" data-testid="learning-metadata-status" role="status">
      <h3>{t('Sentetik öğrenme metadata durumu')}</h3>
      <p>{t('Durum:')} {t(learning.state)}</p>
      {learning.entries_by_role ? <p>{t('S1 olayları:')} {learning.entries_by_role.system1} · {t('S2 olayları:')} {learning.entries_by_role.system2} · {t('Toplam:')} {learning.total_entries ?? 0}</p> : null}
      {learning.duration_ms !== undefined ? <p className="caption">{t('Son metadata yoklaması:')} {learning.duration_ms.toLocaleString(locale())} ms</p> : null}
      {learning.state === 'failed' || learning.state === 'failed_unpersisted' || learning.state === 'recovery_required' ? <p className="caption">{t('Metadata hattı eksik veya hatalı; görev sonucu ve eğitim hazır oluşu bundan çıkarılamaz.')}</p> : null}
      <p className="caption">{t('Yalnız açık seçilmiş sentetik görev metadata’sı; gerçek site verisi, gold veya eğitim izni değildir.')}</p>
    </div> : null}
    {tasks?.supports_remote_learning_metadata && (latest?.kind === 'browser_remote_routes' || latest?.kind === 'browser_remote_static_assets' || formLearning) ? <div className="registry" data-testid="remote-learning-metadata" role="status">
      <h3>{formLearning ? t('HTTPS form metadata kaydı') : latest.kind === 'browser_remote_static_assets' ? dataBundle ? t('JSON görev metadata kaydı') : t('Statik görev metadata kaydı') : t('Uzak rota metadata kaydı')}</h3>
      <p className="caption">{formLearning ? formState ? t('İlk form giriş onayı beklerken exact form ve durum planına bağlı ayrı S1/S2 metadata iznini kaydedip bağlayın. Altı aşama ayrı onay ister; HTML durum değişimi gold veya eğitim değildir.') : t('İlk form giriş onayı beklerken yalnız bu run için ayrı S1/S2 metadata iznini iki aşamada kaydedin ve exact hash’i bağlayın. Dört form aşaması ayrı onay ister; makbuz sonucu gold veya eğitim değildir.') : latest.kind === 'browser_remote_static_assets'
        ? dataBundle ? t('Bekleyen tek JSON paket onayı sırasında yalnız bu run için ayrı S1/S2 metadata iznini iki aşamada kaydedin veya exact hash’i bağlayın. JSON içeriği kaydedilmez; okuma sonucu gold değildir.') : t('Bekleyen tek statik paket onayı sırasında yalnız bu run için ayrı S1/S2 metadata iznini iki aşamada kaydedin veya exact hash’i bağlayın. Okuma sonucu gold değildir; hesap/hak doğrulaması veya eğitim izni yoktur.')
        : t('Bekleyen rota onayı sırasında yalnız bu run için S1/S2 metadata iznini iki aşamada kaydedin veya önceden kaydedilmiş exact hash’i bağlayın. Bu hesap/hak doğrulaması veya eğitim izni değildir.')}</p>
      <p>{t('Aktif run:')} <code>{latest.run_id ?? '—'}</code></p>
      {remoteLearning?.enabled ? <>
        <p>{t('Durum:')} {t(remoteLearning.state)} · {t('S1 olayları:')} {remoteLearning.entries_by_role?.system1 ?? 0} · {t('S2 olayları:')} {remoteLearning.entries_by_role?.system2 ?? 0}</p>
        {remoteLearning.duration_ms !== undefined ? <p className="caption">{t('Son metadata yoklaması:')} {remoteLearning.duration_ms.toLocaleString(locale())} ms</p> : null}
        {remoteLearning.state === 'failed' || remoteLearning.state === 'failed_unpersisted' || remoteLearning.state === 'recovery_required' ? <p className="caption">{t('Metadata hattı eksik veya hatalı; görev sonucu ve eğitim hazır oluşu bundan çıkarılamaz.')}</p> : null}
      </> : <>
        <div data-testid="remote-learning-consent">
          <p className="caption">{t('Yalnız istediğiniz rolleri seçin; profilin istemediği roller sunucuda reddedilir. Ham içerik ve eğitim kapalı kalır.')}</p>
          <label><input type="checkbox" checked={consentSystem1} disabled={!canPrepareConsent} onChange={event => setConsentSystem1(event.target.checked)}/> {t('S1 karar metadata’sı')}</label>
          <label><input type="checkbox" checked={consentSystem2} disabled={!canPrepareConsent} onChange={event => setConsentSystem2(event.target.checked)}/> {t('S2 escalation metadata’sı')}</label>
          <label><input type="checkbox" checked={attestDataRights} disabled={!canPrepareConsent} onChange={event => setAttestDataRights(event.target.checked)}/> {t('Bu run için metadata toplama hakkım olduğunu yerel operatör olarak beyan ediyorum')}</label>
          <button type="button" disabled={!canPrepareConsent || !attestDataRights || (!consentSystem1 && !consentSystem2)} onClick={() => void action(async () => {
            const expiresAt = new Date(Date.now() + 15 * 60_000).toISOString().replace(/\.\d{3}Z$/, '+00:00');
            const roles: ('system1' | 'system2')[] = [
              ...(consentSystem1 ? ['system1' as const] : []),
              ...(consentSystem2 ? ['system2' as const] : [])];
            const preview = await api<RemoteLearningConsentReport>('/api/tasks/remote-learning/consent',
              {job_id: latest.job_id, roles, expires_at: expiresAt, attest_data_rights: true});
            setConsentPreview(preview);
            setConsentConfirmation('');
          })}>{t('Metadata iznini önizle')}</button>
          {consentPreview ? <div data-testid="remote-consent-preview">
            <p>{t('İzin SHA-256:')} <code>{consentPreview.consent_sha256}</code></p>
            <p>{t('Roller:')} {consentPreview.roles.join(', ')} · {t('Bitiş (UTC):')} {consentPreview.expires_at}</p>
            <p className="caption">{t('Yalnız içeriksiz metadata; harici hak doğrulanmadı ve eğitim izni yok. Hash’i inceleyip exact olarak tekrar girin.')}</p>
            <label htmlFor="remote-consent-confirmation">{t('İzin SHA-256 onayı')}</label>
            <input id="remote-consent-confirmation" value={consentConfirmation} maxLength={64} autoComplete="off" spellCheck={false} onChange={event => setConsentConfirmation(event.target.value.trim().toLowerCase())}/>
            <button type="button" disabled={!canPrepareConsent || consentPreview.registered || consentConfirmation !== consentPreview.consent_sha256} onClick={() => void action(async () => {
              const registered = await api<RemoteLearningConsentReport>('/api/tasks/remote-learning/consent',
                {job_id: latest.job_id, roles: consentPreview.roles,
                  expires_at: consentPreview.expires_at, attest_data_rights: true,
                  confirm_sha256: consentConfirmation});
              setConsentPreview(registered);
              setRemoteConsentSha256(registered.consent_sha256);
              setConsentConfirmation('');
            })}>{t('Exact izni kaydet')}</button>
            {consentPreview.registered ? <p role="status">{t('Özel izin kaydedildi; görev akışını başlatmak için hash’i ayrıca bağlayın.')}</p> : null}
          </div> : null}
        </div>
        <label htmlFor="remote-consent-sha256">{t('Yerel metadata izin SHA-256')}</label>
        <input id="remote-consent-sha256" value={remoteConsentSha256} maxLength={64} autoComplete="off" spellCheck={false} onChange={event => setRemoteConsentSha256(event.target.value.trim().toLowerCase())}/>
        <button type="button" disabled={!canPrepareConsent || !/^[a-f0-9]{64}$/.test(remoteConsentSha256)} onClick={() => void action(async () => {
          await api('/api/tasks/remote-learning', {job_id: latest.job_id, consent_sha256: remoteConsentSha256});
          setRemoteConsentSha256('');
        })}>{t('Bu run için metadata kaydını bağla')}</button>
      </>}
    </div> : null}
    {tasks?.browser_transport === 'playwright_mcp' ? <p className="caption" data-testid="browser-mcp-transport">{t('Ubuntu Chromium üzerinde Playwright MCP. Yalnız sabit yerel form ve iki sayfa gezintisi; genel web uygulaması açık değil.')}</p> : null}
    {tasks?.navigation_transport === 'playwright_mcp' && tasks.browser_transport !== 'playwright_mcp' && tasks.kinds?.includes('browser_local_navigation')
      ? <p className="caption" data-testid="navigation-mcp-transport">{t('Yerel form CDP ile sürülür; sentetik Start→Details gezintisi aynı görünür Ubuntu Chromium üzerinde Playwright MCP kullanır. Genel web uygulaması açık değil.')}</p> : null}
    {tasks?.staging_transport === 'playwright_mcp' && tasks.kinds?.includes('browser_staging_workflow')
      ? <p className="caption" data-testid="staging-mcp-transport">{t('Sentetik App → Draft → makbuz akışı görünür Ubuntu Chromium üzerinde Playwright MCP ile çalışır. Gerçek site erişimi kapalıdır.')}</p> : null}
    {tasks?.remote_entry ? <p className="caption" data-testid="remote-entry-scope">{t('Tek HTTPS giriş profili:')} <code>{tasks.remote_entry.entry_url}</code> · {t('Profil SHA-256:')} <code>{tasks.remote_entry.profile_sha256}</code> · {t('Giriş veya alt kaynak yok; gerçek uygulama görevleri hâlâ kapalıdır.')}</p> : null}
    {tasks?.remote_routes ? <p className="caption" data-testid="remote-routes-scope">{t('Sıralı HTTPS rotaları:')} {tasks.remote_routes.route_count} · {t('Plan SHA-256:')} <code>{tasks.remote_routes.plan_sha256}</code> · {t('Her GET ayrı onay ister; uygulama sonucu doğrulanmaz, metadata yalnız exact izinle açılır.')}</p> : null}
    {tasks?.remote_static_assets ? <div className="caption" data-testid="remote-static-assets-scope"><p>{t('Exact HTTPS statik varlıklar:')} {tasks.remote_static_assets.asset_count} · {t('Plan SHA-256:')} <code>{tasks.remote_static_assets.plan_sha256}</code> · {dataBundle ? t('Tek onay giriş, planlı JS/CSS/görsel ve exact JSON GET’leri kapsar; uygulama sonucu doğrulanmaz.') : t('Tek onay giriş ve planlı JS/CSS/görsel GET’lerini kapsar; uygulama sonucu doğrulanmaz.')}</p><ul>{tasks.remote_static_assets.assets.map(url => <li key={url}><code>{url}</code></li>)}</ul>{dataBundle ? <><p>{t('Exact salt okunur JSON GET URL’leri:')}</p><ul>{tasks.remote_static_assets.data_resources?.map(url => <li key={url}><code>{url}</code></li>)}</ul></> : null}</div> : null}
    {tasks?.remote_routes ? <RemoteNavigationGraph tasks={tasks}/> : null}
    {dataBundle && tasks ? <RemoteJsonKnowledge tasks={tasks}/> : null}
    {tasks?.remote_routes ? <RemoteSkillSource tasks={tasks}/> : null}
    {tasks?.remote_routes?.review_sha256 ? <p className="caption" data-testid="remote-route-review-scope">{t('Geçmiş rota metadata inceleme pini:')} <code>{tasks.remote_routes.review_sha256}</code> · {t('Sembolik anahtarlar ancak kaynak ve varsa tüm eşlenmiş giden hedefler ayrı onaylı taze readback ile eşleşirse sonraki karara eklenir; onaylar değişmez.')}</p> : null}
    {kind === 'browser_remote_routes' && tasks?.remote_routes?.review_sha256 && latest?.kind === 'browser_remote_routes' && latest.route_knowledge ? <p className="caption" role="status" data-testid="remote-route-live-knowledge">{t('Son rota metadata gözlemi:')} {latest.route_knowledge.route_index + 1} · {latest.route_knowledge.status === 'matched' ? t('Kaynak parmak izi eşleşti; hedef henüz doğrulanmış olmayabilir.') : t('Eski: rota bilgisi sonraki kararlara taşınmaz.')} {latest.route_knowledge.stale_reason === 'target_fingerprint_changed' ? t('Giden hedef parmak izi değişti.') : null} {t('Bu, gerçek site veya uygulama sonucu doğrulaması değildir.')}</p> : null}
    {dataBundle && tasks?.remote_static_assets?.review_sha256 ? <p className="caption" data-testid="remote-json-review-scope">{t('Geçmiş JSON metadata inceleme pini:')} <code>{tasks.remote_static_assets.review_sha256}</code> · {t('Yalnız onaylı taze JSON readback parmak izi eşleşirse sembolik sayfa anahtarı raporlanır; görev kararına aktarılmaz.')}</p> : null}
    {dataBundle && tasks?.remote_static_assets?.review_sha256 && latest?.kind === 'browser_remote_static_assets' && latest.json_knowledge ? <p className="caption" role="status" data-testid="remote-json-live-knowledge">{t('Son JSON metadata gözlemi:')} {latest.json_knowledge.status === 'matched' ? <><code>{latest.json_knowledge.page_key}</code> · {t('Taslak revizyonu:')} {latest.json_knowledge.draft_revision} · {t('Taze readback parmak izi eşleşti.')}</> : t('Eski: JSON sayfa anahtarı raporlanmaz.')} {t('Bu, gerçek site veya uygulama sonucu doğrulaması değildir.')}</p> : null}
    {tasks?.remote_form ? <p className="caption" data-testid="remote-form-scope">{t('HTTPS form planı:')} <code>{tasks.remote_form.plan_sha256}</code> · {t('Giriş:')} <code>{tasks.remote_form.entry_url}</code> · {t('POST hedefi:')} <code>{tasks.remote_form.submit_url}</code> · {t('Makbuz:')} <code>{tasks.remote_form.receipt_url}</code> · {tasks.remote_form.field_names ? <>{t('Alanlar:')} <code>{tasks.remote_form.field_names.join(', ')}</code></> : <>{t('Alan:')} <code>{tasks.remote_form.field_name}</code></>} · {t('Gövde SHA-256:')} <code>{tasks.remote_form.body_sha256}</code>{tasks.remote_form.cookie_sha256 ? <> · {t('Özel Cookie SHA-256:')} <code>{tasks.remote_form.cookie_sha256}</code> · {t('Statik Cookie oturumu hesap doğrulaması değildir.')}</> : null} · {formState ? <>{t('Durum URL:')} <code>{tasks.remote_form.state_url}</code> · {t('Durum planı SHA-256:')} <code>{tasks.remote_form.state_plan_sha256}</code> · {t('Altı ayrı onay; HTML yanıt değişimi uygulama sonucunu kanıtlamaz.')}</> : publicForm ? t('Açık public plan izni; dört manuel onay. Hesap ve uygulama sonucu doğrulanmaz.') : t('Dört ayrı onay; yalnız .invalid sentetik TLS, gerçek hesap veya uygulama sonucu yok.')}</p> : null}
    {kind === 'browser_remote_form' ? <div className="registry" data-testid="owned-form-invocation" data-run-ref={ownedInvocation?.run_ref ?? ''}>
      <h3>{t(ownedRecipe ? 'Yönetilen sentetik çalıştırılabilir skill' : 'Yönetilen sentetik sabit şablon çağrısı')}</h3>
      {!ownedInvocationSupported ? <p role="status">{t('Bu backend oturumu yönetilen çağrı API’sini sunmuyor; yönetilen eylem kullanılamaz.')}</p>
        : ownedInvocationValue === null ? <p role="status">{t('Bu oturum için yönetilen sahipli sentetik çağrı yapılandırılmamış.')}</p>
        : !ownedInvocation ? <p role="status">{t('Yönetilen çağrı yeteneği bozuk; başlatma ve denetim kapalı.')}</p>
        : <>
          <p>{t('Oturum durumu:')} {t(ownedInvocation.lifecycle)}</p>
          <p className="caption">{t(ownedRecipe ? 'Pinli recipe adımları bu sırayla yürütülür. Yalnız sentetik form; gerçek site, eğitim veya etkinleştirme yetkisi vermez.' : 'Sahipli oturumda tek sabit sentetik şablon. URL, yol, kaynak veya değer seçicisi sunulmaz.')}</p>
          <p className="caption">{t('Profil SHA-256:')} <code>{ownedInvocation.profile_sha256}</code> · {t('Skill SHA-256:')} <code>{ownedInvocation.skill_sha256}</code> · {t('Çağrı SHA-256:')} <code>{ownedInvocation.invocation_sha256}</code></p>
          {ownedRecipe ? <div data-testid="owned-form-recipe">
            <p className="caption">{t('Recipe SHA-256:')} <code>{ownedRecipe.recipe_sha256}</code></p>
            <ol aria-label={t('Pinli skill adımları')} data-testid="owned-recipe-steps">
              {ownedRecipe.steps.map(step => {
                const pending = ownedRecipe.lifecycle === 'running'
                  && latest?.kind === 'browser_remote_form'
                  && approval?.action.tool === ownedRecipeTools[step.operation];
                return <li key={step.step_key} data-operation={step.operation} aria-current={pending ? 'step' : undefined}>
                  <code>{step.step_key}</code> · <code>{step.operation}</code>
                  {pending ? <strong data-testid="owned-recipe-current-step"> · {t('Bu adım onay bekliyor')}</strong> : null}
                </li>;
              })}
            </ol>
            <p className="caption">{t('Listelenen adımlar planı gösterir; yürütme kanıtı yalnız başarılı koşunun açık denetiminden sonra görünür.')}</p>
          </div> : null}
          <p className="caption">{t('Başlat, mevcut uzak browser form akışını açar. Altı eylemin her biri ayrı elle onay ister; tümünü onayla kapalıdır.')}</p>
          {ownedInvocation.lifecycle === 'ready' ? <label>
            <input type="checkbox" data-testid="owned-invocation-opt-in" checked={ownedInvocationOptIn}
              disabled={busy || tasks?.busy || tasks?.reserved || !tasks?.available || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'}
              onChange={event => setOwnedInvocationOptIn(event.target.checked)}/>
            {t('Bu tek kullanımlık sahipli sentetik çağrıyı başlatmayı açıkça seçiyorum')}
          </label> : null}
          {ownedAuditEligible ? <button type="button" data-testid="owned-invocation-audit" disabled={busy || ownedAuditState === 'loading'} onClick={() => void action(async () => {
            const expected = ownedInvocation;
            const signature = ownedInvocationSignature;
            const expectedJobId = latest?.job_id;
            setOwnedAuditState('loading');
            setOwnedAuditResult(null);
            try {
              const response = await api<unknown>('/api/tasks/owned-form-invocation-audit');
              if (ownedInvocationSignatureRef.current !== signature) {
                setOwnedAuditState('unavailable');
                return;
              }
              if (isVerifiedOwnedFormInvocationAudit(response, expected)) {
                const [currentTasks, currentSnapshot] = await Promise.all([
                  api<TaskStatus>('/api/tasks'), api<Snapshot>('/api/state')
                ]);
                const currentInvocation = validOwnedFormInvocation(currentTasks.owned_form_invocation)
                  ? currentTasks.owned_form_invocation : null;
                const currentJob = currentTasks.jobs.find(job => job.job_id === expectedJobId);
                const currentSignature = currentInvocation
                  ? ownedInvocationIdentity(currentSnapshot.runtime.runtime_id, currentInvocation) : '';
                if (ownedInvocationSignatureRef.current !== signature || currentSignature !== signature
                    || !currentInvocation || !['completed', 'audited'].includes(currentInvocation.lifecycle)
                    || currentInvocation.report_sha256 != null
                    && currentInvocation.report_sha256 !== response.report_sha256
                    || currentJob?.kind !== 'browser_remote_form' || currentJob.status !== 'succeeded') {
                  setOwnedAuditState('unavailable');
                  return;
                }
                setOwnedAuditResult({signature, report: response.report, reportSha256: response.report_sha256});
                setOwnedAuditState('idle');
              } else if (response !== null && typeof response === 'object'
                  && (response as Record<string, unknown>).available === false
                  && (expected.mode === 'owned_synthetic_form_recipe'
                    ? (response as Record<string, unknown>).mode === expected.mode
                    : (response as Record<string, unknown>).mode === undefined
                      || (response as Record<string, unknown>).mode === expected.mode)
                  && (response as Record<string, unknown>).status === 'not_ready'
                  && (response as Record<string, unknown>).report === null
                  && (response as Record<string, unknown>).report_sha256 === null) {
                setOwnedAuditState('not_ready');
              } else {
                setOwnedAuditState('unavailable');
              }
            } catch {
              if (ownedInvocationSignatureRef.current === signature) setOwnedAuditState('unavailable');
            }
          })}>{ownedAuditState === 'loading' ? t('Çağrı denetleniyor…') : t('Salt okunur çağrı denetimini çalıştır')}</button> : null}
          {ownedAuditState === 'not_ready' ? <p role="status">{t('Sahipli koşu denetime hazır değil; doğrulanmış bir iddia gösterilmiyor.')}</p> : null}
          {ownedAuditState === 'unavailable' ? <p role="status">{t('Çağrı denetimi kullanılamıyor veya yanıt bozuk; doğrulanmış bir iddia gösterilmiyor.')}</p> : null}
          {visibleOwnedAudit ? <div role="status" data-testid="owned-invocation-audit-result">
            <p><strong>{t(visibleOwnedAudit.report.schema_version === '2.0' ? 'Sentetik recipe yürütmesi doğrulandı' : 'Çağrı yürütmesi doğrulandı')} (<code>{visibleOwnedAudit.report.schema_version === '2.0' ? 'executable_recipe_executed' : 'invocation_execution_verified'}</code>)</strong> · {t('Rapor SHA-256:')} <code>{visibleOwnedAudit.reportSha256}</code></p>
            <p>{t('Altı elle onay ve tek gönderim için taşıma ve beyan edilen durum geri okuma kanıtı var.')}</p>
            <p className="caption"><code>{visibleOwnedAudit.report.schema_version === '2.0' ? 'skill_executed=true · skill_validated=false' : 'skill_executed=false'}</code> · {t('Site sonucu, held-out bağımsızlığı, inceleme, etkinleştirme veya eğitim hazır oluşu doğrulanmış ya da yetkilendirilmiş değildir.')}</p>
          </div> : null}
        </>}
    </div> : null}
    {ownedInvocation?.mode === 'owned_synthetic_form_invocation' && ownedInvocation.lifecycle === 'audited'
      && (!tasks?.owned_form_candidate_execution || tasks.owned_form_candidate_execution.lifecycle === 'previewed')
      && ownedAuditEligible && kind === 'browser_remote_form' && snapshot && latest ? <OwnedSkillCandidate
        key={ownedInvocationSignature + ':' + latest.job_id}
        invocation={ownedInvocation} runtimeId={snapshot.runtime.runtime_id} jobId={latest.job_id}
        disabled={busy || Boolean(tasks?.busy || tasks?.reserved)} onStored={setStoredCandidate}/> : null}
    {kind === 'browser_remote_form' && snapshot && tasks?.parameter_web_goal_execution ? <ParameterWebGoal
      snapshot={snapshot} status={tasks.parameter_web_goal_execution} disabled={busy || Boolean(tasks.busy || tasks.reserved)}/> : null}
    {ownedInvocation?.reuse_admission_sha256 && kind === 'browser_remote_form' && snapshot && tasks ? <OwnedWebGoalCatalog
      source={ownedInvocation} snapshot={snapshot} planning={tasks.owned_web_goal_planning}
      disabled={busy || Boolean(tasks.busy || tasks.reserved)}/> : null}
    {ownedInvocation?.reuse_admission_sha256 && kind === 'browser_remote_form' && snapshot && tasks ? <OwnedSkillPlanning
      key={snapshot.runtime.runtime_id + ':' + ownedInvocation.reuse_admission_sha256}
      source={ownedInvocation} snapshot={snapshot} tasks={tasks} disabled={busy}/> : null}
    {ownedInvocation?.mode === 'owned_synthetic_form_invocation' && ownedInvocation.lifecycle === 'audited'
      && kind === 'browser_remote_form' && snapshot && tasks && tasks.owned_skill_planning?.status !== 'bound' ? <OwnedCandidateExecution
        key={snapshot.runtime.runtime_id + ':' + ownedInvocation.run_ref + ':' + ownedInvocation.invocation_sha256}
        source={ownedInvocation} snapshot={snapshot} tasks={tasks}
        selectedCandidate={storedCandidate} disabled={busy}/> : null}
    {tasks?.decider_preparation?.enabled ? <div className="registry" role="status" data-testid="decider-preparation">
      <p>{t(tasks.decider_preparation.state === 'ready_gpu' ? 'System-1 süreli GPU bekletme:' : 'System-1 CPU hazırlığı:')} {tasks.decider_preparation.state === 'ready' || tasks.decider_preparation.state === 'ready_gpu' ? t('Hazır') : tasks.decider_preparation.state === 'preparing' ? t('Hazırlanıyor') : t('Etkin değil')}</p>
      <p className="caption">{t(tasks.decider_preparation.state === 'ready_gpu' ? 'GPU modeli kısa süre bellekte tutulur; sonraki görevde pinler yeniden denetlenir. Vision, kontrol devri, hata veya süre dolması modeli bırakır; görev/onay kendiliğinden başlamaz.' : 'CPU hazırlığı GPU belleğine yükleme değildir; görev başladığında tam bütünlük denetimi ve çıkarım yine yapılır. Erken başlatılan görev hazırlığı bekleyebilir. Hazırlığın süresinin dolması görev başlatmaz veya yeniden başlatmaz.')}</p>
    </div> : null}
    {tasks?.auto_approval ? <article className="registry" role="status" data-testid="auto-approval-active">
      <h3>{t('Bu görev için otomatik onay etkin')}</h3>
      <p>{t(taskCatalog(tasks.auto_approval.kind)?.label ?? tasks.auto_approval.kind)} · <code>{tasks.auto_approval.job_id}</code></p>
      <p className="caption">{t('Yalnız bu görevin sınırları içindeki eylemler otomatik onaylanır; güvenlik politikası ve bağımsız doğrulama devam eder.')}</p>
    </article> : null}
    {approval ? <article className="registry" data-testid="approval">
      <h3>{t("Eylem onayı bekleniyor")}</h3>
      <p><code>{approval.action.tool}</code> {t("· state")} {approval.action.state_version}</p>
      <p>{approval.action.expected_effect}</p>
      <pre>{JSON.stringify(approval.action.arguments, null, 2)}</pre>
      {approval.action.tool === 'browser.static.open' ? <p className="caption">{dataBundle ? t('Bu tek onay exact giriş, yukarıdaki JS/CSS/görsel ve JSON GET URL’lerini kapsar; plan hash’ini kontrol edin.') : t('Bu tek onay exact giriş ve yukarıda listelenen tüm JS/CSS/görsel URL’lerini kapsar; plan hash’ini kontrol edin.')}</p> : approval.action.tool.startsWith('browser.form.state_') ? <p className="caption">{t('Bu ayrı onay host HTTPS durum GET’i içindir; tarayıcıda yeni sayfa açılmaz. URL ve fazı kontrol edin.')}</p> : approval.action.tool.startsWith('browser.form.') ? <p className="caption">{t('HTTPS form onayında exact hedef URL, alan adı ve POST gövde hash’i görünür; özel değer gösterilmez. Göndermeden önce hedefi kontrol edin.')}</p> : null}
      <p className="caption">{t("Bu eylem ve bağımsız readback içindir; yeni yetki vermez. Bitiş:")} {new Date(approval.expires_at * 1000).toLocaleTimeString(locale())}</p>
      <p>{t("Runtime:")} <code>{approval.action.runtime_id}</code></p>
      <p>{t("Digest:")} <code>{approval.action_sha256}</code></p>
      {[true, false].map(accept => <button key={String(accept)} disabled={busy || Date.now() >= approval.expires_at * 1000} onClick={() => void action(async () => {
        await api(`/api/approvals/${encodeURIComponent(approval.approval_id)}`, {action_sha256: approval.action_sha256, accept});
      })}>{accept ? t('Onayla') : t('Reddet')}</button>)}
    </article> : null}
    {!visibleDesktop ? <GoalPreview busy={busy} action={action}/> : null}
    <div className="registry" data-testid="task-capabilities">
      <h3>{t('Bu oturumdaki görev yetenekleri')}</h3>
      <p className="caption">{t('Kullanılabilirlik sunucunun görev listesinden okunur; kapalı görev başlatılamaz. Liste başarı kanıtı değildir.')}</p>
      {Object.keys(catalog).map(taskKind => <p key={taskKind} data-testid={`task-capability-${taskKind}`}>
        {t(taskCatalog(taskKind).label)} · {tasks === null ? t('Durum bekleniyor') : tasks.available && tasks.kinds?.includes(taskKind) ? t('Açık') : t('Bu oturumda kapalı')}
      </p>)}
      <p className="caption" data-testid="navigation-capability-note">{tasks === null ? t('Görev motoru bilgisi bekleniyor…')
        : tasks.available && tasks.kinds?.includes('browser_local_navigation')
          ? t('Start→Details yalnız sentetik iki sayfa görevidir; her geçiş ayrı elle onaylanır ve bağımsız doğrulanır. Toplu onay kapalıdır.')
          : t('Start→Details bu sunucu oturumunda sunulmuyor; Playwright MCP yapılandırması olmadan başlatılamaz.')}</p>
      {tasks?.kinds?.includes('browser_staging_workflow') ? <p className="caption" data-testid="staging-capability-note">{t('Dört adımlı sentetik uygulama akışında toplu onay kapalıdır; gerçek hedef uygulama yetkisi değildir.')}</p> : null}
      {tasks?.kinds?.includes('browser_remote_entry') ? <p className="caption" data-testid="remote-entry-capability-note">{t('Uzak girişte toplu onay kapalıdır; yalnız tek HTML GET ve salt okunur parmak izi izinlidir.')}</p> : null}
      {tasks?.kinds?.includes('browser_remote_routes') ? <p className="caption" data-testid="remote-routes-capability-note">{t('Sıralı HTTPS okumada toplu onay kapalıdır; her rota ayrı elle onaylanır.')}</p> : null}
      {tasks?.kinds?.includes('browser_remote_static_assets') ? <p className="caption" data-testid="remote-static-assets-capability-note">{dataBundle ? t('JSON GET paketinde toplu onay kapalıdır; giriş, JS/CSS/görsel ve exact JSON URL’leri tek manuel onay kapsamındadır.') : t('Statik JS/CSS/görsel paketinde toplu onay kapalıdır; giriş ve exact varlıklar tek manuel onay kapsamındadır.')}</p> : null}
      {tasks?.kinds?.includes('browser_remote_form') ? <p className="caption" data-testid="remote-form-capability-note">{formState ? t('HTTPS formunda toplu onay kapalıdır; altı eylem ayrı elle onaylanır.') : t('HTTPS formunda toplu onay kapalıdır; giriş, doldurma, POST ve makbuz ayrı elle onaylanır.')}</p> : null}
    </div>
    <label htmlFor="task-kind">{t("Görev türü")} </label>
    <select id="task-kind" value={kind} disabled={busy || tasks?.reserved} onChange={event => setKind(event.target.value)}>
      {(tasks === null ? ['hello'] : advertisedKinds).map(value => <option key={value} value={value}>{t(taskCatalog(value).label)}</option>)}
      {tasks !== null && advertisedKinds.length === 0 ? <option value="hello" disabled>{t('Bu oturumda görev yok')}</option> : null}
    </select>
    <TaskKnowledge tasks={tasks} snapshot={snapshot} kind={kind} busy={busy}/>
    <p>{t(taskCatalog(kind).scope)}</p>
    {kind === 'hello' ? <pre>{'Hello from the local agent.\n'}</pre> : kind === 'browser_remote_form' ? <p className="caption">{publicForm ? t('Görünür Ubuntu Chromium’da kayıtlı public HTTPS formu açılabilir; ham değer veritabanına yazılmaz. Makbuz taşıması uygulama sonucunu doğrulamaz.') : t('Görünür Ubuntu Chromium’da yalnız .invalid sentetik TLS formu açılır; ham değer veritabanına yazılmaz. Makbuz taşıması gerçek uygulama sonucunu doğrulamaz.')}</p> : kind === 'browser_remote_static_assets' ? <p className="caption">{dataBundle ? t('Görünür Ubuntu Chromium’da yalnız giriş, listelenen JS/CSS/görsel ve JSON GET URL’leri yüklenir. Ham JSON saklanmaz; gerçek site sonucu doğrulanmaz.') : t('Görünür Ubuntu Chromium’da yalnız giriş ve listelenen JS/CSS/görsel varlıkları yüklenir. Başlık ve H1 metni yerine SHA-256 parmak izleri saklanır; gerçek site sonucu doğrulanmaz.')}</p> : remote ? <p className="caption">{t('Aynı görünür Ubuntu Chromium üzerinde yalnız kayıtlı HTTPS rotaları ayrı onaylarla açılır. Başlık ve H1 metni yerine yalnız SHA-256 parmak izleri saklanır; gerçek site yetkisi ayrıca gerekir.')}</p> : (kind === 'browser_form' && visibleBrowser) || (kind === 'browser_local_navigation' && visibleBrowser) || (kind === 'browser_staging_workflow' && visibleBrowser) || (kind === 'vision_canvas' && visibleVision) ? <p className="caption">{t("Aynı ağsız Docker masaüstünde görünür Chromium. Kontrolü al görevi iptal eder; eski onay geçersiz olur. Tüm içerik sentetiktir.")}</p> : <p className="caption">{t("Ayrı ağsız, headless Bubblewrap/Chromium runtime. Bilgisayar panelindeki Docker masaüstüsü değildir; kontrol devri bu görevi iptal eder, tarayıcıya manuel erişim vermez. Tüm içerik sentetiktir.")}</p>}
    <p className="caption">{kind === 'browser_remote_form' ? formState ? t('Tek aktif görev · altı eylem ayrı onay ister. Duraklatma veya kontrol devri iptal eder; belirsiz POST tekrar edilmez.') : t('Tek aktif görev · giriş, doldurma, POST ve makbuz ayrı onay ister. Duraklatma veya kontrol devri iptal eder; belirsiz POST tekrar edilmez.') : kind === 'browser_remote_static_assets' ? dataBundle ? t('Tek aktif görev · giriş, JS/CSS/görsel ve exact JSON GET paketi tek manuel onay ister. Duraklatma, kontrol devri, durdurma ve çıkış görevi iptal eder; otomatik tekrar yoktur.') : t('Tek aktif görev · giriş ve exact JS/CSS/görsel paketi tek manuel onay ister. Duraklatma, kontrol devri, durdurma ve çıkış görevi iptal eder; otomatik tekrar yoktur.') : remote ? t('Tek aktif görev · her GET için ayrı onay gerekir. Duraklatma, kontrol devri, durdurma ve çıkış görevi iptal eder; devam veya otomatik tekrar yoktur. Backend yeniden başlatıldığında devam edilmez.') : t("Tek aktif görev · onay en fazla 60 saniye geçerli. Duraklatma run kimliğini korur; Devam et yeni gözlem, karar ve onay ister. Kontrol devri, durdurma ve çıkış görevi iptal eder. Backend yeniden başlatıldığında devam edilmez.")}</p>
    {tasks?.paused ? <p role="status">{t("Görev duraklatıldı. Aynı görevi taze gözlem ve yeni onayla sürdürmek için Devam et düğmesini kullanın.")}</p> : null}
    <p>{!tasks?.available ? t('Görev motoru kapalı. Backend’i --engine decider ile başlatın.') : tasks.real_model ? t('Gerçek yerel Decider · sabit deployment') : t('SENTETİK TEST MOTORU · gerçek model sonucu değildir')}</p>
    {kind === 'vision_canvas' ? <p>{t("Görsel gözlemci:")} {tasks?.real_supervisor ? t('Gerçek Bonsai') : t('Sentetik fixture; görüntü modeli çalışmaz')}</p> : null}
    <div data-testid="auto-approval-option">
      <label><input type="checkbox" data-testid="approve-all" checked={approveAll} aria-describedby="auto-approval-scope" disabled={navigation || !tasks?.supports_approve_all || busy || tasks.busy || tasks.reserved || !tasks.available || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onChange={event => setApproveAll(event.target.checked)}/> {t('Tümünü onayla — yalnız bu görev')}</label>
      <p id="auto-approval-scope" className="caption">{t('Desteklenen görevlerde varsayılan seçilidir; elle onaylamak için işareti kaldırın. Seçim görevi başlatmaz. Başlat düğmesi yalnız seçili sabit görev için ön onay verir; sonraki görevleri veya görev sıralarını kapsamaz. Duraklatma, durdurma, kontrol devri ve çıkış izni kaldırır; devam etme yeniden elle onay gerektirir. Güvenlik politikası ve bağımsız doğrulama değişmez.')}</p>
      {navigation ? <p className="caption">{kind === 'browser_remote_entry' ? t('Uzak HTTPS girişinde toplu onay kapalıdır; tek ağ isteği için ayrı onay gerekir.') : kind === 'browser_remote_routes' ? t('Sıralı HTTPS okumada toplu onay kapalıdır; her rota ayrı elle onaylanır.') : kind === 'browser_remote_static_assets' ? dataBundle ? t('JSON GET paketinde toplu onay kapalıdır; giriş, JS/CSS/görsel ve exact JSON URL’leri tek manuel onay kapsamındadır.') : t('Statik JS/CSS/görsel paketinde toplu onay kapalıdır; giriş ve exact varlıklar tek manuel onay kapsamındadır.') : kind === 'browser_remote_form' ? formState ? t('HTTPS formunda toplu onay kapalıdır; altı eylem ayrı elle onaylanır.') : t('HTTPS formunda toplu onay kapalıdır; giriş, doldurma, POST ve makbuz ayrı elle onaylanır.') : kind === 'browser_staging_workflow' ? t('Dört adımlı akışta toplu onay kapalıdır; her eylem için ayrı onay gerekir.') : t('İki sayfa gezintisinde toplu onay kapalıdır; her geçiş için ayrı onay gerekir.')}</p> : null}
      {!tasks?.supports_approve_all ? <p className="caption">{t('Bu backend tümünü onaylamayı desteklemiyor; eylemler ayrı ayrı elle onaylanır.')}</p> : null}
    </div>
    <div className="registry" data-testid="learning-metadata-option">
      <label><input type="checkbox" checked={!remote && learningMetadata} disabled={remote || !tasks?.supports_learning_metadata || busy || tasks.busy || tasks.reserved || !tasks.available || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'} onChange={event => setLearningMetadata(event.target.checked)}/> {t('Bu sentetik görev için S1/S2 metadata kaydet')}</label>
      <p className="caption">{remote ? t('Bu HTTPS görevinde sentetik öğrenme metadata kaydı kapalıdır.') : tasks?.supports_learning_metadata ? t('Varsayılan kapalıdır; seçim yalnız başlatılan bu göreve uygulanır. Gerçek model çağrısı yoksa ilgili rol için sıfır olay kalır.') : t('Bu oturumda sentetik metadata kaydı yapılandırılmamış.')}</p>
      <p className="caption">{t('Bu seçenek gerçek site verisi toplamaz; incelenmiş örnek, dataset veya fine-tune yetkisi üretmez.')}</p>
    </div>
    <button className="primary" data-testid="start-task" disabled={busy || !tasks?.available || !tasks.kinds?.includes(kind) || tasks.reserved || snapshot?.control.owner !== 'AGENT' || snapshot.control.status !== 'running'
      || Boolean(kind === 'browser_remote_form' && tasks?.parameter_web_goal_execution)
      || Boolean(tasks?.owned_parameter_project_execution != null && !ownedParameterProjectCanStart(tasks.owned_parameter_project_execution))
      || Boolean(kind === 'browser_remote_form' && ownedInvocation && (ownedInvocation.lifecycle !== 'ready' || !ownedInvocationOptIn))
      || Boolean(kind === 'browser_remote_form' && tasks?.owned_parameter_project_execution == null && ownedInvocationSupported && ownedInvocationValue !== null && !ownedInvocation)} onClick={() => void action(async () => {
      if (!snapshot) return;
      await api('/api/tasks', {kind, lease_id: snapshot.control.lease_id, generation: snapshot.control.generation,
        ...(!navigation && approveAll && tasks?.supports_approve_all ? {approve_all: true} : {}),
        ...(!remote && learningMetadata && tasks?.supports_learning_metadata ? {learning_metadata: true} : {})});
      setApproveAll(approvalDefault);
      setLearningMetadata(false);
      setOwnedInvocationOptIn(false);
    })}>{kind === 'browser_remote_form' && ownedInvocation ? t(ownedInvocation.lifecycle === 'ready' ? ownedRecipe ? 'Sahipli sentetik skill’i başlat' : 'Sahipli sentetik çağrıyı başlat' : 'Sahipli sentetik çağrı tek kullanımlıktır') : t(taskCatalog(kind).button)}</button>
    {visibleDesktop ? <details className="task-options"><summary>{t("Önizleme ve sıralı görev seçenekleri")}</summary><GoalPreview busy={busy} action={action}/><Sequences tasks={tasks} snapshot={snapshot} busy={busy} action={action}/></details> : <Sequences tasks={tasks} snapshot={snapshot} busy={busy} action={action}/>}
    <h3>{t("Bu oturumdaki görevler")}</h3>
    {tasks?.owned_parameter_project_execution != null && <OwnedParameterProject status={tasks.owned_parameter_project_execution}/>}
    {tasks?.owned_parameter_project_execution != null && snapshot && <OwnedParameterSkillReuse
      snapshot={snapshot} disabled={busy || Boolean(tasks.busy || tasks.reserved)}/>}
    {tasks?.owned_parameter_project_execution != null && snapshot && <OwnedParameterSkillRelease
      snapshot={snapshot} disabled={busy || Boolean(tasks.busy || tasks.reserved)} />}
    {tasks?.owned_parameter_project_execution != null && snapshot && <OwnedParameterSkillReview
      snapshot={snapshot} disabled={busy || Boolean(tasks.busy || tasks.reserved)} />}
    {tasks?.owned_parameter_project_execution != null && snapshot && <OwnedParameterSkill
      intentSha256={ownedParameterProjectAcceptedIntent(tasks.owned_parameter_project_execution)}
      snapshot={snapshot} disabled={busy || Boolean(tasks.busy || tasks.reserved)}/>}
    <ScientistLab/>
    <div className="event-list">{tasks?.jobs.map(job => <div key={job.job_id} data-testid="task-row">
      <span>{job.kind} · {job.real_model ? t('Gerçek model') : t('Sentetik')}</span><strong>{job.status}</strong>
      {job.run_id ? <button onClick={() => showTrace(job.run_id!)}>{t("İzi aç")}</button> : null}
    </div>)}</div>
  </section>;
}
