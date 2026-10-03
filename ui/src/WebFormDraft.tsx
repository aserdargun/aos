import {useState, type FormEvent} from 'react';
import {api, ApiError, type WebApplicationReport} from './api';
import {t} from './i18n';

type DraftStatus = 'unregistered_draft' | 'private_unactivated_draft';
interface FormTask {
  task_sha256: string; profile_sha256: string; task_key: string;
  state_readback: boolean;
  status: DraftStatus; execution_authorized: false; collection_authorized: false;
  task_file?: string;
}
interface FormPlan {
  plan_sha256: string; profile_sha256: string; task_sha256: string;
  field_name: string | null; field_names: string[]; body_sha256: string; body_bytes: number;
  public_grant_required: boolean; status: DraftStatus;
  execution_authorized: false; collection_authorized: false;
  plan_file?: string; value_file?: string; fields_file?: string;
}

interface OrderedField {name: string; value: string}
interface FormState {
  state_plan_sha256: string; profile_sha256: string; task_sha256: string;
  form_plan_sha256: string; state_url: string; marker_id: string | null;
  submitted_field_name: string | null;
  status: DraftStatus; execution_authorized: false; collection_authorized: false;
  state_plan_file?: string;
}

const checksum = /^[a-f0-9]{64}$/;
const privateFile = /^data\/[a-z0-9_/-]+\/[a-f0-9]{64}\.(json|txt)$/;

