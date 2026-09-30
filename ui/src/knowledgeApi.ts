export interface KnowledgeScope {application_id: string; tenant_id: string; account_role: string}
type Flags = {untrusted: true; execution_authorized: false; training_ready: false; gold: false};
export interface KnowledgeChunk {index: number; start: number; end: number; text: string; chunk_sha256: string}
export interface KnowledgeDocument extends Flags {
  schema_version: '1.0'; kind: 'uploaded_text_knowledge'; scope: KnowledgeScope;
  source_id: string; title: string; text: string; revision: number; previous_sha256: string | null;
  expires_at: string; rights_attested: true; storage_consent: true; synthetic: boolean;
  content_sha256: string; chunks: KnowledgeChunk[];
}
export interface KnowledgePublication {
  schema_version: '1.0'; kind: 'knowledge_publication_preview'; document: KnowledgeDocument;
  document_sha256: string; preview_sha256: string;
}
export type KnowledgeDecision = 'accept' | 'reject' | 'revoke';
export type KnowledgeStatus = 'pending' | 'accepted' | 'rejected' | 'revoked';
export interface KnowledgeReview {
  schema_version: '1.0'; kind: 'knowledge_review_preview'; scope: KnowledgeScope;
  document_sha256: string; decision: KnowledgeDecision; previous_review_sha256: string | null; preview_sha256: string;
}
export interface KnowledgeInspection extends Flags {
  schema_version: '1.0'; kind: 'knowledge_document_inspection'; document_sha256: string; document: KnowledgeDocument;
  current: boolean; expired: boolean; review_status: KnowledgeStatus; review_sha256: string | null;
}
export interface KnowledgeSummary {
  document_sha256: string; source_id: string; title: string; revision: number; content_sha256: string;
  expires_at: string; synthetic: boolean; current: boolean; expired: boolean;
  review_status: KnowledgeStatus; review_sha256: string | null;
}
export interface KnowledgeCatalog extends Flags {
  schema_version: '1.0'; kind: 'knowledge_catalog'; scope: KnowledgeScope; documents: KnowledgeSummary[];
  limits: typeof knowledgeLimits;
}
export interface KnowledgeHit {
  document_sha256: string; content_sha256: string; review_sha256: string; source_id: string; title: string;
  revision: number; chunk_index: number; chunk_sha256: string; start: number; end: number;
  text: string; score: number; synthetic: boolean;
}
export interface KnowledgeSearch extends Flags {
  schema_version: '1.0'; kind: 'knowledge_search'; scope: KnowledgeScope; query_sha256: string;
  method: 'deterministic_lexical'; top_k: number; context_chars: number; used_context_chars: number; hits: KnowledgeHit[];
}
export const knowledgeLimits = {max_documents: 128, max_revisions: 32, max_text_chars: 32768,
  max_text_bytes: 65536, chunk_chars: 1024, max_chunks_per_document: 32,
  max_query_chars: 512, max_top_k: 8, max_context_chars: 8192} as const;
