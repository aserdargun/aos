# Scientist control/terminal v2: bounded source review

## Decision and observation boundary

**Source candidate only; not jointly admitted, installed or runtime-enabled.**
The proposed integer-clock contract is materially more complete than the v1
descriptors. It is not a drop-in replacement for AOS's current admission2.0 or
terminal-v1 verifier. No AOS source, historical record, schema pin or default
admission setting changes in this review.

Observation: **30 September 2026, 21:46:30 UTC / 1 October 00:46:30 Istanbul**.
Scientist HEAD was `ebb8f5dfe1b1dc48e45fbb18cc7f42cf20bf0217`; the new adapter,
contract directory and two test files were untracked, and the control store was
modified. This is a bounded, non-atomic working-tree observation, not a claim
that the whole counterpart tree is frozen or reviewed. The draft changed during
inspection: earlier missing artifacts and missing admitted-terminal boot checks
were superseded by the source observed here.

Only public source, schemas, documentation and synthetic test source were read.
Independent hashing used stdlib JSON/file reads, not Scientist imports or test
execution. No private evidence/DB, GPU, process, socket or service was accessed.
Authored tests are not reported as executed acceptance.

| Observed Scientist file | File SHA-256 |
|---|---|
| `lab/llm/aos_control_contract_v2.py` | `d6ae113bf4b32066f89ab4312f5fafb5c3b5860528b1fbbfa3efb62425de4821` |
| `lab/llm/aos_gpu_control_store.py` | `b7a633347c48814fce191365d3e2b8b5db458cca80435fd51267151ebaa1ed5b` |
| `lab/llm/aos_gpu_control.py` | `5aa0059156acbd0fad27ad3d8cf35775032ade11fb278de274fcdf6cacebda94` |
| `lab/llm/aos_profile_output.py` | `1021e5b87dfb3154dc92ee109324f7e010ff3b55f15623859bfc455efb714b71` |
| `lab/llm/contracts/control_v2/README.md` | `5bdd07db5f449d57d49980481b5f3c2f1791ba747f41cf344a4a56bc3642ed35` |
| `tests/test_aos_control_contract_v2.py` | `c8586befc3c871f62914652e03aec6eda703dff454b90fa68cc176291d6c6705` |
| `tests/test_aos_terminal_evidence.py` | `1a1a269e8aed00dcf27aba9180238a412bb038bfaf5deb9d0c6bfd5879127b9f` |

## Actual full-schema artifacts

Directory: `lab/llm/contracts/control_v2/`. Bundle name is
`aos-scientist-control-contract.v2`, integer version2. Its exact four fields are
`name`, `version`, `schemas`, `adapter_source_sha256`. Canonical hashing is
sorted, compact, finite UTF-8 JSON with `ensure_ascii=false`, without LF; files
may carry a final LF. The bundle does not hash itself.

All eight schema hashes were independently recomputed and matched the bundle;
the adapter file hash also matched its source pin. This establishes artifact
consistency, not runtime behavior or authority.

- Bundle canonical SHA: `8dd9dbfccd2c0cc93d4ab556b06ee4b1fe21ebe85e11616dd046cae1926a65f2`.
- Bundle file SHA: `0b44b1e70e6ca917ec0046efa432e4d4848ee50a101b59c27764b6ebcba445a0`.

| Complete schema filename | Canonical SHA-256 |
|---|---|
| `request.schema.json` | `59c4b2d3f5c26aac04b481fb7b2eabb9b8b3af075b1e54fc50b1e2016522cc4b` |
| `response.template.schema.json` | `d7f5ab68e70f176a71e9ef54aafb756d3266030612d05c581f8d10cf6e7e627e` |
| `terminal.schema.json` | `d1d1ce446163138b0f9aa2510da202db6be10f51bc36f93abb1973a8ba3763aa` |
| `legacy-terminal.schema.json` | `5f45354887fdf558351376b84b1bc3fc03c83c8d0b3c7051da913c8cc146d774` |
| `admission-binding.schema.json` | `cc80bff4dc708f105aa2cb4af63e63df116f7c21358573eac754304a03e05aac` |
| `capability.schema.json` | `48ecb9a10f2ef01100cdf2c20aafef2703413f51cfbbc1c61e533d8dd2b56063` |
| `cleanup-grant.schema.json` | `372300005cc4619f97b4385687cfcc5aa8e46e65a93d0a155c6a99b909563b10` |
| `terminal-evidence.schema.json` | `aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c` |

