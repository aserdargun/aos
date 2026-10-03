import {useEffect, useRef, useState, type FormEvent} from 'react';
import {api, ApiError, type ImageDraftCapability, type StaticQueryCapability, type WebApplicationReport} from './api';
import {t} from './i18n';
import {HTTPSPreflight} from './HTTPSPreflight';
import {WebTaskDraft} from './WebTaskDraft';
import {WebFormDraft} from './WebFormDraft';
import './web_applications.css';

type Environment = 'local_test' | 'staging' | 'production';
type LearningRequest = 'disabled' | 'requested';

interface DraftFields {
  applicationKey: string;
  environment: Environment;
  tenantKey: string;
  accountRole: string;
  entryUrl: string;
  allowedOrigins: string;
  taskKeys: string;
  system1: LearningRequest;
  system2: LearningRequest;
  dataRightsRef: string;
  retentionDays: string;
}

interface DraftProfile {
  schema_version: '1.0';
  application_key: string;
  revision: number;
  previous_sha256: string | null;
  environment: Environment;
  tenant_key: string;
  account_role: string;
  entry_url: string;
  allowed_origins: string[];
  task_keys: string[];
  learning: {
    system1: LearningRequest;
    system2: LearningRequest;
    data_rights_ref: string | null;
    retention_days: number;
    raw_screenshots: false;
    automatic_training: false;
    automatic_promotion: false;
  };
}

const emptyFields: DraftFields = {
  applicationKey: '', environment: 'staging', tenantKey: '', accountRole: '',
  entryUrl: '', allowedOrigins: '', taskKeys: '', system1: 'disabled', system2: 'disabled',
  dataRightsRef: '', retentionDays: '30'
};

function lines(value: string): string[] {
  return value.split(/[\r\n,]+/).map(item => item.trim()).filter(Boolean);
}

function profileFrom(fields: DraftFields, parent: WebApplicationReport | null): DraftProfile {
  return {
    schema_version: '1.0', application_key: fields.applicationKey.trim(), revision: parent ? parent.revision + 1 : 1,
    previous_sha256: parent?.profile_sha256 ?? null, environment: fields.environment, tenant_key: fields.tenantKey.trim(),
    account_role: fields.accountRole.trim(), entry_url: fields.entryUrl.trim(),
    allowed_origins: lines(fields.allowedOrigins).sort(), task_keys: lines(fields.taskKeys).sort(),
    learning: {
      system1: fields.system1, system2: fields.system2,
      data_rights_ref: fields.dataRightsRef.trim() || null,
      retention_days: Number(fields.retentionDays), raw_screenshots: false,
      automatic_training: false, automatic_promotion: false
    }
  };
}

function isDraftReport(report: WebApplicationReport, profile: DraftProfile): boolean {
  return report.status === 'draft' && report.application_key === profile.application_key
    && report.revision === profile.revision && /^[a-f0-9]{64}$/.test(report.profile_sha256)
    && report.execution_authorized === false && report.collection_authorized === false
    && report.training_ready === false && report.promotion_authorized === false;
}

interface Props {
  managerScope?: {project: string; port: number};
  inventory: WebApplicationReport[] | null | 'unavailable';
  imageDraftCapability: ImageDraftCapability;
  staticQueryCapability: StaticQueryCapability;
  onRegistered: () => void;
  onError: (failure: unknown) => void;
}

