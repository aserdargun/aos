import {useState, type FormEvent} from 'react';
import {api, ApiError, type HTTPSPreflightReport, type WebApplicationReport} from './api';
import {t} from './i18n';

const signalCounts = ['form_count', 'script_count', 'stylesheet_count', 'image_count',
  'password_input_count', 'cross_origin_resource_count', 'unclassified_resource_count'] as const;
const formSignalCounts = ['post_form_count', 'cross_origin_form_action_count',
  'unclassified_form_action_count'] as const;
const refreshSignalCounts = ['meta_refresh_count', 'cross_origin_meta_refresh_count',
  'unclassified_meta_refresh_count'] as const;

function validSignals(value: HTTPSPreflightReport['entry_html_signals'], version: '1.1' | '1.2' | '1.3'): boolean {
  if (value === null) return true;
  if (value === undefined || typeof value !== 'object'
      || value.static_html_only !== true || value.browser_or_account_verified !== false
      || Object.keys(value).length !== signalCounts.length + 2
        + (version !== '1.1' ? formSignalCounts.length : 0)
        + (version === '1.3' ? refreshSignalCounts.length : 0)) return false;
  if (!signalCounts.every(key => Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 65536)) return false;
  return version === '1.1' || (formSignalCounts.every(key => typeof value[key] === 'number'
    && Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 65536)
    && value.post_form_count! <= value.form_count
    && value.cross_origin_form_action_count! + value.unclassified_form_action_count! <= value.form_count
    && (version !== '1.3' || (refreshSignalCounts.every(key => typeof value[key] === 'number'
      && Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 65536)
      && value.cross_origin_meta_refresh_count! + value.unclassified_meta_refresh_count!
        <= value.meta_refresh_count!)));
}

