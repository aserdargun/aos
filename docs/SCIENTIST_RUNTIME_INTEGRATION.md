# Scientist runtime integration — partial, not admitted

## Explicit verified report retention — 1 October

The typed Lab service now exposes `save_report_async(run_id,
expected_report_sha256=...)` and `read_saved_report(run_id,readback_id)` through
authenticated closed-field console POSTs. Save is separate from ordinary read,
freezes the original task across fresh triple readback and rechecks authority
inside its own SQL transaction. Immutable migration0027/schema1.0 records retain
only verified report content in the private original database. Inventory is
metadata-only; explicit saved reads are offline and require fresh report rights.
History cannot resolve uncertain tasks, mint authority, replay actions or prove
GPU release. Bounds, replacement denial and nested checksums are enforced.

Source profile `lab_readback_history_candidate_v1` adds the readback module,
schema and migration to the unchanged60-member membership, producing63 selected
members. Selected-source identity is not full artifact/dependency/runtime proof.
Scientist remains the only coordinated real GPU acceptance runner; this slice
does not imply joint runtime admission or deployment.


## Verified Lab start state and research report purpose — 1 October

`lab.start` returns the state from its independent same-run GET, not the POST
ACK. Remote run ID and `reused` remain unchanged; a terminal ACK cannot regress
or conflict with observed terminal state. A queued ACK followed by a verified
failed run now persists the observed failed handle in the original action result.
This means the start control was acknowledged, not that an experiment succeeded.
Lost/invalid/foreign/revoked readback leaves the original intent uncertain and
does not permit blind retry or a fresh action/key workaround.

`lab.report` rejects baseline-purpose terminal status before fetching the report;
the reusable `verify_scientist_report` also rejects baseline purpose even if run,
terminal state and canonical report hashes match. Baseline measurement therefore
cannot be adopted as completed research. Report/status JSON remains transient
readback, not a durable report history or GPU-release proof.

Root212 CPU/synthetic tests passed in57.348s,
`data/scientist-lab-lifecycle-root-20261001.log`; final focused50 tests passed
in18.185s, `data/scientist-lab-lifecycle-focused-20261001.log`. The two baseline
regressions failed before the fix; seven new START tests use owned HTTP and actual
service/SQLite, including independent pre-POST intent visibility and uncertain
terminal-regression persistence. An existing drift fixture raced its status
mutation after report send; mutation now precedes sending the already-built
report, making the intended status drift deterministic without weakening checks.
No real Scientist code, model/GPU, live site or deployment was executed here.

## Independent Lab stop readback — 1 October

`lab.stop` now treats its POST ACK as provisional: it validates the exact bound
run/non-baseline purpose, rechecks current typed authority and separately GETs
the same owner-visible run within the original deadline. The returned status
must independently show stop requested or a terminal state; a terminal ACK
cannot regress/change terminal state. A normal stop_requested→terminal transition
is allowed. The journal records the independently observed status, not just the
POST response. Lost/invalid readback keeps the effect uncertain and its original
durable intent unresolved; no blind POST replay is performed.

This uses existing Scientist GET/POST contracts only. Scientist source scopes
status by authenticated owner/origin and commits stop_requested before background
cleanup. Neither stop_requested nor terminal status proves GPU drain/release;
the retained independent physical-proof and sole scheduler gates remain separate.
This is a source change, not deployment or real Scientist/GPU acceptance.

Current checkout clarification: both `services/decider/broker_worker.py` and
`services/bonsai/broker_worker.py`, plus `services/broker_runtime.py`, exist now.
Historical `--shared-gpu-turns`, `--lab-external-*`, `gpu_turn.py` and
`lab_external.py` are not current interfaces. `serve_desktop` uses explicit broker
socket and trusted injected runtime/Lab providers. Do not copy historical hooks,
invent default paths or create another GPU ownership authority.

## Current original-inference closure

The [append-only resolution API](SCIENTIST_RESOLUTION.md) now complements
retained evidence-v3 transport and independent budget/release verification.
Original intent/state/receipt/deadline bytes are retained; exact proof-backed
resolution permits fresh admission without reset/replay. Pending control and
other-session uncertainty still block startup. These source/CPU slices supersede
older algorithm-missing notes below, not production provider/config or real GPU
acceptance gates. The Scientist session remains the sole integrated GPU runner.

Current short transfer and read-only source fingerprint command:
[Scientist handoff](SCIENTIST_HANDOFF.md). This identifies untracked runtime work
without treating Git diff, CPU mocks or a dirty checkout as joint admission.

## Owned local HTTP shutdown (30 September 2026)

Lab client/service now close admission before shutdown, signal only their own
local async control, and await the exact owned HTTP worker and service result
callbacks before CLI lifespan closes SQLite. This is local resource cleanup,
not experiment cancellation, GPU drain/release or permission to restart work.
New dispatch checks local cancellation before network I/O; a request already
attempted may still complete remotely and remains subject to durable uncertainty.

Close waits are bounded within 1..10 seconds and never cancel/kill an unrelated
process. Failed cleanup leaves the client closed and raises a clear error; callers
can recheck the same handle, not reopen or resubmit. A held POST during close
keeps intent pending; late completion cannot revive admission or acknowledge
under the closing policy. Actual CLI lifecycle now exercises the service drain
before store teardown. Existing default/native startup behavior is unchanged.

48 focused CPU/mock HTTP/startup tests pass in 21.219 seconds:
`data/scientist-runtime-local-close-20260930.log`. The final closing-policy and
actual startup/lifespan tests additionally pass separately (3 tests, 2.677 seconds):
`data/scientist-runtime-local-close-final-20260930.log`. They prove waiting for the
owned operation, no new effects/proposals, durable pending intent, bounded failed
cleanup and recheck without replay, and same-thread callback/store shutdown.
Older broad core evidence predates these two cleanup regressions and is not
recounted. No GPU, real Scientist, user-instance restart/deploy or remote stop ran.

## Current acceptance checkpoint and restart fences (30 September 2026)

