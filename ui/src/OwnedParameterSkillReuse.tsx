import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';
import {validOwnedParameterSkillRelease} from './OwnedParameterSkillRelease';

type ObjectValue = Record<string, unknown>;
type Predecessor = {intent_sha256: string; receipt_sha256: string};
type Preview = {admission: ObjectValue; binding: ObjectValue; admission_sha256: string; binding_sha256: string; confirm_sha256: string;
  predecessor?: Predecessor; transition_sha256?: string};
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const flags = ['execution_authorized', 'execution_performed', 'activation_authorized', 'training_ready', 'gpu_release_verified'];
const releaseFlags = ['native_model_verified', 'execution_authorized', 'activation_authorized', 'training_ready', 'gpu_release_verified'];
const denied = [...flags, 'native_model_verified', 'released_skill_verified', 'site_outcome_verified', 'account_scope_verified', 'held_out_independence_verified', 'replay_authorized', 'runtime_started', 'reviewed'];
function parameters(text: string): Record<string, string> | null {
  try {
    const value: unknown = JSON.parse(text);
    if (!object(value) || Object.keys(value).length < 2 || Object.keys(value).length > 8
      || !Object.entries(value).every(([key, content]) => /^[a-z][a-z0-9_-]{0,63}$/.test(key) && typeof content === 'string'
        && Array.from(content).length >= 1 && Array.from(content).length <= 128
        && new TextEncoder().encode(content).length <= 256 && !/[\u0000-\u001f\u007f]/.test(content))) return null;
    return value as Record<string, string>;
  } catch {return null;}
}
async function canonicalRecord(value: ObjectValue, key: string): Promise<boolean> {
  return object(value[key]) && typeof value[key + '_canonical'] === 'string' && (value[key + '_canonical'] as string).length <= 262144
    && knowledgeHash(value[key + '_sha256']) && value[key + '_canonical'] === knowledgeCanonical(value[key])
    && await knowledgeTextHash(value[key + '_canonical'] as string) === value[key + '_sha256'];
}
export async function validOwnedParameterSkillReusePreview(value: unknown, releaseHash: string, selectionHash: string,
  expected: Record<string, string>, snapshot: Snapshot, predecessor?: Predecessor): Promise<Preview | null> {
  if (!object(value) || value.schema_version !== '1.0' || !flags.every(key => value[key] === false)
    || !await canonicalRecord(value, 'admission') || !await canonicalRecord(value, 'binding')
    || !knowledgeHash(value.confirm_sha256)) return null;
  const admission = value.admission as ObjectValue;
  const binding = value.binding as ObjectValue;
  if (admission.schema_version !== '1.0' || admission.kind !== 'owned_parameter_skill_manual_reuse_admission'
    || admission.status !== 'compiled_manual_reuse_preview' || admission.synthetic !== true || admission.development_only !== true
    || admission.model_calls !== 0 || !denied.every(key => admission[key] === false)
    || !object(admission.source) || !object(binding.source) || !object(binding.authority) || !object(binding.oracle)
    || knowledgeCanonical(admission.parameters) !== knowledgeCanonical(expected)
    || knowledgeCanonical(binding.parameters) !== knowledgeCanonical(expected)
    || binding.schema_version !== '1.0' || binding.kind !== 'web_goal_parameter_execution_binding' || binding.synthetic !== true
    || ![...flags, 'site_outcome_verified'].every(key => binding[key] === false)
    || binding.confirm_sha256 !== value.confirm_sha256 || binding.proposal_sha256 !== (predecessor ? value.transition_sha256 : value.admission_sha256)
    || binding.source.reuse_admission_sha256 !== value.admission_sha256
    || binding.authority.runtime_id !== snapshot.runtime.runtime_id || binding.authority.lease_id !== snapshot.control.lease_id
    || binding.authority.generation !== snapshot.control.generation || binding.authority.owner !== 'AGENT' || binding.authority.status !== 'running') return null;
  const source = admission.source;
  if (predecessor) {
    if (!object(value.predecessor) || Object.keys(value.predecessor).length !== 2
      || knowledgeCanonical(value.predecessor) !== knowledgeCanonical(predecessor)
      || !object(value.transition) || Object.keys(value.transition).length !== 5
      || value.transition.schema_version !== '1.0' || value.transition.kind !== 'owned_parameter_skill_reuse_next_task'
      || value.transition.previous_intent_sha256 !== predecessor.intent_sha256
      || value.transition.previous_receipt_sha256 !== predecessor.receipt_sha256
      || value.transition.admission_sha256 !== value.admission_sha256 || !knowledgeHash(value.transition_sha256)
      || await knowledgeDigest(value.transition) !== value.transition_sha256) return null;
  } else if (Object.hasOwn(value, 'transition') || Object.hasOwn(value, 'predecessor') || Object.hasOwn(value, 'transition_sha256')) return null;
  if (source.schema_version !== '1.0' || source.kind !== 'owned_parameter_skill_manual_reuse_source'
    || source.release_sha256 !== releaseHash || source.selection_sha256 !== selectionHash
    || binding.source.release_sha256 !== releaseHash || binding.source.selection_sha256 !== selectionHash
    || !validKnowledgeScope(source.scope) || !object(source.release) || !object(source.selection)
    || await knowledgeDigest(source) !== binding.catalog_sha256) return null;
  for (const [kind, checksum] of [['release', releaseHash], ['selection', selectionHash]] as const) {
    const record = source[kind] as ObjectValue;
    if (!await validOwnedParameterSkillRelease({schema_version: '1.0', [kind]: record, [kind + '_canonical']: knowledgeCanonical(record),
      [kind + '_sha256']: checksum, ...Object.fromEntries(releaseFlags.map(key => [key, false]))}, kind)) return null;
  }
  if (source.selection.release_sha256 !== releaseHash || source.release.review_sha256 !== source.review_sha256
    || source.release.candidate_sha256 !== source.candidate_sha256 || source.selection.review_sha256 !== source.review_sha256
    || source.selection.candidate_sha256 !== source.candidate_sha256
    || knowledgeCanonical(source.scope) !== knowledgeCanonical(source.release.scope)
    || knowledgeCanonical(source.scope) !== knowledgeCanonical(source.selection.scope)
    || binding.oracle.schema_version !== '1.0' || binding.oracle.kind !== 'web_goal_whole_record_contract'
    || knowledgeCanonical(binding.oracle.scope) !== knowledgeCanonical(source.scope)
    || binding.oracle.maximum_reported_posts !== 1 || binding.oracle.ambiguous_effect !== 'stop_without_retry'
    || binding.oracle.expected_record_sha256 !== admission.expected_record_sha256
    || binding.case_key !== admission.case_key || binding.parameter_variant_sha256 !== admission.parameter_variant_sha256
    || binding.form_body_sha256 !== admission.form_body_sha256
    || knowledgeCanonical(binding.field_bindings) !== knowledgeCanonical(admission.field_bindings)
    || knowledgeCanonical(binding.invocation) !== knowledgeCanonical(admission.invocation)) return null;
  if (!['candidate_sha256', 'review_sha256', 'family_sha256', 'source_fingerprint_sha256'].every(key =>
    source[key] === (source.release as ObjectValue)[key] && source[key] === (source.selection as ObjectValue)[key])
    || !['manifest_sha256', 'source_run_ref', 'recipe_sha256'].every(key => source[key] === (source.release as ObjectValue)[key])
    || !object(source.release.family) || source.field_binding_sha256 !== source.release.family.field_binding_sha256
    || !['source_run_ref', 'source_invocation_sha256', 'candidate_sha256', 'review_sha256', 'profile_sha256', 'task_sha256', 'skill_sha256', 'recipe_sha256'].every(key =>
      knowledgeHash(source[key]) && (binding.source as ObjectValue)[key] === source[key])
    || !['skill_plan_sha256', 'case_inputs_sha256'].every(key => (binding.source as ObjectValue)[key] === admission[key])) return null;
  for (const key of ['skill_plan', 'case_inputs', 'form_plan', 'state_plan', 'record_config', 'invocation']) {
    if (!object(admission[key]) || await knowledgeDigest(admission[key]) !== admission[key + '_sha256']) return null;
  }
  if (!object(admission.invocation) || !['execution_authorized', 'collection_authorized', 'recipe_executed', 'skill_executed',
    'site_outcome_verified', 'reviewed', 'activation_authorized', 'training_ready'].every(key => (admission.invocation as ObjectValue)[key] === false)
    || admission.invocation.maximum_post_count !== 1 || admission.invocation.fresh_approval_per_stage !== true
    || !Array.isArray(admission.field_bindings)) return null;
  const fields: Record<string, string> = {};
  const used = new Set<string>();
  for (const field of admission.field_bindings) {
    if (!object(field) || typeof field.parameter_key !== 'string' || !Object.hasOwn(expected, field.parameter_key)
      || used.has(field.parameter_key) || typeof field.form_field_name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(field.form_field_name)
      || Object.hasOwn(fields, field.form_field_name)) return null;
    fields[field.form_field_name] = expected[field.parameter_key]; used.add(field.parameter_key);
  }
  const {confirm_sha256, ...unsigned} = binding;
  if (used.size !== Object.keys(expected).length || knowledgeCanonical(fields) !== knowledgeCanonical(binding.oracle.expected_fields)
    || await knowledgeDigest(admission.field_bindings) !== source.field_binding_sha256
    || await knowledgeDigest(fields) !== admission.expected_record_sha256 || await knowledgeDigest(unsigned) !== confirm_sha256
    || await knowledgeDigest({domain: 'site_skill_parameter_variant_v1', parameters: expected}) !== admission.parameter_variant_sha256
    || admission.parameter_variant_sha256 === source.source_parameter_variant_sha256
    || admission.invocation_sha256 === source.source_invocation_sha256) return null;
  return {admission, binding, admission_sha256: value.admission_sha256 as string,
    binding_sha256: value.binding_sha256 as string, confirm_sha256: value.confirm_sha256,
    ...(predecessor ? {predecessor, transition_sha256: value.transition_sha256 as string} : {})};
}

