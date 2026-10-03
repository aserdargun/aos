# Scientist three-profile output contract proposal

Observation: 30 September 2026, 19:57:40–19:58:01 UTC. **Design only; no
joint agreement, installed schema, deployed adapter or runtime admission.**
This focused response to Scientist note 78 supplements, rather than replaces,
[the historical control review](SCIENTIST_CONTROL_RESPONSE.md).

Only public documentation, synthetic contract examples and source were read.
No private evidence, database, process or GPU was inspected or operated. The
35 counterpart examples were read as authored bytes, not executed acceptance.
No Python, worker, schema, migration or admission flag changes accompany this
proposal. The existing three profile IDs and six-field integer-wire1 infer
request remain unchanged; free-goal Supervisor inference remains unsupported.

## Observed source, not assumed closure

| Surface | Actual behavior |
| --- | --- |
| `scientist_protocol.py` | Closed infer/receipt envelopes and generation; nested `response` and `usage` remain dictionaries. Strict framing, duplicate-key and finite-value checks do not themselves close those dictionaries. |
| Decider broker worker | Returns `deployment_digest`, `prediction`, `metrics`; adds `broker_activation_load_ms` to the model session's seven metric fields. Prediction keys are request option **IDs**, mapped from model labels. |
| `scientist_decision.py` | Checks deployment/correlation, typed Prediction, exact option membership and probability sum. Metrics remain a dictionary. |
| Bonsai broker worker | Forwards the raw OpenAI-compatible server object after its single-completion/stop check. Provider metadata and extra fields are not currently projected away. |
| `scientist_supervisor.py` | Reads one stop completion, assistant content, rejects duplicate/nonfinite inner JSON, then calls the native RecoveryPlan or VisionScene parser. Outer provider dictionaries remain open. |
| Scientist `_profile_payload` / `_validate_bonsai_body` | Fixed role-specific request shapes; Bonsai request's inner schema hash must equal `profile.response_schema_sha256`. |
| Scientist `_extract_usage` | Decider copies all metrics; Bonsai extracts exactly prompt/completion token counts, with nonnegative integer checks. It does not establish recursive response closure. |

The existing `response_schema_sha256` therefore has an important meaning:
for Bonsai it pins the **inner requested content schema**, not an OpenAI wrapper
or complete `{response,usage,generation}` schema. Reusing that field for a new
wrapper hash would break current request validation and silently change identity.

## Explicit opt-in and proposed artifacts

Proposed output contract identifier: `aos-scientist-profile-output.v2`, integer
version 2. The independent version distinguishes it from current legacy output;
it does not rename the three fixed model profiles or change infer wire1.

Add a closed `output_contract` pin to the future profile configuration and its
stable admission/profile pin: exactly `{name,version,bundle_sha256}` with the
name above, integer version 2 and lowercase 64-hex digest. Its addition requires
an agreed revised full control schema digest, not acceptance of unknown fields
by the old validator. Profile config hash and capabilities must commit it.
Keep `response_schema_sha256` unchanged as the Bonsai inner schema pin; require
it to match the corresponding bundled inner schema. For Decider, define its
non-null inner-template pin explicitly in the new profile configuration.

Proposed future directory `schemas/scientist-profile-output-v2/`:

- `decider-response.template.schema.json`: exact shape in the next section;
  request-derived probability properties and selection enum are instantiated.
- `decider-usage.schema.json`: exact eight-field metric object below.
- `bonsai-response.schema.json`: closed projected completion and usage below.
- `bonsai-usage.schema.json`: exact two token counters below.
- `bonsai-recovery-content.schema.json`: full current RecoveryPlan schema.
- `bonsai-vision-content.schema.json`: full current VisionScene schema.
- `result.schema.json`: three explicit profile-selected variants of the exact
  result wrapper, including the closed generation schema; no open-object branch.
- `bundle.json`: closed name/version, exact relative-file-to-SHA-256 properties
  for those seven files, and `adapter_source_sha256` and
  `request_binding_source_sha256` pins. No caller-supplied file names or URLs.

These are proposed filenames, **not files delivered in this milestone**. Full
JSON Schema 2020-12 artifacts, validators and byte fixtures must be authored and
compared by both sides before hashes can be published. No placeholder is an
agreed schema hash. Every object below is closed with every listed key required;
no defaults, unknown fields, permissive union or `additionalProperties:true`.