Local caller/worker/CLI/Lab startup seams are implemented and CPU/mock-verified.
Shared capability/principal/reconcile/cancel/drain/release and Scientist-owned
real GPU acceptance remain unverified. Sections below preserve earlier milestone
snapshots, newest first; their older open-work statements are historical, not the
current queue. Nothing here claims deployed runtime or admitted source pair.

The acceptance audit found a real restart gap: a fresh desktop session could queue
a new Lab start while the same store held another session's lost-ACK intent.
The before-fix regression fails with no rejection:
`data/scientist-runtime-restart-regression-before-20260930.log`.
Lab queue and durable effect dispatch now fence unresolved intents across the
whole store, not only the current session. Scientist desktop start/resume also
fence unresolved Lab effects. No row is reset/deleted, authority binding rewritten
or remote effect retried. Independent existing-bound status remains separate.

Pinned broker engine objects cannot be replaced by a native/fixture engine, and
unbrokered native knowledge/owned planning setup is rejected in Scientist mode.
Read-only inventory continues working on a rejected replacement and reports the
invalid model binding. It also exposes unresolved Lab-effect counts, so restart
blocking is visible in the EN/TR panel without exposing other sessions' contents.

The broad pre-fence source snapshot passes 1921 CPU tests, 275 skips, no failures/
errors in 174.097 seconds: `data/capability-check-kdxolsrg/report.json`.
The subsequent final targeted run passes 46 CPU/mock + real Chromium synthetic UI
tests in 17.943 seconds: `data/scientist-runtime-restart-complete-20260930.log`.
The final inventory/pinned-engine projection assertions pass separately in two
tests (0.655 seconds): `data/scientist-runtime-restart-final-20260930.log`.
These are distinct snapshots, not totals to add. Existing focused SQLite warning
evidence remains recorded. No real model/GPU/Scientist experiment was run.

Browser plugin absent: existing Python Playwright/Chromium verifies expanded
metadata and unresolved Lab count in EN/TR at desktop1280x800/mobile390x844, no
overflow/page error/framework overlay and GET-only refresh. Private screenshots:
`data/scientist-ui-qa-lphwktz1/`; external mobile copy:
`/tmp/aos-scientist-restart-20260930/disabled-mobile-tr.png`. TypeScript passes.
Live UI/app and Scientist processes/files were not changed, deployed or restarted.

Transfer to Scientist: evaluate the pinned current source, not historical patched
copies. Local startup seams are ready for contract confirmation; do not grant
runtime based on source markers or mock success. Confirm authenticated common
capabilities/principal plus trusted reconciliation and exact cleanup evidence
before removing any admission block. Scientist remains the sole GPU test runner.

## Lab host startup composition (30 September 2026)

`serve_desktop.main` accepts optional typed `ScientistLabStartup` host configuration
and `scientist_verify_lab_capability`. The immutable configuration pins explicit
loopback API authority, existing private token file, principal, allowed suites,
program version, authorization context digest and bounded control timeout. The
canonical schema is `schemas/scientist_lab_startup.schema.json`. No shared default
URL/path or new remote endpoint is invented, and token values never enter CLI
arguments, configuration export, model input or inventory.

Lab composition requires Scientist inference mode and a separate trusted Lab
capability verifier. Missing policy/config, nonloopback authority or nonprivate
credential fails before desktop/DB/token creation. Credential preflight is a local
bounded read, not a network call or evidence of admitted remote principal. The
actual controller and prepared client are passed to ScientistLabService and the
existing console's `scientist_lab=` seam; task/action policy, separate exact human
approval, durable intent, independent readback and async control remain unchanged.

27 CPU/mock startup/console tests pass in 9.640 seconds:
`data/scientist-runtime-lab-startup-20260930.log`. A startup test uses the actual
create_console/ASGI authentication and Scientist service rather than a fake console:
propose → no request before approval → exact approval → start → independent queued
status → terminal report/status/hash verification → durable acknowledged inventory.
The authority is the test's own ephemeral SyntheticLabServer, DesktopRuntime is
FixtureDesktop, Docker/uvicorn are mocked and no listener/GPU/model is started.
The terminal report is synthetic, not a completed Scientist research experiment.
The configured credential survives shutdown; its value is not printed/exported.

Transfer to Scientist: all local startup seams now exist. Supply trusted confirmed
providers only after shared version/principal/cleanup agreement; no CLI flag or
frozen config grants admission. `aos-scientist-runtime.v1` remains a proposal.
Trusted reconcile/cancel/drain/release and Scientist-owned integrated GPU acceptance
remain open. Actual source HEAD `01dab7d` was read-only; local changes were not
merged, reset or tested here. Historical core counts predate the new startup tests.

## Desktop CLI source activation (30 September 2026)

`scripts/serve_desktop.py` now exposes `--engine scientist` and an explicit absolute
`--scientist-broker-socket`; it never guesses the shared socket path. Standalone
CLI use intentionally fails before token/DB/DesktopRuntime creation because no
joint runtime provider has been confirmed. A trusted host embedding may call
`main(scientist_confirm_runtime=verified_host_provider)` only after agreement;
the callback must complete or raise, not return a boolean grant. This is a local
host seam, not an invented Scientist capability endpoint or approval file.

Preflight checks pinned source profile digests before runtime startup. Duplicate,
nonobject and nonfinite manifest input, unsupported capability, missing socket,
native reuse/prewarm/GPU-idle and unbrokered owned-model paths are rejected.
Accepted host configuration routes the existing console scheduler through the
actual Scientist factory, including optional vision S2, without constructing a
native Decider/Bonsai engine. Native knowledge-answer generation remains disabled
for this mode; read-only document inventory does not grant model execution.
Normal existing disabled/fixture/native modes are unchanged.

10 CPU/mock startup/desktop tests pass in 0.248 seconds:
`data/scientist-runtime-startup-20260930.log`. They prove default failure before
runtime/DB/token/listener side effects and an injected synthetic provider's actual
S1/S2 factory-to-console wiring. The positive startup uses a private temporary
SQLite store and FixtureDesktop with mocked Docker/uvicorn; no listener, real
desktop, broker or GPU model was started. Existing core evidence predates the
six new startup tests and is not recounted. The managed user instance remains
untouched. Lab API service startup composition, shared trusted reconciliation
and Scientist-owned joint GPU acceptance remain open.

