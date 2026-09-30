import {useState, type FormEvent} from 'react';
import {api, ApiError, type ImageDraftCapability, type StaticQueryCapability, type WebApplicationReport} from './api';
import {t} from './i18n';

interface TaskDraftPreview {
  task_sha256: string;
  profile_sha256: string;
  task_key: string;
  route_count: number;
  status: 'unregistered_draft';
  execution_authorized: false;
  collection_authorized: false;
}

interface TaskDraftReceipt extends Omit<TaskDraftPreview, 'status'> {
  status: 'private_unactivated_draft';
  task_file: string;
}

interface RouteDraftPreview {
  plan_sha256: string; profile_sha256: string; task_sha256: string;
  route_count: number; status: 'unregistered_draft';
  execution_authorized: false; collection_authorized: false;
}

interface RouteDraftReceipt extends Omit<RouteDraftPreview, 'status'> {
  status: 'private_unactivated_draft'; route_plan_file: string;
}

type StaticAsset = {url: string; content_type: 'text/css' | 'text/javascript' | 'application/javascript' |
  'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'};
const imageTypes = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] as const;

interface StaticDraftPreview {
  plan_sha256: string; profile_sha256: string; task_sha256: string;
  asset_count: number; status: 'unregistered_draft';
  execution_authorized: false; collection_authorized: false;
}

interface StaticDraftReceipt extends Omit<StaticDraftPreview, 'status'> {
  status: 'private_unactivated_draft'; static_plan_file: string;
}

interface ReadonlyDataDraftPreview extends StaticDraftPreview {
  data_count: number;
}

interface ReadonlyDataDraftReceipt extends Omit<ReadonlyDataDraftPreview, 'status'> {
  status: 'private_unactivated_draft'; readonly_data_plan_file: string;
}

function managedCommand(operation: 'preview-remote-entry' | 'preview-remote-routes' | 'preview-remote-static-assets' | 'preview-remote-readonly-data' | 'start',
                        task: TaskDraftReceipt, route?: RouteDraftReceipt, staticAssets?: StaticDraftReceipt,
                        readonlyData?: ReadonlyDataDraftReceipt): string {
  const argumentsList = [
    './scripts/aos-v1', operation,
    '--remote-entry-profile-sha256', task.profile_sha256,
    '--remote-entry-task-file', task.task_file
  ];
  if (route) argumentsList.push('--remote-routes-plan-file', route.route_plan_file);
  if (staticAssets) argumentsList.push('--remote-static-assets-plan-file', staticAssets.static_plan_file);
  if (readonlyData) argumentsList.push('--remote-readonly-data-plan-file', readonlyData.readonly_data_plan_file);
  return argumentsList.join(' ');
}

function ManagedCommands({task, route, staticAssets, readonlyData}: {task: TaskDraftReceipt; route?: RouteDraftReceipt; staticAssets?: StaticDraftReceipt; readonlyData?: ReadonlyDataDraftReceipt}) {
  const previewOperation = readonlyData ? 'preview-remote-readonly-data' : staticAssets ? 'preview-remote-static-assets' : route ? 'preview-remote-routes' : 'preview-remote-entry';
  return <div data-testid={readonlyData ? 'web-readonly-data-commands' : staticAssets ? 'web-static-commands' : route ? 'web-route-commands' : 'web-entry-commands'}>
    <p className="caption">{readonlyData
      ? t('Önce ağsız JSON plan önizlemesini çalıştırın. Güvenli durdurma sonrası start yeni oturum açar; tek manuel onay exact giriş, JS/CSS/görsel ve JSON GET URL’lerini kapsar.')
      : staticAssets
      ? t('Önce ağsız statik plan önizlemesini çalıştırın. Güvenli durdurma sonrası start yeni oturum açar; site isteği ancak Tasks ekranında exact kapsam için tek manuel onaydan sonra yapılır.')
      : t('Önce ağsız önizlemeyi çalıştırın. Sonra mevcut oturumu inceleyip güvenle durdurun; start yalnız yeni oturum açar ve siteye istek yapmaz. Görevde her GET ayrı onay ister.')}</p>
    <p>{t('1 · Ağsız manager önizlemesi')}</p><pre>{managedCommand(previewOperation, task, route, staticAssets, readonlyData)}</pre>
    <p>{t('2 · Güvenli durdurma sonrası yeni oturum')}</p><pre>{managedCommand('start', task, route, staticAssets, readonlyData)}</pre>
  </div>;
}

