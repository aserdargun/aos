# Scientist CPU capability adapter

This additive adapter supports the separately reviewed
`scientist.lab-cpu-capability.v1` contract. It is not a replacement for the
local-model capability, GPU admission, native exclusion or human approval.
The supported profile is explicitly `scientist-cpu-mode-grid.v1`: synthetic
mode-grid research, `mode-grid.v1`, bounded experiments/wall time and exactly
zero model tokens. A zero-token request alone never selects this profile.

## Source composition

`src/aos/scientist_cpu_capability.py` provides:

- `ScientistCpuCapability`: strict parsing of the separate wire response.
- `ScientistCpuReviewedGrant`: an explicit trusted review binding the expected
  capability and its hash to an authority, principal and authorization context.
- `ScientistCpuCapabilityVerifier(client, grant)`: a callable for the existing
  `ScientistLabService(..., verify_capability=verifier)` hook.

`create_scientist_cpu_service(controller, client, grant)` in
`src/aos/scientist_cpu_session.py` provides the concrete composition for a supplied
`DesktopController` and its store. It requires a fresh, unbound, inactive client
with only the exact reviewed suite; it installs the verifier and the existing
Lab service's approval, intent and readback callbacks. Construction performs no
remote request, creates no runtime and grants no approval. The supplied service
can be passed to the existing `create_console(..., scientist_lab=service)` path.
Authenticated console routes still require a matching explicit approval before
POST and persist the intent before dispatch. Per-control deadlines and bounded
local close are inherited; closing the client does not prove remote cleanup.

The canonical AOS schemas are
[`scientist_cpu_capability.schema.json`](../schemas/scientist_cpu_capability.schema.json)
and [`scientist_cpu_reviewed_grant.schema.json`](../schemas/scientist_cpu_reviewed_grant.schema.json).
The [example](../examples/scientist_cpu_capability.json) is explicitly synthetic.
The reviewed capability owner must equal the configured principal; mismatched
owner/token configurations are rejected before a readback request.

The same configured `ScientistLabClient` must be used by the service and verifier.
Startup wiring must construct the reviewed grant from operator-reviewed private
configuration; it must not derive one by trusting whatever an endpoint returns.
Installing the verifier does not authorize an action or persist its intent.
Existing task/policy, controller generation, human approval, durable journal and
independent report verification remain required and unchanged.

Each verification issues an authenticated, bounded read-only
`GET /v1/aos-cpu-capability/{suite_id}` using the existing loopback transport.
It verifies the exact owner/suite/program and all suite, entry, provider,
snapshot and CPU-grant hashes, budget ceilings and unchanged transport/task
bindings. Unknown versions, additional fields, duplicate JSON keys, changed
capabilities, expired deadlines and read failures fail closed. All four
allocation, GPU-release, native-inference and launch flags must be literal
`false`; model tokens must be integer `0`, not Boolean `false`.

The reviewed source-manifest hashes are **review metadata**, not proof of the
running remote process or its imported code. The capability endpoint does not
attest those manifests. A separate exact source/config/runtime review is needed
before running the joint acceptance; merely filling in these hash fields does
not perform that review or grant deployment rights.

## Explicit desktop startup composition

The trusted Python entrypoint now accepts
`scripts.serve_desktop.main(scientist_lab_config=config, scientist_cpu_grant=grant)`.
`config` must be a `ScientistLabStartup` and `grant` a separately reviewed
`ScientistCpuReviewedGrant`. This is not a new unauthenticated CLI switch or an
automatic loader of endpoint claims. A deployment owner supplies both typed
objects after reviewing the actual source/config/runtime pair and credential.
Do not construct the grant by accepting whatever capability an endpoint returns.

The process arguments must select `--engine fixture`, an explicit non-default
`--workspace`, `--database`, `--console-assets-root` and a port other than 8765.
Vision is disabled or explicitly fixture-only. Native Decider/Bonsai, prewarm,
owned skill reuse, shared broker hooks and mixed capability verifiers are
rejected before creating the desktop. Existing runtime ownership, source pins,
path checks and session cleanup still apply; this hook is not deployment consent.

`prepare_scientist_cpu_startup` revalidates the grant and startup objects, private
credential and exact authority/principal/single-suite/program/context match.
It makes no remote request. After the existing controller starts, the same
prepared client and reviewed grant are bound through `create_scientist_cpu_service`
to the actual authenticated console. The existing lifespan closes local controls;
it does not stop or prove cleanup of a remote Scientist experiment. Per-action
fresh capability verification, human approval, intent and independent readback
remain mandatory. Inventory still reports `joint_runtime_admitted: false`.

