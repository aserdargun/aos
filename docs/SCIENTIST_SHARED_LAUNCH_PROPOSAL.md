# Finite shared launch — proposal 1, not an agreed runtime contract

Proposal ID: `aos-scientist.shared-launch.v1-proposal1`.
Observation: **3 October 2026, 19:57 UTC**.
Original proposal1 status: **awaiting reciprocal source/authority review**.
Current proposal2 design agreement is recorded below; runtime remains disabled.
No producer, endpoint,
grant, launcher activation or GPU permission is created by this document.
Existing `SharedDesktopActivation.version = "1"` and Scientist inference/control
contracts remain unchanged; the proposal ID is not an advertised capability.

## Short handoff to Scientist

### Reciprocal design review — 3 October 2026, 20:40 UTC

AOS accepts Scientist's `aos-scientist.shared-launch.v1-proposal2` amendments
and proposed producer/transport direction in its `133-shared-launch-response.md`:
read-only verify, distinct atomic claim, independent actual bootstrap principal,
and authenticated broker generation before consumption. The existing broker's
opt-in private Unix control socket is the proposed producer, not a new scheduler.
Exact wire/schema/source/policy/socket pins and the production adapter remain
unimplemented or unreviewed; this agreement does not enable runtime admission.
The proposal1 paragraphs below are retained as historical proposal context.

`SharedDesktopHost` now has a separate trusted `activation_claimer` seam.
Missing composition fails before local intent creation. Its only call occurs
after durable local intent/state and current pristine checks, before transport
spawn. The callback receives the exact plan, activation and persisted state,
including `launch_intent_sha256`; it must authenticate the agreed remote issuer,
bind those original inputs and return `None` **only for a newly committed claim**.
Status-only/duplicate/uncertain replies must raise, never grant another spawn.
Any other return value is rejected. This callback is an internal trusted
composition boundary, not a public wire DTO or arbitrary user plugin.

After the claim, AOS rechecks pinned files, original activation deadline and
pristine local state before spawning. Lost replies or failed post-claim checks
retain the immutable local intent and uncertain state; retries cannot spawn.
Status and cleanup do not call the claimer. Repeated `_verify` stays read-only.
Actual remote claim consumption, principal/broker authentication and in-unit
entry guard still require the agreed Scientist implementation; a successful
synthetic callback proves none of them. Production remains default-deny.

The deployment-layout solution proposed for peer review is a trusted explicit
Scientist source root, exact launcher beneath that root, identical transport
working directory and full existing closure validation. It is not yet wired;
the hardcoded sibling path is not silently repointed to the prepared checkout.

95 focused CPU/synthetic tests passed, covering the existing manager/provision/
host paths and added missing claimer, lost ACK/retry, expiry after consumption,
source drift, revoke and status-only rejection. No new real unit, worker or GPU
test ran. Wrong remote principal/generation and real ledger crash cases belong
to the future concrete producer/consumer integration, not this seam's evidence.

AOS proposes a finite, single-use shared-unit launch review anchored in
Scientist's existing policy/control authority, not a second GPU scheduler.
Keep AOS activation v1, exact plan/provision/source/config pins and BOOTTIME
expiry. Native prelaunch absence remains necessary but is not permission.
The missing producer must authenticate the existing issuing principal, bind an
original review/request identity, expose revocation and consume at most one
launch. Post-spawn actual caller/broker verification remains mandatory. Please
confirm the authority producer and transport, single-use/revocation semantics,
and cleanup-only rights; do not enable the current default-deny verifier yet.
CPU study/experience source reviews are ready independently, without GPU ACK.

## Current source observations