Transfer to Scientist: current entrypoint is `serve_desktop.main` with an explicit
trusted host provider; historical shared-gpu-turns/lab-external flags are not
silently added or treated as universal. Pin the actual source/diff/manifest and
confirm capabilities/principal/cleanup before enabling it. Current wire1 and
`aos-scientist-runtime.v1` proposal still do not constitute joint admission.

## Nonblocking Lab control and durable cancellation (30 September 2026)

The actual console execute/status/report routes now await async Lab service calls.
Only bounded HTTP/token-file I/O runs in a worker thread; authority/approval/intent
callbacks and final journal acknowledgment remain on the owning event loop and
SQLite writer thread. The client uses a per-call shallow worker sharing the same
effect lock and attempted-action set, not a second scheduler or client identity.
The original callbacks remain unchanged and the original deadline is retained.

While HTTP is active, concurrent controls are rejected but the read-only inventory
still responds. Cancellation returns to the caller promptly, does not cancel the
remote experiment, retains committed intent, and fences future effects. The exact
worker handle remains active until bounded HTTP and callbacks complete; no reset
or release is inferred. Failed result persistence still produces uncertainty.

54 CPU/mock HTTP/SQLite/caller tests pass in 23.458 seconds; final two responsive/
cancellation regressions also pass separately. A held POST allows actual ASGI
inventory to answer below 300 milliseconds while the effect is still pending;
host callback thread identity and durable commit are checked. Actual Human takeover
during a held stop prevents the late ACK closing the newer authority. Cancellation
does not replay the POST or acknowledge the intent, and callback exceptions are
consumed without event-loop errors. Evidence:
`data/scientist-runtime-lab-async-20260930.log` and
`data/scientist-runtime-lab-async-final-20260930.log`.
Initial synchronous-race fixture was updated to a held real synthetic HTTP response
and main-thread takeover, not weakened to permit SQLite writes from worker threads.
The wider focused run emitted existing unclosed-SQLite ResourceWarnings; it is not
claimed warning-free. The older 1904-pass core predates these changes.

Scientist HEAD observed `01dab7d97a97c4402ce416cebc6506f22834c131` read-only.
Its source preflight now distinguishes runtime_v1 from historical_hooks; dirty/
untracked source is rejected, and even clean source markers do not admit runtime.
UDS still offers only infer; shared capability/reconcile/cancel is unagreed.
No Scientist tests/files/processes, live AOS instance, GPU tests or deployment were
changed by this session. This improves control-plane responsiveness, not model
inference latency or actual joint GPU acceptance.

## Read-only inference handoff visibility (30 September 2026)

The existing authenticated `/api/scientist/jobs` now includes an inference
inventory from the current private SQLite store. It exposes only bounded current
session metadata (latest 20 IDs, state, profile/deployment/request digests,
owner/generation and worker unit/invocation), plus unresolved counts for other
sessions. Prompts, images, model output, lease secrets, tokens and broker peer
credentials are not returned. No remote call, reset, cancellation or DB write
occurs. Empty inventory is not GPU release; receipt-recorded stays unresolved.

The EN/TR Tasks Scientist panel shows these counts and the required trusted
release/reconciliation gate, separately from experiment controls. Local socket
cleanup is explicitly not GPU cleanup. This is source-only UI, not deployed to
the existing managed instance. The factory's blocked admission cannot be cleared
by refreshing or opening the panel.

19 initial CPU/mock + real Chromium synthetic-panel tests passed in 9.059 seconds;
the final 18 CPU console/desktop tests additionally include actual durable receipt
readback with private response redaction (7.608 seconds). Browser plugin was absent,
so existing Python Playwright/Chromium was used. EN/TR desktop1280x800/mobile390x844
show the blocking state and expandable digests without overflow/page errors or
framework overlays; refresh is GET-only. Screenshots:
`data/scientist-ui-qa-icif60df/` and external copies
`/tmp/aos-scientist-inventory-20260930/`. TypeScript and private Vite build passed;
the pre-existing large-chunk warning remains. Live `ui/dist` was not modified.

The earlier 1904-pass core predates four new inventory tests; totals are not added
to that historical run. Common admitted capability/reconcile/cancel remains
unagreed in current Scientist source; no shared endpoint or path was invented.

## Explicit desktop startup binding (30 September 2026)

`create_scientist_desktop_scheduler` constructs a fresh DesktopScheduler with S1
and optional vision S2 broker callers, one shared asynchronous transport and the
existing private SQLite intent journal. Pass it as the existing `scheduler=` to
`create_console`; no new HTTP route, scheduler or GPU ownership service is added.
The broker socket and manifests must be explicit. Joint capability/version
confirmation is a host callback, default-deny; this is not an agreed Scientist
capability endpoint and cannot be enabled by a model or boolean toggle.

The binding rechecks the actual controller lease/generation, job/run/current
stored State, active runtime and existing knowledge/failure/web authorization
guards before dispatch and receipt commit. The durable context digest pins State
and finite options (or vision evidence), not human approval. Tool effects retain
the existing independent exact human action approval and gateway policy.

Any unresolved inference intent anywhere in the current store blocks start/resume,
including receipt-recorded rows and rows from prior sessions. No row is reset or
automatically treated as GPU cleanup. There is still no trusted reopening surface.
Fresh startup confirmation and per-call confirmation are mandatory. Existing
managed instances and native workers were not replaced, stopped or restarted.

49 targeted CPU/mock tests pass in 2.309 seconds. The actual DesktopController,
DesktopScheduler, typed Operator, private SQLite, synthetic authenticated UDS,
human approval and authorized temporary file gateway execute one Hello task;
the file is independently read back and existing verification records pass.
The next job is rejected before insertion. Revocation before acceptance sends
nothing; a late reply after actual controller Human takeover cannot publish a
decision or write the file, and the durable intent stays pending.
Evidence: `data/scientist-runtime-desktop-20260930.log`.

