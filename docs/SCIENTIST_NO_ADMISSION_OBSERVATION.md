# Never-received inference: observational closure proposal

Status: **wire shape agreed; CPU consumer prototype implemented; real closure
and runtime providers unverified**. This is not a GPU-release receipt, runtime
admission, lease grant, new scheduler or deployed endpoint. Feature identity:
`no-admission-observation.v1`, full recursive schema SHA
`92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63`.
The independently reviewed Scientist schema is mirrored at
`schemas/scientist_no_admission_observation.schema.json`; embedded legacy AOS
admission2.0 definitions match the actual local model. CPU agreement does not
configure any real observer, canonical, physical or recovery authority.

## Implemented AOS candidate

### Agreed retained close-only context and implemented AOS consumer

The separate closed context schema is mirrored at
`schemas/scientist_no_admission_recovery.schema.json`, canonical recursive SHA
`6d173eb1231989b3b3304ae08455f9b10c192dc0aa37afa54acccb223615690b`
and raw SHA608ad06c177abcabb4c9591b2a7757fe5af55c58c34106dd914429bf63e36619.
Both sessions agreed its exact nested shape; the earlier proposal below is
superseded where it differs. The original wire92791 remains unchanged.
Purpose is `close_retained_no_admission`; the context has no embedded self-hash.
Independent authority pins the canonical digest of the entire context, separately
from its raw bytes. `retained` binds the original raw/observation/schema hashes,
canonical store/schema, cleanup scope, historical observer and producer pins;
`principal` binds a distinct typed current generation/source/config. The original
scope is unchanged. The fresh issued/expires interval is at most60s, follows the
historical observation and never renews its original TTL or inference deadline.
All inference/GPU-release/observation-reissue permissions are false.

`ScientistNoAdmissionRetainedVerifier.verify(raw, recovery_raw)` uses the original
strict decode/original-history/link checks without calling or overriding the v1
current-clock/liveness verifier. It requires independently configured historical
pins, the frozen recovery schema and four default-deny trusted callbacks:
`verify_current`, `verify_canonical`, `verify_physical`, `verify_retained_source`.
Each receives `(observation, context)`. They must authenticate actual current
principal/source/authority, the committed historical row and all ten request
surfaces, complete repeated absence including the historical observer, and all
retained original source bytes. A hash or synthetic callback is not authority.
Current scope must remain the same stopped/PAUSED store/session/runtime/lease/
generation. Reused original caller/broker/observer process identities are rejected,
even if a changed unit/invocation creates a different generation hash.

`ScientistNoAdmissionRetainedJournal.append(raw, recovery_raw)` owns BEGIN IMMEDIATE,
checks all providers before/after insertion, preserves the original proof/intent,
and stores the full fresh context under `recovery_scope_json.retained_recovery`.
Existing migration0028 supports this shape without modification; exact retry is
idempotent, conflicting contexts deny, interrupts/expiry/revocation roll back.
Reply-loss reconciliation must independently read the exact immutable closure,
not blindly invoke append with a new context. No actual DB migration/closure or
recovery provider execution occurred. CPU176 passed; trusted runtime composition
and coordinated acceptance remain open. Source profile
`no_admission_retained_recovery_candidate_v1` contains the original66 members plus
this new schema (67 total); historical66 source pins do not authorize new code.

### Historical minted-observation recovery proposal — 1 October 2026

**Design only; no agreed recovery capability, deployed endpoint or executable
authority.** The actual V3 observation was minted, but AOS consumption failed
before migration. The original v1 expiry and observer generation remain unchanged;
the pending AOS intent and Scientist immutable observation remain retained. New
filenames, fresh v1 minting, changed expiry or treating a dead observer as live
are not recovery mechanisms.

Proposed separate capability `no-admission-observation-recovery.v1` would:

- Reference the original canonical observation hash, raw proof hash, request,
  admission binding and tombstone; independently read the committed historical
  row, without updating, replacing or minting that original observation again.
