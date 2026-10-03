# Scientist shared-launch host adapter

Implemented source, **production disabled**. This is the AOS bridge to the
separately reviewed Scientist proposal2 client, not a new scheduler or issuer.
`ScientistSharedLaunchAdapter` supplies the existing host's `verify` and `claim`
callbacks without importing a sibling checkout, choosing an endpoint or
starting a service. No CLI/UI route enables it automatically.

## Exact candidate boundary

Observed wire: `aos-scientist.shared-launch.transport.v1-proposal2.draft`,
descriptor SHA256
`842fe08b2f7f7dbb1f0d0bcf000a5d3029eb4335114da800324d0e34952478f6`.
The canonical AOS-local review schema is
`schemas/scientist_shared_launch_review.schema.json`; its example is explicitly
synthetic. This local DTO is not a replacement for Scientist's binding/policy
schema and does not issue remote authority. Unknown transport hashes fail closed.

Trusted deployment composition must independently review/pin the actual loaded
Scientist client and complete dependency closure, obtain its descriptor hash,
and supply that client plus the typed original local review. Passing a hash
argument is not proof that an arbitrary Python object implements authentication.
The reviewed Scientist client must authenticate the real current broker on each
exchange using its original systemd/process generation and kernel credentials.
No no-op authenticator is acceptable outside explicitly synthetic tests.

The review binds the original request/binding digests, exact plan/activation
file paths and hashes, provision hash, existing manager process including PID
namespace, same-owner broker generation and original BOOTTIME interval. The
local interval cannot exceed900seconds or outlive activation. A new CLI process
cannot silently inherit the original manager's authority.

`SystemdSharedDesktopTransport(scientist_root=reviewed_root)` still supplies the
reviewed source layout. `SharedDesktopHost` must receive the adapter's `verify`
and `claim` callbacks explicitly; predecessor, native exclusion, cleanup and
remaining production prerequisites retain their separate default-deny gates.

## Read-only verify and one claim

- `verify` re-reads pinned plan/activation, checks the original manager/broker/
  deadlines, calls the client's read-only `request('verify', ...)`, and checks
  response identity, strict status flags and the original call deadline again.
- `claim` additionally binds the starting state to the exact review and reads
  the existing durable provision/launch intent. It sends only the original
  request/binding/intent digests, never caller-selected remote paths or grants.
- Calls are serialized without waiting. Each uses at most3seconds and cannot
  extend the original BOOTTIME window. One claim attempt is retained in memory;
  the existing host's durable intent prevents retry after a process crash.
- Only a nonexpired consumed result with `consumed_now=True` returns `None` to
  the host. Duplicate status, lost reply, foreign request, revoked state, late
  reply or a rollback to reviewed/unconsumed state cannot authorize spawn.
- Read-only verification after claim must still observe consumed state.
  No status or cleanup method here manufactures another claim or GPU release.

## Evidence and limits — 3 October 2026,21:14 UTC

113 focused AOS CPU/synthetic tests passed.12 adapter cases cover the real host
callback sequence, manager/source/client drift, missing/changed transport pin,
duplicate claim, lost ACK, late reply, malformed/revoked status, concurrent call,
foreign durable state and consumed-state regression. Schemas/examples are checked.

An additional private harness exercised the **actual Scientist client/server
framing code and Linux Unix-stream credentials** against the AOS adapter:
fresh claim, duplicate claim and lost ACK,3 PASS. Systemd generation, policy and
claim authority were synthetic fixtures; no real unit, ledger issuance, GPU or
model ran. This is source interoperability evidence, not authorized deployment.
The first harness lacked the server's required contract pin and failed closed;
it was fixed in the synthetic controller, without weakening production checks.
Socket cleanup was also corrected before the passing ResourceWarning-strict run.

Private evidence: `/tmp/aos-shared-launch-wire-20261003-9dopDe/`.
The tested Scientist transport SHA was
`a5aa135c896bffd965b9564b5ea4ffda76c046dde4b927673f04c8428670b881`;
dependency hashes were checked before/after and recorded privately. Scientist
files were read-only; bytecode writes and cached peer bytecode were disabled.

Remaining: reciprocal freeze of exact wire/policy/full source pair, real broker
listener composition, independently authenticated policy/prerequisite verifier,
consumed-claim in-unit and per-inference checks, and original-target physical
cleanup/reconciliation. The adapter intentionally does not implement `enter`,
`close_launch` or unknown future operations by guessing wire fields. Scientist
alone executes any separately admitted integrated GPU acceptance. **GPU HOLD.**