The complete documents contain local definitions and closed object shapes. The
static result definition is deny-all, not an arbitrary-property placeholder.
`derive_response_schema(original_infer_bytes, contract, output_contract)` binds
the independent output bundle and finite request-specific Decider labels.
Its derived schema hash must be retained with the original request; it is not
the static response-template pin or the inner model-content schema pin.

## Implemented draft surface versus missing composition

The pure adapter exposes:

- `load_contract(directory, expected_sha256)`: exact eight artifacts, full
  schema hashes/equality and adapter source pin; no installation or admission.
- `validate_binding(value, contract)` and `validate_capability(value, contract,
  *, boot_id, now_us)`: shape, stable identity and bounded fresh-clock checks.
- `validate_request(raw, contract)` and `validate_response(raw, request_bytes,
  contract, *, boot_id, now_us, original_infer_bytes=None,
  output_contract=None)`: exact canonical frames and correlated typed replies.
- `validate_terminal(raw, contract)`: new terminal2 shape/hash consistency.
- `read_legacy_terminal(raw, contract)`: original v1 bytes/hash, cleanup-only.
- `validate_terminal_evidence(raw, contract, *, expected_target,
  original_infer_bytes=None, output_contract=None)`: original v1 preimage
  consistency inside the new evidence container, never physical attestation.

Wire operations remain the proposed `capability`, `status`, `cancel`,
`reconcile`; no new endpoint or operation is inferred here. Current
`aos_gpu_control.py` was unchanged and no runtime caller of the new module or
`read_terminal_evidence` was found. New test/quality-gate references are not
transport integration.

The dirty store adds `read_terminal_evidence(peer, target, profile_id,
deployment_digest, *, authority, control_request)`. It checks the exact original
row, retained admission, budget, child, preimage hashes and release evidence;
completed result bytes pass the existing stored-result verification. Final
current-authority verification precedes commit. This is useful source-side
progress, but **not a read-only SQL operation**: `BEGIN IMMEDIATE`, authorization
bookkeeping and `_remember_frame(..., "reconcile", ...)` can consume a control
ID/quota. The readonly AOS Tasks inventory must not silently call it.

## Clock and identity semantics

New control/terminal metadata uses integer microseconds in `[0, 2^53-1]` and
`{name: "CLOCK_BOOTTIME", unit: "microseconds", boot_id: UUID}`. Booleans and
float lexemes are rejected for integers. Four configured durations and six
first-assigned timestamps remain distinct; absent phases remain null.

`boottime_us(seconds)` floors the exact supplied binary-float rational;
`duration_us(seconds)` ceilings configured duration. They reject negative,
nonfinite and unsafe values. `project_budget` neither reads the clock nor
reconstructs deadlines. These are sensible conservative conversion primitives,
not permission to rescale/re-hash historical receipts or renew a deadline.

The observed binding now requires equal caller/server boots. New terminals
require recorded/server, budget/recorded and child/recorded boot agreement and
`admitted_boottime_us <= recorded_boottime_us`. Capability freshness requires
`issued <= now < expires`, at most 60 seconds, matching current boot; frame/call
limits are at most 5/10 seconds. Budget coverage and independently bounded
activation/inference deadlines remain enforced. No activation-before-inference
ordering should be added: readiness can shorten inference's assigned deadline.

Still agree the live clock producer: direct integer `CLOCK_BOOTTIME` sampling
versus conversion of retained seconds; never wall-clock or a MONOTONIC fallback.
Cross-boot recovery is a separate authenticated policy, not arithmetic over old
timestamps. `validate_response` currently checks cancellation timestamp presence,
but does not correlate `cancel_clock`/first-cancel time with trusted current boot
and time. Define and test same-boot bounds or an explicit historical cancellation
variant before those fields influence cancellation/reconciliation decisions.

## Proof preimages and fencing: what is and is not established

`aos-scientist-terminal-evidence.v2` has eight closed fields: `schema`, integer
`version`, `target`, `terminal_canonical`, `allocation_canonical`,
`drain_canonical`, `no_admission_canonical`, `result_canonical`. The latter four
are nullable. **This variant embeds original terminal-v1 strings**, not a new
terminal-v2 receipt or v2-native allocation/drain producer. JSON Schema
`contentSchema` is only an annotation; the explicit parser remains mandatory.

Allocation preimages include original request/principal/admission hash, exact
lease owner/fencing token/process generation, original deadline and configured
budget. Drain preimages carry the allocation hash, exact child or no-child
variant, boot/time, handoff stage, late-start fencing, cgroup/GPU observations
and bounded PID lists. The parser binds these values to each other; the store's
existing cleanup check additionally consults retained allocation/child state.
Never-admitted evidence requires exact original target, reason and absence of
allocation/launch state. Hash equality alone establishes none of their physical
truth, provenance or current resolver rights.