- Bind a fresh explicitly authorized cleanup-only principal, request ID and
  recovery attempt to the original AOS store/session/runtime and current
  paused/stopped owner, lease and generation. Old observer authority is historical,
  never substituted for current authority; inference remains unauthorized.
- Carry a fresh bounded, versioned witness with current observer UID/boot/PID/
  start ticks/invocation/cgroup, reviewed source/config/capability pins, repeated
  physical absence checks and all ten request surfaces absent. Canonical fencing
  remains controlled only by Scientist; stale/revoked authorities deny.
- Require AOS to validate both immutable historical provenance and the independent
  current recovery witness before and after an idempotent exact-target journal
  transaction. Conflicting attempts, late admission, wrong owner/generation,
  ambiguous cleanup and expiry retain the original unresolved state.
- Preserve original observation TTL and original inference deadline as historical
  fields. A new cleanup-witness TTL authorizes only the recovery observation, not
  replay, late inference, reservation, release or a new generation's work.
- Use a jointly pinned schema and appended migration only after agreement. Existing
  v1 verifier/Journal freshness and observer-liveness checks are not relaxed.
  Runtime use remains gated on CPU fault cases and a Scientist-owned controlled
  cleanup acceptance; this proposal itself authorizes no execution.

Scientist must confirm a canonical-side protocol that can authenticate the new
current witness without reminting the historical record or bypassing admission
fences. AOS implements no alternative scheduler or recovery authority while that
contract is unresolved. The full native/Lab/GPU/task acceptance remains open.

`ScientistNoAdmissionVerifier.verify(raw)` performs strict pinned schema/canonical
hash checks, independently reads the original intent/capture and actual SQLite
file identity, and checks the current stopped/PAUSED session, lease and advanced
generation. `verify_observer(proof)`, `verify_canonical(proof)`,
`verify_physical(proof)` and `verify_recovery(proof)` are separate trusted host
callbacks returning None or raising. All default to deny. They must authenticate
current source/capability, exact committed canonical tombstone and physical truth
independently of the supplied proof. Their AOS connection is read-only during
verification, preventing callback writes/commits from escaping the transaction.

`ScientistNoAdmissionJournal.append(raw)` owns BEGIN IMMEDIATE, validates before
and after insertion and independently checks durable readback. Appended migration
0028 adds an immutable exact-target closure table, rejects SQL UPDATE/DELETE and
INSERT OR REPLACE, and protects the original intent. Admission fencing recognizes
only the matching original intent/capture closure. Historical closure does not
reauthorize the old generation. Migration28 dataset audit support is implemented.
No actual private runtime DB was migrated or original pending request closed.

`ScientistNoAdmissionPhysicalVerifier(proof)` implements a separate read-only
Linux absence observer. It reuses bounded read/systemd command primitives, not
the allocation-release protocol. It requires independent `verify_provenance`
and `verify_current` gates before and after checks; both default to deny.
Provenance must authenticate the complete original child list and irreversible
late-dispatch/admission fences independently of proof flags. Checks bind the
original caller/broker and every child to the current UID/boot, reject any
existing/reused PID, and require exact inactive or collected systemd unit state.
Original cgroups must be absent, not merely empty, beneath a verified cgroup2
mount. Component traversal rejects symlinks/ambiguous paths and is bounded by
the same ten-second observation deadline. Unit/process/cgroup absence is read
twice with current authority checked again. No process is stopped, no NVIDIA
query/model inference is run and no GPU-release claim is made. Real authority
composition remains unconfigured; synthetic filesystem checks are not real
cleanup proof.