export function WebFormDraft({inventory, managerScope}: {inventory: WebApplicationReport[];
  managerScope?: {project: string; port: number}}) {
  const [profileSha256, setProfileSha256] = useState('');
  const [taskKey, setTaskKey] = useState('');
  const [verificationRef, setVerificationRef] = useState('');
  const [stateReadback, setStateReadback] = useState(false);
  const [taskPreview, setTaskPreview] = useState<FormTask | null>(null);
  const [taskConfirmation, setTaskConfirmation] = useState('');
  const [taskReceipt, setTaskReceipt] = useState<FormTask | null>(null);
  const [submitUrl, setSubmitUrl] = useState('');
  const [receiptUrl, setReceiptUrl] = useState('');
  const [fieldMode, setFieldMode] = useState<'single' | 'multiple'>('single');
  const [fieldName, setFieldName] = useState('');
  const [value, setValue] = useState('');
  const [fields, setFields] = useState<OrderedField[]>([{name: '', value: ''}, {name: '', value: ''}]);
  const [attested, setAttested] = useState(false);
  const [planPreview, setPlanPreview] = useState<FormPlan | null>(null);
  const [planConfirmation, setPlanConfirmation] = useState('');
  const [planReceipt, setPlanReceipt] = useState<FormPlan | null>(null);
  const [stateUrl, setStateUrl] = useState('');
  const [beforeSha256, setBeforeSha256] = useState('');
  const [afterSha256, setAfterSha256] = useState('');
  const [markerEnabled, setMarkerEnabled] = useState(false);
  const [markerId, setMarkerId] = useState('');
  const [beforeMarkerSha256, setBeforeMarkerSha256] = useState('');
  const [afterMarkerSha256, setAfterMarkerSha256] = useState('');
  const [submittedFieldName, setSubmittedFieldName] = useState('');
  const [statePreview, setStatePreview] = useState<FormState | null>(null);
  const [stateConfirmation, setStateConfirmation] = useState('');
  const [stateReceipt, setStateReceipt] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<'unsupported' | 'scope' | 'registration' | null>(null);

  function resetPlan() {
    setPlanPreview(null); setPlanReceipt(null); setPlanConfirmation(''); resetState();
  }
  function resetState() {
    setStatePreview(null); setStateReceipt(null); setStateConfirmation(''); setFailure(null);
  }
  function resetTask() {
    setTaskPreview(null); setTaskReceipt(null); setTaskConfirmation(''); resetPlan();
  }
  const selected = inventory.some(profile => profile.profile_sha256 === profileSha256);
  const taskValid = selected && /^[a-z][a-z0-9_-]{0,63}$/.test(taskKey)
    && /^[a-z][a-z0-9_-]{0,63}$/.test(verificationRef);
  const fieldNames = fieldMode === 'single' ? [fieldName] : fields.map(field => field.name);
  const fieldsValid = fieldMode === 'single'
    ? /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(fieldName)
      && value.length > 0 && value.length <= 2048 && !/[\x00-\x1f\x7f]/.test(value)
    : fields.length >= 2 && fields.length <= 8
      && new Set(fieldNames).size === fieldNames.length
      && fields.every(field => /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(field.name)
        && field.value.length > 0 && field.value.length <= 2048
        && !/[\x00-\x1f\x7f]/.test(field.value));
  const planValid = !!taskReceipt && !!submitUrl && !!receiptUrl
    && fieldsValid && attested;
  const formInput = fieldMode === 'single' ? {field_name: fieldName, value}
    : {fields: fields.map(field => ({...field}))};
  const stateValid = !!taskReceipt?.state_readback && !!planReceipt && !!stateUrl
    && checksum.test(beforeSha256) && checksum.test(afterSha256)
    && beforeSha256 !== afterSha256
    && (!markerEnabled || /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(markerId)
      && checksum.test(beforeMarkerSha256) && checksum.test(afterMarkerSha256)
      && beforeMarkerSha256 !== afterMarkerSha256)
    && (!submittedFieldName || markerEnabled && fieldNames.includes(submittedFieldName));
  const stateInput = {
    state_url: stateUrl, before_sha256: beforeSha256, after_sha256: afterSha256,
    ...(markerEnabled ? {marker_id: markerId,
      before_marker_sha256: beforeMarkerSha256,
      after_marker_sha256: afterMarkerSha256,
      ...(submittedFieldName ? {submitted_field_name: submittedFieldName} : {})} : {})
  };

  function editField(index: number, patch: Partial<OrderedField>) {
    setFields(current => current.map((field, fieldIndex) =>
      fieldIndex === index ? {...field, ...patch} : field));
    resetPlan();
  }

  async function inspectTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!taskValid || busy) return;
    setBusy(true); resetTask();
    try {
      const report = await api<FormTask>('/api/web-applications/form-task-preview', {
        profile_sha256: profileSha256, task_key: taskKey,
        verification_ref: verificationRef, state_readback: stateReadback
      });
      if (report.status !== 'unregistered_draft' || report.profile_sha256 !== profileSha256
          || report.task_key !== taskKey || !checksum.test(report.task_sha256)
          || report.state_readback !== stateReadback
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form task preview');
      }
      setTaskPreview(report);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerTask() {
    if (!taskPreview || taskConfirmation !== taskPreview.task_sha256 || busy) return;
    setBusy(true); setFailure(null);
    try {
      const report = await api<FormTask>('/api/web-applications/form-task-register', {
        profile_sha256: taskPreview.profile_sha256, task_key: taskPreview.task_key,
        verification_ref: verificationRef, state_readback: stateReadback,
        confirm_sha256: taskConfirmation
      });
      if (report.status !== 'private_unactivated_draft'
          || report.task_sha256 !== taskPreview.task_sha256
          || report.profile_sha256 !== taskPreview.profile_sha256
          || report.state_readback !== taskPreview.state_readback
          || !report.task_file || !privateFile.test(report.task_file)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form task receipt');
      }
      setTaskReceipt(report); setTaskPreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  async function inspectPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!taskReceipt || !planValid || busy) return;
    setBusy(true); resetPlan();
    try {
      const report = await api<FormPlan>('/api/web-applications/form-plan-preview', {
        profile_sha256: taskReceipt.profile_sha256, task_sha256: taskReceipt.task_sha256,
        submit_url: submitUrl, receipt_url: receiptUrl,
        ...formInput, attest_non_secret: true
      });
      if (report.status !== 'unregistered_draft'
          || report.profile_sha256 !== taskReceipt.profile_sha256
          || report.task_sha256 !== taskReceipt.task_sha256
          || report.field_name !== (fieldMode === 'single' ? fieldName : null)
          || JSON.stringify(report.field_names) !== JSON.stringify(fieldNames)
          || !checksum.test(report.plan_sha256)
          || !checksum.test(report.body_sha256) || !Number.isInteger(report.body_bytes)
          || typeof report.public_grant_required !== 'boolean'
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form plan preview');
      }
      setPlanPreview(report);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerPlan() {
    if (!taskReceipt || !planPreview || !planValid
        || planConfirmation !== planPreview.plan_sha256 || busy) return;
    setBusy(true); setFailure(null);
    try {
      const report = await api<FormPlan>('/api/web-applications/form-plan-register', {
        profile_sha256: taskReceipt.profile_sha256, task_sha256: taskReceipt.task_sha256,
        submit_url: submitUrl, receipt_url: receiptUrl,
        ...formInput, attest_non_secret: true,
        confirm_sha256: planConfirmation
      });
      if (report.status !== 'private_unactivated_draft'
          || report.plan_sha256 !== planPreview.plan_sha256
          || report.profile_sha256 !== taskReceipt.profile_sha256
          || report.task_sha256 !== taskReceipt.task_sha256
          || report.field_name !== planPreview.field_name
          || JSON.stringify(report.field_names) !== JSON.stringify(planPreview.field_names)
          || report.body_sha256 !== planPreview.body_sha256
          || report.body_bytes !== planPreview.body_bytes
          || report.public_grant_required !== planPreview.public_grant_required
          || !report.plan_file || !privateFile.test(report.plan_file)
          || (fieldMode === 'single'
            ? !report.value_file || !privateFile.test(report.value_file) || !!report.fields_file
            : !report.fields_file || !privateFile.test(report.fields_file) || !!report.value_file)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form plan receipt');
      }
      setPlanReceipt(report); setPlanPreview(null); setValue('');
      setFields(current => current.map(field => ({...field, value: ''})));
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  async function inspectState(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!taskReceipt || !planReceipt || !stateValid || busy) return;
    setBusy(true); resetState();
    try {
      const report = await api<FormState>('/api/web-applications/form-state-preview', {
        profile_sha256: taskReceipt.profile_sha256,
        task_sha256: taskReceipt.task_sha256,
        form_plan_sha256: planReceipt.plan_sha256, ...stateInput
      });
      if (report.status !== 'unregistered_draft'
          || report.profile_sha256 !== taskReceipt.profile_sha256
          || report.task_sha256 !== taskReceipt.task_sha256
          || report.form_plan_sha256 !== planReceipt.plan_sha256
          || report.state_url !== stateUrl
          || report.marker_id !== (markerEnabled ? markerId : null)
          || report.submitted_field_name !== (markerEnabled ? submittedFieldName || null : null)
          || !checksum.test(report.state_plan_sha256)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form state preview');
      }
      setStatePreview(report);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'scope');
    } finally { setBusy(false); }
  }

  async function registerState() {
    if (!taskReceipt || !planReceipt || !statePreview || !stateValid
        || stateConfirmation !== statePreview.state_plan_sha256 || busy) return;
    setBusy(true); setFailure(null);
    try {
      const report = await api<FormState>('/api/web-applications/form-state-register', {
        profile_sha256: taskReceipt.profile_sha256,
        task_sha256: taskReceipt.task_sha256,
        form_plan_sha256: planReceipt.plan_sha256, ...stateInput,
        confirm_sha256: stateConfirmation
      });
      if (report.status !== 'private_unactivated_draft'
          || report.state_plan_sha256 !== statePreview.state_plan_sha256
          || report.profile_sha256 !== taskReceipt.profile_sha256
          || report.task_sha256 !== taskReceipt.task_sha256
          || report.form_plan_sha256 !== planReceipt.plan_sha256
          || report.state_url !== stateUrl
          || report.marker_id !== statePreview.marker_id
          || report.submitted_field_name !== statePreview.submitted_field_name
          || !report.state_plan_file || !privateFile.test(report.state_plan_file)
          || report.execution_authorized !== false || report.collection_authorized !== false) {
        throw new Error('Unexpected form state receipt');
      }
      setStateReceipt(report); setStatePreview(null);
    } catch (error) {
      setFailure(error instanceof ApiError && error.status === 404 ? 'unsupported' : 'registration');
    } finally { setBusy(false); }
  }

  const command = taskReceipt?.task_file && planReceipt?.plan_file
      && (planReceipt.fields_file || planReceipt.value_file && planReceipt.field_name)
      && (!taskReceipt.state_readback || stateReceipt?.state_plan_file)
    ? ['--remote-entry-profile-sha256', taskReceipt.profile_sha256,
      '--remote-entry-task-file', taskReceipt.task_file,
      '--remote-form-plan-file', planReceipt.plan_file,
      ...(planReceipt.fields_file
        ? ['--remote-form-fields-file', planReceipt.fields_file]
        : ['--remote-form-field-name', planReceipt.field_name!,
           '--remote-form-value-file', planReceipt.value_file!]),
      '--remote-form-public-plan-sha256', planReceipt.plan_sha256,
      ...(stateReceipt?.state_plan_file
        ? ['--remote-form-state-plan-file', stateReceipt.state_plan_file,
           '--remote-form-public-state-plan-sha256', stateReceipt.state_plan_sha256]
        : [])].join(' ') : null;

  return <section data-testid="web-form-draft">
    <h3>{t('Özel HTTPS form taslağı')}</h3>
    <p className="caption">{t('Tek veya 2–8 sıralı alanlı stateless form. Önizleme ağsızdır; kayıt mevcut oturumu değiştirmez. Parola, token, çerez veya müşteri verisi girmeyin.')}</p>
    <form onSubmit={event => void inspectTask(event)}>
      <div className="web-profile-grid">
        <label>{t('Görev profili')}<select value={profileSha256} disabled={busy}
          onChange={event => {setProfileSha256(event.target.value); resetTask();}}>
          <option value="">{t('Profil seçin')}</option>
          {inventory.map(profile => <option key={profile.profile_sha256} value={profile.profile_sha256}>
            {profile.application_key} · v{profile.revision} · {profile.profile_sha256}
          </option>)}
        </select></label>
        <label>{t('Profildeki görev anahtarı')}<input value={taskKey} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setTaskKey(event.target.value); resetTask();}}/></label>
        <label>{t('Bağımsız doğrulayıcı referansı')}<input value={verificationRef} autoComplete="off"
          maxLength={64} disabled={busy} onChange={event => {setVerificationRef(event.target.value); resetTask();}}/></label>
      </div>
      <label><input type="checkbox" checked={stateReadback} disabled={busy}
        onChange={event => {setStateReadback(event.target.checked); resetTask();}}/>
        {t('Ayrı önce/sonra durum GET planı hazırlayacağım (toplam altı manuel onay).')}</label>
      <button type="submit" disabled={!taskValid || busy}>{t('Form görevini önizle')}</button>
    </form>
    {taskPreview ? <div data-testid="web-form-task-preview">
      <p>{t('Taslak SHA-256:')} <code>{taskPreview.task_sha256}</code></p>
      <label>{t('Exact görev SHA-256 onayı')}<input value={taskConfirmation} maxLength={64}
        autoComplete="off" disabled={busy} onChange={event => setTaskConfirmation(event.target.value)}/></label>
      <button type="button" disabled={busy || taskConfirmation !== taskPreview.task_sha256}
        onClick={() => void registerTask()}>{t('Özel form görevini kaydet')}</button>
    </div> : null}
    {taskReceipt ? <p role="status" data-testid="web-form-task-registered">
      {t('Özel form görevi kaydedildi:')} <code>{taskReceipt.task_file}</code>
    </p> : null}
    {taskReceipt ? <form data-testid="web-form-plan" onSubmit={event => void inspectPlan(event)}>
      <h3>{t('Exact HTTPS form planı')}</h3>
      <div className="web-profile-grid">
        <label>{t('Form POST URL’si')}<input value={submitUrl} autoComplete="off" maxLength={2048}
          disabled={busy} onChange={event => {setSubmitUrl(event.target.value); resetPlan();}}/></label>
        <label>{t('Makbuz GET URL’si')}<input value={receiptUrl} autoComplete="off" maxLength={2048}
          disabled={busy} onChange={event => {setReceiptUrl(event.target.value); resetPlan();}}/></label>
        <label>{t('Form alan modu')}<select value={fieldMode} disabled={busy}
          onChange={event => {
            setFieldMode(event.target.value as 'single' | 'multiple');
            setFieldName(''); setValue('');
            setFields([{name: '', value: ''}, {name: '', value: ''}]);
            setAttested(false); resetPlan();
          }}>
          <option value="single">{t('Tek alan')}</option>
          <option value="multiple">{t('2–8 sıralı alan')}</option>
        </select></label>
      </div>
      {fieldMode === 'single' ? <div className="web-profile-grid">
        <label>{t('Form alan adı')}<input value={fieldName} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setFieldName(event.target.value); resetPlan();}}/></label>
        <label>{t('Gizli olmayan test değeri')}<input type="password" value={value} autoComplete="off"
          maxLength={2048} disabled={busy} onChange={event => {setValue(event.target.value); resetPlan();}}/></label>
      </div> : <div data-testid="web-form-fields">
        <p className="caption">{t('Alanları formdaki DOM sırasıyla girin; sıra exact POST gövdesinin parçasıdır.')}</p>
        {fields.map((field, index) => <div className="web-profile-grid" key={index}>
          <label>{t('Alan adı')} {index + 1}<input value={field.name} autoComplete="off" maxLength={64}
            disabled={busy} onChange={event => editField(index, {name: event.target.value})}/></label>
          <label>{t('Test değeri')} {index + 1}<input type="password" value={field.value}
            autoComplete="off" maxLength={2048} disabled={busy}
            onChange={event => editField(index, {value: event.target.value})}/></label>
          <button type="button" disabled={busy || fields.length <= 2}
            onClick={() => {setFields(current => current.filter((_, fieldIndex) => fieldIndex !== index)); resetPlan();}}>
            {t('Alanı kaldır')} {index + 1}</button>
        </div>)}
        <button type="button" disabled={busy || fields.length >= 8}
          onClick={() => {setFields(current => [...current, {name: '', value: ''}]); resetPlan();}}>
          {t('Alan ekle')}</button>
      </div>}
      <label><input type="checkbox" checked={attested} disabled={busy}
        onChange={event => {setAttested(event.target.checked); resetPlan();}}/>
        {t('Değerin parola, token, çerez veya müşteri verisi olmadığını onaylıyorum.')}</label>
      <button type="submit" disabled={!planValid || busy}>{t('Form planını önizle')}</button>
    </form> : null}
    {planPreview ? <div data-testid="web-form-plan-preview">
      <p>{t('Plan SHA-256:')} <code>{planPreview.plan_sha256}</code></p>
      <p>{t('POST gövde SHA-256:')} <code>{planPreview.body_sha256}</code> · {planPreview.body_bytes} bytes</p>
      <p className="caption">{taskReceipt?.state_readback
        ? t('Durum okumasıyla altı ayrı manuel onay gerekir; kayıt site sonucu veya görev yetkisi değildir.')
        : t('Dört ayrı manuel onay gerekir; site sonucu, yürütme ve toplama yetkisi verilmez.')}</p>
      <label>{t('Exact plan SHA-256 onayı')}<input value={planConfirmation} maxLength={64}
        autoComplete="off" disabled={busy} onChange={event => setPlanConfirmation(event.target.value)}/></label>
      <button type="button" disabled={busy || planConfirmation !== planPreview.plan_sha256}
        onClick={() => void registerPlan()}>{t('Özel form planını kaydet')}</button>
    </div> : null}
    {planReceipt ? <div data-testid="web-form-plan-registered" role="status">
      <p>{t('Özel form planı kaydedildi:')} <code>{planReceipt.plan_file}</code></p>
      <p className="caption">{t('Değer özel dosyada tutulur; bu ekranda veya komut satırında gösterilmez. Dosyayı kullanım sonrası silin.')}</p>
    </div> : null}
    {taskReceipt?.state_readback && planReceipt ? <form data-testid="web-form-state" onSubmit={event => void inspectState(event)}>
      <h3>{t('Ayrı HTTPS durum planı')}</h3>
      <p className="caption">{t('Önce/sonra exact HTML hashlerini bağımsız kaynaktan girin. GET yan etkili olabilir; durum değişimi tek başına uygulama sonucu değildir.')}</p>
      <div className="web-profile-grid">
        <label>{t('Durum GET URL’si')}<input value={stateUrl} autoComplete="off" maxLength={2048}
          disabled={busy} onChange={event => {setStateUrl(event.target.value); resetState();}}/></label>
        <label>{t('Önce HTML SHA-256')}<input value={beforeSha256} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setBeforeSha256(event.target.value); resetState();}}/></label>
        <label>{t('Sonra HTML SHA-256')}<input value={afterSha256} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setAfterSha256(event.target.value); resetState();}}/></label>
      </div>
      <label><input type="checkbox" checked={markerEnabled} disabled={busy}
        onChange={event => {setMarkerEnabled(event.target.checked); if (!event.target.checked) setSubmittedFieldName(''); resetState();}}/>
        {t('Exact tekil HTML marker geçişini de bağla')}</label>
      {markerEnabled ? <div className="web-profile-grid">
        <label>{t('Marker ID')}<input value={markerId} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setMarkerId(event.target.value); resetState();}}/></label>
        <label>{t('Önce marker SHA-256')}<input value={beforeMarkerSha256} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setBeforeMarkerSha256(event.target.value); resetState();}}/></label>
        <label>{t('Sonra marker SHA-256')}<input value={afterMarkerSha256} autoComplete="off" maxLength={64}
          disabled={busy} onChange={event => {setAfterMarkerSha256(event.target.value); resetState();}}/></label>
        <label>{t('Gönderilen alanla marker eşleşmesi · isteğe bağlı')}<select value={submittedFieldName}
          disabled={busy} onChange={event => {setSubmittedFieldName(event.target.value); resetState();}}>
          <option value="">{t('Eşleştirme yok')}</option>
          {fieldNames.map(name => <option key={name} value={name}>{name}</option>)}
        </select></label>
        {submittedFieldName ? <p className="caption">{t('Sonra marker SHA-256, seçilen özel alan değerinin boşluk-normalize edilmiş metin hash’i olmalıdır; uyuşmazlık ağ isteğinden önce reddedilir. Bu kalıcı uygulama sonucu kanıtı değildir.')}</p> : null}
      </div> : null}
      <button type="submit" disabled={!stateValid || busy}>{t('Durum planını önizle')}</button>
    </form> : null}
    {statePreview ? <div data-testid="web-form-state-preview">
      <p>{t('Durum planı SHA-256:')} <code>{statePreview.state_plan_sha256}</code></p>
      <label>{t('Exact durum planı SHA-256 onayı')}<input value={stateConfirmation} maxLength={64}
        autoComplete="off" disabled={busy} onChange={event => setStateConfirmation(event.target.value)}/></label>
      <button type="button" disabled={busy || stateConfirmation !== statePreview.state_plan_sha256}
        onClick={() => void registerState()}>{t('Özel durum planını kaydet')}</button>
    </div> : null}
    {stateReceipt ? <p role="status" data-testid="web-form-state-registered">
      {t('Özel durum planı kaydedildi:')} <code>{stateReceipt.state_plan_file}</code>
    </p> : null}
    {planReceipt ? <div>
      {taskReceipt?.state_readback && !stateReceipt ? <p className="caption">{t('Durum planı kayıt edilmeden altı onaylı manager komutu gösterilmez.')}</p> : null}
      {planReceipt.public_grant_required && command && managerScope ? <p role="alert">{t('Adlandırılmış projede remote form başlatma bu sürümde desteklenmiyor; varsayılan oturuma ait komut gösterilmez.')}</p> : null}
      {planReceipt.public_grant_required && command && !managerScope ? <div data-testid="web-form-commands">
        <p>{t('1 · Ağsız manager önizlemesi')}</p><pre>./scripts/aos-v1 {taskReceipt?.state_readback ? 'preview-remote-form-state' : 'preview-remote-form'} {command}</pre>
        <p>{t('2 · Güvenli durdurma sonrası yeni oturum')}</p><pre>./scripts/aos-v1 start {command}</pre>
        <p className="caption">{t('Public hedef için exact plan grant’i komuttadır; komutlar burada çalıştırılmaz. Siteye istek ancak Tasks ekranında ayrı onaylarla yapılır.')}</p>
      </div> : !planReceipt.public_grant_required ? <p className="caption">{t('Sentetik .invalid hedef için public manager komutu yoktur; yalnız test backend’i kullanılabilir.')}</p> : null}
    </div> : null}
    {failure ? <p role="alert">{failure === 'unsupported'
      ? t('Bu backend form taslağı API’sini sunmuyor; dosya yazılmadı.')
      : failure === 'scope' ? t('Form profili, görevi veya plan kapsamı geçersiz; dosya yazılmadı.')
      : t('Form kaydı tamamlanmadı; özel dosya durumunu inceleyin.')}</p> : null}
  </section>;
}
