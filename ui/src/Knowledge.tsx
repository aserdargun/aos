import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';
import {useLanguage} from './i18n';
import {knowledgeHash, knowledgeIdentifier, validKnowledgeScope, validKnowledgePublication,
  validKnowledgeInspection, validKnowledgeReview, validKnowledgeCatalog, validKnowledgeSearch,
  type KnowledgeScope, type KnowledgePublication, type KnowledgeInspection, type KnowledgeReview,
  type KnowledgeCatalog, type KnowledgeSearch, type KnowledgeDecision} from './knowledgeApi';
import './knowledge.css';
import {KnowledgeAnswer} from './KnowledgeAnswer';

export function Knowledge({tasks, snapshot, busy, refreshKey}: {tasks: Tasks | null; snapshot: Snapshot | null; busy: boolean; refreshKey: number}) {
  const tr = useLanguage() === 'tr';
  const text = (en: string, translated: string) => tr ? translated : en;
  const [scope, setScope] = useState<KnowledgeScope>({application_id: '', tenant_id: '', account_role: ''});
  const [sourceId, setSourceId] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [previous, setPrevious] = useState('');
  const [expires, setExpires] = useState(() => new Date(Date.now() + 7 * 86400000).toISOString());
  const [rights, setRights] = useState(false);
  const [storage, setStorage] = useState(false);
  const [synthetic, setSynthetic] = useState(false);
  const [publication, setPublication] = useState<KnowledgePublication | null>(null);
  const [catalog, setCatalog] = useState<KnowledgeCatalog | null>(null);
  const [inspection, setInspection] = useState<KnowledgeInspection | null>(null);
  const [review, setReview] = useState<KnowledgeReview | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [reviewConsent, setReviewConsent] = useState(false);
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState(4);
  const [contextChars, setContextChars] = useState(4096);
  const [search, setSearch] = useState<KnowledgeSearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const available = tasks?.knowledge_available === true;
  const ready = available && snapshot?.runtime.running === true && snapshot.control.owner === 'AGENT'
    && snapshot.control.status === 'running' && !busy && !tasks?.busy && !tasks?.reserved && !tasks?.approval;
  const scopeKey = `${scope.application_id}:${scope.tenant_id}:${scope.account_role}:${available}:${refreshKey}:${snapshot?.runtime.runtime_id}
    :${snapshot?.runtime.image_id}:${snapshot?.control.lease_id}:${snapshot?.control.generation}:${snapshot?.control.owner}:${snapshot?.control.status}`;
  const scopeRef = useRef(scopeKey); scopeRef.current = scopeKey;
  const serial = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const publicationMatches = publication && publication.document.source_id === sourceId && publication.document.title === title
    && publication.document.text === content && publication.document.previous_sha256 === (previous || null)
    && publication.document.expires_at === expires && publication.document.synthetic === synthetic && rights && storage;
  const expiresAt = Date.parse(expires);
  const draftValid = validKnowledgeScope(scope) && knowledgeIdentifier(sourceId) && Array.from(title).length >= 1
    && Array.from(title).length <= 200 && Boolean(content.trim()) && !content.includes('\u0000') && Array.from(content).length <= 32768
    && new TextEncoder().encode(content).length <= 65536 && (!previous || knowledgeHash(previous))
    && /^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$/.test(expires) && Number.isFinite(expiresAt)
    && expiresAt > Date.now() && expiresAt <= Date.now() + 30 * 86400000 && rights && storage;

  function invalidate() {
    serial.current += 1; pending.current?.abort(); setLoading(false); setError(false);
    setPublication(null); setReview(null); setConfirmation(''); setReviewConsent(false);
  }
  useEffect(() => {invalidate(); setCatalog(null); setInspection(null); setSearch(null);}, [scopeKey]);
  useEffect(() => () => {serial.current += 1; pending.current?.abort();}, []);

  async function request(operation: () => Promise<void>) {
    if (!available || loading || !validKnowledgeScope(scope)) return;
    const requestId = ++serial.current;
    const controller = new AbortController(); pending.current = controller;
    setLoading(true); setError(false);
    try {await operation();}
    catch {if (serial.current === requestId && !controller.signal.aborted) {
      setError(true); setPublication(null); setReview(null); setInspection(null); setSearch(null); setCatalog(null);
      setConfirmation(''); setReviewConsent(false);
    }}
    finally {if (serial.current === requestId) setLoading(false);}
  }
  function boundRequest() {
    const expectedSerial = serial.current;
    const expectedScope = scopeKey;
    const signal = pending.current?.signal;
    const current = () => {if (serial.current !== expectedSerial || scopeRef.current !== expectedScope || signal?.aborted) throw new Error('stale');};
    async function post<Result>(operation: string, body: object): Promise<Result> {
      current();
      const result = await api<Result>(`/api/knowledge/${operation}`, {schema_version: '1.0', ...body}, signal);
      current(); return result;
    }
    async function lease() {
      if (!snapshot || !ready) throw new Error('stale');
      const fresh = await api<Snapshot>('/api/state', undefined, signal); current();
      if (fresh.runtime.runtime_id !== snapshot.runtime.runtime_id || fresh.runtime.image_id !== snapshot.runtime.image_id
          || fresh.runtime.running !== true || fresh.control.lease_id !== snapshot.control.lease_id
          || fresh.control.generation !== snapshot.control.generation || fresh.control.owner !== 'AGENT'
          || fresh.control.status !== 'running') throw new Error('stale');
      return {lease_id: fresh.control.lease_id, generation: fresh.control.generation};
    }
    return {post, lease, current};
  }
  async function previewPublication() {
    if (!draftValid || !ready) return;
    const {post, current} = boundRequest();
    setPublication(null); setReview(null); setConfirmation('');
    const expected = {source_id: sourceId, title, text: content, previous_sha256: previous || null,
      expires_at: expires, rights_attested: true, storage_consent: true, synthetic};
    const result = await post<unknown>('publish-preview', {scope, ...expected});
    if (!await validKnowledgePublication(result, scope, expected)) throw new Error('invalid');
    current(); setPublication(result as KnowledgePublication);
  }
  async function publish() {
    if (!publication || !publicationMatches || !draftValid || !ready || confirmation !== publication.preview_sha256) return;
    const {post, lease, current} = boundRequest();
    const result = await post<unknown>('publish', {preview: publication, confirm_sha256: confirmation, ...await lease()});
    if (!await validKnowledgeInspection(result, scope, publication.document_sha256)
        || (result as KnowledgeInspection).review_status !== 'pending' || !(result as KnowledgeInspection).current
        || (result as KnowledgeInspection).expired) throw new Error('invalid');
    current(); setInspection(result as KnowledgeInspection); setPublication(null); setConfirmation(''); setCatalog(null); setSearch(null);
  }
  async function loadCatalog() {
    const {post} = boundRequest();
    const result = await post<unknown>('catalog', {scope});
    if (!validKnowledgeCatalog(result, scope)) throw new Error('invalid');
    setCatalog(result);
  }
  async function inspect(documentHash: string) {
    const {post, current} = boundRequest();
    setInspection(null); setReview(null); setConfirmation(''); setReviewConsent(false);
    const result = await post<unknown>('inspect', {scope, document_sha256: documentHash});
    if (!await validKnowledgeInspection(result, scope, documentHash)) throw new Error('invalid');
    current(); setInspection(result as KnowledgeInspection);
  }
  async function previewReview(decision: KnowledgeDecision) {
    if (!inspection || !ready || (decision === 'revoke' ? inspection.review_status !== 'accepted' : !inspection.current || inspection.expired)) return;
    const {post, current} = boundRequest();
    setReview(null); setPublication(null); setConfirmation(''); setReviewConsent(false);
    const result = await post<unknown>('review-preview', {scope, document_sha256: inspection.document_sha256, decision});
    if (!await validKnowledgeReview(result, inspection, decision)) throw new Error('invalid');
    current(); setReview(result as KnowledgeReview);
  }
  async function recordReview() {
    if (!inspection || !review || !ready || !reviewConsent || confirmation !== review.preview_sha256) return;
    const {post, lease, current} = boundRequest();
    const result = await post<unknown>('review', {preview: review, confirm_sha256: confirmation, ...await lease()});
    const expectedStatus = {accept: 'accepted', reject: 'rejected', revoke: 'revoked'}[review.decision];
    if (!await validKnowledgeInspection(result, scope, review.document_sha256)
        || (result as KnowledgeInspection).review_status !== expectedStatus
        || (review.decision !== 'revoke' && (!(result as KnowledgeInspection).current || (result as KnowledgeInspection).expired))
        || (result as KnowledgeInspection).review_sha256 === review.previous_review_sha256) throw new Error('invalid');
    current(); setInspection(result as KnowledgeInspection); setReview(null); setConfirmation(''); setReviewConsent(false); setSearch(null); setCatalog(null);
  }
  async function searchKnowledge() {
    const {post, current} = boundRequest(); setSearch(null);
    const result = await post<unknown>('search', {scope, query, top_k: topK, context_chars: contextChars});
    if (!await validKnowledgeSearch(result, scope, query, topK, contextChars)) throw new Error('invalid');
    const validated = result as KnowledgeSearch;
    const sources = new Map<string, KnowledgeInspection>();
    for (const documentHash of new Set(validated.hits.map(hit => hit.document_sha256))) {
      const source = await post<unknown>('inspect', {scope, document_sha256: documentHash});
      if (!await validKnowledgeInspection(source, scope, documentHash) || !(source as KnowledgeInspection).current
          || (source as KnowledgeInspection).expired || (source as KnowledgeInspection).review_status !== 'accepted') throw new Error('invalid');
      sources.set(documentHash, source as KnowledgeInspection);
    }
    for (const hit of validated.hits) {
      const source = sources.get(hit.document_sha256)!;
      const chunk = source.document.chunks[hit.chunk_index];
      if (!chunk || hit.content_sha256 !== source.document.content_sha256 || hit.review_sha256 !== source.review_sha256
          || hit.source_id !== source.document.source_id || hit.title !== source.document.title || hit.revision !== source.document.revision
          || hit.synthetic !== source.document.synthetic || hit.chunk_sha256 !== chunk.chunk_sha256 || hit.text !== chunk.text
          || hit.start !== chunk.start || hit.end !== chunk.end) throw new Error('invalid');
    }
    current(); setSearch(validated);
  }
  function statusLabel(status: string) {
    return ({pending: text('Awaiting separate review', 'Ayrı inceleme bekliyor'), accepted: text('Accepted for lookup', 'Arama için kabul edildi'),
      rejected: text('Rejected', 'Reddedildi'), revoked: text('Review revoked', 'İnceleme geri alındı')} as Record<string, string>)[status] ?? '';
  }
  const readEnabled = available && validKnowledgeScope(scope) && !loading;
  return <section className="panel knowledge-panel" data-testid="knowledge-panel">
    <h2>{text('Documents you can review and look up', 'İnceleyip arayabileceğiniz belgeler')}</h2>
    <p className="caption">{text('Save authorized text locally, review its exact version separately, then search with source citations. Retrieved text is untrusted. Steps 1–3 do not call a model. Section 4 requires separate local-inference permission; no task, training, or deployment change is authorized.',
      'İzinli metni yerel saklayın, tam sürümünü ayrıca inceleyin ve kaynak atıflarıyla arayın. Getirilen metin güvenilmeyen içeriktir. 1–3. adımlar model çağırmaz. 4. bölüm ayrı yerel çıkarım izni ister; görev, eğitim veya deployment değişimi yetkisi vermez.')}</p>
    {!available ? <p data-testid="knowledge-unavailable" role="status">{text('This backend does not provide document knowledge. Open a separately configured session to use it.',
      'Bu backend belge bilgisi özelliğini sunmuyor. Kullanmak için ayrıca yapılandırılmış oturum açın.')}</p> : <>
      <div className="knowledge-fields" data-testid="knowledge-scope">
        <label>{text('Application ID', 'Uygulama kimliği')}<input data-testid="knowledge-application" value={scope.application_id} maxLength={100} autoComplete="off"
          onChange={event => {invalidate(); setScope({...scope, application_id: event.target.value});}}/></label>
        <label>{text('Tenant ID', 'Kiracı kimliği')}<input data-testid="knowledge-tenant" value={scope.tenant_id} maxLength={100} autoComplete="off"
          onChange={event => {invalidate(); setScope({...scope, tenant_id: event.target.value});}}/></label>
        <label>{text('Account role', 'Hesap rolü')}<input data-testid="knowledge-role" value={scope.account_role} maxLength={100} autoComplete="off"
          onChange={event => {invalidate(); setScope({...scope, account_role: event.target.value});}}/></label>
      </div>
      <p className="caption">{text('These exact identifiers bound storage and lookup; they do not verify an external account or grant site access.',
        'Bu tam kimlikler saklama ve arama kapsamını sınırlar; dış hesabı doğrulamaz veya site erişimi vermez.')}</p>
      <div className="knowledge-step">
        <h3>{text('1 · Choose a document and storage consent', '1 · Belge seçin ve saklama izni verin')}</h3>
        <div className="knowledge-fields">
          <label>{text('Source ID', 'Kaynak kimliği')}<input data-testid="knowledge-source" value={sourceId} maxLength={100} autoComplete="off" onChange={event => {invalidate(); setSourceId(event.target.value);}}/></label>
          <label>{text('Document title', 'Belge başlığı')}<input data-testid="knowledge-title" value={title} maxLength={200} autoComplete="off" onChange={event => {invalidate(); setTitle(event.target.value);}}/></label>
          <label>{text('Expires at (UTC)', 'Bitiş zamanı (UTC)')}<input data-testid="knowledge-expires" value={expires} placeholder="2026-10-01T12:00:00Z" autoComplete="off" onChange={event => {invalidate(); setExpires(event.target.value);}}/></label>
          <label>{text('Previous document SHA-256 (revision only)', 'Önceki belge SHA-256 (yalnız yeni sürüm)')}<input data-testid="knowledge-previous" value={previous} maxLength={64} autoComplete="off" onChange={event => {invalidate(); setPrevious(event.target.value);}}/></label>
        </div>
        <label className="knowledge-text">{text('Document text', 'Belge metni')}<textarea data-testid="knowledge-text" value={content} rows={7} onChange={event => {invalidate(); setContent(event.target.value);}}/></label>
        <label className="knowledge-text">{text('Load a UTF-8 text file', 'UTF-8 metin dosyası seçin')}<input data-testid="knowledge-file" type="file" accept="text/plain,.txt,.md" onChange={event => {
          const file = event.target.files?.[0]; invalidate(); setContent(''); if (!file) return;
          const fileSerial = serial.current; const fileScope = scopeRef.current;
          if (file.size > 65536) {setError(true); return;}
          void file.arrayBuffer().then(buffer => {
            if (serial.current !== fileSerial || scopeRef.current !== fileScope) return;
            const value = new TextDecoder('utf-8', {fatal: true}).decode(buffer);
            if (Array.from(value).length > 32768) throw new Error('limit');
            setContent(value);
          }).catch(() => {if (serial.current === fileSerial && scopeRef.current === fileScope) setError(true);});
        }}/></label>
        <p className="caption">{text('Plain text only · up to 32,768 characters / 65,536 UTF-8 bytes · expiry within 30 days. File selection stays in this page until you request a preview. Do not include secrets or personal data without the necessary rights.',
          'Yalnız düz metin · en çok 32.768 karakter / 65.536 UTF-8 bayt · en çok 30 gün geçerlilik. Önizleme isteyene kadar dosya seçimi bu sayfada kalır. Gerekli haklar olmadan sır veya kişisel veri eklemeyin.')}</p>
        <label className="knowledge-check"><input data-testid="knowledge-rights" type="checkbox" checked={rights} onChange={event => {invalidate(); setRights(event.target.checked);}}/>{text('I have the necessary rights to use this document.', 'Bu belgeyi kullanmak için gerekli haklara sahibim.')}</label>
        <label className="knowledge-check"><input data-testid="knowledge-storage" type="checkbox" checked={storage} onChange={event => {invalidate(); setStorage(event.target.checked);}}/>{text('I consent to storing this exact text locally for review and scoped lookup.', 'Bu tam metnin inceleme ve kapsamlı arama için yerel saklanmasına izin veriyorum.')}</label>
        <label className="knowledge-check"><input data-testid="knowledge-synthetic" type="checkbox" checked={synthetic} onChange={event => {invalidate(); setSynthetic(event.target.checked);}}/>{text('This is synthetic test content.', 'Bu sentetik test içeriğidir.')}</label>
        <button data-testid="knowledge-preview" disabled={!ready || !draftValid || loading} onClick={() => void request(previewPublication)}>{text('Preview exact document', 'Tam belgeyi önizle')}</button>
        {publication && publicationMatches ? <div data-testid="knowledge-publication">
          <p>{text('Revision', 'Sürüm')} {publication.document.revision} · {publication.document.chunks.length} {text('chunks', 'parça')}</p>
          <p>{text('Content SHA-256', 'İçerik SHA-256')}: <code>{publication.document.content_sha256}</code></p>
          <p>{text('Document SHA-256', 'Belge SHA-256')}: <code>{publication.document_sha256}</code></p>
          <p>{text('Preview SHA-256', 'Önizleme SHA-256')}: <code data-testid="knowledge-publication-hash">{publication.preview_sha256}</code></p>
          <label>{text('Enter the exact preview SHA-256', 'Tam önizleme SHA-256 değerini girin')}<input data-testid="knowledge-publish-confirm" value={confirmation} maxLength={64} autoComplete="off" onChange={event => setConfirmation(event.target.value)}/></label>
          <button data-testid="knowledge-publish" disabled={!ready || !draftValid || loading || confirmation !== publication.preview_sha256} onClick={() => void request(publish)}>{text('Store document for separate review', 'Belgeyi ayrı inceleme için sakla')}</button>
        </div> : null}
      </div>
      <div className="knowledge-step">
        <h3>{text('2 · Review an exact stored version', '2 · Saklanan tam sürümü inceleyin')}</h3>
        <button data-testid="knowledge-catalog" disabled={!readEnabled} onClick={() => void request(loadCatalog)}>{text('List documents in this scope', 'Bu kapsamdaki belgeleri listele')}</button>
        {catalog ? <div data-testid="knowledge-catalog-result">{!catalog.documents.length ? <p>{text('No documents in this scope.', 'Bu kapsamda belge yok.')}</p> : catalog.documents.map(document => <article className="registry" key={document.document_sha256}>
          <h4>{document.title} · {text('Revision', 'Sürüm')} {document.revision}</h4>
          <p>{document.source_id} · {statusLabel(document.review_status)}{document.current ? '' : text(' · superseded', ' · eski sürüm')}{document.expired ? text(' · expired', ' · süresi doldu') : ''}</p>
          <code>{document.document_sha256}</code><p><button data-testid="knowledge-inspect" disabled={!readEnabled} onClick={() => void request(() => inspect(document.document_sha256))}>{text('Open exact version', 'Tam sürümü aç')}</button></p>
        </article>)}</div> : null}
        {inspection ? <div data-testid="knowledge-inspection">
          <h4>{inspection.document.title} · {text('Revision', 'Sürüm')} {inspection.document.revision}</h4>
          <p data-testid="knowledge-review-status">{statusLabel(inspection.review_status)}{inspection.current ? '' : text(' · superseded', ' · eski sürüm')}{inspection.expired ? text(' · expired', ' · süresi doldu') : ''}</p>
          <p>{text('Document SHA-256', 'Belge SHA-256')}: <code>{inspection.document_sha256}</code></p>
          <p>{text('Content SHA-256', 'İçerik SHA-256')}: <code>{inspection.document.content_sha256}</code></p>
          <p>{text('Expires at (UTC)', 'Bitiş zamanı (UTC)')}: {inspection.document.expires_at}</p>
          <details><summary>{text('Read untrusted stored text', 'Saklanan güvenilmeyen metni oku')}</summary><pre data-testid="knowledge-stored-text">{inspection.document.text}</pre></details>
          <div className="knowledge-actions">{(inspection.review_status === 'pending' ? ['accept', 'reject'] : inspection.review_status === 'accepted' ? ['revoke'] : []).map(decision => <button key={decision} data-testid={`knowledge-${decision}-preview`}
            disabled={!ready || loading || (decision !== 'revoke' && (!inspection.current || inspection.expired))} onClick={() => void request(() => previewReview(decision as KnowledgeDecision))}>
            {decision === 'accept' ? text('Preview acceptance for lookup', 'Arama kabulünü önizle') : decision === 'reject' ? text('Preview rejection', 'Reddi önizle') : text('Preview review revocation', 'İnceleme geri almayı önizle')}</button>)}</div>
        </div> : null}
        {review ? <div data-testid="knowledge-review">
          <p>{text('Separate review', 'Ayrı inceleme')}: {review.decision}</p>
          <p>{text('Review preview SHA-256', 'İnceleme önizlemesi SHA-256')}: <code data-testid="knowledge-review-hash">{review.preview_sha256}</code></p>
          <label className="knowledge-check"><input data-testid="knowledge-review-consent" type="checkbox" checked={reviewConsent} onChange={event => setReviewConsent(event.target.checked)}/>{text('I reviewed this exact version and authorize this review decision.', 'Bu tam sürümü inceledim ve bu inceleme kararına izin veriyorum.')}</label>
          <label>{text('Enter the exact review preview SHA-256', 'Tam inceleme önizlemesi SHA-256 değerini girin')}<input data-testid="knowledge-review-confirm" value={confirmation} maxLength={64} autoComplete="off" onChange={event => setConfirmation(event.target.value)}/></label>
          <button data-testid="knowledge-review-save" disabled={!ready || loading || !reviewConsent || confirmation !== review.preview_sha256} onClick={() => void request(recordReview)}>{text('Record separate review', 'Ayrı incelemeyi kaydet')}</button>
        </div> : null}
        <p className="caption">{text('Acceptance enables scoped lookup only. Revocation removes the version from future lookup; it does not erase stored text or retract previously viewed excerpts.',
          'Kabul yalnız kapsamlı aramayı açar. Geri alma sürümü sonraki aramalardan çıkarır; saklanan metni silmez veya daha önce görülmüş alıntıları geri çekmez.')}</p>
      </div>
      <div className="knowledge-step">
        <h3>{text('3 · Search reviewed documents with citations', '3 · İncelenmiş belgeleri atıflarla arayın')}</h3>
        <div className="knowledge-fields">
          <label>{text('Search text', 'Arama metni')}<input data-testid="knowledge-query" value={query} maxLength={512} autoComplete="off" onChange={event => {invalidate(); setQuery(event.target.value); setSearch(null);}}/></label>
          <label>{text('Maximum results', 'En çok sonuç')}<input data-testid="knowledge-top-k" type="number" min={1} max={8} value={topK} onChange={event => {invalidate(); setTopK(Number(event.target.value)); setSearch(null);}}/></label>
          <label>{text('Context character budget', 'Bağlam karakter bütçesi')}<input data-testid="knowledge-context" type="number" min={256} max={8192} value={contextChars} onChange={event => {invalidate(); setContextChars(Number(event.target.value)); setSearch(null);}}/></label>
        </div>
        <button data-testid="knowledge-search" disabled={!readEnabled || !query.trim() || Array.from(query).length > 512 || !Number.isInteger(topK) || topK < 1 || topK > 8 || !Number.isInteger(contextChars) || contextChars < 256 || contextChars > 8192}
          onClick={() => void request(searchKnowledge)}>{text('Search accepted text', 'Kabul edilmiş metni ara')}</button>
        {search ? <div data-testid="knowledge-search-result">
          <p>{text('Deterministic lexical lookup', 'Deterministik sözcük araması')} · {search.used_context_chars} / {search.context_chars} {text('characters', 'karakter')}</p>
          {!search.hits.length ? <p>{text('No accepted, current, unexpired match in this exact scope.', 'Bu tam kapsamda kabul edilmiş, güncel ve süresi dolmamış eşleşme yok.')}</p> : search.hits.map(hit => <article className="registry" data-testid="knowledge-hit" key={`${hit.document_sha256}:${hit.chunk_index}`}>
            <h4>{hit.title} · {text('Revision', 'Sürüm')} {hit.revision} · {text('Chunk', 'Parça')} {hit.chunk_index + 1}</h4>
            <pre>{hit.text}</pre>
            <dl><dt>{text('Source / character offsets', 'Kaynak / karakter aralığı')}</dt><dd>{hit.source_id} · {hit.start}–{hit.end}</dd>
              <dt>{text('Document SHA-256', 'Belge SHA-256')}</dt><dd><code>{hit.document_sha256}</code></dd>
              <dt>{text('Content SHA-256', 'İçerik SHA-256')}</dt><dd><code>{hit.content_sha256}</code></dd>
              <dt>{text('Chunk SHA-256', 'Parça SHA-256')}</dt><dd><code>{hit.chunk_sha256}</code></dd>
              <dt>{text('Review SHA-256', 'İnceleme SHA-256')}</dt><dd><code>{hit.review_sha256}</code></dd></dl>
          </article>)}
        </div> : null}
      </div>
      <KnowledgeAnswer scope={scope} tasks={tasks} snapshot={snapshot} busy={busy}/>
      {!ready ? <p className="caption">{text('Storage and review require a running idle agent session with fresh control. Read-only lookup remains available.',
        'Saklama ve inceleme, taze kontrol kimliğiyle çalışan boş ajan oturumu ister. Salt okunur arama kullanılabilir.')}</p> : null}
      {loading ? <p role="status">{text('Checking scoped document evidence…', 'Kapsamlı belge kanıtı denetleniyor…')}</p> : null}
      {error ? <p role="alert" data-testid="knowledge-error">{text('The response, source, scope, or control changed, or a limit was exceeded. Request a fresh preview or inspect again.',
        'Yanıt, kaynak, kapsam veya kontrol değişti ya da sınır aşıldı. Yeni önizleme isteyin veya yeniden inceleyin.')}</p> : null}
    </>}
  </section>;
}