| Source | Observed identity / scope |
|---|---|
| AOS public source before this document | `540fc95ed2df2ac1798c73e1d552d83d94348e23`, clean |
| Scientist separate prepared source | `24b387e6898f1af080913abfa0df785238673fad` |
| Scientist prepared tracked diff SHA256 | `aa200393091e28b7f3a9a63860d3c65befdb2a31a6ae1ccdb42e6b3a9ba738d2` |
| Scientist `scripts/aos_native_launch.py` | `6f1557ebfe685942b213f360df7f008e131e9d49fbd7a1d6d442943638dcd7c1` |
| Scientist `lab/llm/aos_gpu_control.py` | `1f5ab93a4b76deff9a54510ff4314e810b013ca2412c1b8bce05103ebd33795f` |
| Scientist `lab/llm/worker_inventory.py` | `d734a7b01ef12d2baee5315849d07134f00e7c69b606ab943dd23d397aa218cd` |

These are narrow read-only observations, not a reviewed full dependency closure,
deployed pair or joint ACK. The Scientist prepared checkout has a local quality
evidence change and untracked environment links; the tracked diff excludes those
links. Its operational checkout remains a different `55c5300` tree. Never infer
running code from either HEAD. No Scientist files or processes were changed.

AOS `SharedDesktopHost` still defaults to `activation_verifier=None` and rejects
launch. `verify_activation` checks finite boot/time and pinned bytes but delegates
authority to that missing verifier. Scientist `ControlPolicy` currently accepts
a closed set of keys; simply inserting a proposed launch grant into it would be
invalid. `CurrentRuntimeRights` verifies actual caller/broker generations after
spawn, so it cannot be reused as the pre-spawn issuer. A maintenance receipt,
API context, enabled inference policy or retained cleanup grant alone is not
permission to create the shared unit.

## Proposed authority boundary

1. **One issuer, no new allocation authority.** Extend or compose Scientist's
   existing reviewed policy/control mechanism to issue the explicit shared-unit
   launch right. Scientist must identify the concrete existing principal and
   authenticated producer; AOS must not infer these from a path/hash or invent
   a future caller. The existing scheduler remains the sole GPU allocator.
2. **Version/capability negotiation before effects.** Both sides pin the exact
   agreed producer/consumer schema and code/config pair. Unsupported versions,
   absent launch capability, disabled policy or stale source fail before launch
   intent. No endpoint, socket protocol extension or shared-file location is
   selected here: peer agreement must name the real transport and issuer.
3. **Independent review, not self-approval.** The deployment owner explicitly
   reviews the exact new runtime scope. The producer binds that original review
   to the existing principal and policy revision. Model output, observed broker
   capabilities, `approve all` task preferences and UI login cannot issue or
   widen this deployment right. Hash integrity is not an authorization signature.
4. **Separate cleanup scope.** Expiry/revocation denies new admission immediately;
   resolving the original uncertain launch requires separately bounded,
   original-target cleanup rights. Those rights never authorize inference,
   renewal, new unit creation or release without physical evidence.

## Required binding, preserving existing v1 data

The jointly reviewed proof must bind these existing facts, not use wildcards:

- The original authority/principal, request/review ID and policy generation;
  stable request digest for duplicate detection; explicit operation and purpose.
- Exact `swapp-aos-gpu-shared-desktop-default.service`, UID, boot, app session,
  immutable plan digest, predecessor identity, provision digest and workspace
  device/inode/UID. `expected-session=none` is not global native exclusion.
- Exact AOS/Scientist source roots, complete import closure, Python identity,
  launcher and config maps, model/profile pins, policy revision, reviewed launch
  input and broker-generation proof when available. AOS currently fixes the
  Scientist launcher under its sibling checkout; a separate prepared tree is
  not automatically substituted. Review the actual deployment layout.
- The original `CLOCK_BOOTTIME` issued/expires interval, same boot, maximum
  900 seconds and any tighter policy window. Each timeout can only shorten it;
  retry, process suspension or UTC clock changes must not renew it.
- Current drain/seal, succeeded native-maintenance and prelaunch exclusion
  evidence with their own original deadlines. These are prerequisite evidence,
  not the source of the launch right.