The final source snapshot also passes 1904 core CPU tests with 275 skips and no
failures/errors in 167.563 seconds: `data/capability-check-kokf2aiw/report.json`.
This includes the new caller/startup tests; skipped live/GPU acceptance is not
converted into success.

Registry integration now recognizes the broker Bonsai identity as Bonsai, not
Decider; the CPU test checks pinned weight selection/backend and enabled=0.
Initial tests exposed incomplete synthetic registry pins; the fixture was fixed
without bypassing actual registry validation. Model/systemd authentication and
CUDA results remain synthetic, not real deployment/GPU acceptance. Multi-turn
GPU, trusted release/reconciliation and joint admission remain unverified.

## Bonsai recovery and vision caller routing (30 September 2026)

`ScientistBonsaiSupervisor` and `ScientistBonsaiVisionSupervisor` now implement the
existing recovery/describe caller interfaces using the same asynchronous broker
client as S1. Native schema pins, deployment digests, prompts and bounded payloads
are preserved; the emitted bodies pass the actual current broker-worker validator.
No local server is spawned and no native GPU fallback is attempted.

Current host task/control verification defaults to deny and runs before dispatch
and after receipt. Dispatch hooks cannot change deployment pins. Receipt correlation,
complete single-choice output, strict duplicate/nonfinite-safe JSON, exact recovery
evidence and capture/state-bound vision output are checked before publishing a plan.
Image bytes/dimensions/digest are validated before dispatch. A model plan still
cannot authorize effects or release GPU ownership.

30 targeted CPU/mock tests passed in 0.503 seconds, including eight new Supervisor
tests, the existing S1 callers and broker workers. Evidence:
`data/scientist-runtime-supervisor-20260930.log`. Backend responses, cgroup and CUDA
positive paths are synthetic; no GPU/model/live Scientist request ran. The earlier
1891-pass core run predates these eight new tests and is not recounted as 1899.

Scientist HEAD `ba12a0c1b25c3ddb08157c62855c57c970d7203c` was observed read-only;
its latest proposal-stop candidate does not change broker/API source paths. Shared
capability/principal, trusted reconcile/cancel/drain and explicit startup activation
remain open. Source callers existing is not deployment or joint admission.

Transfer to Scientist: both S1 and S2 callers now emit the current fixed-profile
wire-v1 payloads. Confirm `aos-scientist-runtime.v1` capability/version and trusted
terminal reconciliation before allowing sequential turns. Receipt-recorded intent
remains closed, and local cancellation is not remote release. This session did not
run GPU acceptance, change Scientist files or restart the user's app.

## S1 async caller routing (30 September 2026)

`ScientistDecisionEngine` implements the existing finite DecisionEngine boundary:
fresh AGENT state, immutable deployment pin, existing native request/options and
validated Prediction. It dispatches only `aos.decider.turn.v1`, rechecks authority
after receipt, and never falls back to a direct native GPU worker.

`ScientistAsyncTurnClient` runs authenticated socket I/O in a thread while bridging
authority, durable intent and receipt callbacks onto the owning event loop. This
preserves the existing SQLite writer thread and keeps the UI loop responsive.
Local cancellation stops socket waiting, retains pending intent and blocks new
turns; it is not remote cancellation or release. Original deadlines are retained.

47 focused CPU/mock tests pass, including real private SQLite journal and synthetic
UDS receipts, event-loop responsiveness, callback thread identity, lost ACK,
cancellation cleanup/no retry, changed authority/deployment and invalid predictions.
Evidence: `data/scientist-runtime-decision-20260930.log`; broader core evidence:
`data/capability-check-g1pwinta/report.json` (1891 pass, 275 skip, no failures/errors).
No GPU, real model, live Scientist or deployed app acceptance is claimed.

Transfer to Scientist: caller and worker source paths now exist; wire v1 remains
the current inference wire, while `aos-scientist-runtime.v1` remains a proposal,
not jointly admitted. Confirm the capability/version/principal and trusted
reconciliation/cancel/drain contract before reopening receipt-recorded intents.
Bonsai caller and explicit startup activation remain open. Scientist is the sole
integrated GPU acceptance runner; the user app was not restarted or modified.

## Actual checkout worker bridge (30 September 2026)

The current checkout now contains `services/decider/broker_worker.py` and
`services/bonsai/broker_worker.py`, independently implemented against Scientist's
current fixed argv, environment and four-field ready/go handshake. The historical
isolated copy was read, not copied or merged. Its Decider adapter called
`prepare_gpu`, absent from this checkout; the current native ModelSession now
implements fresh one-time GPU preparation without decision inference or reuse.

Shared standard-library `services/broker_runtime.py` checks fixed profile/request/
deployment/nonce, offline loading, private work directory, broker unit cgroup and
isolated network namespace before any model loading. Ready and go are bounded,
strict duplicate/nonfinite-safe JSON; exact request/profile/deployment/nonce must
match. Gate and manifest reads reject symlinks and changed files. Original
activation/inference budgets use Linux boottime. Workers run one inference and
exit; they do not acquire, allocate, release or reopen a GPU lease themselves.

Decider verifies existing model/code/dependency pins and returns its existing
prediction/metrics contract. Bonsai verifies model/runtime/native artifact hashes,
uses only the fixed manifest server/weights/projector, loopback private API-key
file, bounded schema-only recovery/vision input and output, and original HTTP
deadlines. It waits for its own server child to exit before publishing output;
failed cleanup produces no successful output. Parent/child exit remains weaker
than GPU release: Scientist must still prove exact unit/cgroup/device drain and
quarantine uncertain cleanup before another owner acquires the GPU.

47 CPU/mock native-worker/prewarm tests pass in
`data/scientist-runtime-broker-workers-20260930.log`. They prove gate rejection,
readiness-before-infer, fixed command/key handling, one-time mock CUDA preparation,
ready/go/output correlation and failed-cleanup suppression. Real CPU subprocesses
outside broker cgroup fail before model loading. CUDA, cgroup and model/server
positive paths are mocked; no real GPU or model inference ran. Vision shares the
entry point but is not separately GPU-accepted.

