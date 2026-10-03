import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';

type ObjectValue = Record<string, unknown>;
type Kind = 'release' | 'selection';
type Operation = 'preview' | 'release' | 'read' | 'inventory' | 'select-preview' | 'select' | 'rollback-preview' | 'rollback';
type Evidence = {kind: Kind; value: ObjectValue; canonical: string; checksum: string};
type RecoveryEvidence = {value: ObjectValue; canonical: string; checksum: string; record: Evidence};
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: ObjectValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
const flags = ['native_model_verified', 'execution_authorized', 'activation_authorized', 'training_ready', 'gpu_release_verified'];
const denied = [...flags, 'released_skill_verified', 'site_outcome_verified', 'account_scope_verified', 'held_out_independence_verified', 'replay_authorized', 'runtime_started'];
const commonKeys = ['schema_version', 'kind', 'purpose', 'synthetic', 'development_only', 'reviewed', 'model_calls',
  'review_sha256', 'candidate_sha256', 'scope', 'family_sha256', ...denied];
const sourceKeys = ['source_fingerprint_sha256', 'release_directory_sha256', 'release_parent_identity', 'database_identity', 'actor'];
const releaseKeys = ['family', 'revision', 'parent_release_sha256', 'review_directory_sha256', 'manifest_sha256', 'source_run_ref', 'recipe_sha256'];
const selectionKeys = ['operation', 'sequence', 'previous_selection_sha256', 'previous_release_sha256', 'release_sha256'];
const nullableHash = (value: unknown) => value === null || knowledgeHash(value);
const ordinal = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 64;
function validScope(value: unknown): boolean {
  return validKnowledgeScope(value) && ['synthetic-crm-note', 'synthetic-inventory-note'].includes(value.application_id)
    && value.tenant_id === 'synthetic-tenant' && value.account_role === 'editor';
}
function identity(value: unknown): boolean {
  return object(value) && exact(value, ['device', 'inode', 'owner', 'mode'])
    && ['device', 'inode', 'owner', 'mode'].every(key => Number.isSafeInteger(value[key]) && (value[key] as number) >= (key === 'inode' ? 1 : 0));
}
function family(value: unknown): value is ObjectValue {
  return object(value) && exact(value, ['provenance_kind', 'scope', 'task_key', 'field_binding_sha256', 'outcome_sha256'])
    && value.provenance_kind === 'manual_authored_synthetic_project' && validScope(value.scope)
    && typeof value.task_key === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value.task_key)
    && knowledgeHash(value.field_binding_sha256) && knowledgeHash(value.outcome_sha256);
}
function common(value: ObjectValue, kind: Kind): boolean {
  return value.schema_version === '1.0' && value.kind === 'owned_parameter_skill_manual_' + kind
    && value.purpose === (kind === 'release' ? 'development_release' : 'development_selected')
    && value.synthetic === true && value.development_only === true && value.reviewed === true && value.model_calls === 0
    && denied.every(key => value[key] === false) && validScope(value.scope)
    && ['review_sha256', 'candidate_sha256', 'family_sha256'].every(key => knowledgeHash(value[key]));
}
function chainFields(value: ObjectValue, kind: Kind): boolean {
  if (kind === 'release') return ordinal(value.revision) && nullableHash(value.parent_release_sha256)
    && (value.revision === 1) === (value.parent_release_sha256 === null) && knowledgeHash(value.recipe_sha256);
  return ordinal(value.sequence) && ['select', 'rollback'].includes(String(value.operation)) && knowledgeHash(value.release_sha256)
    && nullableHash(value.previous_selection_sha256) && nullableHash(value.previous_release_sha256)
    && (value.sequence === 1) === (value.previous_selection_sha256 === null)
    && (value.sequence === 1) === (value.previous_release_sha256 === null)
    && (value.operation !== 'rollback' || value.sequence !== 1);
}
export async function validOwnedParameterSkillRelease(value: unknown, kind: Kind): Promise<Evidence | null> {
  if (!object(value) || !exact(value, ['schema_version', kind, kind + '_canonical', kind + '_sha256', ...flags])
    || value.schema_version !== '1.0' || !flags.every(key => value[key] === false) || !object(value[kind])
    || typeof value[kind + '_canonical'] !== 'string' || !knowledgeHash(value[kind + '_sha256'])) return null;
  const content = value[kind] as ObjectValue;
  const canonical = value[kind + '_canonical'] as string;
  const checksum = value[kind + '_sha256'] as string;
  if (canonical.length > 65536 || canonical !== knowledgeCanonical(content) || await knowledgeTextHash(canonical) !== checksum
    || !exact(content, [...commonKeys, ...sourceKeys, ...(kind === 'release' ? releaseKeys : selectionKeys)])
    || !common(content, kind) || !chainFields(content, kind) || content.actor !== 'local_authenticated_user'
    || !identity(content.database_identity) || !identity(content.release_parent_identity)
    || !knowledgeHash(content.source_fingerprint_sha256) || !knowledgeHash(content.release_directory_sha256)) return null;
  if (kind === 'release' && (!family(content.family) || knowledgeCanonical(content.scope) !== knowledgeCanonical(content.family.scope)
    || await knowledgeDigest(content.family) !== content.family_sha256
    || !['review_directory_sha256', 'manifest_sha256', 'source_run_ref'].every(key => knowledgeHash(content[key])))) return null;
  return {kind, value: content, canonical, checksum};
}
export async function validOwnedParameterSkillReleaseRecovery(value: unknown): Promise<RecoveryEvidence | null> {
  if (!object(value) || !exact(value, ['schema_version', 'recovery', 'recovery_canonical', 'recovery_sha256', ...flags])
    || value.schema_version !== '1.0' || !flags.every(key => value[key] === false) || !object(value.recovery)
    || typeof value.recovery_canonical !== 'string' || !knowledgeHash(value.recovery_sha256)) return null;
  const content = value.recovery;
  const canonical = value.recovery_canonical;
  if (canonical.length > 65536 || canonical !== knowledgeCanonical(content)
    || await knowledgeTextHash(canonical) !== value.recovery_sha256
    || !exact(content, ['schema_version', 'kind', 'operation', 'synthetic', 'development_only', 'reviewed', 'model_calls',
      'record_sha256', 'record_stage', 'record', 'release_sha256', 'family_sha256', 'scope', 'database_identity',
      'release_directory_sha256', 'release_parent_identity', 'release_store_identity', ...denied])
    || content.schema_version !== '1.0' || content.kind !== 'owned_parameter_skill_release_recovery'
    || content.operation !== 'restore_anchored_release_record' || content.synthetic !== true
    || content.development_only !== true || content.reviewed !== false || content.model_calls !== 0
    || !denied.every(key => content[key] === false) || !validScope(content.scope)
    || !['record_sha256', 'release_sha256', 'family_sha256', 'release_directory_sha256'].every(key => knowledgeHash(content[key]))
    || !['database_identity', 'release_parent_identity', 'release_store_identity'].every(key => identity(content[key]))
    || (content.record_stage !== 'release' && content.record_stage !== 'selection') || !object(content.record)) return null;
  const kind = content.record_stage;
  const record = await validOwnedParameterSkillRelease({schema_version: '1.0', [kind]: content.record,
    [kind + '_canonical']: knowledgeCanonical(content.record), [kind + '_sha256']: content.record_sha256,
    ...Object.fromEntries(flags.map(key => [key, false]))}, kind);
  if (!record || content.release_sha256 !== (kind === 'release' ? record.checksum : record.value.release_sha256)
    || !['family_sha256', 'scope', 'database_identity', 'release_directory_sha256', 'release_parent_identity'].every(
      key => knowledgeCanonical(content[key]) === knowledgeCanonical(record.value[key]))) return null;
  return {value: content, canonical, checksum: value.recovery_sha256, record};
}
function summary(value: unknown, kind: Kind): value is ObjectValue {
  return object(value) && exact(value, [...commonKeys, 'record_sha256', ...(kind === 'release'
    ? ['release_sha256', 'revision', 'parent_release_sha256', 'recipe_sha256']
    : ['selection_sha256', ...selectionKeys])]) && common(value, kind) && chainFields(value, kind)
    && knowledgeHash(value.record_sha256) && value.record_sha256 === value[kind + '_sha256'];
}
export async function validOwnedParameterSkillReleaseInventory(value: unknown): Promise<ObjectValue | null> {
  if (!object(value) || !exact(value, ['schema_version', 'inventory', ...flags]) || value.schema_version !== '1.0'
    || !flags.every(key => value[key] === false) || !object(value.inventory)) return null;
  const inventory = value.inventory;
  if (!exact(inventory, ['schema_version', 'kind', 'synthetic', 'development_only', 'source_current_verified',
    'execution_authorized', 'activation_authorized', 'training_ready', 'families']) || inventory.schema_version !== '1.0'
    || inventory.kind !== 'owned_parameter_skill_manual_release_inventory' || inventory.synthetic !== true || inventory.development_only !== true
    || !['source_current_verified', 'execution_authorized', 'activation_authorized', 'training_ready'].every(key => inventory[key] === false)
    || !Array.isArray(inventory.families) || inventory.families.length > 64) return null;
  const usedFamilies = new Set<string>();
  const usedRecords = new Set<string>();
  let total = 0;
  for (const group of inventory.families) {
    if (!object(group) || !exact(group, ['family_sha256', 'family', 'releases', 'selection_sha256', 'selected_release_sha256', 'sequence', 'selection'])
      || !family(group.family) || !knowledgeHash(group.family_sha256) || usedFamilies.has(group.family_sha256)
      || await knowledgeDigest(group.family) !== group.family_sha256 || !Array.isArray(group.releases)
      || group.releases.length === 0 || group.releases.length > 64 || !nullableHash(group.selection_sha256)
      || !nullableHash(group.selected_release_sha256) || !Number.isSafeInteger(group.sequence) || (group.sequence as number) < 0 || (group.sequence as number) > 64) return null;
    usedFamilies.add(group.family_sha256);
    let previous: string | null = null;
    for (const [index, release] of group.releases.entries()) {
      if (!summary(release, 'release') || release.family_sha256 !== group.family_sha256 || release.revision !== index + 1
        || release.parent_release_sha256 !== previous || knowledgeCanonical(release.scope) !== knowledgeCanonical(group.family.scope)
        || usedRecords.has(release.release_sha256 as string)) return null;
      previous = release.release_sha256 as string; usedRecords.add(previous); total += 1;
    }
    const selection = group.selection;
    if (group.sequence === 0 ? selection !== null || group.selection_sha256 !== null || group.selected_release_sha256 !== null
      : !summary(selection, 'selection') || selection.sequence !== group.sequence
        || selection.selection_sha256 !== group.selection_sha256 || selection.release_sha256 !== group.selected_release_sha256
        || selection.family_sha256 !== group.family_sha256
        || knowledgeCanonical(selection.scope) !== knowledgeCanonical(group.family.scope)
        || !group.releases.some(release => release.release_sha256 === group.selected_release_sha256
          && release.review_sha256 === selection.review_sha256 && release.candidate_sha256 === selection.candidate_sha256)) return null;
    total += group.sequence as number;
    if (total > 64) return null;
  }
  return inventory;
}

