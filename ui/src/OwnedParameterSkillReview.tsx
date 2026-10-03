import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {t, useLanguage} from './i18n';
import {knowledgeCanonical, knowledgeDigest, knowledgeHash, knowledgeTextHash, validKnowledgeScope} from './knowledgeApi';

type ObjectValue = Record<string, unknown>;
type Operation = 'preview' | 'accept' | 'read' | 'revoke-preview' | 'revoke' | 'recovery-preview' | 'recover';
type RecordKind = 'review' | 'status' | 'revocation' | 'recovery';
type Evidence = {kind: RecordKind; value: ObjectValue; canonical: string; checksum: string};
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === 'object' && !Array.isArray(value);
const flags = ['native_model_verified', 'activation_authorized', 'execution_authorized', 'training_ready', 'gpu_release_verified'];
const denied = [...flags, 'released_skill_verified', 'site_outcome_verified', 'replay_authorized', 'account_scope_verified', 'held_out_independence_verified', 'runtime_started'];
const commonKeys = ['schema_version', 'kind', 'synthetic', 'development_only', 'reviewed', 'model_calls', ...denied];
const scopedKeys = ['scope', 'review_directory_sha256', 'review_parent_identity'];
const decisionKeys = ['decision', 'review_scope', 'reviewer'];
const reviewHashes = ['candidate_sha256', 'intent_sha256', 'manifest_sha256', 'source_fingerprint_sha256', 'source_run_ref',
  'source_invocation_sha256', 'recipe_audit_sha256', 'recipe_sha256', 'field_binding_sha256', 'form_body_sha256'];
const exact = (value: ObjectValue, keys: string[]) => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
function common(value: ObjectValue): boolean {
  return value.schema_version === '1.0' && value.synthetic === true && value.development_only === true
    && denied.every(key => value[key] === false) && value.model_calls === 0;
}
function scoped(value: ObjectValue): boolean {
  return validKnowledgeScope(value.scope) && ['synthetic-crm-note', 'synthetic-inventory-note'].includes(value.scope.application_id)
    && value.scope.tenant_id === 'synthetic-tenant' && value.scope.account_role === 'editor'
    && knowledgeHash(value.review_directory_sha256) && object(value.review_parent_identity)
    && Object.keys(value.review_parent_identity).length === 4
    && ['device', 'inode', 'owner', 'mode'].every(key => Number.isSafeInteger((value.review_parent_identity as ObjectValue)[key])
      && ((value.review_parent_identity as ObjectValue)[key] as number) >= (key === 'inode' ? 1 : 0));
}
function validReview(value: ObjectValue): boolean {
  return exact(value, [...commonKeys, ...scopedKeys, ...decisionKeys, ...reviewHashes])
    && common(value) && scoped(value) && value.kind === 'owned_parameter_skill_manual_review'
    && value.decision === 'accept' && value.review_scope === 'synthetic_manual_recipe_review'
    && value.reviewer === 'local_authenticated_user' && value.reviewed === true
    && reviewHashes.every(key => knowledgeHash(value[key]));
}
function validRevocation(value: ObjectValue): boolean {
  return exact(value, [...commonKeys, ...scopedKeys, ...decisionKeys, 'review_sha256', 'candidate_sha256', 'source_fingerprint_sha256'])
    && common(value) && scoped(value) && value.kind === 'owned_parameter_skill_manual_review_revocation'
    && value.decision === 'revoke' && value.review_scope === 'synthetic_manual_recipe_review'
    && value.reviewer === 'local_authenticated_user' && value.reviewed === false
    && ['review_sha256', 'candidate_sha256', 'source_fingerprint_sha256'].every(key => knowledgeHash(value[key]));
}
function sameRevocationSource(review: ObjectValue, revocation: ObjectValue): boolean {
  return ['candidate_sha256', 'source_fingerprint_sha256', ...scopedKeys].every(key => knowledgeCanonical(review[key]) === knowledgeCanonical(revocation[key]));
}