Scientist source HEAD observed `2f66560e274eb7b47e827e45f08f1143c246e8b6`;
its broker/API/worker argv and handshake are unchanged relative to the reviewed
pair. Concurrent local Director/CLI recovery changes were read-only and not
altered here. Its expired-unattempted-stop finding is already addressed in AOS
migration 0020 and the preceding focused acceptance; intent rows are never reset.

Transfer to Scientist: worker paths are now real source, but historical
shared-gpu-turns/lab-external CLI flags still do not exist. Do not admit the old
profile on filenames alone. Re-pin the actual source/deployment pair only after
joint capability/version/cleanup agreement. AOS caller-engine routing and explicit
admitted startup remain open; the existing user app stays untouched. Scientist
remains the only integrated GPU acceptance runner.

## Fresh human control and stale approval rejection (30 September 2026)

For an independently bound Lab run the service now builds a fresh control envelope
from the actual DesktopController after takeover. Original session/runtime/context,
API principal, suite, request and remote run remain pinned; only local control
owner/lease/generation advance. Old action/onay envelopes are never rewritten.
Different-runtime restart, cross-session adoption and context drift stay denied.

Append-only migration 0020 rejects unattempted pending/approved actions after fence
change or expiry, preserving original approver and exact task/action/body. The
reason distinguishes stale controller, expiry and human rejection. Intent and
acknowledged records cannot be reset. A new stop requires fresh human approval;
it is still a Lab stop request, not GPU release or scheduler ownership transfer.

59 focused CPU console/journal/audit/review/backup tests pass. Actual controller
takeover rejects the old stop and allows fresh Human inspection/approved stop,
while Human start remains denied. Expired unattempted approval no longer blocks
fresh stop. A late old stop response leaves durable intent unresolved, preserves
new Human authority and permits readback, not new effects. Capability/Lab fixtures
remain mocks. Evidence: `data/scientist-runtime-human-takeover-20260930.log`.

Two existing-Chromium synthetic UI tests pass: EN/TR stale/expired approval
explanations, exact scope/uncertain/no-retry, desktop/mobile and console/overlay
checks. Private evidence `data/scientist-ui-qa-7dqrtyx7/` and
`data/scientist-runtime-human-takeover-ui-20260930.log`. Shared capability,
explicit admitted app startup, trusted reconciliation and Scientist-only GPU
acceptance remain open. No deployment or user-app restart occurred.

## Console/controller connection (30 September 2026)

`ScientistLabService` now composes the existing DesktopController, durable Lab
journal and bounded client. The actual controller session/owner/lease/generation
is checked before proposals, approval consumption, RPC and result persistence.
Program version and suites are pinned by host configuration. The existing console
cookie, exact Host and POST Origin boundary authorizes the explicit human approval
response, bound to the displayed task/action/body SHA. Approve-all does not bypass
Lab approval. Detached Lab jobs do not take the desktop scheduler's foreground
slot; this is not a new GPU scheduler.

The optional `create_console(..., scientist_lab=service)` connection exposes
authenticated inventory and propose/approve/execute/stop/status/report operations.
`/api/scientist/...` are AOS-local console routes, not new Scientist broker
capability or cancellation endpoints. `stop` proposes an exact human-approved
HTTP Lab stop; it cannot revoke a GPU lease itself. No configured service means
inventory is explicitly disabled and all effects fail closed.

The Tasks screen now has an English/Turkish Scientist panel with host-allowed
suites, exact envelope inspection, separate approval and execution, status,
verified report readback and stop approval. Unresolved intent has no retry button.
UI rendering tests found and fixed a stale approved-result display after an
uncertain execution. The panel does not label capability/GPU admission successful.

Seven CPU ASGI console tests exercise the real DesktopController and authenticated
middleware against an owned synthetic Lab HTTP server. The console/client/journal
pool passes 35 tests. Two opt-in existing-Chromium tests exercise the actual React
component in a private static harness (Browser plugin not available): EN/TR,
desktop/mobile, exact human approval then uncertain intent, no automatic execution
or retry, no framework/page errors, and expected HTTP 409 console rejection only.
Evidence: `data/scientist-runtime-console-final-20260930.log`,
`data/scientist-runtime-console-build-20260930.log`,
`data/scientist-ui-qa-1_8r3oix/`. Screenshots are synthetic and private.