export function OwnedParameterSkillRelease({snapshot, disabled}: {snapshot: Snapshot; disabled: boolean}) {
  useLanguage();
  const [reviewHash, setReviewHash] = useState('');
  const [parentHash, setParentHash] = useState('');
  const [readHash, setReadHash] = useState('');
  const [targetHash, setTargetHash] = useState('');
  const [headHash, setHeadHash] = useState('');
  const [recoveryHash, setRecoveryHash] = useState('');
  const [recovery, setRecovery] = useState<RecoveryEvidence | null>(null);
  const [recovered, setRecovered] = useState<RecoveryEvidence | null>(null);
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const [proposal, setProposal] = useState<Evidence | null>(null);
  const [selection, setSelection] = useState<{evidence: Evidence; operation: 'select' | 'rollback'} | null>(null);
  const [result, setResult] = useState<Evidence | null>(null);
  const [inventory, setInventory] = useState<ObjectValue | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [selectionConfirmed, setSelectionConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState('');
  const scope = knowledgeCanonical({runtime: snapshot.runtime.runtime_id, running: snapshot.runtime.running,
    control: snapshot.control, disabled, reviewHash, parentHash, targetHash, headHash, recoveryHash});
  const latest = useRef({scope, snapshot}); latest.current = {scope, snapshot};
  const request = useRef<symbol | null>(null);
  const attempted = useRef(new Set<string>());
  const unresolved = useRef<{kind: Kind; checksum: string} | null>(null);
  useEffect(() => {setProposal(null); setSelection(null); setResult(null); setInventory(null); setConfirmed(false); setSelectionConfirmed(false);
    setRecovery(null); setRecovered(null); setRecoveryConfirmed(false); setMessage('');}, [scope]);
  useEffect(() => () => {request.current = null;}, []);
  const writable = !disabled && !pending && !uncertain && snapshot.runtime.running && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';
  const releaseInputs = knowledgeHash(reviewHash) && (parentHash === '' || knowledgeHash(parentHash));
  const selectionInputs = knowledgeHash(targetHash) && (headHash === '' || knowledgeHash(headHash));
  async function perform(operation: Operation) {
    const write = ['release', 'select', 'rollback'].includes(operation);
    const selecting = operation.startsWith('select') || operation.startsWith('rollback');
    const selected = operation === 'release' ? proposal : write && selecting ? selection?.evidence : null;
    const attemptKey = operation + ':' + selected?.checksum;
    if (request.current !== null || write && (!writable || !selected || attempted.current.has(attemptKey))
      || operation === 'release' && !confirmed || write && selecting && !selectionConfirmed
      || selecting && !selectionInputs || (operation === 'preview' || operation === 'release') && !releaseInputs
      || operation === 'read' && !knowledgeHash(readHash)
      || write && selecting && selection?.operation !== operation) return;
    const captured = latest.current;
    const token = Symbol(operation); request.current = token; setPending(true); setMessage(''); setConfirmed(false); setSelectionConfirmed(false);
    setResult(null); setInventory(null); setRecovery(null); setRecovered(null); setRecoveryConfirmed(false);
    const current = () => request.current === token && latest.current.scope === captured.scope;
    try {
      if (write) {
        const fresh = await api<Snapshot>('/api/state');
        if (!current()) return;
        if (!fresh.runtime.running || fresh.runtime.runtime_id !== captured.snapshot.runtime.runtime_id || fresh.control.owner !== 'AGENT'
          || fresh.control.status !== 'running' || fresh.control.lease_id !== captured.snapshot.control.lease_id
          || fresh.control.generation !== captured.snapshot.control.generation) throw new Error('stale_control');
        attempted.current.add(attemptKey); unresolved.current = {kind: selected!.kind, checksum: selected!.checksum};
        if (operation === 'release') setReadHash(selected!.checksum);
        setUncertain(true); setProposal(null); setSelection(null);
      }
      const body = {schema_version: '1.0',
        ...(operation === 'inventory' ? {} : operation === 'read' ? {release_sha256: readHash}
          : selecting ? {release_sha256: targetHash, expected_selection_sha256: headHash || null}
          : {review_sha256: reviewHash, expected_parent_release_sha256: parentHash || null}),
        ...(operation === 'release' ? {confirm_release_sha256: selected!.checksum, human_confirmation: 'RELEASE_MANUAL_SKILL'} : {}),
        ...(write && selecting ? {confirm_selection_sha256: selected!.checksum, human_confirmation: operation === 'select' ? 'SELECT_MANUAL_SKILL' : 'ROLLBACK_MANUAL_SKILL'} : {}),
        ...(write ? {lease_id: captured.snapshot.control.lease_id, generation: captured.snapshot.control.generation} : {})};
      const value = await api<unknown>('/api/tasks/parameter-project-skill-release/' + operation, body);
      if (!current()) return;
      if (operation === 'inventory') {
        const checked = await validOwnedParameterSkillReleaseInventory(value);
        if (!current()) return;
        if (!checked) throw new Error('invalid_inventory');
        setInventory(checked); setMessage('Geçmiş envanter doğrulandı.');
        if (unresolved.current?.kind === 'selection' && (checked.families as ObjectValue[]).some(group => group.selection_sha256 === unresolved.current!.checksum)) {
          setUncertain(false); unresolved.current = null;
        }
      } else {
        const checked = await validOwnedParameterSkillRelease(value, selecting ? 'selection' : 'release');
        if (!current()) return;
        if (!checked || write && checked.checksum !== selected!.checksum
          || operation === 'read' && checked.checksum !== readHash
          || !selecting && operation !== 'read' && (checked.value.review_sha256 !== reviewHash || checked.value.parent_release_sha256 !== (parentHash || null))
          || selecting && (checked.value.release_sha256 !== targetHash || checked.value.previous_selection_sha256 !== (headHash || null)
            || checked.value.operation !== (operation.startsWith('rollback') ? 'rollback' : 'select'))) throw new Error('invalid_release');
        if (operation === 'preview') {setProposal(checked); setMessage('Önizleme doğrulandı; kayıt henüz yazılmadı.');}
        else if (operation.endsWith('preview')) {setSelection({evidence: checked, operation: operation.startsWith('rollback') ? 'rollback' : 'select'}); setMessage('Önizleme doğrulandı; kayıt henüz yazılmadı.');}
        else {
          setResult(checked); setMessage(write ? 'Manuel metadata kaydı doğrulandı; runtime etkinleştirilmedi.' : 'Sürüm kaydı ve güncel kaynağı doğrulandı.');
          if (write || unresolved.current?.kind === checked.kind && unresolved.current.checksum === checked.checksum) {setUncertain(false); unresolved.current = null;}
        }
      }
    } catch {
      if (current()) {setProposal(null); setSelection(null); setResult(null); setInventory(null); setMessage('Sürüm yanıtı veya güncel kaynak doğrulanamadı.');}
    } finally {if (request.current === token) {request.current = null; setPending(false);}}
  }
  async function performRecovery(write: boolean) {
    const selected = recovery;
    const attemptKey = 'recover:' + recoveryHash;
    if (request.current !== null || !knowledgeHash(recoveryHash)
      || write && (!writable || !selected || !recoveryConfirmed || attempted.current.has(attemptKey))) return;
    const captured = latest.current;
    const token = Symbol('recovery'); request.current = token;
    setPending(true); setMessage(''); setRecoveryConfirmed(false); setRecovered(null);
    setProposal(null); setSelection(null); setResult(null); setInventory(null); setConfirmed(false); setSelectionConfirmed(false);
    const current = () => request.current === token && latest.current.scope === captured.scope;
    try {
      if (write) {
        const fresh = await api<Snapshot>('/api/state');
        if (!current()) return;
        if (!fresh.runtime.running || fresh.runtime.runtime_id !== captured.snapshot.runtime.runtime_id
          || fresh.control.owner !== 'AGENT' || fresh.control.status !== 'running'
          || knowledgeCanonical(fresh.control) !== knowledgeCanonical(captured.snapshot.control)) throw new Error('stale_control');
        attempted.current.add(attemptKey);
        unresolved.current = {kind: selected!.record.kind, checksum: selected!.record.checksum};
        if (selected!.record.kind === 'release') setReadHash(selected!.record.checksum);
        setUncertain(true);
      }
      setRecovery(null);
      const value = await api<unknown>('/api/tasks/parameter-project-skill-release/' + (write ? 'recover' : 'recovery-preview'), {
        schema_version: '1.0', record_sha256: recoveryHash,
        ...(write ? {confirm_recovery_sha256: selected!.checksum, human_confirmation: 'RESTORE_ANCHORED_RELEASE_RECORD',
          lease_id: captured.snapshot.control.lease_id, generation: captured.snapshot.control.generation} : {})});
      if (!current()) return;
      const checked = await validOwnedParameterSkillReleaseRecovery(value);
      if (!current()) return;
      if (!checked || checked.record.checksum !== recoveryHash || write && (checked.checksum !== selected!.checksum
        || checked.canonical !== selected!.canonical)) throw new Error('invalid_recovery');
      if (write) {
        setRecovered(checked); setUncertain(false); unresolved.current = null;
        setMessage('Eksik sabitlenmiş metadata kaydı geri yüklendi; yeni sürüm, seçim veya yürütme yetkisi oluşturulmadı.');
      } else {
        setRecovery(checked); setMessage('Kurtarma önizlemesi doğrulandı; eksik kayıt henüz geri yüklenmedi.');
      }
    } catch {
      if (current()) {setRecovery(null); setRecovered(null); setMessage('Kurtarma yanıtı veya güncel kaynak doğrulanamadı.');}
    } finally {if (request.current === token) {request.current = null; setPending(false);}}
  }
  return <section className="panel" data-testid="parameter-skill-release">
    <h3>{t('Manuel skill geliştirme sürümü')}</h3>
    <p>{t('Yalnız incelenmiş sentetik manuel metadata. Sürüm veya seçim araç yürütme, yeniden kullanım, gerçek model, etkinleştirme veya eğitim izni değildir.')}</p>
    <p>{t('Özgün proje kaynağı sabittir; başka sürümün güncel kaynak denetimi ayrı exact proje yapılandırması gerektirir.')}</p>
    <label>{t('Kabul edilmiş inceleme SHA-256')}<input data-testid="manual-release-review" value={reviewHash} maxLength={64} disabled={pending} onChange={event => setReviewHash(event.target.value)}/></label>
    <label>{t('Beklenen önceki sürüm SHA-256 (boş = önceki sürüm yok)')}<input data-testid="manual-release-parent" value={parentHash} maxLength={64} disabled={pending} onChange={event => setParentHash(event.target.value)}/></label>
    <button type="button" data-testid="manual-release-preview" disabled={pending || !releaseInputs} onClick={() => void perform('preview')}>{t('Manuel sürümü önizle')}</button>
    {proposal && <><pre data-testid="manual-release-proposal">{proposal.canonical}</pre>
      <label><input type="checkbox" data-testid="manual-release-confirm" checked={confirmed} disabled={!writable} onChange={event => setConfirmed(event.target.checked)}/>{t('Gösterilen exact sürüm hash’iyle özel geliştirme sürümü kaydını onaylıyorum.')}</label>
      <button type="button" data-testid="manual-release-write" disabled={!writable || !confirmed || attempted.current.has('release:' + proposal.checksum)} onClick={() => void perform('release')}>{t('Manuel sürümü kaydet')}</button></>}
    <label>{t('Sürüm SHA-256')}<input data-testid="manual-release-read-hash" value={readHash} maxLength={64} disabled={pending} onChange={event => {setReadHash(event.target.value); setResult(null);}}/></label>
    <button type="button" data-testid="manual-release-read" disabled={pending || !knowledgeHash(readHash)} onClick={() => void perform('read')}>{t('Sürümü yeniden oku')}</button>
    <button type="button" data-testid="manual-release-inventory" disabled={pending} onClick={() => void perform('inventory')}>{t('Geçmiş sürüm ve seçim envanterini oku')}</button>
    {inventory && <><p>{t('Envanter geçmiş metadata’dır; güncel kaynak veya yeniden kullanım yetkisini kanıtlamaz.')}</p><pre data-testid="manual-release-history">{knowledgeCanonical(inventory)}</pre></>}
    <label>{t('Hedef sürüm SHA-256')}<input data-testid="manual-selection-target" value={targetHash} maxLength={64} disabled={pending} onChange={event => setTargetHash(event.target.value)}/></label>
    <label>{t('Beklenen seçim SHA-256 (boş = seçim yok)')}<input data-testid="manual-selection-head" value={headHash} maxLength={64} disabled={pending} onChange={event => setHeadHash(event.target.value)}/></label>
    <button type="button" data-testid="manual-select-preview" disabled={pending || !selectionInputs} onClick={() => void perform('select-preview')}>{t('Seçimi önizle')}</button>
    <button type="button" data-testid="manual-rollback-preview" disabled={pending || !selectionInputs} onClick={() => void perform('rollback-preview')}>{t('Önceki seçilmiş sürüme dönüşü önizle')}</button>
    {selection && <><pre data-testid="manual-selection-proposal">{selection.evidence.canonical}</pre>
      <label><input type="checkbox" data-testid="manual-selection-confirm" checked={selectionConfirmed} disabled={!writable} onChange={event => setSelectionConfirmed(event.target.checked)}/>{t('Gösterilen exact seçim hash’iyle yalnız manuel geliştirme seçimini onaylıyorum.')}</label>
      <button type="button" data-testid="manual-selection-write" disabled={!writable || !selectionConfirmed || attempted.current.has(selection.operation + ':' + selection.evidence.checksum)} onClick={() => void perform(selection.operation)}>{t(selection.operation === 'select' ? 'Manuel sürümü seç' : 'Manuel sürüm seçimini geri al')}</button></>}
    {result && <pre data-testid="manual-release-result">{result.canonical}</pre>}
    <h4>{t('Eksik sabitlenmiş kaydı kurtar')}</h4>
    <p>{t('Yalnız mevcut geçmişe sabitlenmiş eksik dosya geri yüklenir. Yeni sürüm, seçim, etkinleştirme veya yürütme izni oluşturulmaz; otomatik kurtarma yoktur.')}</p>
    <label>{t('Eksik sürüm veya seçim kaydı SHA-256')}<input data-testid="manual-recovery-record" value={recoveryHash} maxLength={64}
      disabled={pending} onChange={event => setRecoveryHash(event.target.value)}/></label>
    <button type="button" data-testid="manual-recovery-preview" disabled={pending || !knowledgeHash(recoveryHash)}
      onClick={() => void performRecovery(false)}>{t('Eksik kayıt kurtarmasını önizle')}</button>
    {recovery && <><pre data-testid="manual-recovery-proposal">{recovery.canonical}</pre>
      <p>{t('Kurtarma onayı SHA-256')}: <code data-testid="manual-recovery-hash">{recovery.checksum}</code></p>
      <label><input type="checkbox" data-testid="manual-recovery-confirm" checked={recoveryConfirmed} disabled={!writable}
        onChange={event => setRecoveryConfirmed(event.target.checked)}/>{t('Gösterilen exact kurtarma hash’iyle yalnız sabitlenmiş eksik kaydın geri yüklenmesini onaylıyorum.')}</label>
      <button type="button" data-testid="manual-recovery-write" disabled={!writable || !recoveryConfirmed || attempted.current.has('recover:' + recoveryHash)}
        onClick={() => void performRecovery(true)}>{t('Sabitlenmiş eksik kaydı geri yükle')}</button></>}
    {recovered && <pre data-testid="manual-recovery-result">{recovered.canonical}</pre>}
    {pending && <p role="status">{t('Sürüm kanıtı denetleniyor…')}</p>}
    {uncertain && <p role="alert" data-testid="manual-release-uncertain">{t('Metadata yazma sonucu belirsiz. Tekrar gönderilmez; bilinen sürümü veya seçim envanterini salt okunur denetleyin.')}</p>}
    {message && <p role="status" data-testid="manual-release-message">{t(message)}</p>}
  </section>;
}