Canonical JSON is UTF-8, sorted keys, compact separators, finite numbers,
`ensure_ascii=false`, without LF for hashing; frames add exactly one LF. Hash
the complete canonical schema documents, including their definitions, not field
descriptors. Bundle hash covers canonical `bundle.json` without a self-hash.
Require exact canonical result bytes before hashing on both sides; do not
independently normalize numeric spellings after signing. Integer fields require
integer lexemes and reject booleans and `1.0`; floats remain permitted only in
the explicitly numbered probability/timing fields below. Schema validation is
additional to duplicate-key, byte-bound and cross-field checks.

## Decider: exact response and usage

For `aos.decider.turn.v1`, proposed response is exactly:

```text
{
  deployment_digest: H,
  prediction: {selected_option: one original option ID,
               probabilities: {each original option ID: probability}},
  metrics: DeciderUsage
}
DeciderUsage = {
  latency_ms: number[0,720000],
  load_ms: number[0,720000],
  inference_ms: number[0,720000],
  reused: false,
  prepared_cpu: false,
  input_tokens: integer[1,1536],
  peak_vram_bytes: integer[0,9007199254740991],
  broker_activation_load_ms: number[0,600000]
}
usage = exact same DeciderUsage value as response.metrics
```

`H` is lowercase 64-hex and equals the immutable request deployment. Timing
bounds are proposed protocol ceilings, not measured acceptance or a substitute
for tighter pinned profile deadlines. Metrics describe distinct worker timings:
`latency_ms` need not include separately measured broker activation. Do not
invent equality or summation between them. `peak_vram_bytes` is the reported
allocator metric, not physical whole-GPU drain proof. No output token count is
invented for the classifier. Fresh broker sessions currently make `reused` and
`prepared_cpu` false; a resident-model profile would need a separate agreement.

The probability map is **not closed by arbitrary string additional properties**.
Freeze the request's ordered 2–10 unique nonempty IDs and labels before infer.
For each request instantiate `properties` and `required` with exactly those IDs,
`additionalProperties:false`, and `selected_option.enum` with those same IDs.
Each probability is a finite nonboolean number in [0,1]; absolute sum error is
at most 1e-6. Neither labels nor model output may introduce an ID. Do not add an
argmax policy that current Prediction does not require.

Pin the deterministic instantiator in the bundle, derive its schema only from
the persisted canonical request, and retain the derived schema SHA-256 in the
future AOS admission/resolution record. Scientist independently reconstructs it
from the same request. The static template alone is **not** a fully closed
per-request validator; the derived schema plus original request hash is the
validation authority. This introduces no seventh infer field and no arbitrary
schema submitted by a model. JSON string/byte limits remain additionally bounded
by the complete request frame; no lossy ID truncation or Unicode normalization.

## Bonsai: pinned projection, then closed inner content

For both Bonsai profiles propose this exact projected response:

```text
{
  choices: [{finish_reason: "stop",
             message: {role: "assistant", content: string}}],
  usage: {prompt_tokens: integer[0,16384],
          completion_tokens: integer[0,512]}
}
usage = exact same two-field value as response.usage
```

Choices has exactly one item. Content is at most 65536 UTF-8 bytes and must be
one JSON object; a character-count bound alone is insufficient. Prompt and
completion additionally obey the pinned context/output and request max-token
limits; prompt+completion cannot exceed the context limit. These are reported
token counts, not fabricated counters or wall-clock/GPU usage. Missing or
malformed usage denies completion; never fill it with zero. No total-token
field is required or synthesized.

This is an **explicit new adapter**, not a statement that raw llama.cpp output
already has this shape. Scientist must validate and project the pinned raw
producer response before persisting/hashing its result. AOS must install the
matching adapter/validator only with the new output-contract pin. Existing
legacy paths retain their current behavior and remain ineligible for the new
controlled admission; no shape guessing or fallback after validation failure.

The producer-specific raw metadata allowlist and projector source must be pinned
and fixture-tested together. Raw metadata such as id/object/created/timings may
be deliberately omitted only by that reviewed adapter, not by a generic
"ignore unknown fields" rule. Reject unknown raw fields until that allowlist is
agreed. Reject alternate choices, non-stop finish, tool calls, nonempty refusal
or hidden reasoning channels; do not silently discard an alternate action.
If a raw model identity is present, verify it before omitting it; outer immutable
profile/deployment binding remains mandatory. Require explicit assistant role
in the new path; do not inherit the legacy parser's missing-role default.