export function WebTaskDraft({inventory, imageDraftCapability, staticQueryCapability}: {inventory: WebApplicationReport[]; imageDraftCapability: ImageDraftCapability; staticQueryCapability: StaticQueryCapability}) {
  const imageDraftsAvailable = imageDraftCapability === 'supported';
  const [profileSha256, setProfileSha256] = useState('');
  const [taskKey, setTaskKey] = useState('');
  const [verificationRef, setVerificationRef] = useState('');
  const [routeCount, setRouteCount] = useState(1);
  const [confirmation, setConfirmation] = useState('');
  const [preview, setPreview] = useState<TaskDraftPreview | null>(null);
  const [receipt, setReceipt] = useState<TaskDraftReceipt | null>(null);
  const [routesText, setRoutesText] = useState('');
  const [routeConfirmation, setRouteConfirmation] = useState('');
  const [routePreview, setRoutePreview] = useState<{report: RouteDraftPreview; routes: string[]} | null>(null);
  const [routeReceipt, setRouteReceipt] = useState<RouteDraftReceipt | null>(null);
  const [staticAssets, setStaticAssets] = useState<StaticAsset[]>([{url: '', content_type: 'application/javascript'}]);
  const staticQueryBlocked = staticQueryCapability !== 'supported' && staticAssets.some(asset => asset.url.includes('?'));
  const [staticConfirmation, setStaticConfirmation] = useState('');
  const [staticPreview, setStaticPreview] = useState<{report: StaticDraftPreview; assets: StaticAsset[]} | null>(null);
  const [staticReceipt, setStaticReceipt] = useState<StaticDraftReceipt | null>(null);
  const [readonlyDataUrls, setReadonlyDataUrls] = useState<string[]>(['']);
  const [readonlyDataConfirmation, setReadonlyDataConfirmation] = useState('');
  const [readonlyDataPreview, setReadonlyDataPreview] = useState<{report: ReadonlyDataDraftPreview; assets: StaticAsset[]; dataResources: {url: string}[]} | null>(null);
  const [readonlyDataReceipt, setReadonlyDataReceipt] = useState<ReadonlyDataDraftReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<'unsupported' | 'scope' | 'registration' | null>(null);
  const selected = inventory.some(profile => profile.profile_sha256 === profileSha256);
  const taskFieldsValid = /^[a-z][a-z0-9_-]{0,63}$/.test(taskKey)
    && /^[a-z][a-z0-9_-]{0,63}$/.test(verificationRef);


  function reset() {
    setPreview(null);
    setReceipt(null);
    setConfirmation('');
    setFailure(null);
    setRoutesText('');
    setRouteConfirmation('');
    setRoutePreview(null);
    setRouteReceipt(null);
    setStaticAssets([{url: '', content_type: 'application/javascript'}]);
    setStaticConfirmation('');
    setStaticPreview(null);
    setStaticReceipt(null);
    setReadonlyDataUrls(['']);
    setReadonlyDataConfirmation('');
    setReadonlyDataPreview(null);
    setReadonlyDataReceipt(null);
  }

  async function inspect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !taskFieldsValid || busy) return;
    setBusy(true);
    reset();
    try {
      const result = await api<TaskDraftPreview>('/api/web-applications/task-preview', {
        profile_sha256: profileSha256, task_key: taskKey, verification_ref: verificationRef,
        route_count: routeCount
      });
      if (result.status !== 'unregistered_draft' || result.profile_sha256 !== profileSha256
          || result.task_key !== taskKey || result.route_count !== routeCount
          || !/^[a-f0-9]{64}$/.test(result.task_sha256)
          || result.execution_authorized !== false || result.collection_authorized !== false) {
        throw new Error('Unexpected task draft preview');
      }
      setPreview(result);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function register() {
    if (!preview || confirmation !== preview.task_sha256 || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await api<TaskDraftReceipt>('/api/web-applications/task-register', {
        profile_sha256: preview.profile_sha256, task_key: preview.task_key,
        verification_ref: verificationRef, route_count: preview.route_count,
        confirm_sha256: confirmation
      });
      if (result.status !== 'private_unactivated_draft'
          || result.task_sha256 !== preview.task_sha256
          || result.profile_sha256 !== preview.profile_sha256
          || result.route_count !== preview.route_count
          || !/^data\/[a-z0-9_/-]+\/[a-f0-9]{64}\.json$/.test(result.task_file)
          || result.execution_authorized !== false || result.collection_authorized !== false) {
        throw new Error('Unexpected task draft receipt');
      }
      setReceipt(result);
      setPreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  function enteredRoutes(): string[] {
    const routes = routesText.replace(/\r/g, '').split('\n');
    if (routes.at(-1) === '') routes.pop();
    return routes;
  }

  async function inspectRoutes(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!receipt || busy) return;
    const routes = enteredRoutes();
    if (routes.length !== receipt.route_count) return;
    setBusy(true);
    setRoutePreview(null);
    setRouteReceipt(null);
    setRouteConfirmation('');
    setFailure(null);
    try {
      const report = await api<RouteDraftPreview>('/api/web-applications/routes-preview', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256, routes
      });
      if (report.status !== 'unregistered_draft' || report.profile_sha256 !== receipt.profile_sha256
          || report.task_sha256 !== receipt.task_sha256 || report.route_count !== routes.length
          || !/^[a-f0-9]{64}$/.test(report.plan_sha256)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected route draft preview');
      }
      setRoutePreview({report, routes});
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerRoutes() {
    if (!routePreview || !receipt || routeConfirmation !== routePreview.report.plan_sha256 || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await api<RouteDraftReceipt>('/api/web-applications/routes-register', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256,
        routes: routePreview.routes, confirm_sha256: routeConfirmation
      });
      if (result.status !== 'private_unactivated_draft'
          || result.plan_sha256 !== routePreview.report.plan_sha256
          || result.task_sha256 !== receipt.task_sha256
          || result.profile_sha256 !== receipt.profile_sha256
          || result.route_count !== routePreview.routes.length
          || !/^data\/[a-z0-9_/-]+\/[a-f0-9]{64}\.json$/.test(result.route_plan_file)
          || result.execution_authorized !== false || result.collection_authorized !== false) {
        throw new Error('Unexpected route draft receipt');
      }
      setRouteReceipt(result);
      setRoutePreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  function editStaticAssets(assets: StaticAsset[]) {
    setStaticAssets(assets);
    setStaticConfirmation('');
    setStaticPreview(null);
    setStaticReceipt(null);
    setReadonlyDataConfirmation('');
    setReadonlyDataPreview(null);
    setReadonlyDataReceipt(null);
    setFailure(null);
  }

  function editReadonlyDataUrls(urls: string[]) {
    setReadonlyDataUrls(urls);
    setReadonlyDataConfirmation('');
    setReadonlyDataPreview(null);
    setReadonlyDataReceipt(null);
    setFailure(null);
  }

  async function inspectReadonlyData(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!receipt || receipt.route_count !== 1 || busy
        || staticAssets.some(asset => !asset.url) || staticQueryBlocked || readonlyDataUrls.some(url => !url)) return;
    const assets = staticAssets.map(asset => ({...asset}));
    const dataResources = readonlyDataUrls.map(url => ({url}));
    setBusy(true);
    setReadonlyDataPreview(null);
    setReadonlyDataReceipt(null);
    setReadonlyDataConfirmation('');
    setFailure(null);
    try {
      const report = await api<ReadonlyDataDraftPreview>('/api/web-applications/readonly-data-preview', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256,
        assets, data_resources: dataResources
      });
      if (report.status !== 'unregistered_draft' || report.profile_sha256 !== receipt.profile_sha256
          || report.task_sha256 !== receipt.task_sha256 || report.asset_count !== assets.length
          || report.data_count !== dataResources.length || !/^[a-f0-9]{64}$/.test(report.plan_sha256)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected read-only data draft preview');
      }
      setReadonlyDataPreview({report, assets, dataResources});
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerReadonlyData() {
    if (!receipt || !readonlyDataPreview
        || (staticQueryCapability !== 'supported' && readonlyDataPreview.assets.some(asset => asset.url.includes('?')))
        || readonlyDataConfirmation !== readonlyDataPreview.report.plan_sha256 || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await api<ReadonlyDataDraftReceipt>('/api/web-applications/readonly-data-register', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256,
        assets: readonlyDataPreview.assets, data_resources: readonlyDataPreview.dataResources,
        confirm_sha256: readonlyDataConfirmation
      });
      if (result.status !== 'private_unactivated_draft'
          || result.plan_sha256 !== readonlyDataPreview.report.plan_sha256
          || result.profile_sha256 !== receipt.profile_sha256
          || result.task_sha256 !== receipt.task_sha256
          || result.asset_count !== readonlyDataPreview.assets.length
          || result.data_count !== readonlyDataPreview.dataResources.length
          || !/^data\/[a-z0-9_/-]+\/[a-f0-9]{64}\.json$/.test(result.readonly_data_plan_file)
          || result.execution_authorized !== false || result.collection_authorized !== false) {
        throw new Error('Unexpected read-only data draft receipt');
      }
      setReadonlyDataReceipt(result);
      setReadonlyDataPreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  async function inspectStaticAssets(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!receipt || receipt.route_count !== 1 || busy || staticAssets.some(asset => !asset.url)
        || staticQueryBlocked) return;
    const assets = staticAssets.map(asset => ({...asset}));
    setBusy(true);
    setStaticPreview(null);
    setStaticReceipt(null);
    setStaticConfirmation('');
    setFailure(null);
    try {
      const report = await api<StaticDraftPreview>('/api/web-applications/static-assets-preview', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256, assets
      });
      if (report.status !== 'unregistered_draft' || report.profile_sha256 !== receipt.profile_sha256
          || report.task_sha256 !== receipt.task_sha256 || report.asset_count !== assets.length
          || !/^[a-f0-9]{64}$/.test(report.plan_sha256)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected static asset draft preview');
      }
      setStaticPreview({report, assets});
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerStaticAssets() {
    if (!receipt || !staticPreview || staticConfirmation !== staticPreview.report.plan_sha256 || busy
        || (staticQueryCapability !== 'supported' && staticPreview.assets.some(asset => asset.url.includes('?')))) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await api<StaticDraftReceipt>('/api/web-applications/static-assets-register', {
        profile_sha256: receipt.profile_sha256, task_sha256: receipt.task_sha256,
        assets: staticPreview.assets, confirm_sha256: staticConfirmation
      });
      if (result.status !== 'private_unactivated_draft'
          || result.plan_sha256 !== staticPreview.report.plan_sha256
          || result.profile_sha256 !== receipt.profile_sha256
          || result.task_sha256 !== receipt.task_sha256
          || result.asset_count !== staticPreview.assets.length
          || !/^data\/[a-z0-9_/-]+\/[a-f0-9]{64}\.json$/.test(result.static_plan_file)
          || result.execution_authorized !== false || result.collection_authorized !== false) {
        throw new Error('Unexpected static asset draft receipt');
      }
      setStaticReceipt(result);
      setStaticPreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  return <section data-testid="web-task-draft">
    <h3>{t('Özel uzak giriş görev taslağı')}</h3>
    <p className="caption">{t('Kayıtlı staging/production profili için bir giriş veya sıralı HTTPS rota taslağı hazırlanır. Önizleme ağ isteği veya dosya yazımı yapmaz; kayıt çalışan oturumu değiştirmez.')}</p>
    <form onSubmit={event => void inspect(event)}>
      <div className="web-profile-grid">
        <label>{t('Görev profili')}<select value={profileSha256} disabled={busy}
          onChange={event => {setProfileSha256(event.target.value); reset();}}>
          <option value="">{t('Profil seçin')}</option>
          {inventory.map(profile => <option key={profile.profile_sha256} value={profile.profile_sha256}>
            {profile.application_key} · v{profile.revision} · {profile.profile_sha256}
          </option>)}
        </select></label>
        <label>{t('Profildeki görev anahtarı')}<input value={taskKey} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setTaskKey(event.target.value); reset();}}/></label>
        <label>{t('Bağımsız doğrulayıcı referansı')}<input value={verificationRef} autoComplete="off"
          maxLength={64} disabled={busy} onChange={event => {setVerificationRef(event.target.value); reset();}}/></label>
        <label>{t('Planlanan sayfa sayısı')}<select value={routeCount} disabled={busy}
          onChange={event => {setRouteCount(Number(event.target.value)); reset();}}>
          {Array.from({length: 8}, (_, index) => index + 1).map(count =>
            <option key={count} value={count}>{count}</option>)}
        </select></label>
      </div>
      <button type="submit" disabled={!selected || !taskFieldsValid || busy}>{t('Görevi önizle')}</button>
    </form>
    {preview ? <div data-testid="web-task-preview">
      <p>{t('Taslak SHA-256:')} <code>{preview.task_sha256}</code></p>
      <p className="caption">{t('Her planlı sayfa için taze eylem onayı gerekir. Ağ/toplama/yürütme yetkisi verilmedi.')}</p>
      <label>{t('Exact görev SHA-256 onayı')}<input value={confirmation} maxLength={64}
        autoComplete="off" disabled={busy} onChange={event => setConfirmation(event.target.value)}/></label>
      <button type="button" disabled={busy || confirmation !== preview.task_sha256}
        onClick={() => void register()}>{t('Özel görev dosyasını kaydet')}</button>
    </div> : null}
    {receipt ? <><p role="status" data-testid="web-task-registered">
      {t('Özel taslak kaydedildi:')} <code>{receipt.task_file}</code> · SHA-256 <code>{receipt.task_sha256}</code>
      <span className="caption"> {t('Yeni managed oturumda ayrıca exact profil ve dosya pinleri gerekir; mevcut oturum değişmedi.')}</span>
    </p>{receipt.route_count === 1 ? <ManagedCommands task={receipt}/> : null}</> : null}
    {receipt && receipt.route_count === 1 ? <form data-testid="web-static-draft"
      onSubmit={event => void inspectStaticAssets(event)}>
      <h3>{t('Exact statik JS/CSS/görsel planı')}</h3>
      <p className="caption">{t('Yalnız aynı-origin canonical HTTPS JS/CSS/görsel URL’leri (en çok 8). Sabit sorguya sır veya kişisel veri koymayın. Önizleme ağ açmaz; kayıt yürütme izni vermez.')}</p>
      {staticQueryBlocked ? <p className="caption" data-testid="web-static-query-capability-unavailable">{t('Bu backend statik varlık sorgusunu doğrulamıyor. Sorgusuz URL kullanın veya güvenli yeni backend oturumu açıp sayfayı yenileyin.')}</p> : null}
      {imageDraftCapability === 'unconfirmed' ? <p className="caption" data-testid="web-image-capability-unavailable">{t('Görsel planı için güncel backend yeteneği doğrulanamadı; JS/CSS kullanılabilir. Backend güncellendikten sonra sayfayı yenileyin.')}</p> : null}
      {staticAssets.map((asset, index) => <div className="web-profile-grid" key={index}>
        <label>{t('Statik varlık URL’si')} {index + 1}<input type="url" value={asset.url} maxLength={2048}
          disabled={busy} onChange={event => editStaticAssets(staticAssets.map((item, position) =>
            position === index ? {...item, url: event.target.value} : item))}/></label>
        <label>{t('İçerik türü')} {index + 1}<select value={asset.content_type} disabled={busy}
          onChange={event => editStaticAssets(staticAssets.map((item, position) =>
            position === index ? {...item, content_type: event.target.value as StaticAsset['content_type']} : item))}>
          <option value="application/javascript">application/javascript</option>
          <option value="text/javascript">text/javascript</option>
          <option value="text/css">text/css</option>
          {imageDraftsAvailable ? imageTypes.map(type => <option key={type} value={type}>{type}</option>) : null}
        </select></label>
        <button type="button" disabled={busy || staticAssets.length === 1}
          onClick={() => editStaticAssets(staticAssets.filter((_, position) => position !== index))}>{t('Varlığı kaldır')}</button>
      </div>)}
      <button type="button" disabled={busy || staticAssets.length === 8}
        onClick={() => editStaticAssets([...staticAssets, {url: '', content_type: 'application/javascript'}])}>{t('Varlık ekle')}</button>
      <button type="submit" disabled={busy || staticQueryBlocked || staticAssets.some(asset => !asset.url)}>{t('Statik planı önizle')}</button>
      {staticPreview ? <div data-testid="web-static-preview">
        <p>{t('Plan SHA-256:')} <code>{staticPreview.report.plan_sha256}</code></p>
        <label>{t('Exact plan SHA-256 onayı')}<input value={staticConfirmation} maxLength={64}
          autoComplete="off" disabled={busy} onChange={event => setStaticConfirmation(event.target.value)}/></label>
        <button type="button" disabled={busy || staticQueryBlocked || staticConfirmation !== staticPreview.report.plan_sha256}
          onClick={() => void registerStaticAssets()}>{t('Özel statik planı kaydet')}</button>
      </div> : null}
      {staticReceipt ? <><p role="status" data-testid="web-static-registered">
        {t('Özel statik plan kaydedildi:')} <code>{staticReceipt.static_plan_file}</code>
        {' · '}SHA-256 <code>{staticReceipt.plan_sha256}</code>
      </p><ManagedCommands task={receipt} staticAssets={staticReceipt}/></> : null}
    </form> : null}
    {receipt && receipt.route_count === 1 ? <form data-testid="web-readonly-data-draft"
      onSubmit={event => void inspectReadonlyData(event)}>
      <h3>{t('Exact salt okunur JSON GET planı')}</h3>
      <p className="caption">{t('Yukarıdaki JS/CSS/görsel varlıklarını ve en çok dört aynı-origin JSON GET URL’sini kullanır. Varlık ve JSON URL’lerinde sabit canonical sorgu olabilir; sorguya sır veya kişisel veri koymayın. Önizleme ağ açmaz; kayıt görev onayı değildir.')}</p>
      {readonlyDataUrls.map((url, index) => <div className="web-profile-grid" key={index}>
        <label>{t('JSON GET URL’si')} {index + 1}<input type="url" value={url} maxLength={2048}
          disabled={busy} onChange={event => editReadonlyDataUrls(readonlyDataUrls.map((item, position) =>
            position === index ? event.target.value : item))}/></label>
        <button type="button" disabled={busy || readonlyDataUrls.length === 1}
          onClick={() => editReadonlyDataUrls(readonlyDataUrls.filter((_, position) => position !== index))}>{t('JSON URL’sini kaldır')}</button>
      </div>)}
      <button type="button" disabled={busy || readonlyDataUrls.length === 4}
        onClick={() => editReadonlyDataUrls([...readonlyDataUrls, ''])}>{t('JSON URL’si ekle')}</button>
      <button type="submit" disabled={busy || staticQueryBlocked || staticAssets.some(asset => !asset.url)
        || readonlyDataUrls.some(url => !url)}>{t('JSON planını önizle')}</button>
      {readonlyDataPreview ? <div data-testid="web-readonly-data-preview">
        <p>{t('Plan SHA-256:')} <code>{readonlyDataPreview.report.plan_sha256}</code></p>
        <label>{t('Exact plan SHA-256 onayı')}<input value={readonlyDataConfirmation} maxLength={64}
          autoComplete="off" disabled={busy} onChange={event => setReadonlyDataConfirmation(event.target.value)}/></label>
        <button type="button" disabled={busy || staticQueryBlocked || readonlyDataConfirmation !== readonlyDataPreview.report.plan_sha256}
          onClick={() => void registerReadonlyData()}>{t('Özel JSON planını kaydet')}</button>
      </div> : null}
      {readonlyDataReceipt ? <><p role="status" data-testid="web-readonly-data-registered">
        {t('Özel JSON planı kaydedildi:')} <code>{readonlyDataReceipt.readonly_data_plan_file}</code>
        {' · '}SHA-256 <code>{readonlyDataReceipt.plan_sha256}</code>
      </p><ManagedCommands task={receipt} readonlyData={readonlyDataReceipt}/></> : null}
    </form> : null}
    {receipt && receipt.route_count >= 2 ? <form data-testid="web-route-draft"
      onSubmit={event => void inspectRoutes(event)}>
      <h3>{t('Sıralı HTTPS rota planı')}</h3>
      <p className="caption">{t('İlk satır profilin exact sorgusuz giriş URL’si olmalı; sonraki rotalarda aynı origin’de sabit canonical sorgu olabilir. Sorguya sır veya kişisel veri koymayın. Önizleme siteye gitmez.')}</p>
      <label>{t('Planlı rotalar · satır başına bir URL')}<textarea rows={receipt.route_count}
        value={routesText} maxLength={16384} disabled={busy}
        onChange={event => {setRoutesText(event.target.value); setRoutePreview(null);
          setRouteReceipt(null); setRouteConfirmation(''); setFailure(null);}}/></label>
      <button type="submit" disabled={busy || enteredRoutes().length !== receipt.route_count}>
        {t('Rota planını önizle')}</button>
      {routePreview ? <div data-testid="web-route-preview">
        <p>{t('Plan SHA-256:')} <code>{routePreview.report.plan_sha256}</code></p>
        <label>{t('Exact plan SHA-256 onayı')}<input value={routeConfirmation} maxLength={64}
          autoComplete="off" disabled={busy} onChange={event => setRouteConfirmation(event.target.value)}/></label>
        <button type="button" disabled={busy || routeConfirmation !== routePreview.report.plan_sha256}
          onClick={() => void registerRoutes()}>{t('Özel rota planını kaydet')}</button>
      </div> : null}
      {routeReceipt ? <p role="status" data-testid="web-route-registered">
        {t('Özel rota planı kaydedildi:')} <code>{routeReceipt.route_plan_file}</code>
        {' · '}SHA-256 <code>{routeReceipt.plan_sha256}</code>
      </p> : null}
      {routeReceipt ? <ManagedCommands task={receipt} route={routeReceipt}/> : null}
    </form> : null}
    {failure === 'unsupported' ? <p role="status">{t('Bu sunucu sürümünde görev taslağı API’si yok; dosya yazılmadı.')}</p> : null}
    {failure === 'scope' ? <p role="status">{t('Profil, görev anahtarı veya doğrulayıcı kapsamı geçersiz; dosya yazılmadı.')}</p> : null}
    {failure === 'registration' ? <p role="status">{t('Görev kaydı tamamlanmadı; özel dosya durumunu inceleyin.')}</p> : null}
  </section>;
}