This separate CPU fixture path is deliberately unavailable in the shared-only
promotion candidate: that candidate continues to require `--engine scientist`
before CPU startup preparation. Its patch and source hashes were reviewed and
rebased for this addition, not applied to the live source. No source check gives
the default service this new configuration. Actual remote/UI acceptance is still
separate from the synthetic desktop startup and ASGI tests.

## Console request limits

The CPU service adds `request_limits` to its authenticated inventory response:
`profile: scientist-cpu-mode-grid.v1`, `source: reviewed_configuration`, exact
`suite`, `track: mode`, `max_experiments`, `max_wall_seconds` and `model_tokens: 0`.
These are local reviewed configuration values, not a fresh capability observation,
experiment admission, GPU reservation or a new Scientist wire contract. Reading
inventory makes no remote request and still reports joint admission as false.

The EN/TR panel offers only the CPU track and validates explicit budgets against
these limits before enabling a proposal. A refreshed lower limit invalidates an
already entered oversized budget. Unknown profiles, malformed limits and suite or
program mismatches disable new proposals instead of falling back to wider limits.
Existing approvals/results remain separately visible. Older non-CPU inventory
without this optional metadata keeps the existing protocol-range form and warning;
it is not inferred to be a CPU grant. The server remains authoritative and rejects
invalid budgets even when a caller bypasses the UI.

## Contract identities

Optional context controls additionally require the local backend inventory to
advertise `supported_context_fields: [field_intent, prior_experience]`. This is
local DTO/UI compatibility metadata, not remote eligibility or execution authority.
An older backend omits it: the new UI hides those inputs and sends the legacy
request shape. If support disappears while a context draft is selected, the draft
cannot be submitted silently without its context. UI-only promotion does not
upgrade the running backend or install a CPU grant.

The reviewed Scientist wire schema SHA-256 is
`44c142fa51200e846971df8e57d6c83b0fe18bf1694eb8c4835471cc521b750e`.
The isolated Scientist CPU source candidate has manifest
`2a3f9fca0fa82c07950dbb7433880b22ba4ea327b569452ec6f9170bb41eb3bb`,
based on commit `384f05213fb71997dd2899a0687b73cd4f2080b6`.
AOS independently rehashed all twelve listed candidate files; this is source
review, not evidence that the candidate runs in the existing Scientist service.

CPU capability hashes use JSON with `ensure_ascii=False`, `sort_keys=True`,
`separators=(',', ':')`, `allow_nan=False`, encoded as UTF-8. This contract is
separate from existing AOS maintenance/native-exclusion hashes, which retain
the established escaped-ASCII JSON canonicalization. Do not globally change
`aos.contracts.canonical` to accommodate the CPU capability.

## Acceptance boundaries and next steps

Focused tests exercise strict response parsing, owner/hash/budget/deadline
rejection, the existing transport request shape and the real service/journal
approval boundary using synthetic observations. Their dated results are in
[STATUS](STATUS.md); mocks do not establish a live Scientist experiment.

On 3 October 2026, a separately reviewed isolated CPU/API/Scorer run completed
through this composition and its independent status/report/status and retained
report path. The controller/decision/approval driver was explicitly a fixture;
the remote API, experiment and Scorer were real. The exact one-experiment,
600-second, zero-token acceptance and private evidence references are recorded
in [STATUS](STATUS.md). This does not establish deployed user-interface or model
decision acceptance. A second controlled run verified a pipeline/baseline-boundary
stop, a separately approved repeat stop, terminal recovery and independent retained
`stopped` report. Durable Scientist SQL timestamps put the observed score job's
completion before both the trigger and stop event: active-Scorer interruption
is **unproven**, despite the private helper's earlier overbroad success flag.
The original evidence is preserved; the corrected classification and exact-owned
remote cleanup evidence are recorded separately in [STATUS](STATUS.md).
Optional `field_intent` and `prior_experience` forwarding is now implemented in
the isolated source checkout through typed DTOs, service, approval-bound request
and EN/TR proposal controls. [Task context](SCIENTIST_TASK_CONTEXT.md) documents
the strict contract and manual-reference limits. This does not deploy the panel
or verify a real run with selected history; the earlier CPU acceptance exercised
neither new optional field. Web specialization and control-panel deployment
remain subsequent integration work. Synthetic ASGI tests exercise the real console routes
with a fixture controller and mocked Scientist responses; they are not a real
user, model or Scientist experiment acceptance. The native/GPU acceptance remains
a separate gate.
