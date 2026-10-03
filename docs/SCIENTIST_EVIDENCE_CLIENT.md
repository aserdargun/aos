# Scientist evidence control client

This is an opt-in AOS control transport library, not runtime admission, a GPU
allocator or a terminal-resolution policy. Scientist owns the existing scheduler
and the eventual coordinated GPU acceptance. No listener is started here.

## Explicit configuration

`ScientistEvidenceClient` requires the absolute path of the already reviewed
Scientist control socket and an explicitly pinned `ScientistEvidenceCodec`.
There is no default endpoint, policy discovery, automatic configuration update
or fallback to inference/control-v1. The codec uses the full reviewed public
schema at `schemas/scientist_evidence_transport.schema.json`:

- Transport canonical SHA-256:
  `7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`.
- Evidence schema canonical SHA-256:
  `aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`.

The separately generated request schema is not the full transport schema pin.
Scientist's private policy must explicitly enable both evidence pins. Existing
admission2.0 records and v1 capability/terminal canonical bytes are unchanged.

## Authorization and durable intent are mandatory

The production default authenticates the socket's Linux `SO_PEERCRED` against
the pinned broker's current systemd invocation, PID/start ticks, boot and cgroup.
The private user-owned socket and its parent must retain their inode identities.
An authenticated broker alone does not authorize a target or prove GPU release.

Two independent trusted callbacks are required, and defaults reject before
connecting:

- `authorize(request, peer)` must verify the exact original request/profile,
  principal and owner/generation, current retained-target or explicit cleanup
  authority, and the current boot-scoped capability expiry. It receives copies
  and must complete with `None` or raise. It is rechecked before dispatch and
  before publishing metadata, including after slow peer observations.
- `persist_intent(payload_bytes, sha256, deadline, peer)` must durably retain
  the exact canonical control request, control ID and authenticated peer before
  dispatch. It is a control-intent writer, not the existing inference intent
  writer; migration0018 must not be used with a different request shape. It must
  reject stale authority and close any crash/restart replay window. The source
  implementation is `ScientistEvidenceJournal` with additive migration0024.

Both `capability` and `reconcile` can create remote durable records or consume
reserved quota. Neither is an automatic UI inventory read. The original-store
ledger does not create a new GPU ownership authority. Durable callback composition
with production task authority remains a separate acceptance.

## Original-store ledger

`ScientistEvidenceJournal` shares the exact original `TrajectoryStore` and
admission history2.0. Additive migration0024 creates immutable control requests
and separate immutable response records; migrations0018 and0023 are unchanged.
Every request anchors the original inference request/hash/profile/deployment,
original caller-generation hash and admission-record hash. Missing or legacy
admission is rejected, never adopted. Pending requests block another control for
the same original target even after process restart or a new current lease.

The current binding must retain the original session/runtime and match the
running controller's owner, lease and generation. Both AGENT and HUMAN can request
cleanup only through an explicit trusted `verify_control` callback; human
takeover does not inherit authority merely from historical admission. Original
inference expiry is not renewed or used to authorize new inference. Current
control deadlines remain separately bounded to ten seconds and are rechecked
after slow callbacks before each transaction commits.

Compose client callbacks explicitly as `journal.authorize`,
`journal.persist_intent` and `journal.record_response`. The original-target
`verify_control(request, original_record, current_binding, peer)` provider remains
default-denied. It must check fresh retained-target/cleanup rights and expiry,
return `None` or raise, and must never commit, roll back or otherwise take
ownership of the journal's connection transaction. It is not a model-provided
function or a replacement scheduler.

The optional client response callback receives copied metadata and peer. Journal
response persistence checks original request, saved peer, exact current binding,
codec correlation, original admission and deadline in one transaction. A response
append settles only the control exchange; the original inference intent remains
unresolved. A later authorization failure can leave a valid historical ACK while
the client still fences publication as uncertain. Neither pending-count zero
nor `inspect()` is GPU-release proof or permission to replay/start another task.

The Tasks panel shows redacted local pending-control counts without invoking
authority callbacks. Legacy tables remain absent instead of being upgraded on
read. Unknown/malformed metadata is not displayed as zero. Canonical dataset
audit supports migration24 and continues to audit versions7–23 read-only.

Caller restart/session transfer, resolution of abandoned control intents,
independent physical proof and inference resolution remain separate gates.

## Bounded exchange, not automatic recovery

`exchange(request, cancel_event=...)` accepts only the exact reviewed evidence
namespace and operations. It uses one LF-terminated canonical request and one
bounded LF-terminated response followed by EOF, a finite positive timeout of at
most ten seconds, and a single in-flight request. No retries occur.

An attempted control ID cannot be repeated within the client. Any ambiguity
after dispatch, including partial send, timeout, cancellation, malformed/lost
response, revoked authorization or a changed broker generation, latches
`uncertain_control_id` and rejects further exchanges. Local cancellation stops
waiting; it does not cancel a Scientist experiment or prove remote cleanup.
`retryable=true` in a validated remote error does not permit blind repetition.
An in-memory fence is not sufficient after a process crash; the durable callback
must preserve that restriction independently.

The return value is authenticated, correlated transport metadata only. Exact
terminal/result canonical bytes may enter `ScientistTerminalVerifier` only with
an independently retained original budget, pinned result validation, current
resolver authority and independently verified physical proof. Matching hashes,
idle/quiesce, a stop ACK or a successful exchange cannot release a lease, clear
an unresolved intent, restart a worker or grant a new task.

## Source coordination

```sh
.venv/bin/python -m scripts.scientist_source_report \
  --source-profile evidence_client_candidate_v2
```

This explicit profile adds the authenticated client to the 38 evidence transport
sources. Legacy profile membership stays unchanged. Scientist must explicitly
review and recognize the new profile; it is not silently mapped to its older
transport-only preflight. Source identity remains admission-denied and is not
a complete dependency/configuration/deployment attestation.

Current runtime status is **partial**. CPU owned-socket fixtures, source/schema
agreement and package validation do not establish real GPU release or joint
runtime acceptance. Evidence and unrun gates are maintained in `docs/STATUS.md`.