export const knowledgeIdentifier = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
export const knowledgeHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value: Record<string, unknown>, fields: string[]) => Object.keys(value).length === fields.length && fields.every(field => Object.hasOwn(value, field));
const length = (value: string) => Array.from(value).length;
const integer = (value: unknown, minimum: number, maximum: number): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
const nullableHash = (value: unknown) => value === null || knowledgeHash(value);
const date = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
const shortText = (value: unknown): value is string => typeof value === 'string' && length(value) >= 1 && length(value) <= 200;
const flagKeys = ['untrusted', 'execution_authorized', 'training_ready', 'gold'];
const flags = (value: Record<string, unknown>) => value.untrusted === true && value.execution_authorized === false && value.training_ready === false && value.gold === false;
export function validKnowledgeScope(value: unknown): value is KnowledgeScope {
  return record(value) && exact(value, ['application_id', 'tenant_id', 'account_role']) && Object.values(value).every(knowledgeIdentifier);
}
export function sameKnowledgeScope(value: unknown, expected: KnowledgeScope): boolean {
  return validKnowledgeScope(value) && value.application_id === expected.application_id && value.tenant_id === expected.tenant_id && value.account_role === expected.account_role;
}
export function knowledgeCanonical(value: unknown): string {
  return canonical(value);
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (record(value)) return `{${Object.keys(value).sort().map(key => `${canonical(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value).replace(/[\u0080-\uffff]/g, character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}
export async function knowledgeTextHash(value: string): Promise<string> {
  const result = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(result), byte => byte.toString(16).padStart(2, '0')).join('');
}
export const knowledgeDigest = (value: unknown) => knowledgeTextHash(canonical(value));
const documentKeys = ['schema_version', 'kind', 'scope', 'source_id', 'title', 'text', 'revision', 'previous_sha256',
  'expires_at', 'rights_attested', 'storage_consent', 'synthetic', 'content_sha256', 'chunks', ...flagKeys];
export async function validKnowledgeDocument(value: unknown, scope: KnowledgeScope): Promise<boolean> {
  if (!record(value) || !exact(value, documentKeys) || value.schema_version !== '1.0' || value.kind !== 'uploaded_text_knowledge'
      || !flags(value) || !sameKnowledgeScope(value.scope, scope) || !knowledgeIdentifier(value.source_id) || !shortText(value.title)
      || typeof value.text !== 'string' || !value.text.trim() || value.text.includes('\u0000') || length(value.text) > 32768
      || new TextEncoder().encode(value.text).length > 65536 || !integer(value.revision, 1, 32)
      || !nullableHash(value.previous_sha256) || (value.revision === 1) !== (value.previous_sha256 === null)
      || !date(value.expires_at) || value.rights_attested !== true || value.storage_consent !== true || typeof value.synthetic !== 'boolean'
      || !knowledgeHash(value.content_sha256) || value.content_sha256 !== await knowledgeTextHash(value.text)
      || !Array.isArray(value.chunks) || value.chunks.length !== Math.ceil(length(value.text) / 1024)) return false;
  const characters = Array.from(value.text);
  for (const [index, chunk] of value.chunks.entries()) {
    if (!record(chunk) || !exact(chunk, ['index', 'start', 'end', 'text', 'chunk_sha256']) || chunk.index !== index
        || chunk.start !== index * 1024 || chunk.end !== Math.min((index + 1) * 1024, characters.length)
        || chunk.text !== characters.slice(index * 1024, (index + 1) * 1024).join('') || !knowledgeHash(chunk.chunk_sha256)) return false;
    if (chunk.chunk_sha256 !== await knowledgeTextHash(chunk.text as string)) return false;
  }
  return true;
}
export async function validKnowledgePublication(value: unknown, scope: KnowledgeScope, expected: Record<string, unknown>): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'kind', 'document', 'document_sha256', 'preview_sha256'])
      || value.schema_version !== '1.0' || value.kind !== 'knowledge_publication_preview' || !record(value.document)
      || !await validKnowledgeDocument(value.document, scope) || !knowledgeHash(value.document_sha256)
      || value.document_sha256 !== await knowledgeDigest(value.document) || !knowledgeHash(value.preview_sha256)) return false;
  const {preview_sha256, ...content} = value;
  return preview_sha256 === await knowledgeDigest(content) && Object.entries(expected).every(([key, expectedValue]) => value.document && (value.document as Record<string, unknown>)[key] === expectedValue);
}
const status = (value: unknown) => ['pending', 'accepted', 'rejected', 'revoked'].includes(String(value));
const reviewBinding = (value: Record<string, unknown>) => status(value.review_status) && nullableHash(value.review_sha256)
  && (value.review_status === 'pending') === (value.review_sha256 === null);
