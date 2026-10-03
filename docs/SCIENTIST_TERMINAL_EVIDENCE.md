# Scientist terminal evidence candidate

This is a source-shaped, default-denied verification adapter, distinct from the
opt-in evidence control client, resolution journal or jointly admitted runtime.
Scientist remains the sole allocation authority and coordinated GPU test runner.

## Two separate pins

The current Scientist producer emits `aos-scientist-terminal.v1` with no integer
version field and boot-scoped seconds. Its descriptor hash lists the emitted
fields; it is **not** a complete recursive JSON Schema. AOS therefore requires
both the explicit descriptor pin and a separate complete local evidence-schema
pin. The local schema is `schemas/scientist_terminal_evidence.schema.json`.
It is a proposal to confirm with the counterpart, not a replacement descriptor
silently inserted into an existing original admission.

Descriptor SHA:
`4d4981728ddaae6c9b8f0fd0b64cd0ff07374f59d864c1b9db64dccdc37584c2`.
Full local evidence schema canonical SHA:
`5c3dd6fcffcf773dd13c2f64e56c4c539435d5b51153e3d997d1aee12aa7ddc2`.

`ScientistTerminalVerifier` requires original history2.0, independently retained
expected original budget, and explicit trusted callbacks. It never opens an
invented endpoint, probes a GPU, starts/stops a worker, modifies an intent,
unfences a task or adopts historical ownership.
Expected budget must not come from the terminal being verified. The legacy
control projection does not supply an independent retained budget source, and
the new evidence wrapper's original-budget content does not become independent
merely because it is transported over an authenticated socket.

## Original request and result binding

The verifier requires exact bounded canonical UTF-8 terminal/result bytes.
Duplicate/nonfinite JSON, unknown fields, malformed child identities and wrong
hashes are rejected. Wire canonicalization follows Scientist's `ensure_ascii=false`
semantics, retaining Unicode content without normalizing recorded bytes.

The original request hash, principal/caller generation, profile/deployment,
config/content-schema pins, complete original admission binding/hash, budget,
boot and deadlines must match. A mismatch between the original AOS stable hash
and Scientist wire-canonical binding hash is rejected, not rewritten. In
particular non-ASCII generation paths require canonicalization agreement before
joint admission; existing AOS history bytes/hashes remain unchanged.

Completed results must match the terminal result hash and independently bound
child unit/invocation/PID/cgroup, then pass the injected original pinned profile
validator. A noncompleted outcome cannot publish a result. Unsupported outcome,
state, reason or insufficient child/allocation evidence fails closed.

## Physical release is a separate trust boundary

Legacy control replies expose terminal/result and evidence hashes, not the
allocation/drain proof preimages. The separately pinned opt-in
`aos-scientist-control-evidence.v2` wrapper now transports exact canonical
preimages from Scientist's retained records without upgrading v1 authority.
Possessing those preimages or matching hashes is still insufficient to
prove that a worker exited, late starts are fenced, its cgroup is empty, or GPU
resources are absent. Mandatory `verify_proof` must independently verify those
facts against the original owner/allocation/fence/child, or a never-admitted
target with no allocation/launch. It is denied by default.

Mandatory `verify_resolver` independently authenticates current resolver rights
before and after the proof callback. Historical admission is not current cleanup
authority, and a rotated policy or restarted caller cannot inherit rights from
this adapter. Completed results additionally require `validate_result`; its
default also denies. Trusted callbacks receive deep copies and must return
`None` or raise, never an unverified success flag.

Null-admission tombstones, allocated-without-child receipts, cross-boot recovery
and caller-restart authority transfer are not adopted through this path.
Cleanup failure/quarantine, idle, stop ACK or hash-only control responses cannot
clear AOS's unresolved intent. A successful CPU fixture is not real GPU release.

The control client must authenticate the current broker generation, persist
the exact control request before dispatch and recheck current target authority.
Both capability minting and reconcile can have durable remote effects;
`retryable=true` is not permission to repeat an ambiguous request. Decoder/client
success remains separate from physical proof and append-only journal resolution.

## Source preflight and remaining gates

`scripts.scientist_source_report --source-profile terminal_candidate_v1` extends
the explicit admission2.0 selected-source scope with this module and full schema.
An expected selected-source pin can reject changes before runtime composition;
the report still grants no admission or release authority.

Confirm the complete local evidence schema and exact source/config pair with
Scientist, provide authenticated proof/readback and cleanup rights, agree full
control/terminal clock/version semantics, then implement append-only resolution
without rewriting0018 or releasing the unresolved fence on an uncertain result.
Only Scientist runs the eventual coordinated native GPU acceptance.