export async function validOwnedParameterSkillReview(value: unknown, kind: RecordKind, expected: string): Promise<Evidence | null> {
  const keys = ['schema_version', kind, kind + '_canonical', kind + '_sha256', ...flags];
  if (!object(value) || Object.keys(value).length !== keys.length || !keys.every(key => Object.hasOwn(value, key))
    || value.schema_version !== '1.0' || !flags.every(key => value[key] === false)
    || !object(value[kind]) || typeof value[kind + '_canonical'] !== 'string' || !knowledgeHash(value[kind + '_sha256'])) return null;
  const content = value[kind] as ObjectValue;
  const canonical = value[kind + '_canonical'] as string;
  const checksum = value[kind + '_sha256'] as string;
  if (canonical.length > 262144 || canonical !== knowledgeCanonical(content) || await knowledgeTextHash(canonical) !== checksum
    || !common(content)) return null;
  if (kind === 'review' ? content.candidate_sha256 !== expected : kind === 'recovery'
    ? content.record_sha256 !== expected : content.review_sha256 !== expected) return null;
  if (kind === 'recovery') {
    if (!exact(content, [...commonKeys, ...scopedKeys, 'operation', 'record_sha256', 'record_stage', 'record',
      'candidate_sha256', 'review_sha256', 'source_fingerprint_sha256', 'review_store_identity'])
      || content.kind !== 'owned_parameter_skill_review_recovery' || content.operation !== 'restore_anchored_review_record'
      || content.reviewed !== false || !scoped(content) || !object(content.record) || !object(content.review_store_identity)
      || !exact(content.review_store_identity, ['device', 'inode', 'owner', 'mode'])
      || !['device', 'inode', 'owner', 'mode'].every(key => Number.isSafeInteger((content.review_store_identity as ObjectValue)[key])
        && ((content.review_store_identity as ObjectValue)[key] as number) >= (key === 'inode' ? 1 : 0))
      || !['candidate_sha256', 'review_sha256', 'record_sha256', 'source_fingerprint_sha256'].every(key => knowledgeHash(content[key]))
      || !sameRevocationSource(content.record, content) || await knowledgeDigest(content.record) !== content.record_sha256) return null;
    if (content.record_stage === 'accept' ? !validReview(content.record) || content.review_sha256 !== content.record_sha256
      : content.record_stage !== 'revoke' || !validRevocation(content.record) || content.record.review_sha256 !== content.review_sha256) return null;
  }
  if (kind === 'review' && !validReview(content) || kind === 'revocation' && !validRevocation(content)) return null;
  if (kind === 'status') {
    if (!exact(content, [...commonKeys, 'review_sha256', 'review', 'revocations', 'status'])
      || content.kind !== 'owned_parameter_skill_manual_review_status' || !object(content.review) || !validReview(content.review)
      || await knowledgeDigest(content.review) !== content.review_sha256 || !Array.isArray(content.revocations)
      || content.revocations.length > 1 || content.status !== (content.revocations.length ? 'revoked' : 'accepted')
      || content.reviewed !== (content.revocations.length === 0)) return null;
    for (const entry of content.revocations) {
      if (!object(entry) || Object.keys(entry).length !== 2 || !object(entry.revocation) || !validRevocation(entry.revocation)
        || entry.revocation.review_sha256 !== content.review_sha256 || entry.revocation.candidate_sha256 !== content.review.candidate_sha256
        || !sameRevocationSource(content.review, entry.revocation)
        || await knowledgeDigest(entry.revocation) !== entry.revocation_sha256) return null;
    }
  }
  return {kind, value: content, canonical, checksum};
}

