# Reviewed document knowledge and retrieval

## Boundary

This work adds a local, reviewed document-retrieval foundation for application knowledge beyond the fixed Hello workflow. Users explicitly submit text to the authenticated console. The service does not read arbitrary host paths, fetch URLs, scrape a site, upload content or infer consent from task-level Approve all.

Documents have exact application, tenant and account-role labels, immutable content/source hashes, revisions, expiry and separate review history. These labels are local corpus partitions selected by the authenticated console owner; they are not proof of a remote application's account permissions or a multi-user identity provider. A rights acknowledgement is the owner's declaration, not an automated legal review or secret scanner.

Retrieval checks scope, source integrity, current revision, review and expiry before ranking. Results are bounded, source-cited **untrusted evidence**. They cannot authorize tools, hosts, accounts, execution, training or promotion. A document saying “ignore the policy” remains document text; neither ingestion nor search executes it.

## Workflow

1. Select the exact application/tenant/account-role corpus scope and enter a source label, revision and document text, or select a local plain UTF-8 file. The UI checks byte size before reading and rejects malformed UTF-8. Do not submit secrets or data without permission.
2. Preview storage. Preview alone creates no document and performs no external I/O.
3. Give explicit rights/storage consent and exact preview confirmation under the current control lease. The document is privately stored; publication does not make it approved for retrieval.
4. Separately review the exact document. Accepted current sources may be retrieved; pending, rejected or revoked sources may not.
5. Search only the selected scope. Inspect source and chunk references rather than interpreting rank as truth or model confidence.
6. Revoke a source or publish/review a new revision when the knowledge changes. Historical records remain auditable; they do not silently become current knowledge.

User-supplied labels and expiry do not establish that a live site is unchanged. Automatic site freshness rechecks, connectors and continuous collection require their own authorized contracts.

## Retrieval versus generation

The first backend uses deterministic local lexical matching, not downloaded embedding weights, a vector database or semantic similarity. English/Turkish relevance fixtures test the declared matching behavior, not general language understanding. Index/version and ranking limits must remain explicit and reproducible.

No model is called by ingestion, review, inventory or search. A separate explicit-permission [Bonsai extractive answer lane](DOCUMENT_KNOWLEDGE_ANSWERS.md) can now consume reviewed evidence. Automatic task-context attachment, evaluated free-form generation, general RAG quality, task success, fine-tuning and deployment promotion remain separate acceptance gates. A retrieved passage is not a gold training answer or an executable skill.

Matching uses NFKD/case folding and removal of combining marks, then unique query-term overlap. Ties are ordered by document hash and chunk index. Only whole hashed chunks fit the requested context budget; oversized chunks are skipped, not silently truncated.

## API contract and limits

Authenticated exact-body POST operations under `/api/knowledge/`:

- `publish-preview`: schema version, exact scope, source ID, title, submitted text, predecessor hash or null, UTC expiry, rights/storage acknowledgements and synthetic flag.
- `publish`: exact returned preview, its confirmation hash and current lease/generation.
- `review-preview`: exact scope/document selection and accept, reject or revoke decision.
- `review`: exact review preview, confirmation hash and current lease/generation.
- `inspect`: exact scope and document hash; returns source/chunk integrity, revision, expiry and review status.
- `catalog`: exact scope; returns bounded metadata inventory, not implicit permission to search every tenant.
- `search`: exact scope, query, top-k and context-character budget; returns source, content, review and chunk hashes with character spans and bounded text.

The scope fields are `application_id`, `tenant_id` and `account_role`. Source and scope identifiers are bounded simple identifiers, not paths or URLs. The v1 contract limits the store to 128 documents, 32 revisions per source, 32,768 Unicode code points and 65,536 UTF-8 bytes per document. Chunks contain at most 1,024 code points, at most 32 per document. Queries contain at most 512 code points; top-k is at most 8 and returned context at most 8,192 code points. Character spans are Unicode code-point offsets, not UTF-8 byte or JavaScript UTF-16 offsets.

Content and chunk hashes cover exact UTF-8 text; document and confirmation hashes cover canonical JSON. A changed source, preview, review predecessor or scope cannot borrow an earlier confirmation. Knowledge operations create no task and do not use task-level Approve all.

The 128-document cap includes all scopes and historical revisions; at most 512 private review records are retained. Context budgets are 256–8,192 code points. Publication expiry must be strict UTC RFC3339 and no more than 30 days ahead. Publishing a pending successor immediately supersedes the old accepted revision. Accepted historical or expired sources can still be revoked; revoked sources cannot be reaccepted.

Old backends with no knowledge capability receive no knowledge operations from the UI. A static UI rebuild does not load new Python endpoints into a running backend; activation needs a separately controlled restart, not a silent hot upgrade.

`scripts/serve_desktop.py --knowledge-root` defaults to the private `data/document-knowledge` directory. An embedded `create_console` with `knowledge_root=None` disables the capability. Private corpus records can persist across managed sessions; that does not establish remote account authorization. Canonical requests/responses are in `schemas/knowledge_*.schema.json`; `examples/knowledge.json` is explicitly synthetic.

## Private storage and release

Document bodies, chunks, review records and indexes are private local runtime artifacts. Keep them outside Git and source archives. Canonical schemas and examples describe contracts using synthetic content only. Revoking retrieval access does not erase past outputs or automatically unlearn a trained model; this slice does not train a model.

Executed tests and the implemented API limits are recorded in [STATUS](STATUS.md). The broader product gates remain in [continuous improvement](CONTINUOUS_IMPROVEMENT.md) and [ROADMAP](ROADMAP.md).
