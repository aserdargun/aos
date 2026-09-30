import {useEffect, useRef, useState} from 'react';
import {api, type NavigationGraph, type PageDraftRegistration, type PageDraftSeed,
  type RouteKnowledgeCandidatePreview, type RouteKnowledgeReviewReceipt, type Tasks} from './api';
import {t} from './i18n';

export function RemoteNavigationGraph({tasks}: {tasks: Tasks}) {
  const runs = tasks.jobs.filter(job => job.kind === 'browser_remote_routes' &&
    job.status === 'succeeded' && job.run_id !== null);
  const runKey = runs.map(job => job.run_id).join(':');
  const [selectedBefore, setSelectedBefore] = useState('');
  const [selectedAfter, setSelectedAfter] = useState('');
  const [report, setReport] = useState<NavigationGraph | null>(null);
  const [pageKey, setPageKey] = useState('');
  const [selectedRoute, setSelectedRoute] = useState('');
  const [seed, setSeed] = useState<PageDraftSeed | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [registration, setRegistration] = useState<PageDraftRegistration | null>(null);
  const [candidate, setCandidate] = useState<RouteKnowledgeCandidatePreview | null>(null);
  const [candidateConfirmation, setCandidateConfirmation] = useState('');
  const [acknowledgeMetadata, setAcknowledgeMetadata] = useState(false);
  const [review, setReview] = useState<RouteKnowledgeReviewReceipt | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [seedUnavailable, setSeedUnavailable] = useState(false);
  const [registrationUnavailable, setRegistrationUnavailable] = useState(false);
  const [candidateUnavailable, setCandidateUnavailable] = useState(false);
  const [reviewUnavailable, setReviewUnavailable] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const before = runs.some(job => job.run_id === selectedBefore) ? selectedBefore : runs[1]?.run_id ?? '';
  const after = runs.some(job => job.run_id === selectedAfter) ? selectedAfter : runs[0]?.run_id ?? '';
  const routeIndex = report?.stable_pages.some(page => String(page.route_index) === selectedRoute)
    ? selectedRoute : String(report?.stable_pages[0]?.route_index ?? '');
  const validPageKey = /^[a-z][a-z0-9_-]{0,63}$/.test(pageKey);

  useEffect(() => {
    pending.current?.abort();
    setReport(null);
    setSeed(null);
    setConfirmation('');
    setRegistration(null);
    setCandidate(null);
    setReview(null);
    setCandidateConfirmation('');
    setAcknowledgeMetadata(false);
    setUnavailable(false);
    setSeedUnavailable(false);
    setRegistrationUnavailable(false);
    setCandidateUnavailable(false);
    setReviewUnavailable(false);
    setLoading(false);
    setSaving(false);
    setRegistering(false);
    setPreviewing(false);
    setReviewing(false);
  }, [before, after, runKey, tasks.remote_routes?.plan_sha256]);
  useEffect(() => () => pending.current?.abort(), []);

  async function compare() {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setReport(null);
    setSeed(null);
    setRegistration(null);
    setCandidate(null);
    setReview(null);
    setUnavailable(false);
    setLoading(true);
    try {
      const result = await api<NavigationGraph>('/api/tasks/navigation-graph',
        {before_run_id: before, after_run_id: after}, controller.signal);
      if (!controller.signal.aborted) setReport(result);
    } catch {
      if (!controller.signal.aborted) setUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  async function saveDraft() {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setSeed(null);
    setConfirmation('');
    setRegistration(null);
    setCandidate(null);
    setReview(null);
    setSeedUnavailable(false);
    setRegistrationUnavailable(false);
    setSaving(true);
    try {
      const result = await api<PageDraftSeed>('/api/tasks/page-draft-seed',
        {before_run_id: before, after_run_id: after,
          route_index: Number(routeIndex), page_key: pageKey}, controller.signal);
      if (!controller.signal.aborted) setSeed(result);
    } catch {
      if (!controller.signal.aborted) setSeedUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }

  async function previewKnowledge() {
    if (!registration || !seed) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setCandidate(null);
    setReview(null);
    setCandidateConfirmation('');
    setAcknowledgeMetadata(false);
    setCandidateUnavailable(false);
    setReviewUnavailable(false);
    setPreviewing(true);
    try {
      const result = await api<RouteKnowledgeCandidatePreview>('/api/tasks/page-knowledge-preview',
        {before_run_id: before, after_run_id: after, route_index: seed.route_index,
          page_key: registration.page_key, knowledge_sha256: registration.knowledge_sha256}, controller.signal);
      if (!controller.signal.aborted) setCandidate(result);
    } catch {
      if (!controller.signal.aborted) setCandidateUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setPreviewing(false);
    }
  }

  async function recordMetadataReview() {
    if (!registration || !candidate || !acknowledgeMetadata ||
        candidateConfirmation !== candidate.candidate_sha256) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setReview(null);
    setReviewUnavailable(false);
    setReviewing(true);
    try {
      const result = await api<RouteKnowledgeReviewReceipt>('/api/tasks/page-knowledge-review',
        {before_run_id: before, after_run_id: after, route_index: candidate.candidate.route_index,
          page_key: registration.page_key, knowledge_sha256: registration.knowledge_sha256,
          confirm_candidate_sha256: candidateConfirmation,
          acknowledge_metadata_only: true}, controller.signal);
      if (!controller.signal.aborted) setReview(result);
    } catch {
      if (!controller.signal.aborted) setReviewUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setReviewing(false);
    }
  }

  async function registerDraft() {
    if (!seed || confirmation !== seed.draft_sha256) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setRegistration(null);
    setRegistrationUnavailable(false);
    setRegistering(true);
    try {
      const result = await api<PageDraftRegistration>('/api/tasks/page-draft-register',
        {before_run_id: before, after_run_id: after, route_index: seed.route_index,
          page_key: seed.page_key, confirm_sha256: confirmation}, controller.signal);
      if (!controller.signal.aborted) setRegistration(result);
    } catch {
      if (!controller.signal.aborted) setRegistrationUnavailable(true);
    } finally {
      if (!controller.signal.aborted) setRegistering(false);
    }
  }

  return <section className="registry" data-testid="remote-navigation-graph">
    <h3>{t('Gözlenen gezinme grafiği')}</h3>
    <p className="caption">{t('İki tamamlanmış rota görevi seçin. Karşılaştırma salt okunurdur; yeni GET veya yetki vermez.')}</p>
    {runs.length < 2 ? <p>{t('Karşılaştırma için iki tamamlanmış rota görevi gerekir.')}</p> : <>
      <label>{t('Önceki koşu')} <select aria-label={t('Önceki koşu')} value={before}
        onChange={event => setSelectedBefore(event.target.value)}>
        {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
      </select></label>
      <label>{t('Sonraki koşu')} <select aria-label={t('Sonraki koşu')} value={after}
        onChange={event => setSelectedAfter(event.target.value)}>
        {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
      </select></label>
      <button disabled={loading || saving || registering || previewing || reviewing || !before || !after || before === after} onClick={() => void compare()}>{t('Rotaları karşılaştır')}</button>
    </>}
    {loading ? <p role="status">{t('Tarihsel kanıt denetleniyor…')}</p> : null}
    {unavailable ? <p role="status">{t('Grafik kaynağı değişti veya kullanılamıyor.')}</p> : null}
    {report ? <div role="status" data-testid="remote-navigation-graph-result">
      <p>{t('Sabit sayfalar:')} {report.stable_pages.length} / {report.route_count} · {t('Gözlenen kenarlar:')} {report.observed_edges.length}</p>
      <p>{t('Sabit rota indeksleri:')} {report.stable_pages.map(page => page.route_index + 1).join(', ') || '—'}</p>
      <p>{t('Planlı bağlantılar:')} {report.observed_edges.map(edge => `${edge.source_route_index + 1} → ${edge.target_route_index + 1}`).join(', ') || '—'}</p>
      <p>{t('Değişen rotalar:')} {report.changed_route_indices.map(index => index + 1).join(', ') || '—'} · {t('Eksik link örnekleri:')} {report.incomplete_link_sample_indices.map(index => index + 1).join(', ') || '—'}</p>
      <p className="caption">{t('Bu tarihsel aday gerçek site haritası, semantik sayfa kimliği, inceleme, görev-içi bilgi veya uygulama sonucu değildir.')}</p>
      {report.stable_pages.length ? <div data-testid="remote-page-draft-seed">
        <label>{t('Sabit rota')} <select aria-label={t('Sabit rota')} value={routeIndex}
          onChange={event => {setSelectedRoute(event.target.value); setSeed(null); setRegistration(null); setCandidate(null); setReview(null); setSeedUnavailable(false); setRegistrationUnavailable(false);}}>
          {report.stable_pages.map(page => <option key={page.route_index} value={page.route_index}>
            {page.route_index + 1}</option>)}
        </select></label>
        <label>{t('Sayfa anahtarı')} <input aria-label={t('Sayfa anahtarı')} value={pageKey}
          onChange={event => {setPageKey(event.target.value); setSeed(null); setRegistration(null); setCandidate(null); setReview(null); setSeedUnavailable(false); setRegistrationUnavailable(false);}} maxLength={64}/></label>
        <button disabled={loading || saving || registering || previewing || reviewing || !validPageKey} onClick={() => void saveDraft()}>
          {t('Özel sayfa taslağı kaydet')}</button>
        <p className="caption">{t('Anahtar sizin semantik iddianızdır. Taslak yalnız özel dosyaya yazılır; kayıt, inceleme veya görev yetkisi oluşmaz.')}</p>
      </div> : null}
      {saving ? <p role="status">{t('Taslak kaynağı denetleniyor…')}</p> : null}
      {seedUnavailable ? <p role="status">{t('Taslak kaydedilemedi; kaynak değişmiş veya dosya mevcut olabilir.')}</p> : null}
      {seed && !registration ? <div data-testid="remote-page-draft-seed-result">
        <p role="status">
        {t('Özel taslak kaydedildi:')} <code>{seed.page_key}</code> · {t('Taslak SHA-256:')} <code>{seed.draft_sha256}</code>.
        {' '}{t('Dosyayı özel site-page-seeds dizininde inceleyin; otomatik kaydedilmedi.')}
        </p>
        <label>{t('Taslak SHA-256 onayı')} <input aria-label={t('Taslak SHA-256 onayı')}
          value={confirmation} onChange={event => setConfirmation(event.target.value)} maxLength={64}/></label>
        <button disabled={registering || confirmation !== seed.draft_sha256}
          onClick={() => void registerDraft()}>{t('Onaylanan taslağı kaydet')}</button>
        <p className="caption">{t('Kayıt, tarihsel kaynağı yeniden denetler; semantik anahtar incelemesi veya görev yetkisi değildir.')}</p>
      </div> : null}
      {registering ? <p role="status">{t('Taslak yeniden denetleniyor…')}</p> : null}
      {registrationUnavailable ? <p role="status">{t('Taslak kaydı reddedildi; kaynak veya özel dosya değişmiş olabilir.')}</p> : null}
      {registration ? <p role="status" data-testid="remote-page-draft-registration-result">
        {t('İncelenmemiş taslak kaydedildi:')} <code>{registration.knowledge_sha256}</code>.
        {' '}{t('Bu kayıt görevde kullanılmaz; ayrı site bilgisi incelemesi gerekir.')}
      </p> : null}
      {registration && !review ? <button disabled={previewing || reviewing}
        onClick={() => void previewKnowledge()}>{candidate ? t('Adayı yeniden önizle') : t('Metadata adayını önizle')}</button> : null}
      {previewing ? <p role="status">{t('Metadata adayı denetleniyor…')}</p> : null}
      {candidateUnavailable ? <p role="status">{t('Metadata adayı kullanılamıyor; kaynak veya taslak değişmiş olabilir.')}</p> : null}
      {candidate && !review ? <div data-testid="remote-page-knowledge-candidate">
        <p>{t('Metadata adayı:')} <code>{candidate.candidate.page_key}</code> · {t('Sürüm:')} {candidate.candidate.draft_revision} · {t('Rota:')} {candidate.candidate.route_index + 1}</p>
        <p>{t('Aday SHA-256:')} <code>{candidate.candidate_sha256}</code></p>
        <p className="caption">{t('Yalnız tarihsel profil ve readback metadata eşleşir; hesap, semantik doğruluk veya uygulama sonucu kanıtlanmaz.')}</p>
        <label>{t('Aday SHA-256 onayı')} <input aria-label={t('Aday SHA-256 onayı')}
          value={candidateConfirmation} onChange={event => setCandidateConfirmation(event.target.value)} maxLength={64}/></label>
        <label><input type="checkbox" checked={acknowledgeMetadata}
          onChange={event => setAcknowledgeMetadata(event.target.checked)}/>
          {t('Yalnız metadata incelemesi olduğunu kabul ediyorum')}</label>
        <button disabled={reviewing || !acknowledgeMetadata || candidateConfirmation !== candidate.candidate_sha256}
          onClick={() => void recordMetadataReview()}>{t('Metadata incelemesini kaydet')}</button>
      </div> : null}
      {reviewing ? <p role="status">{t('Metadata incelemesi yeniden denetleniyor…')}</p> : null}
      {reviewUnavailable ? <p role="status">{t('Metadata incelemesi reddedildi; adayı yeniden önizleyin.')}</p> : null}
      {review ? <p role="status" data-testid="remote-page-knowledge-review-result">
        {t('Metadata inceleme kaydı:')} <code>{review.review_sha256}</code>.
        {' '}{t('Bu kayıt çalışan oturumu etkinleştirmez; sonraki başlangıçta exact pin gerekir.')}
      </p> : null}
    </div> : null}
  </section>;
}
