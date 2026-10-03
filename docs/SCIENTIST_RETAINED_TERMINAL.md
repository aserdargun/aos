# Retained budget and terminal release verification

This is a source-only, read-only AOS adapter. It neither resolves the original
inference journal nor grants GPU ownership. Native/GPU acceptance remains with
the Scientist session; no running user work is stopped by these adapters.

## Counterpart contract proposal

Keep the existing terminal-v1 and evidence-container canonical bytes unchanged.
Deliver `aos-scientist-original-budget-witness.v1` from independently retained
Scientist intent/allocation/readiness rows, alongside the existing evidence.
Bind the original request hash, caller generation, profile/deployment/config,
response schema, original admission hash and allocation hash. The current
Scientist `ControlStore.read_original_budget` method is a trusted in-process API,
not an AOS socket endpoint. AOS must not read Scientist's private database.

Scientist's evidence-v3 wrapper is separately opt-in, not automatically enabled
runtime authority. Its nullable cleanup-only witness variants are intentionally
unsupported by AOS history2.0. Transfer/review the complete public schema and pins;
do not silently change evidence-v2 or map a new source profile to an old one.

## Explicit retained evidence-v3 transport

Namespace `aos-scientist-control-evidence.v3`, integer3, keeps only `capability`
and `reconcile`. Reconcile data is exactly `{evidence, original_budget_witness}`.
The reviewed full public schema's canonical SHA is
`cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070`;
inner evidence schema remains
`aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`.
Use the supplied authenticated control socket; this adapter creates no listener.
Scientist private policy must separately pin
`retained_evidence_transport_schema_sha256`; AOS does not write that policy.

The separately constructed `ScientistRetainedEvidenceCodec` requires the exact
retained target-capability preimage for successful reconcile decoding. A matching
capability hash supplied alone is insufficient. The original terminal clocks and
v1 admission hashes are not rewritten, and v2 requests/pins remain v2-only.
Its wire validation is structural, not current authorization or physical proof.

The same authenticated `ScientistEvidenceClient` and original-store
`ScientistEvidenceJournal` accept the explicitly selected codec. Additive0025
extends the same immutable ledger with exact namespace/version2 or3 pairs and
matching request/response versions. It preserves historical24 row bytes and
the cross-version same-target pending fence; it adds no allocation authority.
The journal ACK still does not settle the original inference request.

`ScientistRetainedTerminalVerifier.verify_response` decodes only a successful
v3 reconcile through that codec, then runs the independent retained budget and
release-proof composition. Capability discovery and error frames are not terminal
evidence. Production callbacks are still required; a synthetic broker cannot
prove Scientist runtime or actual GPU release.

## Implemented composition

`ScientistBudgetWitnessVerifier` accepts strict canonical bytes, checks the full
local schema pin and exact immutable admission history, then calls a mandatory
independent current-source verifier before returning the typed original budget.
The budget cannot be copied from the terminal under examination. Hash agreement
alone cannot satisfy the trusted-source callback; it is denied by default.

An optional trusted `read_source(request, original)` callback now returns the
complete independently retained witness dictionary. The existing verifier
strictly parses/bounds it and compares every canonical field with the candidate,
including original target/principal, profile/config/response pins, admission,
allocation and assigned budget. Current `verify_source` authorization remains
mandatory/default deny, before and after each of two independent reads. Inputs
are detached copies and the original immutable history is rechecked. Configuring
a reader alone cannot grant authority; supplying no reader preserves the existing
trusted verifier contract, not an automatic Scientist connection.

Scientist commit `abaaef6633414aa58e8da73d38980ed4c059f2bf` supplies the trusted
in-process `LabAOSControl.read_original_budget(peer, target, profile_id,
deployment_digest, expected_capability_sha256=...)`. Its signature is not the AOS
callback signature. A trusted provider must authenticate the actual current peer,
derive the target from the original request/hash/caller generation, and explicitly
bind the exact successful retained-target discovery ACK capability hash. An
inference capability or admission hash cannot substitute. Errors/revocation remain
fail-closed; the read must not allocate, resolve, refresh budgets or create control
IDs. No public endpoint, cross-process provider or private database read is added
by the AOS callback seam. Production provider/config agreement remains open.

`ScientistReleaseProofVerifier` parses the explicitly pinned full public evidence
schema and exact allocation/drain/no-admission canonical preimages. It checks
cross-bindings, then composes the existing terminal/profile-result verifier.
Its physical callback must independently verify allocation owner/fencing and
actual child/GPU cleanup; a quiesce flag, hash or successful control response is
not such evidence. Resolver and physical providers remain mandatory/default deny.

`ScientistRetainedTerminalVerifier` composes both adapters. It compares the
allocation hash with the independent witness before release verification and
rechecks the retained budget source after physical verification. Each callback
receives copied typed inputs. No callback may renew budgets, admit inference,
resolve journals or pretend synthetic process lists prove actual GPU release.

## Concrete Linux physical observer

`ScientistPhysicalReleaseVerifier(gpu_uuid=..., verify_source=...,
verify_current=..., allow_shared_lanes=False)` is a callable for `verify_physical` in the existing release
composition. Both gates default deny; source receives original/terminal/allocation/
drain/no-admission plus the explicitly configured GPU UUID. It must independently
bind original source, fencing token, child intent nonce, device and irreversible
late-start fencing; current must authenticate the present host authority.
Hash/flag equality cannot implement either callback. They run before and after
fresh bounded OS observations, with detached inputs.

The observer checks exact inactive unit invocation/cgroup/MainPID, or repeated
explicit systemd not-found for an independently fenced collected original child.
It verifies same boot, original current UID, process-generation absence, readable
cgroup2 mount, recursive cgroup emptiness and UUID-scoped NVIDIA compute absence.
Ambiguous unit/cgroup/query/permission data denies. No lease is created/released,
worker stopped/restarted, or GPU test launched by constructing it.

Default policy requires a quiet compute handoff and rejects every CUDA context.
Explicit trusted-host `allow_shared_lanes=True` allows unrelated contexts but
rejects the exact original child PID and all immutable drain-recorded GPU PIDs.
The source gate must independently establish recorded PID provenance, original
allocation no longer active or quarantined, and nonregressing canonical arbiter
fence. Two GPU reads bracket repeated OS cleanup checks. Device and mode are
read-only public properties, captured per verification with mutation guards.
Graphics/display VRAM is not treated as zero; numeric memory observations are
telemetry, not an ownership grant.
Never-admitted/never-started and PID-reuse cases remain unsupported here. Use the
existing explicit proof contract with independently reviewed providers for those
cases; do not substitute idle flags. Actual acceptance must be run only by
Scientist after source/provider/config agreement. `bounded_process.py` belongs
in the reviewed dependency closure; selected55 source observation alone is partial.

## Explicitly still open

- Mutually reviewed source/schema/config pins and authenticated retained-budget transport.
- Production retained-target authority, current resolver and physical readback providers.
- Authenticated cross-process independent source reads and immutable original GPU UUID provenance; current Scientist snapshot v1 has neither transport nor device binding.
- Production wiring of implemented durable original inference resolution; abandoned-control reconciliation remains unsupported.
- Scientist-only coordinated real GPU acceptance and cancellation/recovery proof.

The explicit AOS `retained_evidence_candidate_v3` observation includes 52 selected
files, extending the older48-file release-proof profile with the v3 codec/schema
and additive25. It is not capability, authorization or deployment. Older source
profile membership remains unchanged. CPU synthetic tests validate parsing and
callback sequencing, not actual Scientist execution or GPU cleanup.
