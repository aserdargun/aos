import {useEffect, useRef, useState} from 'react';
import {api, type Snapshot, type Tasks} from './api';
import {useLanguage} from './i18n';
import {knowledgeDigest, knowledgeHash, sameKnowledgeScope, validKnowledgeScope, validKnowledgeSearch,
  type KnowledgeScope, type KnowledgeSearch} from './knowledgeApi';

type Preview = {
  schema_version: '1.0'; kind: 'knowledge_answer_preview'; scope: KnowledgeScope; question: string;
  top_k: number; context_chars: number; retrieval: KnowledgeSearch;
  deployment: {deployment_id: string; kind: string; real_model: boolean; pins?: Record<string, unknown>};
  authority: {session_id: string; lease_id: string; generation: number};
  store_identity: Record<string, number>; confirm_sha256: string;
  untrusted: true; execution_authorized: false; training_ready: false; gold: false;
};
type AnswerStatus = {
  schema_version: '1.0'; kind: string; available: true; answer_id: string | null;
  status: 'idle' | 'pending' | 'ready' | 'needs_human' | 'cancelled' | 'failed';
  bundle_sha256: string | null; real_model: boolean; model_called: boolean;
  citation_binding_verified: boolean; error_code: string | null;
  untrusted: true; execution_authorized: false; training_ready: false; gold: false;
};
type Selection = {schema_version: '1.0'; needs_human: boolean; quotes: {citation_id: string; text: string}[]};
type Report = AnswerStatus & {preview: Preview | null; model_request: Record<string, unknown> | null;
  model_response: Selection | null; bundle: Record<string, unknown> | null;
  historical_binding_verified: boolean; current_source_valid: boolean; current_authority_valid: boolean;
  semantic_relevance_verified: false};
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const flags = (value: Record<string, unknown>) => value.untrusted === true && value.execution_authorized === false
  && value.training_ready === false && value.gold === false;
const exact = (value: Record<string, unknown>, fields: string[]) => Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const statusFields = ['schema_version', 'kind', 'available', 'answer_id', 'status', 'bundle_sha256', 'real_model',
  'model_called', 'citation_binding_verified', 'error_code', 'untrusted', 'execution_authorized', 'training_ready', 'gold'];
function statusValid(value: unknown, answerId?: string | null): value is AnswerStatus & Record<string, unknown> {
  return record(value) && value.schema_version === '1.0' && flags(value) && value.available === true
    && (value.answer_id === null || typeof value.answer_id === 'string' && /^answer-[a-f0-9]{32}$/.test(value.answer_id))
    && (answerId === undefined || value.answer_id === answerId)
    && ['idle', 'pending', 'ready', 'needs_human', 'cancelled', 'failed'].includes(String(value.status))
    && (value.bundle_sha256 === null || knowledgeHash(value.bundle_sha256))
    && typeof value.real_model === 'boolean' && typeof value.model_called === 'boolean'
    && typeof value.citation_binding_verified === 'boolean' && (value.error_code === null || typeof value.error_code === 'string')
    && (!value.citation_binding_verified || ['ready', 'needs_human'].includes(String(value.status)));
}
async function previewValid(value: unknown, scope: KnowledgeScope, question: string, snapshot: Snapshot): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'kind', 'scope', 'question', 'top_k', 'context_chars',
    'retrieval', 'deployment', 'authority', 'store_identity', 'confirm_sha256', 'untrusted', 'execution_authorized', 'training_ready', 'gold'])
    || value.schema_version !== '1.0' || value.kind !== 'knowledge_answer_preview' || !flags(value)
    || !sameKnowledgeScope(value.scope, scope) || value.question !== question || value.top_k !== 4 || value.context_chars !== 4096
    || !record(value.authority) || !exact(value.authority, ['session_id', 'lease_id', 'generation'])
    || typeof value.authority.session_id !== 'string' || !/^desktop-session-[a-f0-9]{32}$/.test(value.authority.session_id)
    || value.authority.lease_id !== snapshot.control.lease_id || value.authority.generation !== snapshot.control.generation
    || !record(value.deployment) || !exact(value.deployment, ['deployment_id', 'kind', 'real_model', 'pins'])
    || !record(value.deployment.pins) || typeof value.deployment.real_model !== 'boolean'
    || typeof value.deployment.deployment_id !== 'string' || typeof value.deployment.kind !== 'string'
    || !record(value.store_identity) || !exact(value.store_identity, ['device', 'inode', 'owner', 'mode'])
    || !Object.values(value.store_identity).every(item => Number.isSafeInteger(item) && Number(item) >= 0)
    || !knowledgeHash(value.confirm_sha256)
    || !await validKnowledgeSearch(value.retrieval, scope, question, 4, 4096)) return false;
  const preview = value as unknown as Preview;
  if (preview.deployment.real_model && (preview.deployment.kind !== 'bonsai_native_knowledge_answerer'
    || preview.deployment.pins?.knowledge_answer_protocol !== 'aos-knowledge-answer-selection-v1'
    || preview.deployment.deployment_id !== 'bonsai-' + await knowledgeDigest(preview.deployment.pins))) return false;
  if (!preview.retrieval.hits.length) return false;
  const {confirm_sha256, ...content} = value;
  return await knowledgeDigest(content) === confirm_sha256;
}

