# Reviewed knowledge → local Bonsai extractive answers

## Boundary and workflow

This is a model consumer of the [reviewed document corpus](DOCUMENT_KNOWLEDGE.md), not automatic task execution. An English or Turkish question retrieves current, accepted, unexpired sources from the exact local application/tenant/role partition. Pinned native Bonsai System-2 selects up to four short verbatim passages, or abstains. Host validation requires exact admitted citation IDs, raw chunk hashes and contiguous source substrings. No unrestricted prose answer is presumed factually supported just because it cites a source.

Corpus labels are not external account ACLs. Sources and outputs stay untrusted; quote integrity does not prove relevance, source truth, live-site correctness, causal improvement or gold labels. Questions and citations cannot grant tools, targets, accounts, training, promotion or deployment changes. System-1 task-context consumption, embedding/vector retrieval, evaluated free-form generation and arbitrary-site mastery remain open.

1. Publish and separately accept a document under its exact scope.
2. In Knowledge / Bilgi section 4, enter a question. Preview retrieves up to four whole chunks within 4,096 code points; no model starts or answer artifact is stored.
3. Inspect the exact question, cited text, deployment and hash. Give separate permission for **this local inference and private request/response storage**, then enter the exact SHA-256.
4. Start one answer. AOS reserves its model/task admission lane, releases its own reusable S1 worker if present and calls Bonsai. No task or per-action approval is created; Approve all is unrelated.
5. Read source quotations or abstention, with source/chunk hashes. Changed input/scope/control discards armed permission and stale responses. Cancel stops only this answer's owned model call.

No match fails before inference. Preview/start rechecks consent, question, retrieval, reviews, deployment, control and private-store identity. Guards run after GPU yield, after Bonsai readiness immediately before HTTP dispatch, after response and before/after persistence. Revocation or a successor blocks later stale admission/completion. Already dispatched content cannot be unsent; revocation does not erase past outputs or unlearn models.

Other tasks, planning/adaptation, corpus publication/review and managed restart are blocked while an answer is pending or its model cleanup is running. Pause, takeover, logout, stop and shutdown cancel through existing controls. This coordinates the local AOS session, not every GPU user; foreign processes are not killed.

## Contract and evidence

Authenticated exact-body POST `/api/knowledge-answer/{preview,start,status,report,cancel}` rejects query parameters, duplicate keys and extra fields. Canonical contracts are `schemas/knowledge_answer_*.schema.json` and explicitly synthetic `examples/knowledge_answer.json`.

- `preview`: schema version, scope, question ≤512 code points, top-k 1–8, context 256–8,192, lease/generation. Requires eligible hits.
- `start`: full preview, exact confirmation, boolean consent, current lease/generation. At most eight starts per session and one pending call.
- `status`: answer ID or null for current status. `model_called` is set at HTTP dispatch, not reservation/startup; alone it is not successful inference proof.
- `report`: exact answer ID. The private immutable bundle binds preview, consent, deployment, authority, evidence, actual dispatched request and validated selection. Historical proof and current source/authority validity are separate.
- `cancel`: exact current answer ID; authenticated cancellation does not need new execution authority.

Model output allows only `schema_version`, `needs_human` and `{citation_id,text}` quotes. Quotes are nonblank, ≤512 code points, unique by citation and text, and exact admitted substrings. Abstention has no quotes. Pinning covers schema, protocol `aos-knowledge-answer-selection-v1` and a 768 output-token limit. Truncation, unknown citations, altered quotes and authority claims fail closed. Native runtime verifies model/code/dependencies and cleans up only its own server process.

The UI checks preview/bundle hashes, scope/control, typed flags, quote bounds/substrings, evidence mapping and top-level response equality with the hashed bundle. Flipping a fixture display flag cannot make it real. `execution_authorized`, `training_ready`, `gold` and `semantic_relevance_verified` remain false.

## Activation, privacy and tests

Managed real Decider consoles configure this Bonsai adapter when the knowledge root is enabled. Embedded consoles default to no answer adapter; rendered tests explicitly inject a synthetic one. Old Python backends need a controlled idle `./scripts/aos-v1 restart`; a static UI rebuild is not backend activation.

Questions, source bodies and request/response bundles remain private runtime files, outside source packages. Do not submit secrets or unauthorized company content; no automatic secret-scanning/redaction proof is claimed. These records do not silently become skills or training datasets.

CPU: `PYTHONPATH=src:tests .venv/bin/python -m unittest test_knowledge_answer test_knowledge_answer_model test_knowledge_answer_model_dispatch test_knowledge_answer_console.KnowledgeAnswerConsoleTests`.

Rendered fixture: `AOS_UI_TESTS=1` with `test_knowledge_answer_console.KnowledgeAnswerRenderedTests`, actual Chromium/authenticated TCP API over an isolated descriptor-workspace fixture, not a real model/company site. Existing document UI has separate Docker acceptance.

Real acceptance: separately opt-in `AOS_KNOWLEDGE_ANSWER_REAL_TESTS=1`, with an authorized idle GPU window. Synthetic English/Turkish manuals have predeclared independent expected facts and an unanswerable question. Three cases are not broad held-out quality, full RAG, training or W1–W6 acceptance. Executed results and operational activation are in [STATUS](STATUS.md).
