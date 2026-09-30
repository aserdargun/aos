import {useEffect, useRef, useState} from 'react';
import {api, type JsonPageDraftRegistration, type JsonPageDraftSeed,
  type JsonKnowledgeCandidatePreview, type JsonKnowledgeRecheck,
  type JsonKnowledgeReviewReceipt, type Tasks} from './api';
import {t} from './i18n';
import './remote_json_knowledge.css';

export function RemoteJsonKnowledge({tasks}: {tasks: Tasks}) {
  const runs = tasks.jobs.filter(job => job.kind === 'browser_remote_static_assets' &&
    job.status === 'succeeded' && job.run_id !== null);
  const runKey = runs.map(job => job.run_id).join(':');
  const [selectedBefore, setSelectedBefore] = useState('');
  const [selectedAfter, setSelectedAfter] = useState('');
  const [selectedCurrent, setSelectedCurrent] = useState('');
  const [pageKey, setPageKey] = useState('');
  const [seed, setSeed] = useState<JsonPageDraftSeed | null>(null);
  const [seedConfirmation, setSeedConfirmation] = useState('');
  const [registration, setRegistration] = useState<JsonPageDraftRegistration | null>(null);
  const [knowledgeSha256, setKnowledgeSha256] = useState('');
  const [candidate, setCandidate] = useState<JsonKnowledgeCandidatePreview | null>(null);
  const [candidateConfirmation, setCandidateConfirmation] = useState('');
  const [acknowledgeMetadata, setAcknowledgeMetadata] = useState(false);
  const [review, setReview] = useState<JsonKnowledgeReviewReceipt | null>(null);
  const [reviewSha256, setReviewSha256] = useState('');
  const [recheck, setRecheck] = useState<JsonKnowledgeRecheck | null>(null);
  const [activity, setActivity] = useState<'seed' | 'register' | 'preview' | 'review' | 'recheck' | null>(null);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);
  const before = runs.some(job => job.run_id === selectedBefore) ? selectedBefore : runs[1]?.run_id ?? '';
  const after = runs.some(job => job.run_id === selectedAfter) ? selectedAfter : runs[0]?.run_id ?? '';
  const current = runs.some(job => job.run_id === selectedCurrent) ? selectedCurrent : runs[0]?.run_id ?? '';
  const validKnowledgeHash = /^[a-f0-9]{64}$/.test(knowledgeSha256);
  const validPageKey = /^[a-z][a-z0-9_-]{0,63}$/.test(pageKey);
  const validSeedConfirmation = /^[a-f0-9]{64}$/.test(seedConfirmation);
  const validReviewHash = /^[a-f0-9]{64}$/.test(reviewSha256);

  useEffect(() => {
    pending.current?.abort();
    setSeed(null);
    setSeedConfirmation('');
    setRegistration(null);
  }, [before, after, pageKey, runKey, tasks.remote_static_assets?.plan_sha256]);
  useEffect(() => {
    pending.current?.abort();
    setReview(null);
    setCandidate(null);
    setCandidateConfirmation('');
    setAcknowledgeMetadata(false);
    setActivity(null);
    setError('');
  }, [before, after, knowledgeSha256, runKey, tasks.remote_static_assets?.plan_sha256]);
  useEffect(() => {
    pending.current?.abort();
    setRecheck(null);
    setActivity(null);
  }, [current, reviewSha256, runKey]);
  useEffect(() => () => pending.current?.abort(), []);

  async function request<T>(kind: 'seed' | 'register' | 'preview' | 'review' | 'recheck', path: string,
                            payload: Record<string, unknown>, onResult: (value: T) => void) {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    if (kind === 'seed') {
      setSeed(null);
      setRegistration(null);
      setSeedConfirmation('');
      setCandidate(null);
      setReview(null);
    } else if (kind === 'register') {
      setRegistration(null);
    } else if (kind === 'preview') {
      setCandidate(null);
      setReview(null);
      setCandidateConfirmation('');
      setAcknowledgeMetadata(false);
    } else if (kind === 'review') {
      setReview(null);
    } else {
      setRecheck(null);
    }
    setActivity(kind);
    setError('');
    try {
      const result = await api<T>(path, payload, controller.signal);
      if (!controller.signal.aborted) onResult(result);
    } catch {
      if (!controller.signal.aborted) setError(kind === 'seed'
        ? 'JSON sayfa taslağı oluşturulamadı; kaynak değişmiş veya özel dosya mevcut olabilir.'
        : kind === 'register'
          ? 'JSON sayfa taslağı kaydı reddedildi; hash veya kaynak değişmiş olabilir.'
        : kind === 'preview'
        ? 'JSON metadata adayı kullanılamıyor; kaynak veya taslak değişmiş olabilir.'
        : kind === 'review'
          ? 'JSON metadata incelemesi reddedildi; adayı yeniden önizleyin.'
          : 'Taze JSON koşusu eşleşmedi veya kaynak kullanılamıyor.');
    } finally {
      if (!controller.signal.aborted) setActivity(null);
    }
  }

  return <section className="registry json-knowledge" data-testid="remote-json-knowledge">
    <h3>{t('JSON sayfa metadata incelemesi')}</h3>
    <p className="caption">{t('İki tamamlanmış JSON paket koşusundan özel, incelenmemiş sayfa taslağı oluşturun veya kayıtlı taslağın exact SHA-256 değerini girin. Bu panel görev başlatmaz.')}</p>
    {runs.length < 2 ? <p>{t('İnceleme için iki tamamlanmış JSON paket görevi gerekir.')}</p> : <>
      <div className="json-knowledge-fields">
        <label>{t('Önceki koşu')}<select aria-label={t('JSON önceki koşu')} value={before}
          onChange={event => setSelectedBefore(event.target.value)}>
          {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
        </select></label>
        <label>{t('Sonraki koşu')}<select aria-label={t('JSON sonraki koşu')} value={after}
          onChange={event => setSelectedAfter(event.target.value)}>
          {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
        </select></label>
        <label>{t('Sayfa anahtarı')}<input aria-label={t('JSON sayfa anahtarı')}
          value={pageKey} onChange={event => {setPageKey(event.target.value); setKnowledgeSha256('');}}
          maxLength={64} autoComplete="off"/></label>
        <label>{t('Sayfa taslağı SHA-256')}<input aria-label={t('Sayfa taslağı SHA-256')}
          value={knowledgeSha256} onChange={event => setKnowledgeSha256(event.target.value)}
          maxLength={64} autoComplete="off"/></label>
      </div>
      <button disabled={activity !== null || !validPageKey || !before || !after || before === after}
        onClick={() => void request<JsonPageDraftSeed>('seed', '/api/tasks/json-page-draft-seed',
          {before_run_id: before, after_run_id: after, page_key: pageKey},
          value => {setSeed(value); setKnowledgeSha256('');})}>{t('Özel JSON sayfa taslağı oluştur')}</button>
      {seed && !registration ? <div data-testid="remote-json-page-seed">
        <p>{t('İncelenmemiş özel taslak:')} <code>{seed.page_key}</code> ·
          {' '}{t('Taslak SHA-256:')} <code>{seed.draft_sha256}</code>.</p>
        <p className="caption">{t('JSON taslağını özel json-page-seeds dizininde inceleyin; sayfa anahtarı sizin iddianızdır.')}</p>
        <label>{t('Taslak SHA-256 onayı')}<input aria-label={t('JSON taslak SHA-256 onayı')}
          value={seedConfirmation} onChange={event => setSeedConfirmation(event.target.value)}
          maxLength={64} autoComplete="off"/></label>
        <button disabled={activity !== null || !validSeedConfirmation}
          onClick={() => void request<JsonPageDraftRegistration>('register',
            '/api/tasks/json-page-draft-register',
            {before_run_id: before, after_run_id: after, page_key: pageKey,
              confirm_sha256: seedConfirmation},
            value => {setRegistration(value); setKnowledgeSha256(value.knowledge_sha256);})}>
          {t('JSON sayfa taslağını kaydet')}</button>
      </div> : null}
      {registration ? <p role="status" data-testid="remote-json-page-registration">
        {t('İncelenmemiş JSON taslağı kaydedildi:')} <code>{registration.knowledge_sha256}</code>.
        {' '}{t('Ayrı metadata incelemesi gerekir.')}</p> : null}
      <button disabled={activity !== null || !validKnowledgeHash || !before || !after || before === after}
        onClick={() => void request<JsonKnowledgeCandidatePreview>('preview',
          '/api/tasks/json-knowledge-preview',
          {before_run_id: before, after_run_id: after, knowledge_sha256: knowledgeSha256},
          value => {setCandidate(value); setReview(null);})}>
        {t('JSON metadata adayını önizle')}</button>
    </>}
    {candidate ? <div data-testid="remote-json-knowledge-candidate">
      <p>{t('Sayfa anahtarı:')} <code>{candidate.candidate.page_key}</code> · {t('Sürüm:')} {candidate.candidate.draft_revision} · {t('JSON kaynakları:')} {candidate.candidate.data_count}</p>
      <p>{t('Aday SHA-256:')} <code>{candidate.candidate_sha256}</code></p>
      <p className="caption">{t('Bu yalnız tarihsel taşıma eşleşmesidir; sayfa anlamı, hesap veya uygulama sonucu doğrulanmaz.')}</p>
      <label>{t('Aday SHA-256 onayı')}<input aria-label={t('JSON aday SHA-256 onayı')}
        value={candidateConfirmation} onChange={event => setCandidateConfirmation(event.target.value)}
        maxLength={64} autoComplete="off"/></label>
      <label className="json-knowledge-confirm"><input type="checkbox" checked={acknowledgeMetadata}
        onChange={event => setAcknowledgeMetadata(event.target.checked)}/>
        {t('Yalnız metadata incelemesi olduğunu kabul ediyorum')}</label>
      <button disabled={activity !== null || !acknowledgeMetadata ||
        candidateConfirmation !== candidate.candidate_sha256}
        onClick={() => void request<JsonKnowledgeReviewReceipt>('review',
          '/api/tasks/json-knowledge-review',
          {before_run_id: before, after_run_id: after, knowledge_sha256: knowledgeSha256,
            confirm_candidate_sha256: candidateConfirmation, acknowledge_metadata_only: true},
          value => {setReview(value); setReviewSha256(value.review_sha256);})}>
        {t('JSON metadata incelemesini kaydet')}</button>
    </div> : null}
    {review ? <p role="status" data-testid="remote-json-knowledge-review">
      {t('JSON metadata inceleme kaydı:')} <code>{review.review_sha256}</code>.
      {' '}{t('Bu kayıt yeni görev veya site yetkisi açmaz.')}</p> : null}
    <div className="json-knowledge-recheck">
      <h4>{t('Taze tamamlanmış koşuyu yeniden denetle')}</h4>
      <p className="caption">{t('Review kaydından sonra başlayan ayrı, onaylı JSON paket koşusu gerekir. Eşleşme yalnız sembolik anahtar döner.')}</p>
      <div className="json-knowledge-fields">
        <label>{t('Taze koşu')}<select aria-label={t('JSON taze koşu')} value={current}
          onChange={event => setSelectedCurrent(event.target.value)}>
          {runs.map(job => <option key={job.job_id} value={job.run_id!}>{job.run_id}</option>)}
        </select></label>
        <label>{t('Review SHA-256')}<input aria-label={t('JSON review SHA-256')}
          value={reviewSha256} onChange={event => setReviewSha256(event.target.value)}
          maxLength={64} autoComplete="off"/></label>
      </div>
      <button disabled={activity !== null || !current || !validReviewHash}
        onClick={() => void request<JsonKnowledgeRecheck>('recheck',
          '/api/tasks/json-knowledge-recheck',
          {current_run_id: current, review_sha256: reviewSha256}, setRecheck)}>
        {t('Taze JSON metadata denetimi')}</button>
      {recheck ? <p role="status" data-testid="remote-json-knowledge-recheck">
        {t('Metadata eşleşti:')} <code>{recheck.page_key}</code> · {t('Sürüm:')} {recheck.draft_revision} · {t('Sembolik işaretler:')} {recheck.landmark_keys.join(', ') || '—'}.
        {' '}{t('Görev-içi retrieval, hak, hesap ve sonuç doğrulaması değildir.')}</p> : null}
    </div>
    {activity ? <p role="status">{t('JSON metadata kaynağı denetleniyor…')}</p> : null}
    {error ? <p role="status">{t(error)}</p> : null}
  </section>;
}