export function OwnedParameterSkillReview({snapshot, disabled}: {snapshot: Snapshot; disabled: boolean}) {
  useLanguage();
  const [candidateHash, setCandidateHash] = useState('');
  const [reviewHash, setReviewHash] = useState('');
  const [recordHash, setRecordHash] = useState('');
  const [review, setReview] = useState<Evidence | null>(null);
  const [revocation, setRevocation] = useState<Evidence | null>(null);
  const [recovery, setRecovery] = useState<Evidence | null>(null);
  const [result, setResult] = useState<Evidence | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [revokeConfirmed, setRevokeConfirmed] = useState(false);
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [message, setMessage] = useState('');
  const scope = knowledgeCanonical({runtime: snapshot.runtime.runtime_id, control: snapshot.control, disabled, candidateHash});
  const latest = useRef({scope, snapshot}); latest.current = {scope, snapshot};
  const request = useRef<symbol | null>(null);
  const attempted = useRef(new Set<string>());
  const uncertainReview = useRef<string | null>(null);
  useEffect(() => {setReview(null); setRevocation(null); setRecovery(null); setResult(null); setConfirmed(false); setRevokeConfirmed(false); setRecoveryConfirmed(false); setMessage('');}, [scope]);
  useEffect(() => () => {request.current = null;}, []);
  const writable = !disabled && !pending && !uncertain && snapshot.runtime.running
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';
  const acceptedStatus = result?.kind === 'status' && result.value.review_sha256 === reviewHash && result.value.status === 'accepted';
  const recoverWritable = !disabled && !pending && snapshot.runtime.running
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running';

  async function perform(operation: Operation) {
    const recoveryOperation = operation === 'recovery-preview' || operation === 'recover';
    const write = operation === 'accept' || operation === 'revoke' || operation === 'recover';
    const hash = recoveryOperation ? recordHash : operation === 'preview' || operation === 'accept' ? candidateHash : reviewHash;
    const selected = operation === 'accept' ? review : operation === 'revoke' ? revocation : operation === 'recover' ? recovery : null;
    const sourceReview = operation === 'revoke-preview' && result?.kind === 'status' ? result.value.review : null;
    const writeKey = operation + ':' + selected?.checksum;
    if (request.current !== null || !knowledgeHash(hash) || write && (!(operation === 'recover' ? recoverWritable : writable) || !selected || attempted.current.has(writeKey))
      || operation === 'revoke-preview' && !acceptedStatus
      || operation === 'accept' && !confirmed || operation === 'revoke' && !revokeConfirmed || operation === 'recover' && !recoveryConfirmed) return;
    const captured = latest.current;
    const token = Symbol(operation); request.current = token; setPending(true); setMessage('');
    setConfirmed(false); setRevokeConfirmed(false); setRecoveryConfirmed(false); setResult(null);
    const current = () => request.current === token && latest.current.scope === captured.scope;
    try {
      if (write) {
        const fresh = await api<Snapshot>('/api/state');
        if (!current()) return;
        if (!fresh.runtime.running || fresh.runtime.runtime_id !== captured.snapshot.runtime.runtime_id
          || fresh.control.owner !== 'AGENT' || fresh.control.status !== 'running'
          || fresh.control.lease_id !== captured.snapshot.control.lease_id || fresh.control.generation !== captured.snapshot.control.generation) throw new Error('stale_control');
        attempted.current.add(writeKey); uncertainReview.current = operation === 'accept' ? selected!.checksum
          : operation === 'recover' ? selected!.value.review_sha256 as string : reviewHash;
        if (operation === 'recover') setReviewHash(selected!.value.review_sha256 as string);
        setUncertain(true); setReview(null); setRevocation(null); setRecovery(null);
      }
      const body = {schema_version: '1.0',
        ...(recoveryOperation ? {record_sha256: hash} : operation === 'preview' || operation === 'accept' ? {candidate_sha256: hash} : {review_sha256: hash}),
        ...(operation === 'accept' ? {confirm_review_sha256: selected!.checksum, human_confirmation: 'ACCEPT_MANUAL_REVIEW'} : {}),
        ...(operation === 'revoke' ? {confirm_revocation_sha256: selected!.checksum, human_confirmation: 'REVOKE_MANUAL_REVIEW'} : {}),
        ...(operation === 'recover' ? {confirm_recovery_sha256: selected!.checksum, human_confirmation: 'RESTORE_ANCHORED_REVIEW_RECORD'} : {}),
        ...(write ? {lease_id: captured.snapshot.control.lease_id, generation: captured.snapshot.control.generation} : {})};
      const value = await api<unknown>('/api/tasks/parameter-project-skill-review/' + operation, body);
      if (!current()) return;
      const kind = recoveryOperation ? 'recovery' : operation === 'read' ? 'status' : operation.startsWith('revoke') ? 'revocation' : 'review';
      const checked = await validOwnedParameterSkillReview(value, kind, hash);
      if (!current()) return;
      if (!checked || write && checked.checksum !== selected!.checksum
        || operation === 'revoke-preview' && (!object(sourceReview) || !sameRevocationSource(sourceReview, checked.value))) throw new Error('invalid_review');
      if (operation === 'preview') {
        setReview(checked); setReviewHash(checked.checksum); setMessage('İnceleme önizlemesi doğrulandı; henüz kabul edilmedi.');
      } else if (operation === 'revoke-preview') {
        setRevocation(checked); setMessage('İptal önizlemesi doğrulandı; henüz iptal edilmedi.');
      } else if (operation === 'recovery-preview') {
        setRecovery(checked); setMessage('Geri yükleme önizlemesi doğrulandı; henüz kayıt geri yüklenmedi.');
      } else {
        setResult(checked);
        if (write || operation === 'read' && reviewHash === uncertainReview.current) {setUncertain(false); uncertainReview.current = null;}
        setMessage(operation === 'recover' ? 'Özgün kayıt geri yüklendi; kabul veya iptal durumunu exact inceleme hash’iyle yeniden okuyun.' : operation === 'accept' ? 'Manuel inceleme kabul kaydı doğrulandı.' : operation === 'revoke'
          ? 'Manuel inceleme iptal kaydı doğrulandı.' : 'İnceleme durumu doğrulandı.');
      }
    } catch {
      if (current()) {setReview(null); setRevocation(null); setRecovery(null); setResult(null); setMessage('İnceleme yanıtı doğrulanamadı.');}
    } finally {
      if (request.current === token) {request.current = null; setPending(false);}
    }
  }

  return <section className="panel" data-testid="parameter-skill-review">
    <h3>{t('Manuel aday için insan incelemesi')}</h3>
    <p>{t('Bu inceleme yalnız sentetik manuel bootstrap içindir; yürütme, released skill, gerçek model, eğitim veya etkinleştirme izni vermez.')}</p>
    <label>{t('Yayımlanmış aday SHA-256')}<input data-testid="manual-review-candidate-hash" maxLength={64} disabled={pending}
      value={candidateHash} onChange={event => setCandidateHash(event.target.value)}/></label>
    <button type="button" data-testid="manual-review-preview" disabled={pending || !knowledgeHash(candidateHash)}
      onClick={() => void perform('preview')}>{t('İnceleme önizlemesini oku')}</button>
    {review && <>
      <pre data-testid="manual-review-preview-result">{review.canonical}</pre>
      <label><input type="checkbox" data-testid="manual-review-confirm" checked={confirmed} disabled={!writable}
        onChange={event => setConfirmed(event.target.checked)}/>{t('Gösterilen exact inceleme hash’ini manuel kabul için onaylıyorum.')}</label>
      <button type="button" data-testid="manual-review-accept" disabled={!writable || !confirmed || attempted.current.has('accept:' + review.checksum)}
        onClick={() => void perform('accept')}>{t('Manuel incelemeyi kabul et')}</button>
    </>}
    <label>{t('İnceleme SHA-256')}<input data-testid="manual-review-hash" maxLength={64} disabled={pending}
      value={reviewHash} onChange={event => {setReviewHash(event.target.value); setReview(null); setRevocation(null); setResult(null); setConfirmed(false); setRevokeConfirmed(false);}}/></label>
    <button type="button" data-testid="manual-review-read" disabled={pending || !knowledgeHash(reviewHash)}
      onClick={() => void perform('read')}>{t('İnceleme durumunu oku')}</button>
    <button type="button" data-testid="manual-review-revoke-preview" disabled={pending || !knowledgeHash(reviewHash) || !acceptedStatus}
      onClick={() => void perform('revoke-preview')}>{t('İptal önizlemesini oku')}</button>
    {revocation && <>
      <p>{t('İptal SHA-256')} <code>{revocation.checksum}</code></p><pre>{revocation.canonical}</pre>
      <label><input type="checkbox" data-testid="manual-review-revoke-confirm" checked={revokeConfirmed} disabled={!writable}
        onChange={event => setRevokeConfirmed(event.target.checked)}/>{t('Gösterilen exact iptal hash’ini onaylıyorum.')}</label>
      <button type="button" data-testid="manual-review-revoke" disabled={!writable || !revokeConfirmed || attempted.current.has('revoke:' + revocation.checksum)}
        onClick={() => void perform('revoke')}>{t('Manuel incelemeyi iptal et')}</button>
    </>}
    {result && <pre data-testid="manual-review-result">{result.canonical}</pre>}
    <h4>{t('Eksik inceleme kaydını geri yükle')}</h4>
    <p>{t('Özgün veritabanında önceden yetkilendirilmiş exact kayıt geri yüklenir; görev veya araç tekrar yürütülmez ve yeni izin oluşturulmaz.')}</p>
    <label>{t('Geri yüklenecek kayıt SHA-256')}<input data-testid="manual-review-record-hash" maxLength={64} disabled={pending}
      value={recordHash} onChange={event => {setRecordHash(event.target.value); setRecovery(null); setRecoveryConfirmed(false); setResult(null);}}/></label>
    <button type="button" data-testid="manual-review-recovery-preview" disabled={pending || !knowledgeHash(recordHash)}
      onClick={() => void perform('recovery-preview')}>{t('Geri yükleme önizlemesini oku')}</button>
    {recovery && <>
      <pre data-testid="manual-review-recovery-result">{recovery.canonical}</pre>
      <label><input type="checkbox" data-testid="manual-review-recovery-confirm" checked={recoveryConfirmed} disabled={!recoverWritable}
        onChange={event => setRecoveryConfirmed(event.target.checked)}/>{t('Gösterilen exact geri yükleme hash’iyle yalnız özgün inceleme kaydını geri yüklemeyi onaylıyorum.')}</label>
      <button type="button" data-testid="manual-review-recover" disabled={!recoverWritable || !recoveryConfirmed || attempted.current.has('recover:' + recovery.checksum)}
        onClick={() => void perform('recover')}>{t('Onaylanan inceleme kaydını geri yükle')}</button>
    </>}
    {pending && <p role="status">{t('İnceleme kanıtı denetleniyor…')}</p>}
    {uncertain && <p role="alert" data-testid="manual-review-uncertain">{t('Yazma sonucu belirsiz. Tekrar gönderilmez; exact inceleme hash’iyle salt okunur durum denetimi yapın.')}</p>}
    {message && <p role="status" data-testid="manual-review-message">{t(message)}</p>}
  </section>;
}
