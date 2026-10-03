import {useEffect, useRef, useState} from 'react';
import {api, isOwnedFormRecipeSteps, type ParameterWebGoalStatus, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';

type ObjectValue = Record<string, unknown>;
type Binding = ObjectValue & {confirm_sha256: string; parameters: Record<string, string>; oracle: ObjectValue;
  authority: ObjectValue; source: ObjectValue; invocation: ObjectValue};
type Preview = {binding: Binding; binding_sha256: string};
type ReviewStatus = Extract<ParameterWebGoalStatus, {status: 'review_required'}>;
type JournalStatus = Extract<ParameterWebGoalStatus, {kind: 'web_goal_execution_journal_status'}>;
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const same = (left: unknown, right: unknown) => knowledgeCanonical(left) === knowledgeCanonical(right);
const flags = ['execution_authorized', 'execution_performed', 'site_outcome_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified'];
const sourceHashes = ['source_run_ref', 'source_invocation_sha256', 'candidate_sha256', 'review_sha256', 'release_sha256',
  'selection_sha256', 'reuse_admission_sha256', 'profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256', 'skill_plan_sha256', 'case_inputs_sha256'];
const encode = (value: string) => encodeURIComponent(value).replace(/[!'()*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase()).replace(/%20/g, '+');
async function validPreview(value: unknown, status: ReviewStatus, snapshot: Snapshot): Promise<Preview | null> {
  if (!object(value) || value.schema_version !== '1.0' || !object(value.binding) || typeof value.binding_canonical !== 'string'
    || value.binding_canonical.length > 65536 || value.binding_sha256 !== status.binding_sha256
    || !['execution_authorized', 'training_ready', 'gpu_release_verified'].every(key => value[key] === false)
    || !knowledgeHash(value.binding_sha256) || await knowledgeTextHash(value.binding_canonical) !== value.binding_sha256) return null;
  const binding = value.binding;
  try {if (!same(JSON.parse(value.binding_canonical), binding)) return null;} catch {return null;}
  if (binding.schema_version !== '1.0' || binding.kind !== 'web_goal_parameter_execution_binding' || binding.synthetic !== true
    || !flags.every(key => binding[key] === false) || !object(binding.authority) || !object(binding.source)
    || !object(binding.parameters) || !object(binding.invocation) || !object(binding.oracle) || !Array.isArray(binding.field_bindings)
    || binding.confirm_sha256 !== status.confirm_sha256 || !knowledgeHash(binding.confirm_sha256)
    || binding.authority.runtime_id !== snapshot.runtime.runtime_id || binding.authority.lease_id !== snapshot.control.lease_id
    || binding.authority.generation !== snapshot.control.generation || binding.authority.owner !== 'AGENT'
    || binding.authority.status !== 'running'
    || !validKnowledgeScope(binding.oracle.scope) || binding.oracle.schema_version !== '1.0'
    || binding.oracle.kind !== 'web_goal_whole_record_contract' || !object(binding.oracle.expected_fields)
    || binding.oracle.maximum_reported_posts !== 1 || binding.oracle.ambiguous_effect !== 'stop_without_retry'
    || binding.oracle.source_run_ref !== binding.source.source_run_ref || binding.oracle.case_key !== binding.case_key
    || binding.oracle.profile_sha256 !== binding.source.profile_sha256 || binding.oracle.task_sha256 !== binding.source.task_sha256
    || binding.invocation.schema_version !== '2.0' || binding.invocation.synthetic !== true
    || binding.invocation.status !== 'admission_ready' || binding.invocation.case_key !== binding.case_key
    || binding.invocation.fresh_approval_per_stage !== true || binding.invocation.maximum_post_count !== 1
    || !isOwnedFormRecipeSteps(binding.invocation.steps)
    ) return null;
  const source = binding.source; const invocation = binding.invocation;
  if (!sourceHashes.every(key => knowledgeHash(source[key]))
    || !['execution_authorized', 'collection_authorized', 'recipe_executed', 'skill_executed', 'site_outcome_verified', 'reviewed', 'activation_authorized', 'training_ready'].every(key => invocation[key] === false)
    || !['profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256', 'skill_plan_sha256', 'case_inputs_sha256'].every(key => invocation[key] === source[key])) return null;
  const parameters = binding.parameters;
  const names = Object.keys(parameters);
  if (names.length < 1 || names.length > 8 || names.length !== status.field_count || binding.field_bindings.length !== names.length
    || !names.every(key => /^[a-z][a-z0-9_-]{0,63}$/.test(key) && typeof parameters[key] === 'string'
      && new TextEncoder().encode(parameters[key] as string).length > 0 && new TextEncoder().encode(parameters[key] as string).length <= 256)) return null;
  const fields: Record<string, string> = {};
  const used = new Set<string>();
  for (const field of binding.field_bindings) {
    if (!object(field) || Object.keys(field).length !== 2 || typeof field.parameter_key !== 'string'
      || !Object.hasOwn(parameters, field.parameter_key) || used.has(field.parameter_key)
      || typeof field.form_field_name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(field.form_field_name)
      || Object.hasOwn(fields, field.form_field_name)) return null;
    used.add(field.parameter_key); fields[field.form_field_name] = parameters[field.parameter_key] as string;
  }
  const body = Object.entries(fields).map(([name, content]) => encode(name) + '=' + encode(content)).join('&');
  const {confirm_sha256, ...unsigned} = binding;
  if (await knowledgeDigest(unsigned) !== confirm_sha256 || await knowledgeDigest({domain: 'site_skill_parameter_variant_v1', parameters}) !== binding.parameter_variant_sha256
    || !same(fields, binding.oracle.expected_fields) || await knowledgeDigest(fields) !== binding.oracle.expected_record_sha256
    || await knowledgeDigest(binding.field_bindings) !== binding.invocation.field_binding_sha256
    || await knowledgeTextHash(body) !== binding.form_body_sha256 || new TextEncoder().encode(body).length !== binding.form_body_bytes) return null;
  return {binding: binding as Binding, binding_sha256: value.binding_sha256 as string};
}
function validReport(value: unknown, intent: string, binding: string): value is JournalStatus {
  if (!object(value) || value.schema_version !== '1.0' || value.kind !== 'web_goal_execution_journal_status'
    || value.intent_sha256 !== intent || value.binding_sha256 !== binding || value.confirmation_consumed !== true
    || !['intent_sha256', 'binding_sha256', 'confirmation_sha256', 'authority_sha256', 'source_sha256'].every(key => knowledgeHash(value[key]))
    || !['run_binding_sha256', 'run_ref', 'receipt_sha256'].every(key => value[key] === null || knowledgeHash(value[key]))
    || !['replay_authorized', 'site_outcome_verified', 'training_ready', 'gpu_release_verified'].every(key => value[key] === false)) return false;
  const accepted = value.status === 'accepted_verified';
  return ['uncertain_before_run_binding', 'awaiting_independent_verification', 'accepted_verified'].includes(String(value.status))
    && value.reserved === !accepted && value.task_terminal_verified === accepted && value.record_outcome_verified === accepted
    && (accepted ? knowledgeHash(value.receipt_sha256) && knowledgeHash(value.run_ref) && knowledgeHash(value.run_binding_sha256)
      : value.receipt_sha256 === null && (value.status === 'uncertain_before_run_binding'
        ? value.run_ref === null && value.run_binding_sha256 === null : knowledgeHash(value.run_ref) && knowledgeHash(value.run_binding_sha256)));
}

export function ParameterWebGoal({snapshot, status, disabled}: {snapshot: Snapshot; status: ParameterWebGoalStatus; disabled: boolean}) {
  useLanguage();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [intent, setIntent] = useState<string | null>(null);
  const [report, setReport] = useState<JournalStatus | null>(null);
  const [message, setMessage] = useState('');
  const scope = knowledgeCanonical({runtime: snapshot.runtime.runtime_id, control: snapshot.control, disabled,
    binding: status.binding_sha256, confirmation: status.status === 'review_required' ? status.confirm_sha256 : null,
    intent: status.status === 'review_required' ? null : status.intent_sha256,
    source: status.status === 'review_required' ? null : status.source_sha256});
  const latest = useRef({scope, snapshot, status, disabled}); latest.current = {scope, snapshot, status, disabled};
  const request = useRef<symbol | null>(null);
  const startLocked = useRef(false);
  useEffect(() => {setPreview(null); setConfirmed(false); setReport(null); setMessage(''); request.current = null; setPending(false);}, [scope]);
  const knownIntent = status.status === 'review_required' ? null : status.intent_sha256;
  useEffect(() => {setIntent(null);}, [status.binding_sha256]);
  useEffect(() => {if (knowledgeHash(knownIntent)) {setIntent(knownIntent); startLocked.current = true;}}, [knownIntent, status.binding_sha256]);
  const eligible = !disabled && !startLocked.current && !uncertain && status.status === 'review_required'
    && status.unresolved_intent === false && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';
  async function perform(operation: 'preview' | 'start' | 'report') {
    if (request.current !== null || (operation !== 'report' && !eligible) || (operation === 'start' && (!preview || !confirmed))
      || (operation === 'report' && !intent)) return;
    const captured = latest.current;
    const token = Symbol(operation); request.current = token; setPending(true); setMessage('');
    const active = () => request.current === token && latest.current.scope === captured.scope;
    if (operation === 'start') {startLocked.current = true; setUncertain(true); setConfirmed(false);}
    try {
      const body = operation === 'report' ? {schema_version: '1.0', intent_sha256: intent}
        : {schema_version: '1.0', lease_id: captured.snapshot.control.lease_id, generation: captured.snapshot.control.generation,
          ...(operation === 'start' ? {confirm_sha256: preview!.binding.confirm_sha256, human_confirmation: true} : {})};
      const result = await api<unknown>(`/api/tasks/parameter-web-goal/${operation}`, body);
      if (!active()) return;
      if (operation === 'preview') {
        const checked = captured.status.status === 'review_required' ? await validPreview(result, captured.status, captured.snapshot) : null;
        if (!active()) return;
        if (!checked) throw new Error(t('Sunucu yanıtı incelenen sözleşmeyle eşleşmiyor.'));
        setPreview(checked); setConfirmed(false);
      } else if (operation === 'start') {
        if (!object(result) || !knowledgeHash(result.intent_sha256) || result.binding_sha256 !== captured.status.binding_sha256
          || result.independently_verified !== false || typeof result.job_id !== 'string') throw new Error(t('Sunucu yanıtı incelenen sözleşmeyle eşleşmiyor.'));
        setIntent(result.intent_sha256); setUncertain(false); setMessage('Görev başlatıldı; eylemleri Tasks üzerinden ayrı ayrı onaylayın.');
      } else {
        if (!validReport(result, intent!, captured.status.binding_sha256)) throw new Error(t('Sunucu yanıtı incelenen sözleşmeyle eşleşmiyor.'));
        setReport(result);
      }
    } catch (error) {if (active()) setMessage(error instanceof Error ? error.message : String(error));}
    finally {if (active()) {request.current = null; setPending(false);}}
  }
  return <section className="subpanel"><h3>{t('Parametreli web görevi')}</h3>
    <p className="muted">{t('Yalnız sunucunun sabitlediği sentetik uygulama ve değerler. Model çıkarımı yok; altı eylem ayrı onay gerektirir.')}</p>
    <button disabled={!eligible || pending} onClick={() => void perform('preview')}>{t('Parametreleri önizle')}</button>
    {preview && <><p>{t('Uygulama / kiracı / rol')}: {Object.values(preview.binding.oracle.scope as ObjectValue).join(' / ')}</p>
      <dl>{Object.entries(preview.binding.parameters).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
      <code>{preview.binding_sha256}</code><label><input type="checkbox" checked={confirmed} disabled={!eligible || pending}
        onChange={event => setConfirmed(event.target.checked)}/>{t('Bu tam parametre haritasını ayrı insan onayıyla başlatmayı kabul ediyorum.')}</label>
      <button disabled={!eligible || pending || !confirmed} onClick={() => void perform('start')}>{t('İncelenen görevi başlat')}</button></>}
    {pending && <p role="status">{t('İşlem sürüyor; tekrar gönderilmez.')}</p>}
    {uncertain && <p role="alert">{t('Başlatma sonucu belirsiz. Tekrar başlatmayın; kayıt raporunu okuyun.')}</p>}
    {startLocked.current && !intent && <p>{t('Başlatma kaydı henüz bilinmiyor; Tasks durumundan kurtarma bekleniyor.')}</p>}
    {intent && <button disabled={pending} onClick={() => void perform('report')}>{t('Kayıt raporunu oku')}</button>}
    {message && <p role="status">{t(message)}</p>}
    {report && <><p>{t(report.status === 'accepted_verified' ? 'Görev sonlanması ve tüm kayıt değerleri bağımsız doğrulandı.'
      : 'Bağımsız doğrulama bekleniyor; onay tüketildi, tekrar yok.')}</p><code>{report.receipt_sha256 ?? report.intent_sha256}</code>
      <p className="muted">{t('Bu rapor gerçek model, GPU bırakımı, öğrenme veya genel site kabulü kanıtı değildir.')}</p></>}
  </section>;
}
