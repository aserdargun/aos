# Reviewed documents in a selected-skill S2 proposal

## Implemented boundary

An optional document-context lane for the already-admitted owned selected-skill planner. This is not arbitrary-goal planning or a new executor. The existing two EN/TR goal forms, exact candidate/skill/value, six ordered operations, separate plan bind/start and manual effect approvals stay unchanged. Ordinary System-1 context is a [separate lane](TASK_KNOWLEDGE.md); document context is not automatically added to this skill's S1 requests.

Only new context-capable planning deployments advertise the feature. `BonsaiOwnedSkillPlanner(..., knowledge_context=True)` pins the context protocol and canonical payload schema. Its default remains off: historical no-context deployment identities and requests are unchanged. New owned-reuse server sessions enable the capability; a running old backend is not hot-upgraded. The ordinary plain pilot does not have a selected owned skill and does not display this panel.

## Console workflow

1. Publish and accept privately stored, explicitly permitted sources in Knowledge. Use exact local application/tenant/account-role labels; these are not external account ACL proof.
2. In the configured owned-reuse session, open Tasks → selected skill planning. Enter a supported save-message goal.
3. Expand the optional reviewed-document context panel. Enter the same source scope and a query, then preview. Retrieval is bounded to four current accepted hits and 2,048 source code points. Preview performs no model call or task execution.
4. Inspect citations, untrusted text and the exact confirmation hash. Separately consent to S2 inference and private storage of this context in the proposal record. Source/query/goal/control/runtime changes clear armed consent.
5. Start the context-bound proposal. This only calls the pinned planner; it cannot execute the skill. After a valid proposal, separately confirm/bind its hash, preview execution and explicitly start with manual effect approvals.
6. Explicitly refresh the read-only context report. Historical binding, current source/authority, dispatch and successful real-model use are separate. Refresh works after pause; it never resumes, starts a task or calls a model.

Context and episode collection are currently separate lanes. This inference/storage consent does not license corpus export, labels or training; combined context plus `collect_learning` is rejected pending a distinct rights/redaction/export contract.

## Binding and revocation

The five-minute preview binds a nonce, goal/case, admitted evidence, owned authority/reuse/source/review/selection pins, workspace/store identities, retrieval and the context-capable S2 deployment. Admission consumes the nonce once and writes the exact private intent before scheduling inference. The expiry limits admission; it is not a silent in-flight lease refresh.

The existing native pre-dispatch guard runs first. Context source/control/skill validity is then checked immediately after native server startup and before the HTTP call; an immutable dispatch record binds actual request, context, intent and planning identity. Exact captured `last_request` and normalized `last_response` must match before bundle publication. Checks repeat after return, at selection/bind/start and inside the persisted execution revalidator before effects. Existing hooks are restored on success, error and cancellation, including cancellation before GPU yield.

Revocation blocks subsequent admitted decisions/effects and new selection. It cannot unsend already dispatched text or undo completed effects. Historical reports remain available with current-source false; a source revocation does not itself imply control authority changed.

Bundle version **1.2** adds the separately consented knowledge intent and dispatch. Versions 1.0/1.1 remain valid. A context-bearing bundle's `synthetic` flag reflects its actual document sources; the owned target workflow still remains synthetic. No DB migration, model activation or trainer admission occurs.

## Canonical transport and proof scope

Python and JavaScript serialize numbers such as `0.0` differently. The UI must not rebuild pinned hashes using guessed float formatting or change the native deployment to make a hash pass. Version-1.1 wire envelopes carry exact Python canonical preview/body/pin bytes and exact intent/request bytes for reports. The UI hashes those strings and checks their parsed structure against the displayed records. A version-1.1 start transports canonical preview text; the server parses and validates it without discarding float representations. Inner preview/intent/bundle contracts and historical identities remain unchanged; legacy version-1.0 starts remain supported.

The read-only report includes the exact canonical full proposal bundle and its hash, consent/dispatch/model joins and private request/response. Do not paste reports into public issues or commit their source text. Real S2 model use requires the successful native call and exact dispatched request; fixtures always report model-use false. `execution_authorized`, `gold`, `training_ready`, `downstream_verified`, `semantic_relevance_verified`, causal quality and external scope authorization remain false. Local records are not externally signed attestation.

## Acceptance and open work

CPU service/API tests, rendered EN/TR fixture acceptance and native model/service acceptance are separate; exact runs are recorded in [STATUS](STATUS.md). Opt-in native acceptance uses `AOS_OWNED_SKILL_KNOWLEDGE_REAL_TESTS=1`, pinned Bonsai, synthetic corpus and synthetic admitted skill/controller, with no S1 execution or task effects. Preserve the real capability lock and the running managed application's authenticated idle barrier.

Real managed acceptance additionally uses `AOS_DESKTOP_TESTS=1 AOS_OWNED_SKILL_KNOWLEDGE_MANAGED_TESTS=1`: two isolated source/reuse sessions, EN/TR document-context proposals, separate bind/start, real Decider, six fresh approvals per completed task, independent receipt/state readback and a third run revoked before fill/POST. Exact inference and execution replay requests are rejected. Corpus and site remain synthetic; the S2 report's `downstream_verified=false` is unchanged because the independent execution audit is a separate proof, not a property of inference alone. The candidate loader allows at most 131,072 bytes for the planning artifact, matching planning storage; other artifacts retain the 65,536-byte limit.

Remaining: richer goals/applications, paired S1/S2 context collection with licensed data, held-out relevance/quality, embedding/vector retrieval, external ACL proof and real intranet/site acceptance. This selected-skill chain does not close the [remaining-work queue](REMAINING_WORK.md).