At this handoff Scientist independently advanced to
`29c0685daf09d211abece1c14acec0e81d58d8c9` (resume-stop CLI/tests/evidence).
Read-only diff confirms its broker and Lab API DTO/routes did not change.
Joint admission and GPU acceptance are still unverified. AOS remains at HEAD
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` with local source changes recorded in
`data/scientist-runtime-console-handoff-20260930.json`.

The running user's app was not restarted or deployed. UI build output goes to
`data/scientist-console-ui-build-20260930`, not the live `ui/dist`. CLI/app-factory
activation still requires an admitted joint capability/version configuration;
the service's capability verifier defaults to denial. HTTP control is bounded
and synchronous (default three-second budget); it does not wait for a whole
experiment or hold the controller lock while making the remote HTTP request.

## Durable Lab job and approval milestone (30 September 2026)

Migration 0019 adds separate detached `scientist_lab_jobs` and
`scientist_lab_actions` to the existing private TrajectoryStore. It does not
reuse inference-turn receipts, create GPU leases or add a scheduler. Exact task,
action, body, deadline and human approver are retained. Approval is consumed in
the same FULL-synchronous transaction that commits `intent`, before any POST.
Independently read-only SQLite observations test that commit-before-send order.
Verified API results bind the remote run exactly once and acknowledge the action
atomically; a stop acknowledgment still does not attest GPU release.

`ScientistLabJournal` checks current desktop session owner/runtime/lease/generation
and the immutable task/remote binding. Human authentication and joint capability /
host state authorization default to denial. The CPU fixtures explicitly supply
mock providers; there is no deployed human approval or console dispatcher yet.
Rejected, expired, altered, stale and duplicate approvals cannot produce effects.
Lost acknowledgment or result-persistence failure leaves `intent` durable; even
another preapproved job in the same session cannot send. Reopening the store
does not revive its previous desktop authority or permit blind retry. There is
no automatic reconciliation/reset API.

Eleven focused CPU tests pass in `data/scientist-runtime-lab-journal-focused-20260930.log`.
These use an owned synthetic HTTP server, temporary private SQLite and synthetic
credentials, not Scientist's running API or GPU. Current Scientist source HEAD
`1d8e70264a1731b5858dd65ea72c0e96de094f12` only adds documentation/evidence relative
to the previously inspected pair; its runtime API/broker contract did not change.

Next: bind these durable approvals and jobs to the authenticated console and
current controller state, obtain the agreed capability/reconciliation surface,
then let Scientist alone run coordinated GPU acceptance. The journal library is
not deployment or joint integration completion.

Scientist's latest source review confirms that the Lab DTO/routes are shape
compatible, not admitted or deployed. Its historical compatibility checker
targets the isolated shared-gpu-turns/lab-external adapter: those absent CLI/worker
paths are not universal requirements for this new typed adapter. Do not add empty
compatibility files to bypass preflight; agree the new adapter's capability and
task/policy/cleanup checks instead. Source review is not GPU acceptance.

Observation: 2026-09-30. This AOS-only work preserves the existing goal and
prior work. Scientist is inspected read-only. No services were restarted,
no GPU test, push, merge, deploy or training was initiated.

## Source-confirmed baseline

- AOS HEAD: `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`, newer than `22e5736`.
- Scientist HEAD: `14a2c83fb579e4f658db836cbab8f3e8803bc07f`, newer than `67258cd`.
- At handoff, Scientist advanced independently to
  `4441856dea469605c365052bfdc0c08dae1015ef`; its tracked diff is again empty
  (`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).
  The change list was inspected read-only: compatibility checker, scheduler
  tests and evidence/documentation; the inspected runtime protocol sources
  did not change. Use this newer pair for the next review, not the initial HEAD.
- During the transport continuation Scientist advanced to
  `89117e2362ff6ebf8eb02510ea841e236b12b955`, tracked diff empty. Its
  executor now binds durable request replay to the exact authenticated caller
  principal generation and rejects unbound legacy rows. This source change was
  reviewed read-only; AOS did not run Scientist tests or deploy it. Broker wire
  and service socket lifecycle were unchanged. This was the transport handoff
  Scientist HEAD, not a jointly admitted version pair.
- At durable-intent handoff Scientist HEAD is
  `af4864d9a16d770f8fbd2f1ab5619ac8fd959b1e`, tracked diff empty.
  Its newer stop closure/recovery/migration/CLI changes remain Scientist-owned;
  the source change list was inspected read-only. No broker wire, service or
  API contract source changed in that commit delta. This newer pair still has
  no jointly admitted capability/cancel/reconcile control contract.
- At typed Lab boundary handoff Scientist advanced to
  `5da7c766f8918c1bd1ce90d09d03ad9ceb955d0d`, tracked diff empty.
  That delta contains documentation/native stop expected-CAS evidence only;
  inspected API and broker contract sources were unchanged. AOS did not run
  those Scientist tests or interfere with its processes.
- Both tracked diffs were initially empty; existing untracked
  `AI_SCIENTIST_COORDINATION.md` was preserved and is excluded from packaging.
- Scientist subsequently developed its own local changes. Observed tracked
  binary diff SHA256: `b81b3d81cce70a7d63394e2ffccd64cc4779de24bf6a5e5d8a0b8511c57a5dec`.
  This excludes untracked files and is an observation, not a frozen release.
- At that historical observation the AOS checkout had no `--shared-gpu-turns`, `--lab-external-*`,
  `services/decider/broker_worker.py`, `services/bonsai/broker_worker.py`,
  `src/aos/gpu_turn.py` or `src/aos/lab_external.py`.
- These adapters exist in the isolated historical copy
  `../ai-scientist/data/runtime/aos-coexistence/source-merged-20260929-sol61-0101ef6c806c`.
  Its workers and coordinator were read, not copied or merged. It is not the
  current AOS checkout; historical GPU successes do not qualify this pair.

Scientist's current `lab/llm/aos_gpu_broker.py`, `aos_gpu_service.py`,
`aos_gpu_executor.py`, `gpu_scheduler.py`, `lab/api/contracts.py` and
`lab/api/app.py` are the inspected sources. Its active API and Director were
left untouched. Scientist's new `65-coordinated-lifecycle-acceptance.md` and
source-only compatibility checker likewise explicitly reject current AOS.

## Implemented AOS boundary

`aos.scientist_protocol` and four canonical schemas implement the existing
fixed-frame **wire version 1**, not a new scheduler or network endpoint:

- Canonical UTF-8 request with exact `version/op/request_id/profile_id/`
  `deployment_digest/payload`; SHA256 excludes the terminating newline,
  matching Scientist rather than AOS's ASCII-escaping canonical helper.
- Three existing fixed profiles, 128 KiB newline frames, strict correlation,
  duplicate JSON rejection, deployment and worker unit/cgroup checks.
- Bounded terminal report verification against a separately supplied typed
  status, expected run ID and canonical report hash. `stop_requested` cannot
  qualify as terminal. Both report and status must agree on terminal state.

These protocol functions are codecs/readback validators, **not authenticated transport**, GPU
release attestation, durable task dispatch or research-quality verification.
Caller-supplied status/receipts do not prove peer identity or cleanup. No console,
task/tool route, model engine or Lab API client is attached yet. No implicit
retry or GPU allocation is introduced.

### Authenticated transport library — default admission still denied

`aos.scientist_transport.ScientistTurnClient` now implements the source-confirmed
Unix socket protocol. No socket path is guessed or provisioned: a host must
configure an explicit absolute path. Private user-owned socket/directory and
stable socket inode checks precede dispatch. The default authenticator binds
SO_PEERCRED to the fixed `swapp-lab-gpu-broker.service`, MainPID, process start
ticks, boot ID, invocation and unified cgroup, using the pinned current user
systemd bus. Identity is checked again before sending and after receiving.