export async function validKnowledgeInspection(value: unknown, scope: KnowledgeScope, expectedHash: string): Promise<boolean> {
  return record(value) && exact(value, ['schema_version', 'kind', 'document_sha256', 'document', 'current', 'expired', 'review_status', 'review_sha256', ...flagKeys])
    && value.schema_version === '1.0' && value.kind === 'knowledge_document_inspection' && flags(value)
    && knowledgeHash(expectedHash) && value.document_sha256 === expectedHash && await validKnowledgeDocument(value.document, scope)
    && value.document_sha256 === await knowledgeDigest(value.document) && typeof value.current === 'boolean'
    && typeof value.expired === 'boolean' && record(value.document)
    && value.expired === (Date.parse(value.document.expires_at as string) <= Date.now()) && reviewBinding(value);
}
export async function validKnowledgeReview(value: unknown, inspection: KnowledgeInspection, decision: KnowledgeDecision): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'kind', 'scope', 'document_sha256', 'decision', 'previous_review_sha256', 'preview_sha256'])
      || value.schema_version !== '1.0' || value.kind !== 'knowledge_review_preview' || !sameKnowledgeScope(value.scope, inspection.document.scope)
      || value.document_sha256 !== inspection.document_sha256 || value.decision !== decision
      || value.previous_review_sha256 !== inspection.review_sha256 || !knowledgeHash(value.preview_sha256)) return false;
  const {preview_sha256, ...content} = value;
  return preview_sha256 === await knowledgeDigest(content);
}
export function validKnowledgeCatalog(value: unknown, scope: KnowledgeScope): value is KnowledgeCatalog {
  return record(value) && exact(value, ['schema_version', 'kind', 'scope', 'documents', 'limits', ...flagKeys])
    && value.schema_version === '1.0' && value.kind === 'knowledge_catalog' && sameKnowledgeScope(value.scope, scope) && flags(value)
    && record(value.limits) && exact(value.limits, Object.keys(knowledgeLimits)) && Object.entries(knowledgeLimits).every(([key, limit]) => (value.limits as Record<string, unknown>)[key] === limit)
    && Array.isArray(value.documents) && value.documents.length <= 128 && new Set(value.documents.map(item => record(item) ? item.document_sha256 : null)).size === value.documents.length
    && value.documents.every(item => record(item) && exact(item, ['document_sha256', 'source_id', 'title', 'revision', 'content_sha256', 'expires_at', 'synthetic', 'current', 'expired', 'review_status', 'review_sha256'])
      && knowledgeHash(item.document_sha256) && knowledgeIdentifier(item.source_id) && shortText(item.title) && integer(item.revision, 1, 32)
      && knowledgeHash(item.content_sha256) && date(item.expires_at) && typeof item.synthetic === 'boolean' && typeof item.current === 'boolean'
      && typeof item.expired === 'boolean' && reviewBinding(item));
}
export async function validKnowledgeSearch(value: unknown, scope: KnowledgeScope, query: string, topK: number, contextChars: number): Promise<boolean> {
  if (!record(value) || !exact(value, ['schema_version', 'kind', 'scope', 'query_sha256', 'method', 'top_k', 'context_chars', 'used_context_chars', 'hits', ...flagKeys])
      || value.schema_version !== '1.0' || value.kind !== 'knowledge_search' || !sameKnowledgeScope(value.scope, scope) || !flags(value)
      || value.query_sha256 !== await knowledgeTextHash(query) || value.method !== 'deterministic_lexical' || value.top_k !== topK
      || value.context_chars !== contextChars || !integer(value.used_context_chars, 0, contextChars) || !Array.isArray(value.hits)
      || value.hits.length > topK) return false;
  let used = 0;
  const seen = new Set<string>();
  for (const hit of value.hits) {
    if (!record(hit) || !exact(hit, ['document_sha256', 'content_sha256', 'review_sha256', 'source_id', 'title', 'revision', 'chunk_index', 'chunk_sha256', 'start', 'end', 'text', 'score', 'synthetic'])
        || !knowledgeHash(hit.document_sha256) || !knowledgeHash(hit.content_sha256) || !knowledgeHash(hit.review_sha256)
        || !knowledgeIdentifier(hit.source_id) || !shortText(hit.title) || !integer(hit.revision, 1, 32) || !integer(hit.chunk_index, 0, 31)
        || !knowledgeHash(hit.chunk_sha256) || !integer(hit.start, 0, 32767) || !integer(hit.end, 1, 32768)
        || typeof hit.text !== 'string' || length(hit.text) < 1 || length(hit.text) > 1024 || hit.start !== hit.chunk_index * 1024
        || hit.end !== hit.start + length(hit.text) || hit.chunk_sha256 !== await knowledgeTextHash(hit.text)
        || !integer(hit.score, 1, 512) || typeof hit.synthetic !== 'boolean') return false;
    const key = `${hit.document_sha256}:${hit.chunk_index}`;
    if (seen.has(key)) return false;
    seen.add(key); used += length(hit.text);
  }
  return used === value.used_context_chars && used <= contextChars;
}