`ScientistNoAdmissionRecoveryVerifier(proof)` reads a separately reviewed absolute
cleanup-authority path, with independently configured canonical and raw SHA pins
and a mandatory current-source/authorization callback. Both hashes are equal for
the required exact canonical UTF-8 artifact; neither is taken from the proof.
It rejects aliases/symlinks, hardlinks, nonregular or nonprivate files, oversized
bytes, noncanonical JSON and identity/content changes during readback. The
closed artifact has exactly `schema`, `version`, `target`,
`cleanup_scope_without_context`, `observer_unit`, `purpose`, `inference_allowed`.
Schema is `aos-scientist-no-admission-cleanup-authority.v1`, integer version1,
purpose `observe_no_admission` and `inference_allowed` false. The nested scope
equals the complete wire cleanup scope excluding **only**
`authorization_context_sha256`; its nested `purpose` is retained. The context
hash equals the independently configured artifact canonical SHA. Exact target,
current paused/stopped scope and reviewed observer unit must match; current
authorization is checked before and after reading and the file is read again.
The helper does not configure a real authority or authorize inference.

Scientist's first `460f2880...` review candidate omitted nested scope purpose and
was independently rejected by AOS. Corrected V2 candidate canonical/raw SHA
`0c57c7aa387fc4f7f8f4d50b8714607ac9d750e382bd2b3b07b9c7f15316cdb4`
matches the actual original AOS target/history/store and current stopped/PAUSED
lease/generation in a read-only transaction. This is a shape/scope review only;
runtime authorization and observer/canonical/physical verification remain open.
The observer must remain the same live authenticated unit/process throughout
consumer pre/post validation. Scientist proposes a bounded wait for exact AOS
closure readback or the original observation's at-most60s expiry. A crash or
expiry does not permit reissue, new-generation substitution or automatic retry;
historical crash/restart recovery remains unverified.

All four consumer helper implementations are now present. The separate
`ScientistNoAdmissionCanonicalVerifier` opens only the explicitly reviewed
Scientist DB in read-only mode. It requires an independently configured file
identity and schema SHA, plus mandatory current authority/source-fence gate.
Schema SHA is the canonical digest of sorted `{name,type,sql}` rows for all
tables/triggers except sqlite_sequence; changing any reviewed schema rejects.
It checks the existing ten exact-request surfaces, including `gpu_runtime_bindings`
without owner, fencing-token or launch-state filtering, committed observation bytes
and hashes, immutable target/admission binding and complete dispatch provenance.
It ends the first read transaction before current-authority revalidation and
opens a fresh second snapshot, so a late contradiction is not hidden by SQLite
snapshot isolation. It creates no DB, table, scheduler, allocation or fence.

`ScientistNoAdmissionObserverVerifier` requires an independently reviewed
private config path/raw SHA and explicit current host authorization gate. It
checks config target/cleanup scope and source inventory against the proof, reads
every pinned actual source file twice under shared byte/time bounds, and verifies
the actual fixed observer unit is active with its exact MainPID, InvocationID,
cgroup and live process UID/start ticks/boot. Process/unit observations repeat
around authorization; configuration replacement, source changes, death, PID
reuse and restarted invocation reject. No Scientist module is imported/executed
by AOS and no hash or file permission alone authorizes this helper.

CPU139 checks passed including original journals/release/desktop/factory and
thirteen new canonical/observer tests. Synthetic syscall/source/store checks
exercise these helpers and pre/post Journal composition, not a live observer.
All mandatory real provider gates still require reviewed authorized composition.

Actual Scientist source review caught an important storage-format distinction:
the canonical row's `proof_sha256` hashes **all five** evidence preimages
(`observer`, `cleanup_scope`, `original`, `physical`, `dispatch_provenance`). It
is not the nested physical section's SHA. The AOS reader and synthetic stored
fixture now match that existing producer contract. A RED regression failed with
the incorrect reader;140 CPU checks passed after correction, and an explicit
test rejects substitution of the nested physical hash. This is a compatibility
fix, not proof that any real observation was issued or accepted.