Inner content is never loosely projected. Reject duplicates, nonfinite values,
unknown nested fields, markdown wrappers and trailing data before native parsing.

### Recovery content

Exactly the six current RecoveryPlan keys:

- `diagnosis`: string length 1–600.
- `evidence_refs`: array length 1–4 of strings; its set must equal the original
  trusted evidence IDs, as native `validate_evidence` requires.
- `assumptions`: array of strings, at most 3; bounded by content bytes.
- `revised_plan`: empty, or exactly three closed objects
  `{order,action,expected_result}` with orders 1,2,3 and respective actions
  `observe_workspace`, `create_authorized_file`, `verify_exact_content`;
  each expected result is a string length 1–300.
- `verification_criteria`: exactly `["exact_file_content"]`.
- `needs_human`: boolean; true requires empty plan, false requires all three
  ordered actions. No model-authored tool or authorization expansion.

Use the full `RecoveryPlan.model_json_schema()` artifact, including its custom
plan constraint, plus native `validate_evidence`; generated schema alone does
not encode all native cross-field checks. Do not claim an additional uniqueness
rule for evidence_refs is already implemented: the current native check is set
equality. Any stricter new rule requires explicit versioned agreement.

### Vision content

Exactly `{capture_id,state_version,width,height,elements,needs_human}`.
Capture ID is 32 lowercase hex; state version is a nonnegative integer, narrowed
in the proposed protocol to at most 2^53−1. Width/height are exactly 640/360.
Elements contains at most two closed `{role,label,bbox}` objects. Role is
`button`; label is `SAVE` or `CANCEL`; bbox is exactly `{x,y,width,height}` with
integer x 0–639, y 0–359, width 10–640 and height 10–360. Right/bottom edges must
remain within the capture. Human-required means no elements; otherwise exactly
one of each label, and the boxes must not overlap. Capture ID/state/dimensions
must match the original validated capture and state, not merely their syntax.

Use full `VisionScene.model_json_schema()`, native `unambiguous`/`in_frame` and
`validate_capture`. The proposed safe-integer ceiling is a new protocol bound;
it is not present in the native state-version field today. Preserve the original
image hash/capture authorization outside model output. These shapes describe
the existing narrow synthetic-canvas/recovery profiles, not arbitrary vision or
general task planning.

## Result adapter and control decisions

AOS proposes accepting Scientist's exact result wrapper
`{response,usage,generation}`. This is conditional design agreement, not runtime
admission. Generation remains the closed four-field
`{unit,invocation_id,main_pid,control_group}` object; apply the existing worker
unit syntax and cgroup checks plus the agreed metadata bounds. Bind `main_pid`
to terminal child's `pid`, and match unit/invocation/cgroup. Terminal alone
supplies start ticks/boot ID; never fabricate those from result generation.

Verify canonical result hash against terminal, terminal hash and exact original
target/stable admission pins first; validate profile content/usage and current
task authority before delivering a result. An internal conversion may construct
the existing ScientistTurnReceipt using request/profile/deployment from the
immutable original intent and generation/response/usage from the verified result.
Do not pretend the control result itself contains infer receipt fields. Never
re-hash only the projected content instead of the complete stored wrapper.
Stored result ≤96 KiB and complete frame ≤128 KiB; reject overflow, no truncation.

For note 78's other requested decisions, AOS proposes:

1. Stable admission binds complete original caller/server, policy, source,
   profile and schema/output-contract pins; fresh capability clocks are separate.
   New infer requires exact configured pins, not merely any cached capability.
2. Adopt bounded integer CLOCK_BOOTTIME microseconds with boot UUID for new
   metadata; preserve first-assigned budgets/deadlines. Legacy float receipts
   remain immutable legacy evidence, never converted/rehashed into new proof.
3. Cleanup after rotation must authenticate the new resolver separately while
   preserving original admission identity. Persist a closed cleanup grant bound
   to exact target/original pins and bounded operations; its exact full schema is
   still required, including the cancel-before-intent case. No launch/acquire
   rights follow from cleanup, and disabled new admission must not block cleanup.
4. Reserve bounded refresh/status/cancel/reconcile capacity for retained targets
   before admitting more work. Reserve terminal capacity at first intent, retain
   tombstones, and never evict uncertainty or reset budgets to make room. Exact
   limits and overflow behavior need joint tests, not just a numeric cap.
5. Keep history disabled. A hash-only projection cannot resolve migration0018's
   immutable AOS journal. Resolution needs an additive complete authenticated
   proof record; task takeover may permit GPU cleanup without accepting stale
   Operator output. Uncertain GPU release remains quarantined.