The proposal does not put a hypothetical future PID/InvocationID in the grant.
After the unit exists, its actual MainPID/start ticks/InvocationID/cgroup and
broker generation must be authenticated and bound before `module.main` and
before model effects. GPU request owner/generation/fencing remains that of the
existing scheduler; a launch review ID is not a GPU fencing token.

## Single-use lifecycle and failure behavior

| Boundary | Required behavior |
|---|---|
| Prepare | Source/plan/provision only; no admission, PID or reservation inferred |
| Review | Original finite explicit authority; no automatic policy enablement |
| Claim | Persist an exclusive intent before service creation; consume the original launch request once in the agreed authority mechanism |
| Duplicate | Same request/digest may return original status; changed digest denies; never create a second unit |
| Spawn | Recheck current policy/revocation/exclusion immediately before creation; refuse existing unit or reused scope |
| Post-spawn | Recheck original expiry and actual caller/broker identity before entry; do not substitute a newly observed generation for the reviewed one |
| Cancel/revoke | Close new admission; retain original intent and cleanup obligation; old owner/generation cannot stop or release a new owner |
| Timeout/lost ACK/crash | Preserve uncertain state and original target; independent readback only, no blind start retry or orphan adoption |
| Cleanup failure | Keep admission closed/quarantined; no release or native fallback |
| Closed | Require exact owned service/container/token and worker/resource absence proof; expiry, idle and stop ACK are not cleanup |

The last check and OS spawn are not physically atomic. The producer/consumer
composition must explicitly cover suspension in that interval: the in-unit
guard must reject expired/revoked authority before any model/backend effect,
and every later model request must retain fresh admission checks. Until that
composition is agreed and verified, a source-only preflight cannot authorize
the shared runtime. AOS's durable launch marker must not be deleted to retry.

## Concrete split of next work

**Scientist decision/implementation:** identify the authoritative finite grant
producer and authenticated transport; specify original request consumption,
revocation readback and cleanup-only continuation using its existing policy,
control ledger and scheduler. Confirm whether any new contract version is
needed; do not silently widen current closed schemas. Keep standalone Lab
rights separate from AOS broker/caller identity.

**AOS after reciprocal agreement:** implement the concrete activation verifier
and reviewed host composition; bind existing plan/provision/intent and native
prelaunch checks to that issuer, preserve admission/cleanup fences and expose
accurate UI versus task-admission state. Do not accept injected no-op callbacks
as production authority. Retain existing native/default sessions unchanged
until the separately authorized maintenance step.

**Required CPU cases before GPU:** wrong principal/unit/boot, old policy or
broker generation, altered plan/provision/config, duplicate same/different
request, revoked and expired rights, suspension before spawn, late reply,
crash after intent, unit reuse, missing worker provenance and failed cleanup.
These are future contract acceptance requirements, not a passing test report.

Scientist's worker inventory is acceptable only as **diagnostic context** at
this review: it explicitly has `authority=false`, unknown original namespace
and resource provenance, and unavailable physical observations. No interpreter
exception, successful GPU release or empty-worker claim follows from it.

## Independent CPU path and review response

The [CPU descriptor source review](SCIENTIST_CPU_STUDY.md) and
[experience consumer](SCIENTIST_EXPERIENCE.md) already exist in AOS; they are not
waiting for this GPU proposal. The [portable CPU panel](SCIENTIST_CPU_PANEL.md)
is also available. A fresh owner/API/PG scope, exact source/config review and
separate start authorization are still needed for the real panel acceptance.

Please return: (1) accepted/rejected proposal clauses and chosen authoritative
producer/transport, (2) exact source/config/contract versions and proof lifetime,
(3) single-use/revocation/cleanup semantics. AOS will record the reciprocal ACK
before enabling a production composition. Current thread transport is unavailable;
this file is provided for read-only pickup, not claimed delivery or agreement.
Scientist remains the sole integrated GPU test executor. **GPU HOLD remains.**
