# Bonsai producer projection and pre-journal validation

This is an opt-in source implementation, not jointly admitted GPU integration.
The infer frame stays at integer wire version1 with its existing six fields.
Existing content-schema pins describe RecoveryPlan/VisionScene, not the outer
provider response or usage. Runtime confirmation remains denied by default.

## Explicit output decision

AOS adopts the closed output shape proposed in Scientist note80 for this local
adapter: one stopped choice, content string, optional exact deployment model,
optional assistant role and exactly two nonnegative integer usage counts.
Optional fields are preserved when present; the adapter does not invent a model
or role. This explicitly differs from the earlier AOS design proposal's role
requirement. The outer schema's canonical hash is
`c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d`.
It does not replace any inner content-schema hash or establish agreement on a
complete control/profile bundle.

The native metadata allowlist is derived from Prism revision
`9a9394a895b96003ca842a6041cb28ac49a108f7`: the
[response serializer](https://github.com/PrismML-Eng/llama.cpp/blob/9a9394a895b96003ca842a6041cb28ac49a108f7/tools/server/server-task.cpp),
[message serializer](https://github.com/PrismML-Eng/llama.cpp/blob/9a9394a895b96003ca842a6041cb28ac49a108f7/common/chat.cpp)
and [timing serializer](https://github.com/PrismML-Eng/llama.cpp/blob/9a9394a895b96003ca842a6041cb28ac49a108f7/tools/server/server-common.cpp).
Source-derived metadata closure is not evidence that the pinned binary was
executed successfully through the adapter.

The trusted manifest's optional `bonsai_output_projection` object separately
pins integer version1, the outer schema and exact adapter source bytes. Missing
pin preserves legacy producer behavior; malformed, unsupported or changed pins
must fail before model startup. Adding a pin changes the deployment identity;
it is never an automatic promotion of an existing deployment. No local model
manifest or Scientist profile configuration is rewritten by this feature.
The opt-in adapter also requires that exact `code_revision`; other native
builds require a separately reviewed adapter instead of inheriting this one.

Offline pin inspection does not start a model or authorize execution:

```sh
PYTHONPATH=services .venv/bin/python -c 'import json; from bonsai_projection import projection_pin; print(json.dumps(projection_pin(), sort_keys=True))'
```

Only a separately reviewed deployment may add that object to its private
manifest under `bonsai_output_projection`; current model manifests are unchanged.

## Host-side validation

`validate_bonsai_receipt` runs through the explicit `validate_receipt` callback
before the existing intent journal records the receipt. It verifies original
request/profile/deployment, recursively closed projected response fields and
exact projected usage/receipt usage equality. Completion count must not exceed
the original request's maximum; combined counts must fit the trusted context
limit. The default context limit is16384; smaller deployments must inject their
actual limit using a trusted callback, not a field supplied by a caller.

Inner JSON rejects duplicate keys and nonfinite constants, then uses the existing
strict RecoveryPlan/VisionScene models. Recovery must cite exactly the supplied
evidence and obey existing abstention/action ordering. Vision must bind the
original capture bytes/hash, integer dimensions/state and nonoverlapping boxes.
The callback validates immutable copies; it never normalizes stored receipt
bytes, starts a model, authorizes a tool, or expands the task's scope.

`validate_profile_receipt` dispatches the three current fixed profiles to the
Decider or Bonsai guard. Generic free-goal inference is not added. Rejection
after dispatch leaves pending/uncertain intent and prohibits automatic replay;
status inspection is not a retry or proof of GPU release.

## Remaining integration gates

Producer metadata must be derived from the exact pinned Prism source, rather
than treating arbitrary extra fields as harmless. Semantic alternate channels,
unknown fields and malformed metadata must not disappear through projection.
The pin's adapter bytes and accepted native metadata are part of the source
review, not runtime discovery or an extensible raw provider schema.

Counterpart profile/schema pins, authenticated control/capability semantics,
append-only original admission and resolution journaling, and real GPU
cancellation/transfer/release still require separate acceptance. Scientist alone
runs the coordinated GPU test. See [handoff](SCIENTIST_HANDOFF.md) and
[actual evidence](STATUS.md); CPU fixtures are not native inference.

Latest source acceptance: **133 focused CPU tests passed in4.701s**, including
the source-shaped producer helper→actual synthetic Unix socket→pre-journal
guard→SQLite receipt path for both Bonsai profiles. Mocked worker tests prove
cleanup-before-output and bad/stale pin rejection; no native model was run.

The previous mandatory-role/removed-model incompatibility is historical:
Scientist notes82/83 now preserve the same optional fields and canonical outer
schema. Its output-v2 bundle also pins eight schemas and its own projector source;
that is not the same pin as AOS's version1 native producer adapter.

## Original admission-bound output-v2 guard

The desktop factory now accepts explicit trusted `output_contract` and
`output_context_tokens` arguments. They require same-store original admission
history; a separate `validate_receipt` callback cannot replace this guard.
The closed version2 profile pin is a separate union member, so historical
four-field profile identities retain their exact serialization and hashes.
No migration is rewritten and no historical record acquires a new output pin.
The pinned path now additionally requires explicit admission record version2.0
with separate closed binding/capture/record artifacts. A version1.0 configured
history fails factory construction before task creation. Historical reads remain
possible, but neither four-field nor interim1.0/V2 records authorize new inference.

`admission_profile_validator` reads the original immutable request/admission pair,
requires the exact trusted output contract, request hash, profile and deployment,
then applies the existing three-profile semantic validators before receipt
persistence. The factory also checks the captured pin immediately after durable
intent capture, before sending any infer frame: known wrong-bundle or legacy-pin
requests remain pending but are never dispatched. Invalid returned content after
dispatch instead retains pending uncertainty. The trusted pin is frozen at construction. Historical reads do not
invoke current-authority callbacks; the existing transport and intent journal
still perform current owner/peer/deadline checks separately. A matching output
pin alone is not a grant, full bundle execution proof, or GPU-release evidence.

The Scientist candidate bundle is
`ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e`;
module SHA is
`1021e5b87dfb3154dc92ee109324f7e010ff3b55f15623859bfc455efb714b71`.
Read-only inspection confirms all eight schema pins and both module-source pins.
Selecting that trusted pin remains an explicit deployment/configuration decision;
no private manifest, active profile, running app or scheduler is changed here.
Full control/terminal contract agreement and Scientist-only native acceptance
remain open.