Production admission is **denied by default**. Both trusted host seams must be
implemented before use: an admission verifier for joint capabilities/current
authorization, and a durable intent writer. The latter receives exact canonical
request bytes/hash, original monotonic deadline and authenticated **broker**
generation. That peer is not the AOS caller principal or the scheduler lease;
those identities must be bound by the future task adapter and Scientist broker.
The monotonic deadline needs its boot identity in any restart-aware journal;
it cannot be treated as a portable wall-clock deadline. None of these callback
signatures creates a new authority or proves persistence by itself.

Admission is rechecked before connection, after intent persistence and after
readback. Each callback receives an isolated copy; no callback mutation changes
the frozen wire bytes. The transport reads one bounded frame through EOF,
matching current Scientist's per-connection close, including rejection of a
second frame arriving in a later packet. One bounded original deadline covers
socket I/O, peer checks and intent persistence. No retry regenerates it.
The deadline is checked around host callbacks; it does not forcibly interrupt
a stuck Python callback. The future durable writer must itself bound its I/O.
This client deadline is not transmitted as a new wire field and does not
replace Scientist's fixed profile/scheduler execution deadlines. Expiring it
only makes the local outcome uncertain; it is not remote cancellation.

Disconnect, timeout, malformed/foreign response or generation/authorization
change after attempted send leaves the outcome uncertain and closes this
client's admission for **all** subsequent turns. Completed request IDs are not
resent; after 256 attempts the client refuses instead of discarding replay
history. There is no method to clear uncertainty or claim drain using a boolean.
This is an in-memory transport guard, **not a durable reconciliation journal**;
a new client or process must be gated by the future persisted task adapter.
Closing a socket does not cancel a Scientist ticket, terminate its worker or
release a GPU lease. Broker cancellation/status/readback semantics still need
joint confirmation. Do not attach this synchronous client to cancellable async
model engines before that lifecycle is implemented.

CPU evidence: **26 tests** cover real private synthetic Unix socket exchange
and mocked systemd authentication, rejected UID/PID/invocation/cgroup/boot
changes, revoked admission, missing/failed intent writer, timeout/concurrency,
duplicate request, split extra frames, ambiguous results, mutation, public/
symlink/replaced sockets and original deadline consumption. This is **not** a
live Scientist broker, real systemd principal acceptance or GPU test. Admission
and persistence callbacks in those tests are explicitly fixtures, not a DB.

Reusable S1 cleanup now retains its exact process handle when signaling,
communication or the bounded five-second cleanup wait fails. New startup and
requests fail closed until explicit cleanup succeeds. Reopening is tested with
synthetic CPU workers. Parent process termination is **not** an independent
cgroup/descendant/GPU-unload attestation; only the Scientist authority may
declare the GPU released. Admission quiesce remains distinct from drain.

## Proposal for joint confirmation: `aos-scientist-runtime.v1`

### Typed Lab task/tool/policy API boundary

`aos.scientist_lab` adds a separate `ScientistLabTask`, a bounded start/budget
contract and `ScientistLabAction` extending the common AOS action envelope.
Its four finite tools are **lab.start/status/stop/report**. This does not alter
the ordinary native `Action`/computer registry or disguise a research task as
Hello. The deterministic policy is explicitly `real_model=False`, not a
Decider/Bonsai decision or a third runtime model.

The routes are taken from current Scientist API sources: `POST /v1/runs`,
`GET /v1/runs/{run_id}`, `POST /v1/runs/{run_id}/stop` and
`GET /v1/runs/{run_id}/report`. These are **research control** routes, not
new broker infer-cancellation/capability endpoints. No base URL, port or token
path is defaulted. Only explicit literal loopback HTTP authorities are accepted;
DNS, credentials/query/fragment/path and nonloopback targets are rejected.
Tokens are read from bounded regular same-user single-link mode-0600 files with
no symlink following; values never appear in model input, arguments or logs.

Policy checks configured principal/authority/suite allowlist, exact AOS external
task/run/action IDs, source runtime/lease/state version, finite selected tool,
deadline and exact request hash or bound Lab run ID. Fresh control generation
and deployed service identity **must additionally be verified by the host
authority provider**; a caller-supplied binding or principal string is not
proof. That provider is denied by default, before token reads or HTTP calls.
Start requires Agent-owner metadata; bound inspection/stop permits Agent or
Human metadata with a fresh verified host control fence. Metadata tests are not
actual human takeover/approval acceptance.

Effects additionally require an `authorize_and_persist` provider to verify fresh
human approval and commit the immutable exact action/body before POST. This is
also denied by default. Those callbacks are seams, **not implemented approval
DB or durable detached-job dispatch**. They receive isolated copies and must
compose real AOS control/policy/intent checks; neither a Boolean nor model
output can substitute. Do not route production tasks through fixture callbacks.

Start acknowledgment receives independent owner-visible status readback.
Report receives terminal status → report → unchanged terminal status; run,
origin and canonical report hash are independently checked. Stop ACK must
confirm a stop request or a terminal state; it is never GPU release evidence.
Authority is rechecked around multi-request readbacks. The bounded control
deadline is retained across requests, with per-read remaining socket deadlines;
host callbacks must bound their own I/O. No full training or model download
is requested by the adapter.

After an attempted effect, HTTP failure, invalid/foreign acknowledgment or
authority drift leaves the result uncertain and blocks further effects even
with a new action/key. Read-only bound status/report may still be inspected.
There is no retry, redirect, reset or guessed lost-ACK lookup endpoint. This
guard remains in memory; durable job/approval/action journaling and reconciliation
must still be connected. The inference journal from migration 0018 is **not** a
Lab HTTP start/stop journal and must not be reused with mismatched records.

