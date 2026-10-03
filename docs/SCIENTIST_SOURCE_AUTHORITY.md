# Configured Scientist source verification candidate

The source verifier itself accepts `__call__(selected_bindings, *, deadline=None)`
using an absolute `time.monotonic()` deadline. Its existing five-second read
budget is capped by the original outer budget, never extended. Expired/nonfinite
values deny before runtime callbacks or file access; all sweeps use the same
effective deadline. The mandatory runtime callback remains one-argument and must
honor the trusted caller's existing enclosing deadline scope. A configured host
must supply a deadline-aware source wrapper when a native factory calls its
source callback without keyword arguments. Merely supporting the keyword does
not establish end-to-end propagation. Synchronous filesystem/callback work is
cooperative, not forcibly interruptible or hard-real-time verified.

## Coordinated authentication deadline

`SystemdBrokerAuthenticator.authenticate(pid, uid, *, deadline=None)` and
`still_current(peer, *, deadline=None)`, plus caller
`authenticate(generation, *, deadline=None)` and
`still_current(generation, *, deadline=None)`, accept the same optional absolute
`time.monotonic()` deadline. Pass the original unchanged deadline to every
sequential identity check. Each query uses the smaller of its existing two-second
limit and the remaining outer budget; invalid, nonfinite or expired deadlines
deny before identity/query work. A result completing after the deadline denies.
Omitting the option preserves the local two-second budget. This is cooperative
deadline enforcement, not measured hard-real-time scheduling or GPU acceptance.

The infer transport now forwards its original effective request deadline to
initial peer authentication and both pre-/post-dispatch generation checks.
Injected broker authenticators must accept this keyword too; there is no
TypeError fallback to a budget-renewing legacy call. Expired authentication
before dispatch persists no intent and sends no request. Post-dispatch failure
continues to retain uncertainty and prohibits automatic replay.

`ScientistConfiguredSourceVerifier` supplies the concrete read-only source/config
half of `ScientistBootstrapAdmissionFactory.verify_source`. It creates no policy,
binding, scheduler, GPU lease, socket, deployment or retained-provider authority.
Only a trusted host may provision its inputs; neither ACKs nor observed disk
contents are a source of expected pins.

## Independent inputs

Supply an explicit private policy path, explicit absolute AOS/Scientist source
roots, independently reviewed per-file raw SHA-256 maps, complete reviewed
`ScientistAdmissionBindingV2` objects, and independently pinned model/profile
configuration file paths/hashes. No default path, private configuration discovery,
manifest download, import of Scientist modules or policy enablement occurs.

The mandatory `verify_runtime(selected_bindings)` callback must return `None` or
raise. Defaults deny, as do boolean success returns. It must independently check
the actual configured profiles/artifacts, complete dependency closure, current
caller/service and canonical broker generations, schema/output bundle and current
principal rights. Caller `parent_pid` denotes service MainPID, not kernel PPID.
Minimum producer names and source-report membership are **not** full closure.

Pass this callable as the native factory's `verify_source`; pass that factory to
`serve_desktop.main(scientist_bootstrap_factory=factory)`. All reviewed bindings,
source maps and hashes must correspond to the same separately reviewed enabled
policy/configuration. A disabled configuration plan cannot authorize runtime;
enabling it changes the policy hash and requires new independent reviewed bindings.

## Readback and hashing

The mandatory runtime gate can now use native `SystemdCallerAuthenticator` from
`aos.scientist_transport` for its caller identity subcheck. `authenticate` accepts
only the full typed independently reviewed `ScientistCallerGeneration` and returns
that exact generation; `still_current` denies identity/query errors. Neither
method supplies profile, artifact, dependency, policy or current task authority.

The observer brackets actual caller and reviewed service MainPID start/boot/cgroup
observations around a bounded fixed `systemctl --user show` query. It requires
actual PID/UID, canonical reviewed unit/InvocationID, private owned runtime and
owned non-symlink bus, exact closed non-duplicate properties and unchanged process/
bus identities. `parent_pid` is service MainPID, not kernel PPID; the caller can
be that MainPID or another process in the **exact same** service cgroup. Descendant
cgroups deny: Scientist's current broker enumerates only the exact unit's
`cgroup.procs`, unlike the broader allowance in its older adapter helper.

Do not replace `verify_runtime` with this identity observer alone. Compose the
identity checks with the independently configured profile/output/artifact/
dependency/current-rights verifier and recheck revocation after that work. The
read-only command has a two-second cooperative overall deadline and16KiB output
bound with sanitized environment; no service action, process kill, observed-new-
generation adoption, default launcher or policy activation occurs. Real latency
and deployment acceptance remain unmeasured; avoid claiming these bounds are speed.

Caller observer evidence:222 root CPU/synthetic tests passed in32.029s,
`data/scientist-caller-generation-root-20261001.log`, including14 new synthetic
caller tests and existing source/bootstrap/transport/startup/provider regression.
Actual proc/systemctl observations and process runner were patched; no live
identity observation or service action was performed. Independent review found
no blocking issue. Profile60 membership is unchanged; its source pin changed
because the existing transport implementation changed.

- Policy SHA and each `source_fingerprints` value hash canonical parsed JSON,
  matching Scientist's existing contract. Source/config file hashes use raw bytes.
  Duplicate JSON fields and non-finite numbers deny.
- The policy must be enabled, use the reviewed caller/schema/pins/source maps and
  carry all three policy profiles. Selected reviewed bindings may be a subset.
  Evidence fields must occur as a pair; retained evidence requires that pair.
- Descriptor-relative no-symlink traversal, nonblocking regular single-link files
  and before/after/link metadata checks reject unsafe files and read-time drift.
  Private policy owner/permissions are checked; config/source expectations are
  copied rather than learned or updated.
- Fresh source/config sweeps, current runtime checks and final policy readback
  reject revocation or drift. There is no mtime-only cache or automatic pin renewal.

This is synchronous host verification, including inside original admission SQL.
Reads have cooperative five-second elapsed and32MiB aggregate limits, with8MiB
per-file limits and64KiB policy. These are safety bounds, **not latency evidence**;
blocked filesystem calls or trusted callbacks cannot be forcibly interrupted here.
Do not rehash model weights in repeated callbacks. Heavyweight artifact/dependency
verification needs a separately reviewed mechanism that still proves current
configuration and process generations; a historical manifest alone is insufficient.

## Acceptance boundary

The `configured_source_candidate_v1` report adds this module to the59-member
factory profile, for60 selected source members. Old profile membership is preserved.
Selected-source reports are coordination observations, not runtime admission.
Scientist must review this version pair and configured policy/source maps before
the sole Scientist GPU acceptance run. No real GPU/deployment acceptance follows
from synthetic file, policy, callback or browser fixtures.

Root evidence:208 CPU/synthetic tests passed in31.899s with ResourceWarning as
error, `data/scientist-source-authority-root-20261001.log`. Fifteen new source
tests include real private synthetic files and owned UDS factory composition,
hash/config drift, links, runtime revocation and final policy reread. Independent
review found no blocking issue after correcting policy profile/evidence-pair
shape to match Scientist. A final focused15-test run passed in0.430s after the
producer minimum was completed. No actual private Scientist configuration was read.

The Development EN/TR source-delivery panel now describes the single factory and
source verification boundaries. Its isolated headless Chromium fixture passed
with GPU disabled, `data/scientist-source-authority-ui-20261001.log`; TypeScript
`pnpm exec tsc --noEmit` also passed. The running user's UI was not deployed or
restarted and product acceptance percentages were not inflated.