AOS must retain its independent original-budget comparison, mandatory trusted
proof callback and before/after current-resolver checks. No stop ACK, unknown
state, quarantine, missing evidence, failed cleanup or capacity observation can
clear the unresolved intent. New source export does not make those callbacks
implemented. Null-admission and allocated-without-child shapes remain outside
the current AOS verifier's accepted policy even when syntactically readable.

## Required counterpart decisions and amendments

1. **Freeze the candidate explicitly.** Publish the reviewed source/bundle pair,
   execution evidence for focused CPU tests and acceptance scope. The README's
   phrase "agreed candidate container" must not imply AOS joint acknowledgement:
   this review is source assessment, not agreement or admission.
2. **Choose the authenticated evidence delivery contract.** Response data currently
   permits observation/cleanup-observation, not terminal-evidence. Observation
   accepts terminal2, while the evidence container accepts terminal1. Specify
   which existing operation and closed response variant delivers legacy evidence,
   its capability/cleanup-grant requirements and exact retry/cache semantics.
   Do not swap a v1 response body merely because the store helper accepts its
   `reconcile` request. AOS will not invent a path, operation or envelope.
3. **Tighten identifier grammar.** `_check` uses `re.search`; hash/id/boot patterns
   end in `$` without exact length bounds. Python `$` admits the position before
   a final newline. Use exact lengths plus full-match semantics for fixed tokens,
   with mutation tests across nested pins/generations. This is a static-source
   validation concern, not an executed exploit or native reproducer.
4. **Close cancellation and tombstone clock policy.** Cover fresh cancellation
   boot/time bounds, null-admission principal/recorded boot and supported restart
   recovery separately. Do not weaken historical byte preservation to repair
   unsupported evidence. Add explicit cross-field negative fixtures.
5. **Specify independent budget provenance.** Identify the authoritative original
   assigned-deadline snapshot available to AOS, including readiness updates.
   Allocation's initial lease deadlines and terminal's retained phase deadlines
   are not interchangeable; current capability/reconnect must never mint new
   budget. Returning the terminal's own budget is not independent verification.
6. **Bind policy and proof authority outside pure parsing.** Confirm live peer,
   current resolver/cleanup rights, exact original allocation/fence and late-start
   proof provenance before and after readback. Clarify how the unchanged infer6
   request is atomically associated with the acknowledged original stable binding
   and fresh capability; a separately observed capability is not proof it was
   consumed. Keep output/infer/source/config pins independently trusted.

## Necessary AOS changes, in order; not implemented here

1. After acknowledgement, add a separate opt-in control-v2 loader/parser with
   explicit expected source, bundle and full-schema pins; retain runtime deny.
   Derive/retain original-request output schema and pin separately. Preserve
   six-field infer wire1 and three fixed profiles; free-goal remains unsupported.
2. Introduce an explicitly versioned new admission representation for control2,
   terminal2 and integer freshness. Current **record2.0 means profile-output2
   with control1/terminal1**, not control2. Do not loosen its model, reinterpret
   old pins or rewrite used migrations/append-only records. Preserve exact legacy
   four-field and interim readonly identities. Agree the new record version first.
3. Resolve stable-hash canonicalization explicitly for new records: AOS history
   currently uses ASCII-escaped canonical JSON; Scientist wire uses UTF-8. Current
   terminal verification rejects disagreement. Preserve old hashes; never silently
   normalize non-ASCII identities or use a permissive version-detection fallback.
4. Add the agreed authenticated control/evidence adapter, exact durable control
   intents and bounded uncertainty handling. Read legacy evidence bytes unchanged
   through the existing terminal-v1 verifier and separately provided trusted proof
   and resolver callbacks. No bare parser success or readonly UI action resolves.
5. Only after original budget/proof/current-authority agreement, design additive
   atomic resolution observations against the original unresolved row. No automatic
   retry, replay, new admission or unfencing from a terminal hash. Scientist alone
   runs the eventual coordinated real GPU acceptance.