export function OwnedParameterSkillReuse({snapshot, disabled}: {snapshot: Snapshot; disabled: boolean}) {
  useLanguage();
  const [releaseHash, setReleaseHash] = useState('');
  const [selectionHash, setSelectionHash] = useState('');
  const [input, setInput] = useState('{"record-id":"Synthetic new record","note-text":"Synthetic new note"}');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [status, setStatus] = useState<ObjectValue | null>(null);
  const [accepted, setAccepted] = useState<{evidence: ObjectValue; predecessor: Predecessor; parameters: unknown} | null>(null);
  const [completed, setCompleted] = useState<ObjectValue[]>([]);
  const [historyHash, setHistoryHash] = useState('');
  const [history, setHistory] = useState<ObjectValue | null>(null);
  const [transitionClosed, setTransitionClosed] = useState(false);
  const [message, setMessage] = useState('');
  const scope = knowledgeCanonical({releaseHash, selectionHash, input, runtime: snapshot.runtime.runtime_id, control: snapshot.control, disabled});
  const latest = useRef({scope, snapshot}); latest.current = {scope, snapshot};
  const request = useRef<symbol | null>(null);
  const startAttempted = useRef(false);
  const known = useRef<Preview | null>(null);
  const nextAttempted = useRef(new Set<string>());
  const nextInFlight = useRef(false);
  useEffect(() => {setPreview(null); setConfirmed(false); setStatus(null); setMessage('');}, [scope]);
  useEffect(() => () => {request.current = null;}, []);
  const parsed = parameters(input);
  const eligible = !disabled && !pending && !startAttempted.current && !uncertain && snapshot.runtime.running
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running'
    && knowledgeHash(releaseHash) && knowledgeHash(selectionHash) && parsed !== null;
  const nextEligible = !disabled && !pending && !uncertain && !transitionClosed && !nextInFlight.current && accepted !== null
    && snapshot.runtime.running && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running'
    && knowledgeHash(releaseHash) && knowledgeHash(selectionHash) && parsed !== null
    && (accepted.parameters === null || knowledgeCanonical(parsed) !== knowledgeCanonical(accepted.parameters));
  async function perform(operation: 'preview' | 'start' | 'status' | 'next-preview' | 'next-start' | 'read') {
    const next = operation.startsWith('next-');
    const write = operation === 'start' || operation === 'next-start';
    const readonly = operation === 'status' || operation === 'read';
    if (request.current !== null || !readonly && !(next ? nextEligible : eligible)
      || write && (!preview || !confirmed || next !== Boolean(preview.predecessor))
      || operation === 'next-start' && (!preview?.transition_sha256 || nextAttempted.current.has(preview.transition_sha256))
      || operation === 'read' && !knowledgeHash(historyHash)) return;
    const captured = latest.current;
    const token = Symbol(operation); request.current = token; setPending(true); setConfirmed(false); setMessage('');
    const current = () => request.current === token && latest.current.scope === captured.scope;
    try {
      if (write || operation === 'next-preview') {
        const fresh = await api<Snapshot>('/api/state');
        if (!current()) return;
        if (!fresh.runtime.running || fresh.runtime.runtime_id !== captured.snapshot.runtime.runtime_id || fresh.control.owner !== 'AGENT'
          || fresh.control.status !== 'running' || fresh.control.lease_id !== captured.snapshot.control.lease_id
          || fresh.control.generation !== captured.snapshot.control.generation) throw new Error('stale_control');
      }
      if (write) {
        startAttempted.current = true;
        if (next) {nextAttempted.current.add(preview!.transition_sha256!); nextInFlight.current = true;}
        known.current = preview; setUncertain(true); setPreview(null); setStatus(null);
      }
      const body = operation === 'status' ? {schema_version: '1.0'} : operation === 'read' ? {schema_version: '1.0', intent_sha256: historyHash}
        : {schema_version: '1.0', release_sha256: releaseHash,
        selection_sha256: selectionHash, parameters: parsed, lease_id: captured.snapshot.control.lease_id,
        generation: captured.snapshot.control.generation,
        ...(next ? {previous_intent_sha256: accepted!.predecessor.intent_sha256, previous_receipt_sha256: accepted!.predecessor.receipt_sha256} : {}),
        ...(write ? {confirm_sha256: preview!.confirm_sha256, human_confirmation: true} : {})};
      const value = await api<unknown>('/api/tasks/parameter-project-skill-reuse/' + operation, body);
      if (!current()) return;
      if (operation === 'preview' || operation === 'next-preview') {
        const checked = await validOwnedParameterSkillReusePreview(value, releaseHash, selectionHash, parsed!, captured.snapshot,
          next ? accepted!.predecessor : undefined);
        if (!current()) return;
        if (!checked) throw new Error('invalid_reuse_preview');
        setPreview(checked); setMessage('Önizleme doğrulandı; görev henüz başlamadı.');
      } else if (write) {
        if (!object(value) || typeof value.job_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value.job_id)
          || !knowledgeHash(value.intent_sha256) || value.admission_sha256 !== known.current?.admission_sha256
          || value.binding_sha256 !== known.current?.binding_sha256 || value.independently_verified !== false
          || value.training_ready !== false || value.gpu_release_verified !== false
          || next && (value.transition_sha256 !== known.current?.transition_sha256
            || knowledgeCanonical(value.predecessor) !== knowledgeCanonical(known.current?.predecessor))) throw new Error('invalid_reuse_start');
        setStatus(value); setUncertain(false); setMessage('Ayrı görev başlatıldı; her eylemi Tasks üzerinden onaylayın.');
      } else {
        if (!object(value) || value.training_ready !== false || value.gpu_release_verified !== false) throw new Error('invalid_reuse_status');
        if (value.status === 'review_required') {
          if (operation === 'read' || typeof value.available !== 'boolean' || typeof value.unresolved_intent !== 'boolean' || value.independently_verified !== false) throw new Error('invalid_reuse_status');
        } else {
          const oldTransitionStatus = operation === 'status' && known.current?.predecessor
            && value.intent_sha256 === known.current.predecessor.intent_sha256 && value.receipt_sha256 === known.current.predecessor.receipt_sha256
            && (value.transition_blocked === true || value.transition_in_progress === true);
          if (value.schema_version !== '1.0' || value.kind !== 'web_goal_execution_journal_status'
            || !['intent_sha256', 'binding_sha256', 'confirmation_sha256', 'authority_sha256', 'source_sha256'].every(key => knowledgeHash(value[key]))
            || value.confirmation_consumed !== true || value.replay_authorized !== false || value.site_outcome_verified !== false
            || operation !== 'read' && known.current && value.binding_sha256 !== known.current.binding_sha256 && !oldTransitionStatus
            || operation === 'read' && value.intent_sha256 !== historyHash
            || !['uncertain_before_run_binding', 'awaiting_independent_verification', 'accepted_verified'].includes(String(value.status))) throw new Error('invalid_reuse_status');
          const accepted = value.status === 'accepted_verified';
          if (value.reserved !== !accepted || value.task_terminal_verified !== accepted || value.record_outcome_verified !== accepted
            || (accepted ? !knowledgeHash(value.receipt_sha256) : value.receipt_sha256 !== null)
            || (value.status === 'uncertain_before_run_binding' ? value.run_binding_sha256 !== null || value.run_ref !== null
              : !knowledgeHash(value.run_binding_sha256) || !knowledgeHash(value.run_ref))) throw new Error('invalid_reuse_status');
          if (typeof value.transition_blocked !== 'boolean' || typeof value.transition_in_progress !== 'boolean') throw new Error('invalid_transition_status');
          if (operation === 'read') {setHistory(value); return;}
          startAttempted.current = true;
          setTransitionClosed(value.transition_blocked === true || value.transition_in_progress === true);
          if (oldTransitionStatus) {
            setStatus(value); setMessage('Sonraki görev geçişi kapalı veya sürüyor; önceki sonuç tarihsel kanıttır, tekrar gönderilmez.'); return;
          }
          if (value.status === 'accepted_verified') {
            nextInFlight.current = false;
            setCompleted(previous => previous.some(record => record.intent_sha256 === value.intent_sha256)
              ? previous : [...previous, value].slice(-64));
            setAccepted({evidence: value, predecessor: {intent_sha256: value.intent_sha256 as string, receipt_sha256: value.receipt_sha256 as string},
              parameters: known.current?.admission.parameters ?? null});
            setHistoryHash(value.intent_sha256 as string);
          }
          setUncertain(false);
        }
        setStatus(value); setMessage(value.status === 'accepted_verified' ? 'Görev sonlanması ve tüm kayıt değerleri bağımsız doğrulandı.' : 'Görev kaydı henüz bağımsız doğrulanmadı; tekrar başlatılmaz.');
      }
    } catch {
      if (current()) {setPreview(null); setStatus(null); setMessage('Yeniden kullanım yanıtı, kaynak veya kontrol doğrulanamadı.');}
    } finally {if (request.current === token) {request.current = null; setPending(false);}}
  }
  return <section className="panel" data-testid="parameter-skill-reuse">
    <h3>{t('Seçilmiş manuel skill ile ayrı görev')}</h3>
    <p>{t('Exact seçilmiş sürüm ve yeni parametreler. Önizleme salt okunurdur; ayrı başlatma izni ve her eylem için Tasks onayı gerekir.')}</p>
    <label>{t('Yeniden kullanılacak sürüm SHA-256')}<input data-testid="manual-reuse-release" value={releaseHash} maxLength={64} disabled={pending} onChange={event => setReleaseHash(event.target.value)}/></label>
    <label>{t('Güncel seçim SHA-256')}<input data-testid="manual-reuse-selection" value={selectionHash} maxLength={64} disabled={pending} onChange={event => setSelectionHash(event.target.value)}/></label>
    <label>{t('Yeni parametreler (JSON)')}<textarea data-testid="manual-reuse-parameters" value={input} maxLength={4096} disabled={pending} onChange={event => setInput(event.target.value)}/></label>
    <button type="button" data-testid="manual-reuse-preview" disabled={!eligible} onClick={() => void perform('preview')}>{t('Yeni görev bağını önizle')}</button>
    {accepted && <div data-testid="manual-reuse-predecessor"><p>{t('Önceki doğrulanmış görev — tarihsel kanıt')}</p>
      <pre>{knowledgeCanonical(accepted.evidence)}</pre>
      <button type="button" data-testid="manual-reuse-next-preview" disabled={!nextEligible} onClick={() => void perform('next-preview')}>{t('Yeni parametrelerle sonraki ayrı görevi önizle')}</button></div>}
    {completed.length > 0 && <div data-testid="manual-reuse-completed-history"><p>{t('Önceki doğrulanmış görev — tarihsel kanıt')}</p>
      {completed.map(record => <pre key={record.intent_sha256 as string}>{knowledgeCanonical(record)}</pre>)}</div>}
    {preview && <><pre data-testid="manual-reuse-proposal">{knowledgeCanonical(preview.admission)}</pre><pre>{knowledgeCanonical(preview.binding)}</pre>
      {preview.predecessor && <pre data-testid="manual-reuse-next-predecessor">{knowledgeCanonical(preview.predecessor)}</pre>}
      <label><input type="checkbox" data-testid="manual-reuse-confirm" checked={confirmed} disabled={preview.predecessor ? !nextEligible : !eligible} onChange={event => setConfirmed(event.target.checked)}/>{t('Bu exact bağ ve yeni parametreler için ayrı görev yürütme izni veriyorum.')}</label>
      <button type="button" data-testid="manual-reuse-start" disabled={!(preview.predecessor ? nextEligible : eligible) || !confirmed
        || Boolean(preview.transition_sha256 && nextAttempted.current.has(preview.transition_sha256))}
        onClick={() => void perform(preview.predecessor ? 'next-start' : 'start')}>{t('Onaylanan ayrı görevi başlat')}</button></>}
    <button type="button" data-testid="manual-reuse-status" disabled={pending} onClick={() => void perform('status')}>{t('Yeniden kullanım kayıt durumunu oku')}</button>
    {status && <pre data-testid="manual-reuse-result">{knowledgeCanonical(status)}</pre>}
    <label>{t('Tarihsel görev intent SHA-256')}<input data-testid="manual-reuse-history-hash" maxLength={64} value={historyHash} disabled={pending}
      onChange={event => {setHistoryHash(event.target.value); setHistory(null);}}/></label>
    <button type="button" data-testid="manual-reuse-history-read" disabled={pending || !knowledgeHash(historyHash)} onClick={() => void perform('read')}>{t('Tarihsel görev kaydını oku')}</button>
    {history && <pre data-testid="manual-reuse-history">{knowledgeCanonical(history)}</pre>}
    {pending && <p role="status">{t('Yeniden kullanım kanıtı denetleniyor…')}</p>}
    {uncertain && <p role="alert" data-testid="manual-reuse-uncertain">{t('Başlatma sonucu belirsiz. Tekrar gönderilmez; yalnız kayıt durumunu okuyun.')}</p>}
    {message && <p role="status" data-testid="manual-reuse-message">{t(message)}</p>}
    <p>{t('Bu kayıt gerçek model, genel site, etkinleştirme, eğitim veya GPU bırakımı kabulü değildir.')}</p>
  </section>;
}
