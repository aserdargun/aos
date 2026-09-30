# Reviewed document context for one authorized task

## Implemented scope

A bounded System-1 context consumer for ordinary task kinds advertised by the scheduler, not a new arbitrary-goal executor. Hello, local browser form/navigation/staging, configured remote tasks and vision use the shared Operator decision boundary. System-2 has a separate [selected-owned-skill planning context](OWNED_SKILL_KNOWLEDGE.md) consumer; general S2 task planning and a combined S1/S2 training lane remain open. [Bonsai extractive answers](DOCUMENT_KNOWLEDGE_ANSWERS.md) are also separate.

Reviewed, current, unexpired sources are retrieved from an explicitly selected local application/tenant/account-role partition. These labels are not external account ACL proof. Source content is untrusted: normalized goal, authorized options, exact values, tool policy and runtime lease remain unchanged. No automatic training, activation, promotion or causal quality claim follows.

## Console workflow

1. Publish and accept explicitly permitted text in Knowledge. Never put company/private corpus into source examples.
2. Open Tasks, select an advertised ordinary task and expand **Reviewed document context for one task**.
3. Enter the same local application, tenant and account-role labels and a query. Preview performs lexical retrieval only: at most four hits and 2,048 source code points in this UI.
4. Inspect the exact context, copy its confirmation hash and separately consent to inference and storing this document context in the private task trajectory. Preview expires after five minutes and its unique nonce is consumed once.
5. Choose **Start with context — manual approvals**. A fresh task is created; ordinary Approve all and learning-metadata selections do not apply. Approve each bounded effect with normal task controls and inspect its trace.
6. Select a current-session job in **Read-only task document audit** and explicitly refresh. Inspect historical binding, current source/authority, prepared/dispatched/successful counts, independent outcome and citations. Refresh never starts a task or model. Ordinary jobs without this separate admission cannot produce a report.

Query/scope/task/control/runtime changes discard armed UI consent; stale replies are ignored. Empty retrieval fails before inference. Revocation or supersession blocks subsequent decisions and effects. Already dispatched text or completed effects cannot be unsent or undone by revocation.

## Contracts and proof

Authenticated exact-body `POST /api/tasks/knowledge/{preview,start,report}` rejects queries, duplicate/extra fields and malformed bodies. Canonical contracts: `schemas/task_knowledge_*.schema.json`; explicitly synthetic fixture: `examples/task_knowledge.json`.

- Preview binds retrieval/source/chunk/review hashes, workspace descriptor and store inode identities, task/configuration/model identities and parent runtime/lease/generation. API supports top-k 1–4 and context 256–2,048.
- Start requires the complete unmodified preview, exact hash, boolean consent and current control. Immutable private intent/admission plus a transactional DB event bind one intent to one fresh job. Replay, identity/source drift and mixed owned-skill/failure-guidance/learning lanes fail closed.
- Context is appended before the immutable DECIDE snapshot is persisted. Each decision records exact options/state/snapshot/request hashes and real S1 call identity where configured. Native/reusable engines recheck source/control after startup immediately before inference dispatch. Existing host guards run first and are restored after cancellation/completion.
- Every effect requires a valid source/admission/context/control binding. Pause/takeover cannot refresh the original one-task lease consent. Re-preview and start a fresh task instead of silently reusing the original intent.
- Report request: `{schema_version: "1.0", job_id: "..."}`. Response includes private intent/admission/context/dispatch/model proofs, counts, independent outcome, historical binding and current validity. It contains consented source/actual request text: do not paste into public issues or commit it. The EN/TR audit viewer checks hashes, joins, response/request/options, deployment, unique decision IDs and current parent runtime before displaying a report. Historical lease/expiry differences do not erase past evidence and never re-arm permission.
- Successful `model_proofs` contain the validated stored Prediction and its exact `response_canonical` text/hash, deployment/decision identity and matching context/dispatch marker. Canonical text preserves Python numeric representations, including scientific notation and negative zero; clients hash those bytes instead of guessing a float serializer. Fixtures and unsuccessful calls produce no successful model proofs. These are server-audited local records, not independently signed or externally attested proof.

`context_binding_verified` means prepared, persisted context. `model_request_verified` additionally requires a successful real S1 model_call, actual dispatch/request proof and matching validated Prediction/decision. `knowledge_applied` requires both plus historical binding; deterministic fixtures never satisfy it. None demonstrate improved decisions. `causality_verified`, `scope_authorization_verified`, `gold` and `training_ready` remain false. Independent task readback is separate.

## Validation and remaining gates

`PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -p 'test_task_knowledge*.py'` runs CPU tests; Docker/UI and real inference are explicit opt-ins. Actual evidence, including unsuccessful harness attempts, is in [STATUS](STATUS.md).

Rendered acceptance requires `AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1`, prepared isolated Docker and built Chromium UI. Real acceptance requires `AOS_DESKTOP_TESTS=1 AOS_TASK_KNOWLEDGE_REAL_TESTS=1`, an explicitly idle GPU, pinned Decider and visible Ubuntu Chromium/Playwright MCP. Preserve the capability real lock and managed session authenticated idle barrier; never kill unrelated inference.

Open gates: general goals/tasks and S2 planning, combined S1/S2 context collection, vector/embedding retrieval, independent held-out relevance/quality, external ACLs, licensed datasets and private intranet acceptance. Synthetic task consumption does not complete those gates.
