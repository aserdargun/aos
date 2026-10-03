import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';

type ObjectValue = Record<string, unknown>;
type Candidate = {candidate: ObjectValue; candidate_sha256: string; candidate_canonical: string};
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const base = '/api/tasks/parameter-project-skill';
const denied = ['native_model_verified', 'activation_authorized', 'training_ready', 'gpu_release_verified'];
const bootstrapDenied = [...denied, 'released_skill_verified', 'site_outcome_verified', 'replay_authorized'];

export async function validOwnedParameterSkill(value: unknown, intent: string | null, expectedHash?: string): Promise<Candidate | null> {
  const envelopeKeys = ['schema_version', 'candidate', 'candidate_canonical', 'candidate_sha256', 'status', ...denied];
  if (!object(value) || Object.keys(value).length !== envelopeKeys.length || !envelopeKeys.every(key => Object.hasOwn(value, key))
    || value.schema_version !== '1.0' || value.status !== 'awaiting_manual_review'
    || !denied.every(key => value[key] === false) || !object(value.candidate)
    || !knowledgeHash(value.candidate_sha256) || expectedHash && value.candidate_sha256 !== expectedHash
    || typeof value.candidate_canonical !== 'string' || value.candidate_canonical.length > 262144
    || value.candidate_canonical !== knowledgeCanonical(value.candidate)
    || await knowledgeTextHash(value.candidate_canonical) !== value.candidate_sha256) return null;
  const candidate = value.candidate;
  if (candidate.schema_version !== '1.0' || candidate.kind !== 'audited_manual_bootstrap_candidate'
    || candidate.provenance_kind !== 'manual_authored_synthetic_project' || candidate.status !== 'awaiting_manual_review'
    || candidate.verification_scope !== 'synthetic_manual_bootstrap_run_and_whole_record'
    || !['synthetic', 'development_only', 'manual_recipe_execution_verified', 'task_terminal_verified', 'record_outcome_verified'].every(key => candidate[key] === true)
    || !validKnowledgeScope(candidate.scope)
    || !['synthetic-crm-note', 'synthetic-inventory-note'].includes(candidate.scope.application_id)
    || candidate.scope.tenant_id !== 'synthetic-tenant' || candidate.scope.account_role !== 'editor'
    || !knowledgeHash(candidate.intent_sha256)
    || intent !== null && candidate.intent_sha256 !== intent
    || ![...bootstrapDenied, 'reviewed', 'account_scope_verified', 'held_out_independence_verified', 'execution_authorized', 'runtime_started'].every(key => candidate[key] === false)
    || candidate.model_calls !== 0
    || !['manifest_sha256', 'run_binding_sha256', 'receipt_sha256', 'recipe_audit_sha256', 'source_snapshot_sha256', 'source_fingerprint_sha256', 'form_body_sha256', 'candidate_directory_sha256'].every(key => knowledgeHash(candidate[key]))
    || !object(candidate.bootstrap_intent) || !object(candidate.bootstrap_run_binding) || !object(candidate.bootstrap_receipt)
    || !object(candidate.recipe_audit) || !object(candidate.recipe) || !object(candidate.form_plan) || !object(candidate.parameters)
    || !Array.isArray(candidate.field_bindings) || !object(candidate.bootstrap_intent.source)
    || !object(candidate.bootstrap_intent.source.invocation)
    || !object(candidate.database_identity) || !object(candidate.candidate_parent_identity) || typeof candidate.form_body !== 'string') return null;
  for (const identity of [candidate.database_identity, candidate.candidate_parent_identity]) {
    if (Object.keys(identity).length !== 4 || !['device', 'inode', 'owner', 'mode'].every(key => Number.isSafeInteger(identity[key]) && (identity[key] as number) >= (key === 'inode' ? 1 : 0))) return null;
  }
  const source = candidate.bootstrap_intent.source;
  const invocation = source.invocation as ObjectValue;
  const receipt = candidate.bootstrap_receipt;
  const audit = candidate.recipe_audit;
  const parameters = candidate.parameters;
  if (Object.keys(parameters).length !== 2 || !['record-id', 'note-text'].every(key => typeof parameters[key] === 'string'
    && Array.from(parameters[key] as string).length >= 1 && Array.from(parameters[key] as string).length <= 128
    && new TextEncoder().encode(parameters[key] as string).length <= 256) || candidate.field_bindings.length !== 2) return null;
  const fields: Record<string, string> = {};
  const used = new Set<string>();
  for (const binding of candidate.field_bindings) {
    if (!object(binding) || Object.keys(binding).length !== 2 || typeof binding.parameter_key !== 'string'
      || !Object.hasOwn(parameters, binding.parameter_key) || used.has(binding.parameter_key)
      || typeof binding.form_field_name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(binding.form_field_name)
      || Object.hasOwn(fields, binding.form_field_name)) return null;
    used.add(binding.parameter_key); fields[binding.form_field_name] = parameters[binding.parameter_key] as string;
  }
  if (!['bootstrap_intent', 'bootstrap_run_binding', 'bootstrap_receipt'].every(key => bootstrapDenied.every(flag => (candidate[key] as ObjectValue)[flag] === false))
    || candidate.bootstrap_intent.kind !== 'owned_parameter_project_manual_bootstrap_intent'
    || candidate.bootstrap_run_binding.kind !== 'owned_parameter_project_manual_bootstrap_run_binding'
    || receipt.kind !== 'owned_parameter_project_manual_bootstrap_receipt'
    || candidate.bootstrap_run_binding.intent_sha256 !== candidate.intent_sha256 || receipt.intent_sha256 !== candidate.intent_sha256
    || receipt.run_binding_sha256 !== candidate.run_binding_sha256 || receipt.terminal_status !== 'succeeded'
    || receipt.verification_scope !== candidate.verification_scope
    || !['task_terminal_verified', 'recipe_execution_verified', 'record_outcome_verified'].every(key => receipt[key] === true)
    || knowledgeCanonical(source.scope) !== knowledgeCanonical(candidate.scope)
    || knowledgeCanonical(fields) !== knowledgeCanonical(source.expected_fields)
    || knowledgeCanonical(candidate.bootstrap_run_binding.run_identity) !== knowledgeCanonical(receipt.run_identity)
    || source.manifest_sha256 !== candidate.manifest_sha256
    || audit.schema_version !== '2.0' || audit.status !== 'executable_recipe_execution_verified'
    || audit.source_snapshot_sha256 !== candidate.source_snapshot_sha256 || audit.consumed_approval_count !== 6 || audit.submit_count !== 1
    || !['synthetic', 'transport_readback_verified', 'declared_state_readback_verified', 'invocation_persisted_before_actions', 'invocation_consistent_across_states', 'invocation_execution_verified', 'executable_recipe_executed', 'symbolic_skill_steps_executed', 'skill_executed'].every(key => audit[key] === true)
    || !['skill_validated', 'site_outcome_verified', 'held_out_independence_verified', 'reviewed', 'activation_authorized', 'training_ready'].every(key => audit[key] === false)
    || !Array.isArray(audit.stages) || audit.stages.length !== 6
    || candidate.recipe.synthetic !== true || candidate.recipe.schema_version !== '1.0'
    || !['execution_authorized', 'collection_authorized', 'reviewed', 'activation_authorized', 'training_ready'].every(key => (candidate.recipe as ObjectValue)[key] === false)) return null;
  for (const [content, checksum] of [['bootstrap_intent', 'intent_sha256'], ['bootstrap_run_binding', 'run_binding_sha256'],
    ['bootstrap_receipt', 'receipt_sha256'], ['recipe_audit', 'recipe_audit_sha256']]) {
    if (await knowledgeDigest(candidate[content]) !== candidate[checksum]) return null;
  }
  if (await knowledgeDigest(candidate.recipe) !== invocation.recipe_sha256
    || await knowledgeDigest(candidate.field_bindings) !== invocation.field_binding_sha256
    || await knowledgeDigest(candidate.form_plan) !== invocation.form_plan_sha256
    || await knowledgeTextHash(candidate.form_body) !== candidate.form_body_sha256
    || candidate.form_plan.body_sha256 !== candidate.form_body_sha256
    || new TextEncoder().encode(candidate.form_body).length !== candidate.form_plan.body_bytes) return null;
  return {candidate, candidate_sha256: value.candidate_sha256, candidate_canonical: value.candidate_canonical};
}