CPU evidence uses a synthetic loopback HTTP server, synthetic token and mocked
human/authority/persistence providers. It exercises bounded start + independent
readback, status, stop-request acknowledgment and independently hashed terminal
report; scope/state/lease/principal rejection, revoked authority, lost ACK,
foreign origin/run, report drift/hash, token safety, callback mutation, replay
and denied-default admission. This is not a live Scientist experiment, real
human approval, durable Lab workflow or shared GPU acceptance. Console/native
model/foreground routing is not installed.

### Durable AOS intent journal — reconciliation not yet enabled

Append-only migration **0018** adds `scientist_turn_intents` to the existing
private AOS trajectory DB. This is not a GPU scheduler, a Scientist DB or a new
ownership authority. `ScientistIntentJournal` commits exact canonical request,
digest, control-session/owner/lease/generation/runtime binding, context hash,
original deadline and authenticated broker peer before the transport sends.
The context hash is a provenance pin, **not** human approval or capability proof;
the host must compose its existing typed authorization with journal checks.
No deployed task/tool/CLI route is enabled by this library.

SQLite FULL synchronous mode and the existing single-writer store are reused.
Nested transactions are rejected so a callback cannot return with an uncommitted
intent. Exact duplicate insertion, stale binding and pending prior requests
deny dispatch. Canonical SQL triggers prevent identity/deadline rewrites,
deletion and adoption by a different current session fence. Existing migrations
were not rewritten; schema audit, dataset-review journal and backup validators
now accept version 18 while preserving supported historical versions 7–17.

The transport's optional `record_receipt` hook commits an exact correlated
receipt/peer against the pending row. Failure after send makes the client
uncertain. A received result is deliberately stored as **`receipt_recorded`**,
not drained/released/completed. It does not clear the unresolved-session gate:
even a new client or process cannot automatically start a second request.
The journal has no reset/delete or invented release-proof API. Trusted
Scientist reconciliation/cancel/drain evidence is still required before an
explicit future migration/transition can reopen that gate. This conservative
intermediate state is not a finished reusable runtime or GPU lifecycle.

CPU coverage includes independent SQLite readback, restart retention, old-owner/
generation/lease/runtime rejection, duplicate and foreign-peer result rejection,
revocation, deadline/hash/canonical-frame failures, SQL immutability and a
synthetic Unix transport → committed intent → committed receipt chain. That
chain uses mocked broker authentication and cannot prove GPU cleanup, real
human approval or an authenticated Scientist experiment.

This is a **proposal**, not an implemented capability response. Preserve wire
version 1 and existing Lab APIs; do not add owner/fencing fields to its fixed
inference frame. Broker derives principal from SO_PEERCRED and pinned service
generation. Only Scientist's existing shared scheduler holds GPU leases.

1. Before admitting work, agree on this contract ID, exact source pair and
   dirty-tree source hashes, wire version, supported profiles, deployment
   digests and enabled typed start/status/stop/report capabilities. Missing or
   unsupported capability information denies admission; no guessed endpoint.
2. Persist an immutable authorized intent before dispatch: principal binding,
   external task/run/action IDs, request ID, canonical digest, original deadline
   and fresh human approval. Intent recording does not allocate the GPU.
3. Scientist authenticates service unit/invocation/boot/PID-start/cgroup,
   binds its existing owner/generation/fencing token to the request and acquires
   the existing queue. Neither model output nor an AOS boolean may expand scope.
4. Cancellation/revoke/deadline closes admission and requests cancellation of
   that exact ticket. Stop ACK is not terminal; no new key or blind POST retry
   after a lost acknowledgment. Reconcile the persisted identity first.
5. Only trusted Scientist cleanup can terminate the exact owned invocation,
   verify cgroup and GPU absence and release its lease. Failure/crash/timeout
   keeps its existing quarantine. Stale owner/generation/duplicate release
   must never terminate or release a successor. AOS remains closed meanwhile.
6. Research work stays detached from desktop foreground/input ownership.
   Independently authenticated status/report readback validates terminal
   identity and hashes. Terminal research status alone does not release GPU.

### Transfer to Scientist session

Current pair is **unsupported for GPU admission**. Wire-v1 codecs, S1 cleanup
regressions and a default-denied authenticated transport library are implemented
in AOS, but historical CLI/workers, durable task adapter and authenticated
runtime capability negotiation are not installed. Please confirm
the proposed contract ID and how the existing broker exposes pinned
capabilities, durable uncertain-request reconciliation, cancellation and
trusted cleanup evidence. No new endpoint or shared file is presumed. Also
confirm current API status `purpose` compatibility (the older isolated adapter
predates that optional field) and the principal/external-ID binding.

Scientist remains the **sole integrated GPU test runner**. No integrated run
may start until the compatible pair, explicit opt-in instance, scheduler
reservation and absence of conflicting user work are confirmed. Its independent
automatic-stop fix, database atomics and tests remain Scientist-owned.

## Acceptance still open

Scientist's current `65-coordinated-lifecycle-acceptance.md` accepts the proposal's
wire-v1 direction, three fixed profiles and API `purpose` compatibility; it
explicitly does **not** confirm an implemented joint runtime contract. It also
confirms that the current UDS only supports `infer`: no capability, status,
reconcile or cancel endpoint has been agreed. A disconnect does not immediately
cancel active inference. Preserve the fixed frame rather than adding guessed
fields or pretending those control operations exist. Durable cancellation and
uncertainty reconciliation remain a joint control-surface requirement.

- Wire parsing/correlation and CPU worker cleanup/reopen are tested locally.
- Wrong principal, stale generation, duplicate/revoked requests, crash and
  cleanup failure across the real broker are not covered by codec success.
- Authenticated transport library is CPU-tested, but deployed authenticated
  transport acceptance, durable typed task/tool/policy integration, exact
  intent/reconciliation, installed broker workers and CLI admission remain open.
- Joint version confirmation and Scientist-owned AOS→Lab→AOS GPU handoff,
  bounded experiment/scoring, independent report readback and cancellation/
  recovery acceptance are **not run**. No model/context/VRAM/latency claims.
- Licensing and general CI are separate workstreams, not part of this change.