export function KnowledgeAnswer({scope, tasks, snapshot, busy}: {
  scope: KnowledgeScope; tasks: Tasks | null; snapshot: Snapshot | null; busy: boolean;
}) {
  const tr = useLanguage() === 'tr';
  const text = (english: string, turkish: string) => tr ? turkish : english;
  const [question, setQuestion] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [consent, setConsent] = useState(false);
  const [status, setStatus] = useState<AnswerStatus | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const available = tasks?.knowledge_answer_available === true;
  const ready = available && validKnowledgeScope(scope) && snapshot?.runtime.running === true
    && snapshot.control.owner === 'AGENT' && snapshot.control.status === 'running' && !busy && !tasks?.reserved;
  const identity = `${JSON.stringify(scope)}:${snapshot?.control.lease_id}:${snapshot?.control.generation}:${snapshot?.control.owner}
    :${snapshot?.control.status}:${snapshot?.runtime.runtime_id}:${snapshot?.runtime.image_id}:${available}`;
  const serial = useRef(0);
  const abort = useRef<AbortController | null>(null);
  const invalidate = () => {serial.current++; abort.current?.abort(); setPreview(null); setConfirmation(''); setConsent(false); setStatus(null); setReport(null); setError(false); setLoading(false);};
  useEffect(() => {invalidate(); return () => {serial.current++; abort.current?.abort();};}, [identity, question]);

  async function request(work: (signal: AbortSignal) => Promise<void>) {
    abort.current?.abort();
    const controller = new AbortController(); abort.current = controller;
    const requestId = ++serial.current;
    setLoading(true); setError(false);
    try {await work(controller.signal);}
    catch {if (serial.current === requestId && !controller.signal.aborted) {setError(true); setPreview(null); setReport(null); setConsent(false); setConfirmation('');}}
    finally {if (serial.current === requestId) setLoading(false);}
  }
  async function fresh(signal: AbortSignal) {
    const current = await api<Snapshot>('/api/state', undefined, signal);
    if (!snapshot || current.control.lease_id !== snapshot.control.lease_id || current.control.generation !== snapshot.control.generation
      || current.control.owner !== 'AGENT' || current.control.status !== 'running' || !current.runtime.running
      || current.runtime.runtime_id !== snapshot.runtime.runtime_id || current.runtime.image_id !== snapshot.runtime.image_id) throw new Error('stale');
    return current;
  }
  async function prepare(signal: AbortSignal) {
    const current = await fresh(signal);
    const value = await api<unknown>('/api/knowledge-answer/preview', {schema_version: '1.0', scope, question,
      top_k: 4, context_chars: 4096, lease_id: current.control.lease_id, generation: current.control.generation}, signal);
    if (!await previewValid(value, scope, question, current) || signal.aborted) throw new Error('invalid');
    setPreview(value as Preview); setConfirmation(''); setConsent(false); setStatus(null); setReport(null);
  }
  async function start(signal: AbortSignal) {
    if (!preview || !consent || confirmation !== preview.confirm_sha256) throw new Error('consent');
    const current = await fresh(signal);
    const value = await api<unknown>('/api/knowledge-answer/start', {schema_version: '1.0', preview,
      confirm_sha256: confirmation, consent, lease_id: current.control.lease_id, generation: current.control.generation}, signal);
    if (!statusValid(value) || !record(value) || !exact(value, statusFields) || value.kind !== 'knowledge_answer_status'
      || value.answer_id === null || signal.aborted) throw new Error('invalid');
    setStatus(value); setConsent(false); setConfirmation('');
  }
  async function inspect(signal: AbortSignal, answerId: string) {
    const value = await api<unknown>('/api/knowledge-answer/report', {schema_version: '1.0', answer_id: answerId}, signal);
    if (!statusValid(value, answerId) || !record(value) || value.kind !== 'knowledge_answer_report'
      || !exact(value, [...statusFields, 'preview', 'model_request', 'model_response', 'bundle', 'historical_binding_verified',
        'current_source_valid', 'current_authority_valid', 'semantic_relevance_verified'])
      || value.semantic_relevance_verified !== false || typeof value.historical_binding_verified !== 'boolean'
      || typeof value.current_source_valid !== 'boolean' || typeof value.current_authority_valid !== 'boolean'
      || !preview || !await previewValid(value.preview, scope, question, snapshot!)
      || await knowledgeDigest(value.preview) !== await knowledgeDigest(preview)
      || !record(value.bundle) || await knowledgeDigest(value.bundle) !== value.bundle_sha256
      || !exact(value.bundle, ['schema_version', 'kind', 'answer_id', 'preview', 'consent', 'deployment', 'authority',
        'evidence', 'model_request', 'model_response', 'real_model', 'synthetic', 'citation_binding_verified',
        'semantic_relevance_verified', 'untrusted', 'execution_authorized', 'training_ready', 'gold'])
      || value.bundle.schema_version !== '1.0' || value.bundle.kind !== 'knowledge_answer_bundle'
      || !flags(value.bundle) || value.bundle.answer_id !== answerId || value.model_called !== true
      || value.real_model !== preview.deployment.real_model || value.bundle.real_model !== value.real_model
      || value.bundle.citation_binding_verified !== true || value.bundle.semantic_relevance_verified !== false
      || value.bundle.synthetic !== preview.retrieval.hits.every(hit => hit.synthetic)
      || await knowledgeDigest(value.bundle.preview) !== await knowledgeDigest(preview)
      || await knowledgeDigest(value.bundle.deployment) !== await knowledgeDigest(preview.deployment)
      || await knowledgeDigest(value.bundle.authority) !== await knowledgeDigest(preview.authority)
      || await knowledgeDigest(value.bundle.evidence) !== await knowledgeDigest(preview.retrieval.hits.map((hit, index) => ({
        id: `c${index + 1}`, document_sha256: hit.document_sha256, chunk_sha256: hit.chunk_sha256, text: hit.text})))
      || await knowledgeDigest(value.bundle.model_request) !== await knowledgeDigest(value.model_request)
      || await knowledgeDigest(value.bundle.model_response) !== await knowledgeDigest(value.model_response)
      || !record(value.bundle.consent) || !exact(value.bundle.consent, ['inference_consent', 'storage_consent', 'confirm_sha256'])
      || value.bundle.consent.inference_consent !== true || value.bundle.consent.storage_consent !== true
      || value.bundle.consent.confirm_sha256 !== preview.confirm_sha256
      || !record(value.model_response) || !exact(value.model_response, ['schema_version', 'needs_human', 'quotes'])
      || value.model_response.schema_version !== '1.0' || typeof value.model_response.needs_human !== 'boolean'
      || !Array.isArray(value.model_response.quotes) || value.model_response.quotes.length > 4
      || value.model_response.needs_human !== (value.model_response.quotes.length === 0)
      || !value.historical_binding_verified || !value.citation_binding_verified || signal.aborted) throw new Error('invalid');
    const quotes = value.model_response.quotes;
    const quoteIds = new Set<string>(); const quoteTexts = new Set<string>();
    for (const quote of quotes) {
      if (!record(quote) || !exact(quote, ['citation_id', 'text']) || typeof quote.citation_id !== 'string'
        || !/^c[1-8]$/.test(quote.citation_id) || typeof quote.text !== 'string' || !quote.text.trim()
        || Array.from(quote.text).length > 512 || quoteIds.has(quote.citation_id) || quoteTexts.has(quote.text)
        || !preview.retrieval.hits[Number(quote.citation_id.slice(1)) - 1]?.text.includes(quote.text)) throw new Error('citation');
      quoteIds.add(quote.citation_id); quoteTexts.add(quote.text);
    }
    setReport(value as unknown as Report);
  }
  useEffect(() => {
    if (status?.status !== 'pending' || !status.answer_id || !available) return;
    const answerId = status.answer_id;
    const controller = new AbortController();
    const requestId = serial.current;
    const poll = async () => {
      try {
        const value = await api<unknown>('/api/knowledge-answer/status', {schema_version: '1.0', answer_id: answerId}, controller.signal);
        if (controller.signal.aborted || serial.current !== requestId) return;
        if (!statusValid(value, answerId) || !record(value) || !exact(value, statusFields) || value.kind !== 'knowledge_answer_status') throw new Error('invalid');
        if (value.status === 'ready' || value.status === 'needs_human') await inspect(controller.signal, answerId);
        else if (value.status === 'pending') timer = window.setTimeout(() => void poll(), 600);
        if (!controller.signal.aborted && serial.current === requestId) setStatus(value);
      } catch {if (!controller.signal.aborted && serial.current === requestId) {setError(true); setReport(null);}}
    };
    let timer = window.setTimeout(() => void poll(), 300);
    return () => {controller.abort(); window.clearTimeout(timer);};
  }, [status?.answer_id, status?.status, available, identity, question]);

  return <div className="knowledge-step" data-testid="knowledge-answer-panel">
    <h3>{text('4 · Ask the local model using reviewed sources', '4 · İncelenmiş kaynaklarla yerel modele sorun')}</h3>
    <p className="caption">{text('Separate permission is required to send your question and cited text to Bonsai and privately store the request/response. No task, training, or promotion starts. The answer contains only verified source quotes; relevance and truth are not automatically proven.',
      'Sorunuzu ve atıflı metni Bonsai’ye göndermek ve istek/yanıtı özel saklamak için ayrı izin gerekir. Görev, eğitim veya promotion başlamaz. Yanıt yalnız doğrulanmış kaynak alıntıları içerir; ilgililik ve doğruluk otomatik kanıtlanmaz.')}</p>
    {!available ? <p>{text('Model-backed knowledge answers are unavailable in this backend.', 'Bu backend’de model destekli bilgi yanıtı kullanılamıyor.')}</p> : <>
      <label>{text('Question', 'Soru')}<input data-testid="knowledge-answer-question" value={question} maxLength={512} autoComplete="off" onChange={event => setQuestion(event.target.value)}/></label>
      <button data-testid="knowledge-answer-preview" disabled={!ready || loading || !question.trim() || Array.from(question).length > 512} onClick={() => void request(prepare)}>{text('Preview exact model context', 'Tam model bağlamını önizle')}</button>
      {preview ? <div data-testid="knowledge-answer-preview-result">
        <p>{preview.deployment.real_model ? text('Pinned real Bonsai deployment', 'Pinli gerçek Bonsai deployment') : text('Fixture model — not real inference', 'Fixture model — gerçek çıkarım değil')} · <code>{preview.deployment.deployment_id}</code></p>
        {preview.retrieval.hits.map((hit, index) => <article className="registry" key={`${hit.document_sha256}:${hit.chunk_index}`}><h4>c{index + 1} · {hit.title}</h4><pre>{hit.text}</pre><code>{hit.chunk_sha256}</code></article>)}
        <p>{text('Exact preview SHA-256', 'Tam önizleme SHA-256')}: <code data-testid="knowledge-answer-hash">{preview.confirm_sha256}</code></p>
        <label><input data-testid="knowledge-answer-consent" type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)}/>{text('I permit this exact local inference and private request/response storage.', 'Bu tam yerel çıkarıma ve özel istek/yanıt saklamasına izin veriyorum.')}</label>
        <label>{text('Confirm SHA-256', 'SHA-256 onayı')}<input data-testid="knowledge-answer-confirm" value={confirmation} maxLength={64} autoComplete="off" onChange={event => setConfirmation(event.target.value)}/></label>
        <button data-testid="knowledge-answer-start" disabled={!ready || loading || !consent || confirmation !== preview.confirm_sha256 || status?.status === 'pending'} onClick={() => void request(start)}>{text('Run one local answer', 'Bir yerel yanıt çalıştır')}</button>
      </div> : null}
      {status ? <div data-testid="knowledge-answer-status"><p>{text('Answer state', 'Yanıt durumu')}: {status.status}</p>
        {status.status === 'pending' ? <button data-testid="knowledge-answer-cancel" onClick={() => void request(async signal => {
          const value = await api<unknown>('/api/knowledge-answer/cancel', {schema_version: '1.0', answer_id: status.answer_id}, signal);
          if (!statusValid(value, status.answer_id) || signal.aborted) throw new Error('invalid'); setStatus(value);
        })}>{text('Cancel this answer', 'Bu yanıtı iptal et')}</button> : null}
      </div> : null}
      {report ? <div data-testid="knowledge-answer-report">
        <p>{report.real_model ? text('Real local model response', 'Gerçek yerel model yanıtı') : text('Fixture response — not a real model result', 'Fixture yanıt — gerçek model sonucu değil')}</p>
        {!report.current_source_valid || !report.current_authority_valid ? <p role="alert">{text('Historical result: source or authority is no longer current.', 'Tarihsel sonuç: kaynak veya yetki artık güncel değil.')}</p> : null}
        {report.model_response?.needs_human ? <p>{text('The model abstained; inspect the sources or clarify the question.', 'Model yanıt vermedi; kaynakları inceleyin veya soruyu netleştirin.')}</p> : report.model_response?.quotes.map(quote => {
          const hit = preview!.retrieval.hits[Number(quote.citation_id.slice(1)) - 1];
          return <article className="registry" data-testid="knowledge-answer-quote" key={quote.citation_id}><h4>{quote.citation_id} · {hit.title}</h4><pre>{quote.text}</pre><code>{hit.document_sha256}</code><br/><code>{hit.chunk_sha256}</code></article>;
        })}
        <p>{text('Citation binding verified; semantic relevance, gold labels, and training readiness remain unverified.', 'Atıf bağı doğrulandı; anlamsal ilgililik, gold etiket ve eğitim hazırlığı doğrulanmış değildir.')}</p>
      </div> : null}
    </>}
    {loading ? <p role="status">{text('Checking exact answer evidence…', 'Tam yanıt kanıtı denetleniyor…')}</p> : null}
    {error ? <p role="alert" data-testid="knowledge-answer-error">{text('Evidence, source, response or control changed. Request a fresh preview.', 'Kanıt, kaynak, yanıt veya kontrol değişti. Yeni önizleme isteyin.')}</p> : null}
  </div>;
}