export function OwnedParameterSkill({intentSha256, snapshot, disabled}: {intentSha256: string | null; snapshot: Snapshot; disabled: boolean}) {
  useLanguage();
  const [preview, setPreview] = useState<Candidate | null>(null);
  const [result, setResult] = useState<Candidate | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [readHash, setReadHash] = useState('');
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState('');
  const scope = knowledgeCanonical({intentSha256, runtime: snapshot.runtime.runtime_id, control: snapshot.control, disabled});
  const latest = useRef({scope, snapshot, disabled, intentSha256});
  latest.current = {scope, snapshot, disabled, intentSha256};
  const request = useRef<symbol | null>(null);
  const publishAttempted = useRef(false);
  useEffect(() => {setPreview(null); setConfirmed(false); setResult(null); setMessage('');}, [scope]);
  useEffect(() => () => {request.current = null;}, []);
  const writable = !disabled && !pending && !publishAttempted.current && knowledgeHash(intentSha256)
    && snapshot.runtime.running && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';

  async function perform(operation: 'preview' | 'publish' | 'read') {
    if (request.current !== null || operation !== 'read' && !writable
      || operation === 'publish' && (!preview || !confirmed)
      || operation === 'read' && !knowledgeHash(readHash)) return;
    const captured = latest.current;
    const token = Symbol(operation); request.current = token;
    const expectedHash = operation === 'read' ? readHash : operation === 'publish' ? preview!.candidate_sha256 : undefined;
    const current = () => request.current === token && latest.current.scope === captured.scope;
    setPending(true); setMessage(''); setConfirmed(false);
    if (operation === 'preview') {setPreview(null); setResult(null);}
    if (operation === 'publish') {
      setReadHash(expectedHash!);
    }
    try {
      if (operation === 'publish') {
        const fresh = await api<Snapshot>('/api/state');
        if (!current()) return;
        if (fresh.runtime.runtime_id !== captured.snapshot.runtime.runtime_id || !fresh.runtime.running
          || fresh.control.owner !== 'AGENT' || fresh.control.status !== 'running'
          || fresh.control.lease_id !== captured.snapshot.control.lease_id
          || fresh.control.generation !== captured.snapshot.control.generation) throw new Error('stale_control');
        publishAttempted.current = true; setUncertain(true); setPreview(null); setResult(null);
      }
      const body = operation === 'read' ? {schema_version: '1.0', candidate_sha256: expectedHash}
        : {schema_version: '1.0', intent_sha256: captured.intentSha256,
          ...(operation === 'publish' ? {confirm_candidate_sha256: expectedHash,
            human_confirmation: 'PUBLISH_MANUAL_CANDIDATE', lease_id: captured.snapshot.control.lease_id,
            generation: captured.snapshot.control.generation} : {})};
      const value = await api<unknown>(base + '/' + operation, body);
      if (!current()) return;
      const checked = await validOwnedParameterSkill(value, captured.intentSha256, expectedHash);
      if (!current()) return;
      if (!checked) throw new Error('invalid_candidate');
      if (operation === 'preview') {setPreview(checked); setMessage('Önizleme doğrulandı; henüz kaydedilmedi.');}
      else {setResult(checked); setReadHash(checked.candidate_sha256); setUncertain(false); setMessage('Özel aday kaydı doğrulandı.');}
    } catch {
      if (current()) {setPreview(null); setResult(null); setMessage('Aday yanıtı veya kaynak doğrulanamadı.');}
    } finally {
      if (request.current === token) {request.current = null; setPending(false);}
    }
  }

  const shown = preview ?? result;
  return <section className="panel" data-testid="owned-parameter-skill">
    <h3>{t('Manuel bootstrap skill adayı')}</h3>
    <p>{t('Denetlenmiş sentetik manuel yürütme adayıdır. Released skill, gerçek model, etkinleştirme, eğitim veya GPU bırakımı kanıtı değildir.')}</p>
    <button type="button" data-testid="parameter-skill-preview" disabled={!writable}
      onClick={() => void perform('preview')}>{t('Adayı önizle — salt okunur')}</button>
    {shown && <div data-testid="parameter-skill-result">
      <p>{t('İnsan incelemesi bekleniyor.')}</p>
      <p>{t('Aday SHA-256')} <code>{shown.candidate_sha256}</code></p>
      <p>{t('Kapsam')} <code>{knowledgeCanonical(shown.candidate.scope)}</code></p>
      <pre>{shown.candidate_canonical}</pre>
    </div>}
    {preview && <>
      <label><input type="checkbox" data-testid="parameter-skill-confirm" checked={confirmed} disabled={!writable}
        onChange={event => setConfirmed(event.target.checked)}/>{t('Bu exact aday hash’i için özel manuel inceleme kaydını onaylıyorum.')}</label>
      <button type="button" data-testid="parameter-skill-publish" disabled={!writable || !confirmed}
        onClick={() => void perform('publish')}>{t('Manuel adayı kaydet')}</button>
    </>}
    <label>{t('Aday SHA-256')}<input data-testid="parameter-skill-read-hash" value={readHash} maxLength={64} disabled={pending}
      onChange={event => {setReadHash(event.target.value); setResult(null); setPreview(null); setConfirmed(false);}}/></label>
    <button type="button" data-testid="parameter-skill-read" disabled={pending || !knowledgeHash(readHash)}
      onClick={() => void perform('read')}>{t('Kaydedilmiş adayı yeniden oku')}</button>
    {pending && <p role="status">{t('Aday kanıtı denetleniyor…')}</p>}
    {uncertain && <p role="alert" data-testid="parameter-skill-uncertain">{t('Kayıt sonucu belirsiz. Tekrar gönderilmez; exact hash ile salt okunur denetim yapın.')}</p>}
    {message && <p role="status" data-testid="parameter-skill-message">{t(message)}</p>}
  </section>;
}