export function WebApplicationDrafts({inventory, imageDraftCapability, staticQueryCapability, onRegistered, onError, managerScope}: Props) {
  const [fields, setFields] = useState<DraftFields>(emptyFields);
  const [parentSha, setParentSha] = useState('');
  const [preview, setPreview] = useState<{profile: DraftProfile; report: WebApplicationReport} | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [registered, setRegistered] = useState<WebApplicationReport | null>(null);
  const [pending, setPending] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);

  useEffect(() => () => request.current?.abort(), []);

  function change<Key extends keyof DraftFields>(key: Key, value: DraftFields[Key]) {
    request.current?.abort();
    generation.current += 1;
    setFields(current => ({...current, [key]: value}));
    setPreview(null);
    setConfirmed(false);
    setRegistered(null);
  }

  function selectParent(checksum: string) {
    request.current?.abort();
    generation.current += 1;
    setParentSha(checksum);
    const parent = Array.isArray(inventory) ? inventory.find(item => item.profile_sha256 === checksum) : null;
    setFields(current => ({...current, applicationKey: parent?.application_key ?? ''}));
    setPreview(null);
    setConfirmed(false);
    setRegistered(null);
  }

  async function inspect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const parent = parentSha && Array.isArray(inventory)
      ? inventory.find(item => item.profile_sha256 === parentSha && item.revision < 100) : null;
    if (parentSha && !parent) return;
    const profile = profileFrom(fields, parent || null);
    if ((profile.learning.system1 === 'requested' || profile.learning.system2 === 'requested')
        && profile.learning.data_rights_ref === null) return;
    request.current?.abort();
    const abort = new AbortController();
    request.current = abort;
    const currentGeneration = ++generation.current;
    setPreview(null);
    setConfirmed(false);
    setRegistered(null);
    setUnavailable(false);
    setPending(true);
    try {
      const report = await api<WebApplicationReport>('/api/web-applications/preview', {profile}, abort.signal);
      if (abort.signal.aborted || generation.current !== currentGeneration) return;
      if (!isDraftReport(report, profile)) throw new Error(t('Sunucu beklenen taslak raporunu döndürmedi.'));
      setPreview({profile, report});
    } catch (failure) {
      if (abort.signal.aborted || generation.current !== currentGeneration) return;
      if (failure instanceof ApiError && failure.status === 404) setUnavailable(true);
      else onError(failure);
    } finally {
      if (generation.current === currentGeneration) setPending(false);
    }
  }

  async function register() {
    if (!preview || !confirmed || pending) return;
    const selected = preview;
    const abort = new AbortController();
    request.current = abort;
    setPending(true);
    setUnavailable(false);
    try {
      const report = await api<WebApplicationReport>('/api/web-applications/register', {
        profile: selected.profile, confirm_sha256: selected.report.profile_sha256
      }, abort.signal);
      if (abort.signal.aborted) return;
      if (!isDraftReport(report, selected.profile)
          || report.profile_sha256 !== selected.report.profile_sha256) {
        throw new Error(t('Kayıt raporu önizleme parmak iziyle eşleşmedi.'));
      }
      setRegistered(report);
      setPreview(null);
      setConfirmed(false);
      onRegistered();
    } catch (failure) {
      if (abort.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 404) setUnavailable(true);
      else onError(failure);
    } finally {
      if (!abort.signal.aborted) setPending(false);
    }
  }

  const rightsMissing = (fields.system1 === 'requested' || fields.system2 === 'requested')
    && !fields.dataRightsRef.trim();

  return <section className="panel web-applications" data-testid="web-applications">
    <h2>{t('Web uygulaması profil taslakları')}</h2>
    <p className="caption">{t('Bu kayıtlar yalnız taslaktır. Siteye erişim, görev yürütme, veri toplama veya eğitim izni vermez.')}</p>
    <form onSubmit={event => void inspect(event)}>
      <h3>{t('Yeni profil taslağı')}</h3>
      <p className="caption">{t('Yalnız kapsam bilgisini girin. Parola, oturum çerezi, token veya özel veri girmeyin. Ardıl sürüm için exact ebeveyn taslağını seçin; yeni sürüm çalışan oturumu veya eski görevleri değiştirmez.')}</p>
      <div className="web-profile-grid">
        <label>{t('Önceki profil · isteğe bağlı')}<select value={parentSha} disabled={pending} onChange={event => selectParent(event.target.value)}><option value="">{t('Yeni uygulama · sürüm 1')}</option>{Array.isArray(inventory) ? inventory.filter(item => item.revision < 100).map(item => <option key={item.profile_sha256} value={item.profile_sha256}>{item.application_key} · v{item.revision} · {item.profile_sha256}</option>) : null}</select></label>
        <label>{t('Uygulama anahtarı')}<input required pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} autoComplete="off" value={fields.applicationKey} disabled={pending || Boolean(parentSha)} onChange={event => change('applicationKey', event.target.value)}/></label>
        <label>{t('Ortam')}<select value={fields.environment} disabled={pending} onChange={event => change('environment', event.target.value as Environment)}><option value="local_test">local_test</option><option value="staging">staging</option><option value="production">production</option></select></label>
        <label>{t('Tenant anahtarı')}<input required pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} autoComplete="off" value={fields.tenantKey} disabled={pending} onChange={event => change('tenantKey', event.target.value)}/></label>
        <label>{t('Hesap rolü')}<input required pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} autoComplete="off" value={fields.accountRole} disabled={pending} onChange={event => change('accountRole', event.target.value)}/></label>
        <label className="web-profile-wide">{t('Giriş URL’si')}<input required type="url" maxLength={2048} autoComplete="off" placeholder="https://example.org/app/" value={fields.entryUrl} disabled={pending} onChange={event => change('entryUrl', event.target.value)}/></label>
        <label>{t('İzin verilen origin’ler · satır başına bir tane')}<textarea required rows={3} maxLength={4800} value={fields.allowedOrigins} disabled={pending} onChange={event => change('allowedOrigins', event.target.value)}/></label>
        <label>{t('Görev anahtarları · satır başına bir tane')}<textarea required rows={3} maxLength={2048} value={fields.taskKeys} disabled={pending} onChange={event => change('taskKeys', event.target.value)}/></label>
      </div>
      <fieldset><legend>{t('Öğrenme talepleri · yetki değil')}</legend>
        <div className="web-profile-grid">
          <label>Decider / System-1<select value={fields.system1} disabled={pending} onChange={event => change('system1', event.target.value as LearningRequest)}><option value="disabled">{t('Kapalı')}</option><option value="requested">{t('Talep edildi')}</option></select></label>
          <label>Bonsai / System-2<select value={fields.system2} disabled={pending} onChange={event => change('system2', event.target.value as LearningRequest)}><option value="disabled">{t('Kapalı')}</option><option value="requested">{t('Talep edildi')}</option></select></label>
          <label>{t('Veri hakları referansı')}<input pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} autoComplete="off" required={rightsMissing} value={fields.dataRightsRef} disabled={pending} onChange={event => change('dataRightsRef', event.target.value)}/></label>
          <label>{t('Saklama süresi · gün')}<input required type="number" min={1} max={365} step={1} value={fields.retentionDays} disabled={pending} onChange={event => change('retentionDays', event.target.value)}/></label>
        </div>
      </fieldset>
      {rightsMissing ? <p className="caption" role="status">{t('Öğrenme talebi için veri hakları referansı gerekir; referans hak incelemesi değildir.')}</p> : null}
      <button type="submit" disabled={pending || rightsMissing || Boolean(parentSha && !Array.isArray(inventory))}>{t('Taslağı önizle')}</button>
    </form>
    {unavailable || inventory === 'unavailable' ? <p role="status">{t('Profil API’si bu sunucu oturumunda yok; sonraki başlangıçta kullanılabilir. Kayıt yapılmadı.')}</p> : null}
    {preview ? <div className="web-profile-preview" data-testid="web-profile-preview">
      <h3>{t('Önizleme · kayıt yapılmadı')}</h3>
      <p><code>{preview.report.profile_sha256}</code></p>
      <p>{t('Sürüm')} {preview.profile.revision} · {t('Önceki profil:')} <code>{preview.profile.previous_sha256 ?? t('Yok')}</code></p>
      <p>{t('İstenen öğrenme rolleri:')} {preview.report.requested_learning_roles.length ? preview.report.requested_learning_roles.join(', ') : t('Yok')}</p>
      <p className="caption">{t('Açık kapılar:')} {preview.report.blockers.map(blocker => t(blocker)).join(' · ')}</p>
      <label className="web-profile-confirm"><input type="checkbox" checked={confirmed} disabled={pending} onChange={event => setConfirmed(event.target.checked)}/>{t('Bu parmak izine ait taslağı kaydetmek istiyorum; yürütme veya öğrenme yetkisi vermiyorum.')}</label>
      <button type="button" disabled={pending || !confirmed} onClick={() => void register()}>{t('Önizlenen taslağı kaydet')}</button>
    </div> : null}
    {registered ? <p role="status" data-testid="web-profile-registered">{t('Taslak kaydedildi:')} <code>{registered.profile_sha256}</code></p> : null}
    <h3>{t('Kayıtlı taslaklar')}</h3>
    {inventory === null ? <p role="status">{t('Profil envanteri yükleniyor…')}</p>
      : inventory === 'unavailable' ? null
      : inventory.length === 0 ? <p>{t('Henüz web uygulaması profili yok.')}</p>
      : inventory.map(profile => <article className="registry" key={profile.profile_sha256}>
        <h3>{profile.application_key} · {t('Sürüm')} {profile.revision} · {t('Taslak')}</h3>
        <p><code>{profile.profile_sha256}</code></p>
        <p>{t('İstenen öğrenme rolleri:')} {profile.requested_learning_roles.length ? profile.requested_learning_roles.join(', ') : t('Yok')}</p>
        <p className="caption">{t('Açık kapılar:')} {profile.blockers.map(blocker => t(blocker)).join(' · ')}</p>
      </article>)}
    {Array.isArray(inventory) ? <HTTPSPreflight inventory={inventory}/> : null}
    {Array.isArray(inventory) ? <WebTaskDraft inventory={inventory} imageDraftCapability={imageDraftCapability} staticQueryCapability={staticQueryCapability}/> : null}
    <WebFormDraft inventory={Array.isArray(inventory) ? inventory : []} managerScope={managerScope}/>
  </section>;
}
