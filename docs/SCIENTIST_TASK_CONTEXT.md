# Scientist task intent and selected experience

This opt-in task context uses the existing Scientist `POST /v1/runs` contract.
It does not register a dataset, select arbitrary algorithms, grant execution
rights, train a model or promote a learned adapter. It is separate from the
shared GPU scheduler and the native-start authority still needed for that path.

## User flow

In the Scientist panel, select an allowed suite and enter an explicit track and
budget. Optional context is off by default. The existing proposal preview and
human approval apply to the **whole normalized request**, including any selected
context. Editing a request requires a fresh proposal and approval; an old
approval hash cannot authorize different context.

- **Field intent:** on the mode track, opt in and supply an asset reference,
  `digital_twin` or `predictive_maintenance` goal, and an objective. The asset is
  a user declaration, not verified equipment identity or operational permission.
- **Selected experience:** opt in and supply one source run ID, its independently
  verified report hash, and one to eight experiment/trajectory hash references.
  These are explicit advanced references, not an automatic history browser or
  evidence that every entered record is eligible.
- Inspect the exact proposal before approval. Submitting a proposal is not
  starting a run. Existing authority, current controller generation, fresh
  approval and durable intent checks remain in force before any remote effect.
- Read status and independently verify the resulting report through the normal
  AOS path. A received stop response is not terminal completion or GPU release.

The EN/TR controls reject incomplete or malformed enabled context. Turning an
option off omits it from the proposal. Track changes clear selected optional
context so that hidden values cannot silently enter a later request.

## Exact request additions

`field_intent` contains only:

| Field | Constraint |
|---|---|
| `asset_id` | Trimmed plain text, 1–128 characters |
| `goal_kind` | `digital_twin` or `predictive_maintenance` |
| `objective` | Trimmed plain text, 1–600 characters |

C0 control characters and DEL are rejected before trimming. AOS requires mode
track for field intent. Scientist additionally verifies the registered provider,
installed and authorized mode snapshot, owner and frozen task identity. The CPU
capability describes an admitted suite; it does not attest support for every
optional metadata combination.

`prior_experience` contains only:

| Field | Constraint |
|---|---|
| `source_run_id` | Canonical lowercase UUID-shaped run ID |
| `source_report_sha256` | 64 lowercase hexadecimal characters |
| `records` | 1–8 records with unique experiment IDs |
| `records[].experiment_id` | `exp_` followed by 32 lowercase hexadecimal characters |
| `records[].experiment_sha256` | 64 lowercase hexadecimal characters |
| `records[].trajectory_sha256` | 64 lowercase hexadecimal characters |

Clients cannot supply scores, findings, `field_context`, `prior_findings`,
dataset overrides or algorithm overrides. Scientist revalidates source ownership,
terminal report and eligible measured proposal records before freezing history.
Measured `KEEP`, `KEEP_SIMPLER` or `DISCARD` proposals can be eligible; baseline,
unmeasured rejection/unattempted or protected records are not automatically usable.
No client-side structural check replaces that server evidence check.

For a fixed CPU grid, retained history is advisory: it does not change the
registered algorithm order or establish adaptive learning. Dataset and algorithm
configuration remain bound to the registered suite and its reviewed hashes.

## Compatibility and safety

The typed Start/Task contracts and their canonical schemas include both optional
fields. Absent or null new fields are omitted when serialized; existing request
bytes and persisted task envelopes must remain compatible. Existing null fields
are not globally removed. No SQLite migration or alteration of historical rows
is required by this extension.

The existing service records the exact approved body before dispatch. The
request hash covers normalized context and the unchanged suite/budget/identity
fields. Unknown fields fail closed. A lost acknowledgment is still uncertain;
changing the context or submitting again is not a safe reconciliation strategy.

The [synthetic request fixture](../examples/scientist_lab_context_start.json)
contains invented references and grants no execution authority. It must not be
submitted as if its hashes referred to real Scientist data.

## Delivery boundary

See [STATUS](STATUS.md) for dated validation and source identities. CPU/ASGI tests
use fixture controllers and mocked remote responses. Rendered Chromium checks
use a synthetic panel API; neither proves a real Scientist run with selected
history or a deployment to the active AOS UI. The previous real CPU acceptance
did not exercise these new optional fields and does not validate changed bytes.

This source slice is prepared in the isolated AOS publication checkout. The
running default AOS and its pinned source set are not changed. A later explicit
deployment and fresh source/config/runtime review are required before actual
use. No new study-description endpoint is assumed available or agreed by this
extension. The current manual-reference UI can later gain an independently
reviewed owner-bound history selector without broadening task authority.