The existing AOS terminal module remains file SHA
`43f6d62b919ff471bdb49aa8a48d1fd46977d052570fd25b6e63165c3b6c45ef`;
its schema remains file SHA
`bc71cd129f0df3ebc09d41e96dfe17be8b89e7929947ebaf52aaa1bb96a9c4bd`.
Its v1 descriptor pin remains
`4d4981728ddaae6c9b8f0fd0b64cd0ff07374f59d864c1b9db64dccdc37584c2`,
and separate full local canonical schema pin remains
`5c3dd6fcffcf773dd13c2f64e56c4c539435d5b51153e3d997d1aee12aa7ddc2`.
No integer wire version is added to those v1 receipts. See
[the existing verifier boundary](SCIENTIST_TERMINAL_EVIDENCE.md) and
[original admission history](SCIENTIST_ADMISSION_HISTORY.md).

## Addendum: explicit evidence transport, 30 September 22:04 UTC

This addendum preserves the earlier observation rather than rewriting it.
Scientist HEAD is now `15b6c8f548f324610c805c24279973d83dc58e02`, which commits
the original store exporter and control-contract candidate. The new evidence
codec, transport tests and control/store integration below were still working-tree
changes during this review. Public note86 describes the preceding committed
source delivery; its CPU execution counts are counterpart attribution, not tests
run by AOS. Its historical "socket v1 unchanged" statement is not evidence about
the subsequent dirty bridge.

Bounded source hashes observed at **21:57:28 UTC / 00:57:28 Istanbul**:

| Scientist source | File SHA-256 |
|---|---|
| `lab/llm/aos_evidence_transport.py` | `ef4f65eea67806b6670a8dafecf7ceb545f07238667983b916a1bfa3e8e6c2df` |
| `lab/llm/aos_gpu_control.py` | `dd054d8a7f39df33df18c872a03a7117a4978f551b31eeb8ca9ab8a87808286d` |
| `lab/llm/aos_gpu_control_store.py` | `a3aec8764030a8bbcc7580c84d11dbcf9656c44ef35f898d11c919e82f140c11` |
| `tests/test_aos_evidence_transport.py` | `bc94e7d2a7d7ffeb895ac7bb76a78b6f3c022927f96e60a56ff1909bedc0731f` |
| `tests/test_aos_evidence_integration.py` | `31d337c4032d94a7f6bfb76a6b7617eebb0146ea3512391045f695055667c254` |

The adapter source and all eight control-contract artifacts still matched the
earlier bundle hashes. The new, separately published complete transport schema
is `docs/ai-scientist/contracts/evidence-transport-v2.schema.json`:

- File SHA: `0a18309a4c19824b7753179220eda840e7a8a545aea09b54023cfcce15737f99`.
- Canonical transport SHA: `7e76687f7f0e3e4f8f5dba4d0edbc4d70dbba192f567f92d373b80b056fb12b7`.
- Independent evidence schema canonical SHA remains
  `aa9fd4ea32d480f097b1c79c62fcba1e11ade062bea58d29e575f010c0ed259c`.

Those public schema bytes were independently re-read at **22:04:13 UTC** and
validated by AOS-only code with synthetic request/response bytes. No Scientist
module was imported or executed, and its tests were not run by this review.
Observations remain bounded and non-atomic; no counterpart deployment is inferred.

### Concrete bridge contract

The namespace is **`aos-scientist-control-evidence.v2`**, integer version2,
not `aos-scientist-control.v2`. Operations are only `capability` and `reconcile`.
Its exact ten request fields are `schema`, `version`, `op`, `control_id`,
`profile_id`, `deployment_digest`, `target`, `expected_capability_sha256`,
`evidence_schema_sha256`, `transport_schema_sha256`. Target is mandatory and
contains the original request ID/hash and original peer-generation hash.
Capability requests require null expected capability; reconcile requires the
retained capability hash. Canonical UTF-8 payload excludes the framing LF;
request plus LF is bounded by 8192 bytes, response plus LF by 128 KiB.

The closed response fields are `schema`, `version`, `control_id`, `op`, `ok`,
`capability_sha256`, `data`, `error`. Successful capability data contains the
**original v1 target/cleanup capability**, both schema pins, `admission: denied`
and reason `retained_target_only` or `cleanup_only`. Its original capability hash
and seconds-based freshness are unchanged. Successful reconcile data is the
eight-field terminal-evidence container with original v1 canonical preimage
strings. Error variants carry null data/capability and closed code/retryability;
retryability metadata does not authorize AOS retries.

Current `LabAOSControl` adds an explicit dispatcher on its existing authenticated
control connection. It requires both policy keys `evidence_schema_sha256` and
**`evidence_transport_schema_sha256`**; the latter differs deliberately from
the wire field `transport_schema_sha256`. Partial/mismatching policy pins deny.
Enabled policy requires the new codec, adapter and complete bundle/schema source
dependencies in the Scientist selected-source map. Current peer authentication,
policy checks, 5-second frame and 10-second call limits remain in the existing
server; no new listener/path is established by this review.

