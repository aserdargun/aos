# Original admission bootstrap candidate

## Infer connection staging (1 October 2026)

The infer transport obtains actual `SO_PEERCRED` from a short probe connection,
then closes it before authentication and durable intent/capture preparation.
After the unchanged authorization and persistence callbacks complete, a fresh
dispatch connection must authenticate to the exact same original peer/generation.
Socket inode, cancellation, current-peer and original deadline checks precede
the single infer frame. The empty probe grants no inference or scheduler lease.
Scientist's actual listener rejects probe EOF before decoding/admission/execution
and closes the connection, releasing handler capacity.

This avoids consuming the five-second server frame window during local SQLite
and full source verification. A failed or uncertain earlier intent remains durable;
this patch neither reconciles it nor permits a new store/process to bypass it.
Original peer, phase deadlines, receipt validation and no-blind-retry semantics
remain. CPU sockets prove staging; real joint GPU acceptance remains unproven.

## Post-response deadline phases (1 October 2026)

Scientist and AOS explicitly agreed to separate the bounded control exchange
from subsequent independent capture verification. `prepare_async` retains the
original supplied infer absolute deadline. Probe, authorization, durable audit,
dispatch and response must finish within the smaller of that deadline and the
configured control limit (at most10 seconds). Time spent in initial preparation
is deducted before creating the exchange client; no deadline is rebased.
Only after a timely response does capture verification use the original infer
deadline, not a new relative timeout. Without an outer deadline, the control
deadline continues to bound the entire preparation.

Capability60-second freshness, all current-source/generation checks, cancellation,
transactional capture revalidation and worker-terminal lock retention remain.
Expiring the outer deadline, capability, source or generation still prevents
capture/history and infer dispatch. The Scientist-side per-check bounds remain;
wire version, scheduler authority and timeout values are unchanged. CPU regression
proved this distinction (70 checks); joint native/GPU acceptance is still partial.

`ScientistBootstrapCodec` and `ScientistBootstrapCapture` implement a bounded
reader for the existing Scientist control-v1 `capability` operation, with
`target=null` and `expected_capability_sha256=null`. This creates no inference,
reservation, scheduler, cleanup grant or journal-resolution authority.

## Explicit trust and pins

The codec accepts only canonical closed bootstrap request/success response shapes,
correlated IDs, whole-capability and admission-binding hashes, matching duplicated
policy/source/profile/output fields, current descriptor pins and original60-second
freshness. Wrong versions, targets, operations, extra fields or denied admission
are rejected. The control SHA is a **descriptor pin**, not a full wire schema pin.
There is no published complete live control-v1 bootstrap schema. This source-derived
local candidate needs counterpart review; incompatible control-v2 metadata must
not substitute for it.

The host must supply real current-authority, independently expected-binding and
durable before-send intent callbacks. Defaults deny. The expected-binding verifier
must check configured caller/service generation, broker generation, policy,
per-file source-map fingerprints, manifest/config/response/output pins and schema
pins. Self-consistent ACK hashes are not independent source authority. Source-map
fingerprints are not Git HEAD or selected-source-report hashes.

Both control and inference connections must originate from the same AOS process
and caller service generation. The reader verifies current PID/UID and binds the
ACK server to the independently expected inference `BrokerPeer`. Subprocess
bootstrap helpers cannot stand in for the AOS caller.

## Lifecycle boundary

1. Construct the reader with the original `TrajectoryStore`, explicit private
   socket path, codec, expected peer and trusted callbacks.
2. Explicitly invoke `prepare(request, intent_binding, infer_peer)` **before
   opening the inference socket**, outside the original SQLite transaction.
   A real durable writer must finish before the control frame is sent. It cannot
   open or commit the original intent transaction.
3. Configure original admission history2.0 with the reader as its `capture`.
   During `ScientistIntentJournal.persist_intent`, the reader consumes the exact
   prepared ACK once, checks request/binding/actual inference peer and fresh
   authority again, and returns `ScientistAdmissionCaptureV2` with unmodified
   capability timestamps. No network or commit occurs in this callback.
4. Capture expiry, drift, uncertainty or callback failure denies. There is no
   automatic retry, capability renewal or prepared-ACK replay.

Asynchronous preconnect orchestration is now implemented in source. The scheduler
accepts explicit `bootstrap_capture` and `expected_bootstrap_peer` arguments only
when `history.capture is bootstrap_capture`, all stores are identical, history is
version2.0 and output contract is pinned. The expected-peer getter is a trusted,
quick host/config identity read; it must not perform blocking network work.

`serve_desktop.main(..., scientist_bootstrap_expected_peer=...)` explicitly selects
the factory-created strictly typed capture; absent this option no automatic
bootstrap is detected or activated. The binding freezes request/journal identity,
awaits `prepare_async(..., cancel_event=..., deadline=...)`, then rechecks current
authority, exact history/store/capture and the expected peer before inference.
Final SQL consumption still checks the actual connected inference peer.