## Required implementation and acceptance

Scientist: implement the opt-in config/admission pin, strict per-profile
validators, request-derived Decider closure, pinned Bonsai projector before
result persistence/hash, and complete recursive control/schema variants. AOS:
implement matching pin checks, output/result adapter, source/current-authority
checks and additive resolution journal. Neither side may silently expand scopes.

Before admission, compare exact full schema/bundle hashes and execute shared
synthetic byte fixtures: unknown key at every level; wrong ID/label maps; bool
or float integer fields; NaN/duplicate keys; usage mismatch/overflow; malformed
inner JSON; recovery evidence/action mismatch; stale/overlapping vision boxes;
raw producer extras; terminal/result/generation mismatch; lost ACK without infer
replay; cancellation winning before cached publication; rotated cleanup under
capacity pressure. Metadata fixtures also need actual execution against their
schemas, including unit-regex escaping; authored positive labels are not proof.
Native/authenticated integrated acceptance is a separate Scientist-owned gate.

The current dirty Scientist source now funnels cached publication through
`authorize_cached_result` inside the executor transaction; it checks cancellation,
original identity and exact verified completed-result hash. This is observed
source progress beyond the historical HIGH finding, not an independent test or
proof that every race/cleanup case is discharged. Note 78's reported CPU tests
remain counterpart attribution. This document neither repeats the old source
trace as a current reproduced bug nor declares it fixed/native-accepted.

## Observation fingerprints

Scientist HEAD was `38d513427df88f98f0a73f0598a5b48dfe636a90`;
executor/store were dirty and note 78 untracked. These are separate file
observations, not an atomic counterpart tree snapshot. SHA-256:

```text
Scientist docs/ai-scientist/78-aos-control-contract-resolution.md
3a8efa2d6bff3ceb7158a41a55cbe5192ea3647c7b26ffabbf1fe3a61bb197fa
Scientist docs/ai-scientist/contracts/control-v1-draft/README.md
29e963364c3644247fdb26c168543d2283fb12f2f09fe11778d225c69b90c34f
Scientist docs/ai-scientist/contracts/control-v1-draft/metadata.proposed.schema.json
440352541cc824b88933a17e02f50b6683a97db85ca60092bcbc5a5758866ca6
Scientist docs/ai-scientist/contracts/control-v1-draft/fixtures.manifest.json
21b80c13dd9e105c731dcf118e89522a794ef7982f161ca9b23565ae455c8216
Scientist lab/llm/aos_gpu_executor.py
25557a5655e6f265440566f4d0f329bb9e434282ac64ce56da9e17dfc14ba001
Scientist lab/llm/aos_gpu_control_store.py
106055e89fceb135f11b6b6fbc866885cfa5e8c5df95a8c309ea33a499d6ca82
AOS src/aos/scientist_protocol.py
ef522f8759e7fb00b87a5b1a1ed7e922d5f6a7d1b0f1095dbcf38de68d3b5c60
AOS src/aos/scientist_decision.py
3ff77a16bfa0efba1a7b9feb210f8c42cae96626b3c49cd131cbe8235bb27407
AOS src/aos/scientist_supervisor.py
f9d46e83ea7dde3454fce32a1fd7b4daa5a521b1b333b614413fd5f53d506e4b
AOS src/aos/supervisor.py
95ab0758e111b01b9b2a27c24faa223b2a73dab83bc929736ecc2cb9d86b1a42
AOS src/aos/vision.py
2964820c6648ab06094d2eb84741e08e2564d75c602508dec1bbca2c330a094b
AOS src/aos/contracts.py
866ab8356324b32a6a3743f7b7c36d256ed416fd58925254381c8022dbf127db
AOS services/decider/worker.py
a1e817ba1b099a0f12d20ecf4ad532ee86a21dba212b3cacf66e0437fa02318e
AOS services/decider/broker_worker.py
af04fab479d9d5326fd5456dd1592c9a444ee478831b50ef9070ee0b73d4cb8a
AOS services/bonsai/broker_worker.py
69caf47c1beee4cae9c6c3170c0107acd8d8b80f45b60b5ba72d313ecbc2f528
```

The counterpart manifest's declared schema hash excludes framing LF; the file
SHA above includes its on-disk bytes. They are not interchangeable. Exact fixture
hashes remain in that public manifest; no private evidence manifest was opened.
