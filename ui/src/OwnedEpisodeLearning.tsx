import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot} from './api';
import {useLanguage} from './i18n';
import {OwnedEpisodePreparation} from './OwnedEpisodePreparation';
import type {AdaptationStatus} from './OwnedEpisodeAdaptation';

type Role = 'system1' | 'system2';
type EpisodeStatus = {available: boolean; state?: string; episode_id?: string;
  counts?: {system1: number; system2: number}; training_ready: false;
  adaptation?: AdaptationStatus;
  preparation?: {state: 'idle' | 'pending' | 'verified' | 'failed' | 'cancelled';
    episode_id?: string; conversion_sha256?: string; training_ready: false}};
type EpisodeCandidate = {candidate_id: string; role: Role; input: {request: Record<string, unknown>};
  prediction: Record<string, unknown>; training_ready: false; reviewed: false};
type EpisodeInspection = {schema_version: '1.0'; episode_id: string; available: true; reviewable: boolean;
  candidates: EpisodeCandidate[]; review_sha256: Record<Role, string | null>;
  reviews: Record<Role, null | {receipt_sha256: string; decision: 'accept' | 'reject'; revoked: boolean}>;
  training_ready: false};
type Props = {status: EpisodeStatus; snapshot: Snapshot; disabled: boolean};
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

function validInspection(value: unknown, episodeId: string): value is EpisodeInspection {
  if (value === null || typeof value !== 'object') return false;
  const response = value as Record<string, unknown>;
  if (response.schema_version !== '1.0' || response.episode_id !== episodeId
      || response.available !== true || typeof response.reviewable !== 'boolean'
      || response.training_ready !== false || !Array.isArray(response.candidates)
      || response.review_sha256 === null || typeof response.review_sha256 !== 'object'
      || response.reviews === null || typeof response.reviews !== 'object') return false;
  const selections = response.review_sha256 as Record<Role, unknown>;
  const reviews = response.reviews as Record<Role, unknown>;
  if (!(['system1', 'system2'] as Role[]).every(role => selections[role] === null || hash(selections[role]))) return false;
  if (!(['system1', 'system2'] as Role[]).every(role => {
    const review = reviews[role];
    return review === null || review !== null && typeof review === 'object'
      && hash((review as Record<string, unknown>).receipt_sha256)
      && ['accept', 'reject'].includes(String((review as Record<string, unknown>).decision))
      && typeof (review as Record<string, unknown>).revoked === 'boolean';
  })) return false;
  return response.candidates.every(candidate => {
    if (candidate === null || typeof candidate !== 'object') return false;
    const item = candidate as Record<string, unknown>;
    return hash(item.candidate_id) && ['system1', 'system2'].includes(String(item.role))
      && item.training_ready === false && item.reviewed === false
      && item.input !== null && typeof item.input === 'object'
      && (item.input as Record<string, unknown>).request !== null
      && typeof (item.input as Record<string, unknown>).request === 'object'
      && item.prediction !== null && typeof item.prediction === 'object';
  });
}

