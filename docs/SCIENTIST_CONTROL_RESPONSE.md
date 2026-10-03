# AOS response to Scientist control-contract proposal 75

**Latest source recheck: 30 September 2026, 19:30:03 UTC — source/design review only.** AOS accepts
the proposed direction of separate `aos-scientist-control.v1` and
`aos-scientist-terminal.v1` contracts, subject to the amendments below. This is
not acceptance of finished canonical schemas, a deployed transport, a capability
grant, or shared GPU admission. The current providers remain denied.

The initial review at **18:52:47 UTC** read the complete 254-line Scientist draft at
`../ai-scientist/docs/ai-scientist/75-aos-control-contract-proposal.md`, SHA-256
`887f54f3faed12f99a84b2754dee02dc56d986090fa377e3654ac45919de651c`.
That historical draft was untracked at Scientist HEAD
`cc9511d4b449620b4bc1cc1a57ded41374eeb591`. Its four broker/service/executor/
scheduler source hashes matched the actual files. AOS HEAD was
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` with ongoing local changes.
The latest observation reads the complete **259-line updated proposal 75**, SHA-256
`3c41fc097bc152a1dc9ff90d86e25b87c3c4114f61e86445c57396b1d79183ef`,
and its referenced public source-delivery note
`../ai-scientist/docs/ai-scientist/77-aos-control-source-delivery.md`, SHA-256
`4495a37716130450bf45f2f17c91c33eab0d2ef8b6cb6541d520b37ae94ac9b1`.
Reading began at 19:24:56 UTC. Note 77 changed during the review and was read
in full again after the final recheck; proposal 75 and all six implementation
file hashes below remained unchanged.
At that observation Scientist HEAD remained `cc9511d4b449620b4bc1cc1a57ded41374eeb591`;
the new control modules and notes are untracked, and broker/service/executor/
scheduler have local changes. The old four-source hash match above belongs only
to the initial observation, not this implementation revision.

These mutable observations are not installed-source or deployment attestations.
Scientist files were read only; no private database, process, GPU or live
transport was accessed for this review.

**19:35:08 UTC root read-only follow-up:** Scientist committed the implementation
at `38d513427df88f98f0a73f0598a5b48dfe636a90`; tracked and untracked status is
clean. All six implementation hashes in this response were rechecked and remain
identical. Commit publication does not resolve the source findings below or
prove deployment, joint admission or GPU acceptance. Earlier dirty descriptions
are historical observations, not current checkout status.

## Updated 75 / source delivery 77: amendment disposition

Proposal 75 now points to an implementation; it does not itself settle the
earlier nested-schema and authority questions. Source delivery 77 reports
**95 CPU tests passed, pytest 0.58 seconds, parent exit 0 / 0.793515424 seconds**.
That is an attributed Scientist synthetic peer/process/GPU-double result, not
an AOS rerun, a native drain result or joint deployment acceptance. This review
does not open private receipts or repeat those tests.

The updated public note also attributes a seven-command quality gate to
**exit 0 / 84.518426297 seconds**, with **1479 passed, 7 opt-in skipped,
120 GPU/live deselected**, strict mypy over 148 source files, and successful
wheel build/import. Its isolated R4 patch hash is
`dcb0971624108ff81997e36d189f38dd779779ed1144ecec564a511ef0294a3e`.
These remain Scientist-reported results/artifact identity, not AOS validation.
The linked `review-evidence/aos-control-source-delivery.json` was not opened;
this review uses the public note and independently read source files, not
private evidence or a presumed artifact receipt.

| Earlier amendment | Newly observed source progress | Still required before agreement |
| --- | --- | --- |
| 1. Closed schemas and hashes | `aos_gpu_control.decode_request` enforces exact request/target keys, canonical bytes, duplicate-key denial, integer version and fixed operations/profiles. Errors explicitly carry null capability/data; stored results are capped at 96 KiB before the 128 KiB outer limit. | `CONTROL_CONTRACT` is a field-list/limits descriptor, not complete recursive capability/result/budget/terminal schemas. Terminal still has no integer version. Agree the alternate result shape and nullability/outcome fixtures. |
| 2. Immutable authority versus freshness | Capabilities have 60-second expiry, full caller generation, source/profile/policy hashes and a bounded 256-entry cache. Infer checks a matching current capability and enabled policy. | Turns/terminals do not persist original stable capability security identity. Policy/source rotation blocks external cleanup lookup. Server-generation hash omits broker unit/invocation. |
| 3. Original clocks and budgets | `register_intent` returns the stored original deadline on an exact retry; queue/ready-phase deadlines are stored. Cancel-before-intent distinguishes null admission/envelope/phase deadlines from configured profile maxima. | Float boottime remains the implementation. Jointly pin units, canonical numbers and boot semantics; the earlier integer preference is not implemented. AOS clock/budget journal additions remain open. |
| 4. Tombstones and races | Request ID is a primary key; conflicting original principal/hash/profile/deployment is rejected. Control IDs persist. Submit/acquire and plan/start/go hooks use the same arbiter transaction structure. | Physical subprocess launch is outside the transaction. A committed start followed by delayed launch must remain quarantined without exact child proof. The duplicate-result publication race below is still blocking. |
| 5. Terminal/release proof | Trusted drain recording and terminal creation now join the scheduler release transaction; no-admission records and recovery exist. Missing exact child/drain evidence retains quarantine. | Cached infer paths can bypass the terminal outcome; private work-directory cleanup is incomplete in identified terminal paths. Source presence is not real GPU proof. |
| 6. Restart/history | Cross-generation access is separately policy-gated and redacted to target/state/original-generation hash/receipt hash/release outcome, without adopting the principal or returning model output. | Keep history disabled initially. That projection still cannot satisfy the AOS terminal validator or reopen its immutable journal. |

### Blocking source finding: canceled result can escape a duplicate path

This is a **source-derived race, not an executed reproduction**. At the hashes
below, `aos_gpu_control_store.py:949` chooses `canceled` when cancel intent has
committed, while `gpu_scheduler.py:862` subsequently marks the scheduler ticket
`done` whenever `expired=False`. Those two states describe different facts.
However, `aos_gpu_executor.py:1406` (`_intent`) and `:1565`
(`_completed_result`) still promote `result_ready` to `completed` using the
scheduler `done` flag alone; neither checks the control terminal receipt.

A concrete source interleaving is:

1. Duplicate handler B passes `register_intent` while uncanceled, then pauses
   before `_intent` (`aos_gpu_executor.py:1632`–`:1647`).
2. Handler A stores `result_ready`; cancel commits; trusted release commits a
   **canceled** control terminal and a scheduler **done** ticket.
3. B resumes `_intent`, promotes the cached result and returns it through
   `aos_gpu_executor.py:1648`–`:1651`, bypassing A's terminal check at `:1704`.

A replay that first registers **after** cancellation is rejected; that guard
does not close this already-registered duplicate window. The polling cache
return at `:1724` is another unguarded publication path. Require an exact
`completed` terminal/receipt/result-hash check, serialized with every promotion
and return path. A scheduler release flag cannot substitute for the immutable
outcome. Add a deterministic CPU interleaving regression before admission.

### Other concrete contract changes still required

1. **Persist stable admission identity.** `LabAOSControl.admit_infer` accepts
   any unexpired cached capability matching caller/profile/deployment/policy.
   `ControlStore.register_intent` and `_terminal` omit the original policy,
   source fingerprints, infer/control schema and admission-server identity.
   Persist/expose those stable pins and compare them with AOS's pre-send
   journal. Keep six-field infer unchanged; do not claim its bytes consumed an
   exact freshness-bearing capability hash that was never transmitted.
2. **Keep authenticated cleanup possible after rotation.** Every `handle`
   operation calls `policy.bound_profile`/`verify` before old-target lookup;
   changed policy/source hashes therefore block external cancel/reconcile.
   An unchanged disabled policy can permit control, but that is not a retired-
   policy/deployment cleanup path. Internal recovery alone does not provide
   AOS authenticated resolution. Define original-pin cleanup-only rights.
3. **Bind the broker generation completely.** Service composition hashes only
   `ProcessIdentity(pid, start_ticks, boot_id)`. Add or explicitly agree the
   broker unit/invocation/UID/cgroup binding and its authenticated comparison;
   retain original admission-server identity when a new server reconciles.
4. **Specify the alternate result and nested schema.** Control result is
   `{response, usage, generation}`, not the full infer receipt proposed below;
   its generation contains unit/invocation/main-PID/cgroup. Agree a strict
   adapter tied to original target/profile/deployment and terminal child
   identity, canonical hashes and response-schema validation before Operator
   consumption. Field-list descriptor hashes alone are insufficient.
5. **Close retention and capacity behavior.** Requests/control IDs are capped
   at 10,000/100,000 without deletion. `remember_control` covers capability,
   status, cancel and reconcile; exhaustion can block new control requests
   before lookup, as note 77 acknowledges. Specify refresh/cleanup polling and
   close new admission before external resolution becomes unavailable.
   Separately, a canceled result-ready turn skips `after_release` because the
   exception cleanup is conditioned on `not result_ready`
   (`aos_gpu_executor.py:1743`); scheduler recovery also does not invoke that
   private work-directory cleanup. Add bounded, restart-safe cleanup for these
   committed terminal paths. This is a retention/liveness/privacy gap, not a
   claim that an unproven lease was released. Row-count limits are not a
   measured byte/disk-space budget.

The old AOS source hashes listed below were rechecked and are unchanged. Its
typed control client, caller/capability journal bindings and additive resolution
transition remain missing. Scientist's source advance does not implement those
AOS pieces or grant permission to deploy either side.

Observed Scientist implementation hashes for this review:

```text
aos_gpu_control.py       a53e48e04b07cdccbc8c5a8b00e9163ecd1c697e64f81d296e052685c8d45def
aos_gpu_control_store.py 92fdb5a93be35d54597c48de62d4cabbc76334135bf6c6220bcbdeaaa3d2d15c
aos_gpu_broker.py        23bd7e0690a97b75c2d941a72d4757810a0de26b7d729420d7805d1e56bdc771
aos_gpu_executor.py      98b7304ac046f1fc4c6de8527faf24d306791f2778369c3ba5a32bed041cc121
aos_gpu_service.py       6c40ac109d7f1ab912cd35bffe20f57e6484b08a70857106da94138815d728f6
gpu_scheduler.py         31b440de3d1ded0733c0a6e6aa23e15e398e978c0ae94e40c849f76f3af94b27
```

The following six amendments retain the full acceptance criteria; this new
disposition supersedes initial-review statements that corresponding Scientist
implementation hooks do not yet exist. Partial implementation does not
silently settle an open contract choice.

## Decisions that can be shared now

1. Keep infer **exactly six fields** and integer wire `1` under
   `aos-scientist-runtime.v1`. Keep the separate Lab HTTP research API separate.
   A separately pinned private control socket, served by the same trusted
   Scientist service and the same arbiter database, is acceptable. No socket
   pathname, new endpoint, caller unit or deployment identity is assigned here.
2. Persist the canonical infer bytes/hash and authenticated original caller
   generation before dispatch. AOS already persists the bytes/hash, but must add
   the caller/capability bindings described below. `unknown`, EOF, timeout,
   local cancellation and lost ACK never authorize infer replay.
3. Require exact source/deployment/profile/schema and principal pins, default
   deny, and a bounded capability lifetime. The proposed 60-second lifetime is
   acceptable as an admission-cache limit once its refresh/in-flight rules are
   fixed. Refresh cannot renew an existing turn's budget.
4. Accept durable cancel-before-intent tombstones, queued/acquire and
   cancel/launch serialization, trusted physical drain and an immutable terminal
   record committed with release. Cancel ACK, result-ready, worker exit, idle,
   a receipt hash alone or an ordinary inference response never proves release.
   Keep optional cross-generation history access **disabled** in the initial
   contract; do not represent that choice as solved restart recovery.
5. Accept the proposed finite control capacity and provisional limits: two
   control workers, eight queued connections, 8 KiB requests, 128 KiB responses,
   five-second frame and ten-second total call limits, and at most one second
   of DB lock wait. They still need exact budget accounting and CPU contention
   tests. Disk/retention exhaustion closes admission; it cannot erase a
   tombstone, reopen an ID, or manufacture terminal success.

The only fixed model profiles remain `aos.decider.turn.v1`,
`aos.bonsai.recovery.v1` and `aos.bonsai.vision.v1`. Their existing input/output
and deployment pins remain binding. Free-goal `aos_web_goal_plan` is unsupported
by these profiles; no native fallback or silent profile expansion is accepted.

## Required amendments before schemas and implementation are frozen

### 1. Complete the canonical nested types and cross-field rules

The proposed top-level request/response field sets are a useful basis. The
capability, original budget, server generation, child generation, successful
result, error nullability and optional history projection are still described
in prose rather than exact closed schemas. Supply canonical schemas and paired
positive/negative byte fixtures for every nested object. State field names,
types, maximum lengths/cardinalities, required/null combinations and all enum
values; reject unknown keys recursively and boolean-as-integer values.

Define precisely which canonical bytes produce each generation, capability,
source, allocation, evidence and result hash, including the excluded
`receipt_sha256` field. The terminal field list currently has `schema` but no
integer `version`; either explicitly specify that intentional rule or add and
jointly pin the version before implementations diverge. Do not infer it from
the control envelope. Use the already agreed infer canonicalization unchanged.

For `result`, AOS proposes the full existing validated infer receipt shape
(`version`, request/profile/deployment, worker generation, response, usage),
with `result_sha256` over its canonical JSON excluding the framing newline.
If Scientist chooses another shape, that needs explicit agreement and an AOS
adapter. Define how worker-generation fields match the richer terminal child
generation. A successful result that fails the existing profile/response schema
must not reach the Operator. The maximum nested result must leave room for the
terminal record and control envelope within the 128 KiB response bound; never
truncate a successful response or return partial JSON as a receipt.

Specify `capability_sha256` nullability on an error before a capability can be
verified, and a complete `ok`/`data`/`error` invariant. A retryable control error
permits only another bounded control request, never another infer dispatch.

### 2. Separate capability freshness from immutable turn authority

Hashing `issued_boottime` and `expires_boottime` makes every refresh a different
capability. Infer has no capability field, so the text currently leaves unclear
how the capability recorded by AOS is related to the policy actually used at
server admission when a refresh races dispatch. Define the stable security
identity separately: server generation, authenticated caller generation,
policy/source/deployment/profile/schema pins. Refresh may change freshness but
must not silently change those pins for an in-flight turn.

The server must bind its admitted turn to that exact verified security identity,
and authenticated terminal readback must establish the same binding to AOS's
original journal. Define the correlation without adding fields to infer. A hash
lookup or an unbound latest-capability response is insufficient. AOS will not
claim an exact capability hash was consumed by infer if the wire cannot prove
that fact.

Expiry during a 720-second turn must stop **new** admission without treating a
valid result from the originally admitted turn as a new authorization. Current
control reads may require a refreshed capability, but the turn's original pins,
deadlines and principal remain unchanged. Likewise, policy/deployment rotation
must leave a narrowly authenticated path to cancel/reconcile the original work:
an `admission=denied` capability cannot accidentally disable all cleanup. Define
which original pins are checked for those operations, and whether a fresh
capability can describe cleanup-only rights for a retired deployment.

Bind the server-generation hash to an exact authenticated broker generation,
including its unit and process identity. After broker restart, authenticate the
new server before reading its durable record; preserve the original admission
generation separately. Never equate a newly authenticated server with the old
one by replacing the original journal identity.

### 3. Make clocks and original budgets persistable without invention

Pin the clock domain and units for every timestamp, expiry and deadline.
Scientist uses boot-scoped boottime for scheduling; current AOS transport uses
`time.monotonic()`. These are not interchangeable across suspend or reboot.
AOS preference for new control metadata is bounded integer boottime units with
an explicit boot ID, avoiding unspecified floating-point timestamp hashes.
Agree the exact representation before encoding it. On boot mismatch or missing
clock identity, do not extrapolate a remaining budget.

Separate the original transport/envelope hard deadline and configured queue,
activation, inference, total and token budgets from phase deadlines assigned
later. Queue/admission time does not determine a future actual activation time.
Persist each first-assigned phase deadline when that phase is first entered,
bounded by the original hard caps; reconciliation never recomputes it from now.
Report configured maxima separately from measured usage.

Define the `original_budget` variant for **cancel before any infer intent**.
The control request carries only an infer hash, so no actual infer admission
time or caller transport deadline exists at the server yet. Those values must
be explicitly absent/null, with the independently pinned profile maxima
distinguished from actual turn budgets. Do not fabricate an admission record or
accept caller-supplied budget values to fill the receipt. This applies also to
deadlines for phases that never started.

### 4. Reserve the original request identity through every cancellation race

Accept the proposed shared-transaction tombstone ordering, with a precise
uniqueness rule: cancel-before-intent must reserve the original request-ID
namespace against different request hashes or principal generations, not merely
one exact tuple that another late tuple could bypass. Exact duplicates return
the same first cancel time; conflicting identities fail closed. Foreign
request existence must not be disclosed.

Document and test the actual linearization points for `_intent` → submit,
queued → acquire, acquire → child-intent, child-intent → launch/go, result-ready
→ terminal commit, and release commit → response. Current Scientist helpers
open separate transactions; saying "same transaction order" is not yet an
implementation. The server must prevent a committed tombstone or cancellation
from being bypassed in every intermediate window, including delayed systemd
start and broker restart. Checking once before launch is insufficient.

Define whether `control_id` conflict records are durable for status/capability
as well as cancel, and bound their storage without losing cancel tombstones.
When an expired capability is refreshed, changing its hash changes the
canonical request: use a new control ID or explicitly define a safe alternative;
never relax the same-ID/different-content conflict rule. A dropped response
does not erase the first operation's effect or restart its budget.

### 5. Close the terminal outcome matrix and cleanup failure cases

Provide a closed matrix for `terminal_state`, `release_outcome`, `reason_code`,
result and evidence nullability. `completed` requires a validated result and
trusted release; `never_admitted` cannot describe a request that ever acquired
an allocation, even if no model process launched. A no-child allocated turn
still needs its allocation and late-start/drain evidence before release.
`unknown` is never equivalent to `never_admitted`; only the durable exclusion
of future admission establishes the latter.

Cancelled or failed work may be released; released work is not therefore
successful. Failed or unobservable drain remains quarantined. If a successful
result loses the cancel/terminal transaction race, it must not escape through
the old infer response path. If the terminal commit wins, a later cancel returns
that immutable terminal fact. The infer and control paths must share this rule.

The immutable allocation and drain evidence must bind original principal,
request/hash, profile/config, owner/fencing identity and exact observed child,
including the no-child/late-start case. Physical proof may be recorded before
the final CAS, but only the same-transaction terminal/release commit authorizes
the terminal response. Post-commit bookkeeping failure must not reconstruct a
different terminal receipt. Capacity or disk failure after allocation must
leave trusted bounded cleanup available and the fence closed if durable proof
cannot be committed; failing admission alone does not finish an existing turn.

### 6. Do not silently solve caller restart with a redacted hash

A new invocation must not adopt, cancel, resume or replay the old generation.
Initial history ACL stays disabled. This means restart-spanning AOS uncertainty
is deliberately unresolved even after Scientist's internal cleanup, until an
agreed, separately authorized reconciliation route can prove the exact target.

The optional projection in proposal 75 contains only a terminal receipt hash
and release outcome, not the complete authenticated terminal evidence needed by
the initial AOS validator. Do not use that projection to reopen the journal.
If history reconciliation is later enabled, agree a separate exact schema,
capability/policy grant and durable target-bound resolution semantics. It may
prove old-work resource resolution without granting output access, adoption,
new-task authority or a new budget. Retain original principal and source pins.

## Actual AOS mapping and implementation gaps

| Current source | Already implemented | Required change after agreement |
| --- | --- | --- |
| `scientist_protocol.py` | Six-field canonical infer bytes and newline-excluded request hash; strict profile/receipt identity. | Separate strict control/capability/terminal types and cross-field validation; no additions to infer. |
| `scientist_transport.py` | Private explicit UDS, SO_PEERCRED → pinned broker systemd generation; repeated server checks; default-denied admission; uncertain dispatch blocks retry. | Separate bounded control transport and authentication; full original caller-generation/capability correlation; no caller release API. |
| `scientist_intents.py`, migration 0018 | Durable pre-send canonical frame/hash, desktop owner/lease/generation, **broker** peer, local monotonic deadline; separate inference receipt. | Additive migration for original caller/principal, capability/security pins, clock/boot/budget identities, control intent and independently verified terminal resolution. Broker peer is not caller principal. |
| `scientist_async.py` | Event-loop-owned SQLite callbacks; local cancellation stops waiting and preserves uncertainty. | Dispatch durable remote cancel through an independently bounded authorized control path; local thread exit is not remote cancel/drain. |
| `scientist_desktop.py` | Current typed task/control fence and profile pin checks; denied provider; all recorded inference intents block new admission. | Consume trusted resolution without deleting history; require fresh current task/lease/capability for later work. Resolution must not revive old task authority. |
| Lab service/journal | Separate typed research-run start/status/stop/report, owner scope and independent report hash. | Remain separate: Lab stop/report and local Lab drain are not GPU inference terminal proof. |

Migration 0018 permits only `pending` → `receipt_recorded`, with one immutable
intent per session and no delete. A successful old-style infer receipt leaves
that admission fence closed; it is not an implicit `resolved` state. Do not
rewrite that migration or delete rows to make the second turn pass. A new
resolution journal and explicit admission query are necessary, with exact
target matching and a narrowly authorized transition.

AOS's current `_admit` checks run before dispatch and again before accepting a
result, and its current-session callback rejects stale desktop authority. The
new integration must distinguish cleanup/readback authorization from permission
to act on a task. A terminal result may safely resolve GPU uncertainty while
its output is unusable by the old Operator after takeover. Fresh work still
requires current human/control authorization and a newly admitted source pair.

Observed AOS source hashes for this review:

```text
scientist_protocol.py  ef522f8759e7fb00b87a5b1a1ed7e922d5f6a7d1b0f1095dbcf38de68d3b5c60
scientist_transport.py 805f9da8bde7fcba9936f8e9f1b5c13f09abaa2d83fad2e45051ae854439d252
scientist_intents.py   49eb3df402ad6464170b078fc6769bdad484400598dc7353d1da31bbebe166ef
scientist_async.py     722a9bac9fe975ce3aa783873c2246334ea942b77a9e5b9cd456e7a562d49ebe
scientist_desktop.py   3f32d177375abbc10e56558cd40c7540e1c20a0854163a9a89581a4298e35b43
migration0018         bb0d4644b274ccacdc80867a7d14f92f7c0e5926f84929f24ee774a2681bd8d9
```

## Next shared acceptance boundary

Scientist should first return canonical nested schemas/hash fixtures and the
amended capability, deadline, tombstone and terminal rules. Both sides then
review one exact contract revision and source pair. Scientist closes the
identified publication/cleanup gaps and freezes the now-present same-arbiter
control/terminal implementation and bounded listener; AOS
implements the corresponding typed client and additive journal integration.
Unknown legacy rows remain denied, and optional history support stays off until
its separate proof contract is accepted.

The CPU suite should establish these specific invariants before real GPU work:
expired/rotated capability cannot widen old authority; all original budgets
survive refresh/reconnect; cancel before intent prevents late admission; each
acquire/launch/result/finish race has one terminal outcome; false/missing drain
and boot/generation mismatch cannot reopen; lost ACK never replays infer;
control stays bounded with all infer handlers occupied; full disk leaves
uncertain work fenced. Test doubles must remain labelled CPU/mock evidence.

Only Scientist executes the later coordinated, explicitly admitted real GPU
session. AOS must demonstrate fresh authorized work after trusted prior-turn
resolution, not merely a successful control response. Record real cancellation,
drain/release and fairness/resource evidence separately. This response grants no
permission to deploy, start that session or close the runtime integration goal.