export function HTTPSPreflight({inventory}: {inventory: WebApplicationReport[]}) {
  const [profileSha256, setProfileSha256] = useState('');
  const [entryUrl, setEntryUrl] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [authorizedGet, setAuthorizedGet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<'unsupported' | 'scope' | 'busy' | 'attempted' | null>(null);
  const [report, setReport] = useState<HTTPSPreflightReport | null>(null);
  const selected = inventory.some(profile => profile.profile_sha256 === profileSha256);
  const ready = selected && confirmation === profileSha256 && entryUrl.startsWith('https://')
    && authorizedGet && !busy && report === null;

  function changeProfile(value: string) {
    setProfileSha256(value);
    setEntryUrl('');
    setConfirmation('');
    setAuthorizedGet(false);
    setReport(null);
    setFailure(null);
  }

  async function probe(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setReport(null);
    setFailure(null);
    try {
      const result = await api<HTTPSPreflightReport>('/api/web-applications/preflight', {
        profile_sha256: profileSha256, confirm_sha256: confirmation,
        entry_url: entryUrl, authorized_get: true
      });
      if (result.status !== 'https_entry_reached' || result.profile_sha256 !== profileSha256
          || !['1.0', '1.1', '1.2', '1.3'].includes(result.schema_version)
          || (result.schema_version !== '1.0' && !validSignals(result.entry_html_signals, result.schema_version))
          || result.tls_hostname_verified !== true
          || !/^[a-f0-9]{64}$/.test(result.response_sha256)
          || !Number.isInteger(result.response_bytes) || result.response_bytes < 0
          || result.response_bytes > 65536 || result.browser_connected !== false
          || result.execution_authorized !== false || result.collection_authorized !== false
          || result.training_ready !== false) throw new Error('Unexpected HTTPS preflight report');
      setReport(result);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported'
        : error instanceof ApiError && [400, 422].includes(error.status) ? 'scope'
        : error instanceof ApiError && error.status === 429 ? 'busy' : 'attempted');
    } finally {
      setBusy(false);
    }
  }

  return <form data-testid="https-entry-preflight" onSubmit={event => void probe(event)}>
    <h3>{t('Tek HTTPS giriş kontrolü')}</h3>
    <p className="caption">{t('Yalnız yetkili staging/production hedefinde açıkça seçilen giriş URL’sine tek host GET yapılır. Tarayıcı, hesap, veri toplama veya görev izni değildir. Başarısız ağ denemesi de bu sunucu oturumundaki tek hakkı tüketebilir.')}</p>
    <div className="web-profile-grid">
      <label>{t('Kontrol profili')}<select aria-label={t('Kontrol profili')} value={profileSha256}
        disabled={busy} onChange={event => changeProfile(event.target.value)}>
        <option value="">{t('Profil seçin')}</option>
        {inventory.map(profile => <option key={profile.profile_sha256} value={profile.profile_sha256}>
          {profile.application_key} · v{profile.revision} · {profile.profile_sha256}
        </option>)}
      </select></label>
      <label>{t('Exact giriş URL’si')}<input type="url" aria-label={t('Exact giriş URL’si')}
        value={entryUrl} maxLength={2048} autoComplete="off" disabled={busy}
        onChange={event => {setEntryUrl(event.target.value); setReport(null); setFailure(null);}}/></label>
      <label className="web-profile-wide">{t('Profil SHA-256 onayı')}<input aria-label={t('Profil SHA-256 onayı')}
        value={confirmation} maxLength={64} autoComplete="off" disabled={busy}
        onChange={event => {setConfirmation(event.target.value); setReport(null); setFailure(null);}}/></label>
    </div>
    <label className="web-profile-confirm"><input type="checkbox" checked={authorizedGet} disabled={busy}
      onChange={event => {setAuthorizedGet(event.target.checked); setReport(null); setFailure(null);}}/>
      {t('Bu exact hedefe tek HTTPS GET yapmaya yetkiliyim; GET yan etkili olabilir.')}</label>
    <button type="submit" disabled={!ready}>{t('Tek HTTPS giriş isteğini yap')}</button>
    {report ? <p role="status" data-testid="https-entry-preflight-result">
      {t('HTTPS giriş yanıtı doğrulandı:')} {report.response_bytes} {t('bayt')} · SHA-256 <code>{report.response_sha256}</code>
      <span className="caption"> {t('Yalnız HTTPS taşıması; uygulama sonucu veya hesap doğrulanmadı.')}</span>
      {report.schema_version !== '1.0' && report.entry_html_signals ? <span className="caption" data-testid="https-entry-html-signals">
        {' '}{t('Statik HTML sinyalleri')}: {t('form')} {report.entry_html_signals.form_count},
        {' '}{t('betik')} {report.entry_html_signals.script_count},
        {' '}{t('stil')} {report.entry_html_signals.stylesheet_count},
        {' '}{t('görsel')} {report.entry_html_signals.image_count},
        {' '}{t('parola alanı')} {report.entry_html_signals.password_input_count},
        {' '}{t('başka origin kaynağı')} {report.entry_html_signals.cross_origin_resource_count},
        {' '}{t('sınıflandırılamayan kaynak')} {report.entry_html_signals.unclassified_resource_count}.
        {report.schema_version !== '1.1' ? <>{' '}{t('POST formu')} {report.entry_html_signals.post_form_count},
          {' '}{t('başka origin form eylemi')} {report.entry_html_signals.cross_origin_form_action_count},
          {' '}{t('sınıflandırılamayan form eylemi')} {report.entry_html_signals.unclassified_form_action_count}.</> : null}
        {report.schema_version === '1.3' ? <>{' '}{t('meta yenileme')} {report.entry_html_signals.meta_refresh_count},
          {' '}{t('başka origin meta yönlendirmesi')} {report.entry_html_signals.cross_origin_meta_refresh_count},
          {' '}{t('sınıflandırılamayan meta yönlendirmesi')} {report.entry_html_signals.unclassified_meta_refresh_count}.</> : null}
        {' '}{t('Yalnız ilk HTML yanıtı sayıldı; alt istekler, çalışan JavaScript, tarayıcı ve hesap doğrulanmadı.')}
      </span> : null}
      {report.schema_version !== '1.0' && report.entry_html_signals === null ? <span className="caption">
        {' '}{t('HTML sinyalleri okunamadı; yalnız HTTPS taşıması doğrulandı.')}
      </span> : null}
    </p> : null}
    {failure === 'attempted' ? <p role="status">{t('HTTPS giriş kontrolü kullanılamıyor; ağ denemesi yapılmış olabilir. Oturumu inceleyin.')}</p> : null}
    {failure === 'unsupported' ? <p role="status">{t('Bu sunucu sürümünde HTTPS giriş kontrolü yok; istek yapılmadı. Kontrollü sonraki başlangıçta kullanılabilir.')}</p> : null}
    {failure === 'scope' ? <p role="status">{t('Profil, URL veya onay kapsamı geçersiz; ağ isteği yapılmadı.')}</p> : null}
    {failure === 'busy' ? <p role="status">{t('Başka HTTPS kontrolü sürüyor; bu istek başlatılmadı.')}</p> : null}
  </form>;
}