export function OwnedEpisodeLearning({status, snapshot, disabled}: Props) {
  const language = useLanguage();
  const text = (english: string, turkish: string) => language === 'tr' ? turkish : english;
  const episodeId = status.episode_id;
  const [inspection, setInspection] = useState<EpisodeInspection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [exportHash, setExportHash] = useState<string | null>(null);
  const [revocationRecorded, setRevocationRecorded] = useState(false);
  const serial = useRef(0);
  const scope = `${episodeId}:${snapshot.runtime.runtime_id}:${snapshot.control.lease_id}:${snapshot.control.generation}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  useEffect(() => {
    serial.current += 1;
    setInspection(null); setBusy(false); setError(false); setExportHash(null); setRevocationRecorded(false);
  }, [scope]);
  useEffect(() => () => { serial.current += 1; }, []);
  if (!status.available || !episodeId || !/^episode-[a-f0-9]{32}$/.test(episodeId)) return null;
  const loading = busy;
  const writesDisabled = disabled || busy;

  async function currentControl() {
    const current = await api<Snapshot>('/api/state');
    if (scopeRef.current !== scope || current.runtime.runtime_id !== snapshot.runtime.runtime_id
        || current.control.lease_id !== snapshot.control.lease_id
        || current.control.generation !== snapshot.control.generation
        || current.control.owner !== 'AGENT' || current.control.status !== 'running') {
      throw new Error('episode_control_changed');
    }
    return {lease_id: current.control.lease_id, generation: current.control.generation};
  }

  async function request(operation: 'inspect' | 'review' | 'revoke' | 'export', extra: Record<string, unknown> = {}) {
    const current = ++serial.current;
    setBusy(true); setError(false);
    try {
      const control = operation === 'inspect' ? {} : await currentControl();
      const result = await api<unknown>(`/api/tasks/owned-episode/${operation}`,
        {schema_version: '1.0', episode_id: episodeId, ...extra, ...control});
      if (current !== serial.current || scopeRef.current !== scope) return;
      if (operation === 'export') {
        if (result === null || typeof result !== 'object') throw new Error('invalid_export');
        const response = result as Record<string, unknown>;
        if (response.available !== true || response.episode_id !== episodeId
            || !hash(response.export_sha256) || response.training_ready !== false
            || response.counts === null || typeof response.counts !== 'object'
            || !(['system1', 'system2'] as Role[]).every(role =>
              Number.isInteger((response.counts as Record<string, unknown>)[role]))) throw new Error('invalid_export');
        setExportHash(response.export_sha256);
        return;
      }
      if (operation === 'revoke' && result !== null && typeof result === 'object'
          && (result as Record<string, unknown>).available === false) {
        const response = result as Record<string, unknown>;
        if (response.schema_version !== '1.0' || response.episode_id !== episodeId
            || response.revocation_recorded !== true || response.reviewable !== false
            || !Array.isArray(response.candidates) || response.candidates.length !== 0
            || response.reviews === null || typeof response.reviews !== 'object') throw new Error('invalid_revocation');
        setInspection(null); setExportHash(null); setRevocationRecorded(true);
        return;
      }
      if (typeof episodeId !== 'string' || !validInspection(result, episodeId)) throw new Error('invalid_inspection');
      setInspection(result);
      setExportHash(null); setRevocationRecorded(false);
    } catch {
      if (current === serial.current) {setError(true); setInspection(null); setExportHash(null);}
    } finally {
      if (current === serial.current) setBusy(false);
    }
  }

  function review(role: Role, decision: 'accept' | 'reject') {
    const selection = inspection?.review_sha256[role];
    if (!inspection?.reviewable || !hash(selection) || inspection.reviews[role] !== null) return;
    void request('review', {role, decision, confirm_sha256: selection});
  }
  function revoke(role: Role) {
    const receipt = inspection?.reviews[role];
    if (!receipt || receipt.revoked) return;
    void request('revoke', {role, confirm_sha256: receipt.receipt_sha256});
  }
  const accepted = (['system1', 'system2'] as Role[]).every(role =>
    inspection?.reviews[role]?.decision === 'accept' && inspection.reviews[role]?.revoked === false);
  const stateLabel = status.state === 'reviewable' ? text('reviewable', 'incelemeye hazır')
    : status.state === 'failed' ? text('failed', 'başarısız')
    : status.state === 'cancelled' ? text('cancelled', 'iptal edildi')
    : status.state === 'needs_human' ? text('needs human', 'insan girdisi gerekiyor')
    : status.state === 'disabled' ? text('disabled', 'devre dışı')
    : status.state === 'collecting' ? text('collecting', 'toplanıyor')
    : text('unknown', 'bilinmiyor');

  return <section className="registry" data-testid="owned-episode-learning">
    <h3>{text('Owned synthetic episode', 'Sahipli sentetik bölüm')}</h3>
    <p role="status" data-testid="episode-status"><span data-testid="status">
      {text(`Private collection: ${stateLabel} · System 1: ${status.counts?.system1 ?? 0} · System 2: ${status.counts?.system2 ?? 0}`,
        `Özel toplama: ${stateLabel} · Sistem 1: ${status.counts?.system1 ?? 0} · Sistem 2: ${status.counts?.system2 ?? 0}`)}</span>
    </p>
    <p className="caption">{text('Content stays in local private files. Inspect is explicit. Reviews are immutable; revocation is separate. Export is local and development-only; this does not authorize model training or network export.',
      'İçerik yerel özel dosyalarda kalır. İnceleme açıkça başlatılır. İncelemeler değiştirilemez; geri alma ayrıdır. Dışa aktarım yerel ve yalnız geliştirme içindir; model eğitimi veya ağ dışa aktarımı yetkisi vermez.')}</p>
    <button data-testid="episode-inspect" disabled={loading} onClick={() => void request('inspect')}>
      {text('Inspect episode content', 'Bölüm içeriğini incele')}</button>
    {revocationRecorded ? <p role="status">{text('Review revocation recorded. Current episode evidence is unavailable; no export is offered.',
      'İnceleme geri alma kaydedildi. Bölümün güncel kanıtı kullanılamıyor; dışa aktarım sunulmuyor.')}</p> : null}
    {inspection ? <div data-testid="episode-inspected">
      <p>{text(inspection.reviewable ? 'Source execution is reviewable.' : 'Provisional content only; review is not available until an audited successful execution.',
        inspection.reviewable ? 'Kaynak yürütme incelemeye hazır.' : 'Yalnız geçici içerik; denetlenmiş başarılı yürütme olmadan inceleme yapılamaz.')}</p>
      {(['system1', 'system2'] as Role[]).map(role => {
        const candidates = inspection.candidates.filter(candidate => candidate.role === role);
        const reviewReceipt = inspection.reviews[role];
        const selection = inspection.review_sha256[role];
        return <article key={role} data-testid={`episode-content-${role}`} className="registry">
          <h4>{role === 'system1' ? 'System 1 · Decider' : 'System 2 · Bonsai'}</h4>
          {candidates.length ? candidates.map((candidate, index) => <details key={candidate.candidate_id} open={index === 0}>
            <summary>{text(`Candidate ${index + 1} of ${candidates.length} · ${candidate.candidate_id.slice(0, 12)}`,
              `${candidates.length} adaydan ${index + 1}. · ${candidate.candidate_id.slice(0, 12)}`)}</summary>
            <p className="caption">{text('Private model input', 'Özel model girdisi')}</p>
            <pre style={{maxHeight: '22rem', maxWidth: '100%', overflow: 'auto', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap'}}>{JSON.stringify(candidate.input.request, null, 2)}</pre>
            <p className="caption">{text('Structured prediction (not a target label)', 'Yapılandırılmış tahmin (hedef etiketi değildir)')}</p>
            <pre style={{maxHeight: '22rem', maxWidth: '100%', overflow: 'auto', overflowWrap: 'anywhere', whiteSpace: 'pre-wrap'}}>{JSON.stringify(candidate.prediction, null, 2)}</pre>
          </details>) : <p className="caption">{text('No candidate for this role.', 'Bu rol için aday yok.')}</p>}
          {reviewReceipt ? <p role="status">{text(`Immutable ${reviewReceipt.decision} receipt: `, `Değiştirilemez ${reviewReceipt.decision} makbuzu: `)}<code style={{overflowWrap: 'anywhere', wordBreak: 'break-all'}}>{reviewReceipt.receipt_sha256}</code>
            {reviewReceipt.revoked ? text(' · revoked', ' · geri alındı') : null}</p> : null}
          {candidates.length && inspection.reviewable && reviewReceipt === null && hash(selection) ? <div>
            <p className="caption">{text('Confirm this exact role selection:', 'Bu rol seçimini exact olarak onaylayın:')} <code style={{overflowWrap: 'anywhere', wordBreak: 'break-all'}}>{selection}</code></p>
            <button data-testid={`episode-accept-${role}`} disabled={writesDisabled} onClick={() => review(role, 'accept')}>
              {text('Accept this prediction', 'Bu tahmini kabul et')}</button>
            <button data-testid={`episode-reject-${role}`} disabled={writesDisabled} onClick={() => review(role, 'reject')}>
              {text('Reject this prediction', 'Bu tahmini reddet')}</button>
          </div> : null}
          {reviewReceipt && !reviewReceipt.revoked ? <button data-testid={`episode-revoke-${role}`} disabled={writesDisabled}
            onClick={() => revoke(role)}>{text('Revoke review', 'İncelemeyi geri al')}</button> : null}
        </article>;
      })}
      <button data-testid="episode-export" disabled={writesDisabled || !accepted} onClick={() => {
        if (!inspection) return;
        const receipts = Object.fromEntries((['system1', 'system2'] as Role[]).map(role =>
          [role, inspection.reviews[role]?.receipt_sha256]));
        void request('export', {review_receipts: receipts});
      }}>{text('Export locally (development only)', 'Yerel dışa aktar (yalnız geliştirme)')}</button>
      {exportHash ? <p role="status" data-testid="episode-exported">{text('Local development export: ', 'Yerel geliştirme dışa aktarımı: ')}<code style={{overflowWrap: 'anywhere', wordBreak: 'break-all'}}>{exportHash}</code></p> : null}
      {exportHash ? <OwnedEpisodePreparation episodeId={episodeId} exportSha256={exportHash}
        status={status.preparation} adaptation={status.adaptation} snapshot={snapshot} disabled={disabled || busy}/> : null}
    </div> : null}
    {error ? <p role="alert">{text('Episode evidence or review changed. Inspect again; no training was authorized.', 'Bölüm kanıtı veya incelemesi değişti. Yeniden inceleyin; eğitim yetkilendirilmedi.')}</p> : null}
  </section>;
}