The source profile `no_admission_observation_candidate_v1` is old63 plus the new
module, schema and migration (66 members). Old63 cannot authorize this consumer.
New observer freshness uses integer CLOCK_BOOTTIME microseconds and at most60s;
an expired original inference deadline remains historical and never renewed.
It does not prevent cleanup after timeout. Current lease is explicitly bound;
current generation must exceed the immutable original generation, not be hardcoded
to a single demo pair. Source/runtime provider acceptance and real proof are still
required before reopening.

## Observed gap

AOS can durably capture admission capability and inference intent before sending
an inference frame. A broken connection leaves an uncertain, immutable pending
intent even when Scientist has no matching admission row. The existing retained
provider requires a canonical row and cannot attest that missing-row case.

The original isolated desktop has subsequently stopped, paused ownership and
advanced generation. Existing retained controls require a current running desktop
binding; its old AGENT generation cannot be reused. Original intent and capture
remain immutable. Creating a new DB/session or deleting the pending record is not
a solution. Source fixes preventing the frame timeout do not resolve old effects.

Existing terminal/budget verifier requirements must remain intact. A never-received
request has no canonical admitted time or allocated budget. Do not manufacture
those fields merely to fit an admitted terminal receipt. This proposal needs a
distinct observational verifier and append-only historical closure, not a relaxed
normal terminal verifier.

## Required producer evidence

Scientist is the sole observational producer and existing allocation authority.
The agreed feature must bind all of the following, with bounded closed fields:

- Exact original request ID, canonical request SHA, profile and deployment.
- Independently retained original AOS capability/admission record and its hashes;
  original source/config/profile/output and caller/broker generation pins.
- Exact original caller and broker boot IDs, PIDs, start ticks, invocation IDs and
  cgroups; no substitution of current generation for the original target.
- A consistent canonical observation showing no matching admission, GPU queue,
  child binding or deferred execution for that exact principal/request/hash.
- Independently checked physical absence of the original caller, broker, children
  and cgroups, including a late-dispatch fence. Ambiguous/reused PIDs or unknown
  child provenance must reject; global idle alone is insufficient.
- Current authenticated observer identity, explicit cleanup-only purpose,
  source/capability version pins and bounded observation freshness.

No admission row, fencing token, GPU release event, reservation, budget or model
result is created by this observation. A contradictory row, pending child,
quarantined allocation, failed cleanup or unavailable provenance rejects it.
Scientist must specify the existing trusted surface used; no endpoint/path is
assumed by this document.

## AOS consumer requirements

Keep observation validation separate from permission to append a closure.
Both default to deny without explicitly reviewed trusted providers. Hash equality,
the model's output, matching filenames or an ACK do not prove physical truth.

The consumer independently reads the original request/capture and the current
metadata of the same original store/session/runtime. Current stopped/PAUSED
generation is a cleanup-only fence, not an infer principal. Old generation calls
cannot change it. The trusted recovery authority must be explicitly agreed; the
existing running-task `ScientistIntentBinding` is not widened implicitly.

Only exact-target append-only closure is permissible after fresh observer/source
checks and independent no-admission/physical verification. Original bytes remain
unchanged. Retry with identical evidence is idempotent; different evidence or
authority requires rejection/review. Post-insert authority revocation rolls back
the closure. Historical inspection never refreshes observer authority.

Any new migration must be appended, not rewrite existing real-data migrations.
Global unresolved-intent admission fencing stays in place until the canonical
trusted closure path is implemented, independently verified and accepted by both
projects. Recording proposed evidence does not itself reopen work.

## Acceptance before reopening

CPU/mock coverage must include wrong principal/request/hash/source/version,
old/reused generation, duplicate/different proof, late admission or child
appearance, cancellation, timeout, crash and failed cleanup. Verify immutable
original intent/capture, exact-target scope, append-only/idempotent behavior and
rollback on post-validation revocation. Missing original budget must not be filled
from response data. No accepted proof may allocate GPU or dispatch inference.

After agreement, Scientist alone coordinates real observational closure and the
next bounded native acceptance. AOS independently validates shared evidence. Mock
proof success is not real no-admission closure or joint GPU acceptance.
