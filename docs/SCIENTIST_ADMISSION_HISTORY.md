# Original Scientist admission history

This is an opt-in AOS source path. It is not a new allocation authority, a
jointly admitted control client, or a GPU-release/reconciliation implementation.
Scientist's existing scheduler remains the sole GPU allocation authority.

## What is persisted

Canonical migration0023 adds append-only `scientist_admission_history`, keyed to
the original0018 request. Original request bytes/hash, session/control-binding
hash, authenticated broker peer, complete stable admission binding/hash and
separate capability observation are correlated before commit. Existing0018
bytes, state machine and one-unresolved-turn index are unchanged. Existing
legacy rows are not adopted or furnished with fabricated original identity.

The stable binding follows the current nine-field Scientist source:
server/caller generations, policy hash, source fingerprints, fixed profile and
deployment/manifest/config/content-schema pins, plus infer/control/terminal
schema pins. Every nested record is closed. Capability SHA and issued/expiry
freshness are separate from the stable hash, so refresh is not silently recast
as a new original admission. The captured capability SHA is an AOS observation,
**not proof Scientist consumed that exact freshness-bearing capability**; the
six-field infer frame does not transmit it.

Current source freshness uses boot-scoped seconds. These values and the
existing0018 monotonic deadline are preserved, not silently converted to the
proposed integer-microsecond control protocol. Full clock/schema agreement and
any future additive conversion remain separate requirements.

## Trusted host composition

`ScientistAdmissionHistory(store, capture=..., verify_current=...)` requires
explicit trusted callbacks. Defaults deny capture and current verification.
`ScientistIntentJournal(..., admission_history=history)` captures identity within
the **same BEGIN IMMEDIATE transaction** as its original request. Callback,
owner/generation, peer, identity, freshness and deadline failure prevent dispatch;
ordinary failure rolls back both new rows. The original controller is rechecked
after callbacks. Receipt and later admission checks require the original record
and current configured verifier; disabling it cannot adopt an already captured
request. No callback receives writable original model objects.

New pinned composition explicitly uses `record_version='2.0'`. Separate closed
`ScientistAdmissionBindingV2`, `ScientistAdmissionCaptureV2` and
`ScientistAdmissionRecordV2` require the fifth profile pin and record version2.0.
The corresponding complete `*_v2.schema.json` artifacts are canonical AOS schemas.
Default1.0 continues to accept legacy four-field captures, but cannot create new
interim1.0 records with a V2 profile. Stored1.0 records, including already-written
interim union records, remain byte/hash-checked historical evidence. `read`
dispatches strictly on the stored record version; unknown versions are rejected.
Current verification rejects version mismatches and interim1.0/V2-profile records
before authority callbacks. No failed V2 capture falls back to the legacy branch.

Trusted callbacks must not commit, roll back or transfer the journal's SQL
transaction ownership. A detected ownership violation is an error, not permission
to delete/reset any already committed pending intent.

The explicit `create_scientist_desktop_scheduler(..., admission_history=history)`
factory forwards the same-store history to its actual typed task journals.
Runtime confirmation remains denied by default; the parameter is trusted host
composition, not a UI/API capability grant or a replacement for human approvals.
The record cannot be recaptured, updated or deleted to renew a deadline, change
ownership, or retry uncertain work.

Historical `read` validates canonical hashes and original request/control/peer
correlation without capture/current/clock callbacks. Tasks displays only record
presence and record/binding hashes. Missing legacy identity and malformed data
are distinguishable; neither grants new work or proves release. Public fixture
[scientist_admission_history.json](../examples/scientist_admission_history.json)
is explicitly synthetic and cannot authorize a real request.

## Open joint gates

This source deliberately retains the unresolved fence. There is **no resolution
API**, inferred release from idle/quiesce, automatic replay, or legacy adoption.
Append-only authenticated terminal resolution and retired-policy cleanup rights
still require agreed control/terminal schemas and independent release evidence.
The counterpart's `profile_pin.output_contract` extension is preserved by a
separate closed `ScientistProfilePinV2` union member: exact name
`aos-scientist-profile-output.v2`, lexical integer version2 and bundle hash.
The original four-field member and historical record bytes/hashes remain
unchanged. Missing/null/extra fields or boolean/float versions cannot be promoted
into a version2 pin. Supporting this identity is not current infer authority.
The explicit desktop output guard binds a trusted expected pin to this original
record before semantic validation; see [output contract](SCIENTIST_BONSAI_OUTPUT.md).
Pinned factory construction requires a version2.0 history before any task starts.
The profile shape follows Scientist note84, but independently generated enclosing
schema hashes still require bilateral confirmation; syntax compatibility is not
a jointly admitted deployment/configuration pair.

Scientist alone runs real GPU acceptance after compatible source/configuration
pins and reservation are confirmed. No native model, active user job or live
deployment is modified by this feature. See [STATUS](STATUS.md) for actual CPU
evidence and [SCIENTIST_HANDOFF](SCIENTIST_HANDOFF.md) for the current source pair.