Network runs off-loop; current-authority, expected-binding, durable-write and SQL
state callbacks are marshalled to the host thread. One active operation and one
absolute deadline cover staging plus inference; no stage timeout renews the
original intent deadline. Cancellation retains owned worker/lock tracking until
terminal and prevents late ACK publication, host writes or inference dispatch.

Do not call the synchronous network preparation from an asyncio host callback: it would block
Pause/Stop and authority updates. Do not insert it into inference `persist_intent`
after socket connection: Scientist's inference frame deadline is5 seconds while
control exchange can take10 seconds. The experimental hook was removed after
review; the current async staging path runs before connection instead.

## Trusted admission factory

The concrete [configured source verifier](SCIENTIST_SOURCE_AUTHORITY.md) can supply
the read-only source/config half of `verify_source`, with independently reviewed
pins and a mandatory current runtime/dependency/principal callback. It cannot
derive runtime rights from observed files or its own ACK.

`ScientistBootstrapAdmissionFactory` in `src/aos/scientist_bootstrap_factory.py`
now supplies the actual admission-factory, `confirm_runtime` and `expected_peer`
hooks accepted by `serve_desktop.main`. Construct it with independently reviewed
complete `ScientistAdmissionBindingV2` objects keyed by profile, an explicit control
socket path and the control descriptor pin. Its mandatory `verify_source` callback
must return `None` or raise after independently checking the current source,
dependency/config closure, policy, actual caller/service, canonical broker and
profile/output pins. Default configuration denies; no path, policy, caller or FD
is discovered or authorized automatically.

Pass the instance as `scientist_admission_factory`, its `confirm_runtime` as
`scientist_confirm_runtime`, and its `expected_peer` as
`scientist_bootstrap_expected_peer`, together with the independently pinned output
contract. Alternatively, the single trusted host option
`serve_desktop.main(scientist_bootstrap_factory=factory)` selects exactly those
hooks and derives the shared output contract from the independently reviewed
bindings. Mixing this option with any of those four individual hooks/pins is
rejected before runtime starts. Profiles with different output contracts also
deny; the returned pin is a copied frozen value. This Python option creates no
automatic CLI trust, default socket, policy enablement or retained-provider FD.
It binds only one actual controller/store and builds same-store history2.0
with the typed capture. Expected profiles are copied into immutable canonical
bindings; the complete returned binding must equal independently reviewed pins.
Current desktop lease/generation, original journal and broker generation are
checked again, including inside original admission capture.

Before sending a bootstrap frame, the factory commits an audit-only
`desktop_events` INSERT with exact control ID/frame/hash, original request ID/hash,
typed desktop authority, broker generation and original deadline. It rejects an
existing transaction, duplicate ID, non-durable SQLite configuration, authority
drift and failed exact post-commit readback. A committed row on subsequent failure
is only an **audit intent**, not evidence of dispatch or success. This existing
table is not SQL-enforced append-only: it cannot authorize replay, capability
recovery, inference resolution or GPU release. No migration or second scheduler
is added. Actual inference still requires its original journal/history transaction.

The `bootstrap_factory_candidate_v1` source profile selects59 members. Its
selected-source hash is not a full transitive runtime/config attestation. Real
source authority, service configuration and inherited retained-provider FD must
still be reviewed and supplied by the existing Scientist broker arrangement.

## Evidence and remaining work

Factory root187 CPU/synthetic tests passed in31.642s:
`data/scientist-bootstrap-factory-root-20261001.log`. Includes14 new factory tests
with actual owned Unix sockets, independent committed-event SQLite readback,
actual DesktopController and same-store original admission capture, plus broader
async/startup/transport/rearm/provider regression. These prove source composition,
not production configuration or real GPU integration.

Current root138 CPU/synthetic tests passed in26.746s:
`data/scientist-bootstrap-async-root-20261001.log`. Ten new actual socket/SQLite
tests cover off-loop heartbeat, host-thread callbacks, cancellation, exact original
deadline and prefetch/infer/capture. Seven typed desktop guard tests plus actual
startup forwarding reject identity/version/rebinding drift. No actual model ran.

Previous root108 CPU/synthetic tests passed in6.895s, including18 reader tests:
`data/scientist-bootstrap-root-20261001.log`. Actual owned Unix sockets, independent
synthetic durable control storage, original journal/history2.0 insertion and expiry
rollback were exercised. Scientist code was only read, never imported/executed;
no real capability, model, GPU or deployment acceptance is claimed.

`bootstrap_capture_candidate_v1` selects57 source members, adding this reader to
the56-file physical profile. Older membership stays unchanged. This is still not
full transitive dependency/config attestation. Integration remains **partial**:
mutual bootstrap candidate/config confirmation, actual trusted host activation,
authenticated independent retained-source provider and original-device provenance,
then Scientist-only coordinated GPU/cancellation acceptance remain required.