Capability uses the existing durable `_mint_target` path. Reconcile accepts only
capabilities found in the original target-capability table, not bootstrap infer
capabilities. It creates current retained/cleanup authority and supplies a full
response framing validator to `read_terminal_evidence` **inside** the transaction,
before control-ID reservation and final authority verification. Oversize or
malformed reconcile envelopes therefore roll back that transaction. Exact IDs
remain bound to exact request hashes and shared quota. The codec intentionally
does not treat an expired historical capability reply as fresh; authority checks
at use-time still govern export. This source integration addresses the earlier
missing transport/callsite gap without adopting control2 admission or clocks.

### Implemented AOS slice and verification

`src/aos/scientist_evidence_transport.py` now provides a separate codec only:

```text
ScientistEvidenceCodec(reviewed_schema_bytes, *,
    transport_schema_sha256, evidence_schema_sha256)
encode_request(value) -> canonical payload bytes without LF
decode_request(raw) -> validated request
decode_response(raw, original_request_bytes) -> correlated transport metadata
```

Both pins and complete reviewed schema bytes are explicit trusted configuration;
there is no default enable, socket, clock observation, callback, journal mutation,
resolution, replay or inference. Local-only references are checked even in
unreachable definitions; object schemas must be closed. JSON Schema validation
uses strict integer lexemes, while exact token patterns reject trailing-newline
identities. Request/response correlation, capability hash/target, cleanup original
binding/hash and scope, error variants and immutable canonical embedded strings
are independently checked. Returned metadata does not attest freshness or proof.

`schemas/scientist_evidence_transport.schema.json` retains the exact reviewed
public artifact bytes; the constructor still requires explicit pins and bytes.
`schemas/scientist_evidence_transport_request.schema.json` is the separate local
request schema, not a substitute for the full transport pin. Synthetic fixtures
in `tests/test_scientist_evidence_transport.py` exercise both retained and cleanup
capabilities, Unicode/float preimages, unknown fields, lexical integer mutations,
wrong hashes/targets, malformed/oversized/noncanonical JSON and forbidden refs.
A deliberately nonphysical preimage is accepted only as transport, making the
proof boundary explicit rather than pretending the codec verifies a GPU.

**12 focused tests passed in 1.012s**. Combined codec12 + terminal15 + admission24
+ output8: **59 CPU tests passed in 2.270s**, with ResourceWarning treated as an
error. Log: `data/scientist-evidence-codec-20261001.log`. This overlaps the focused
run; do not add their counts. A separate offline check used the exact live public
schema file through the AOS codec for synthetic capability/reconcile frames.
That is schema interoperability, not bilateral authenticated runtime acceptance.

### Remaining bounded actions

1. Freeze/acknowledge the bridge's source/schema pair and test evidence. The pure
   codec trusts supplied reviewed pins, not a schema fetched from an untrusted
   response. Neither source delivery nor local validation enables runtime.
2. Add an explicitly configured AOS authenticated control client only after the
   exact endpoint/unit/peer and current retained-target/cleanup policy are agreed.
   Preserve durable control intent, bounded deadlines and uncertainty; never use
   an evidence capability to run inference or silently refresh the old admission.
3. Compose original `terminal_canonical`/`result_canonical` UTF-8 bytes with the
   unchanged terminal-v1 verifier. Separately parse and authenticate allocation,
   drain/no-admission preimages against original owner/fence/child and independent
   retained assigned budget. Provide its mandatory current-resolver and physical
   proof callbacks; do not replace them with codec success or a matching hash.
4. Review the bridge's unconditional module import/dependency behavior: even with
   evidence policy absent, current control source imports the new codec, which
   loads bundle artifacts. Packaging and trusted source closure must account for
   this; policy opt-in alone does not make the added import dependency optional.
5. Preserve metadata uncertainty if capability output validation fails after its
   durable mint. Unlike reconcile's new in-transaction validator, capability
   framing currently follows `_mint_target` commit. Confirm/test exact-ID recovery
   without duplicate grants or treating that failure as no side effect.
6. Only a later separately approved atomic resolution design may modify the
   unresolved fence. Cross-boot, null-admission and allocated-without-child policy
   gaps remain explicit. Scientist stays the sole real GPU acceptance executor.

No admission record version change is required merely to carry this bridge:
it intentionally retains admission-record2.0, profile-output2, control1,
terminal1 and seconds capability clocks. The earlier proposed **new native
control2/terminal2** migration remains a different, unimplemented decision.
