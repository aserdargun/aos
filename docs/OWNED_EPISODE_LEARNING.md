# Owned episode content learning

## Implemented scope

An explicitly opted-in owned synthetic episode connects the actual Bonsai goal
proposal to the actual Decider execution decisions. This extends the
[selected-skill planning path](OWNED_SKILL_PLANNING.md); it is not a general
website collector, automatic training pipeline or production skill activation.
Actual acceptance results are recorded separately in [STATUS](STATUS.md).

The collection checkbox is **off by default** and applies to one new proposal.
The host persists immutable consent before creating the native planning task.
Opting out creates no episode consent or copied learning candidates; the normal
private planner provenance and trajectory records still exist. Consent cannot
be added retroactively. Models cannot grant or expand collection authority.

The opt-in proposal bundle is version 1.1 and pins `episode_id` and
`collection_consent_sha256`. Old version-1.0 bundles remain unchanged. Execution
still uses the version-1.4 plan-bound admission and six separate manual approvals.
Collection and inspection never generate additional model calls or actions.

## Actual inputs, provisional predictions

- **System 2:** the exact native planning request and validated bounded
  `OwnedSkillPlan`. It is not a recovery record, hidden reasoning or fabricated
  SQL model call for a run that did not yet exist.
- **System 1:** the exact stored native DECIDE request/options and structured
  prediction, joined to the deployment, state, decision, step and run. Later
  results, human review and target labels are not added to model inputs.

Candidate collection runs at the scheduler's approval boundaries and after the
execution task finishes. Status exposes role counts only. Explicit authenticated
inspection shows actual input and prediction content, including while a task is
running. Incomplete episodes remain provisional: neither a successful proposal
nor an action prediction is automatically a correct training target.

Review requires a successful execution, independent form readback and the full
plan-bound source audit, using the same frozen trajectory snapshot as candidate
derivation. Failed/cancelled runs, missing admission and changed sources cannot
become reviewable. A collection failure does not authorize an action or change
the task result.

## Review, revoke and local export

In Tasks, enable collection **before Generate proposal**. Inspect provisional
content if desired, bind/start separately, approve the six actions, then inspect
the completed episode. Each role has its own explicit accept/reject operation.
The immutable receipt binds the exact sorted candidate content, consent,
execution identity and stable terminal source fingerprint. Acceptance records a
human decision to use the exact prediction as a development reference; it does
not imply independent expert correctness or training authorization.

Both roles must have current, accepted, non-revoked receipts for an explicit
local export. Export writes separate System-1 and System-2 JSONL files and a
content-addressed manifest. Inputs and targets are separate. All rows use one
conservative synthetic fixture-family leakage group and `development_only`;
different values, languages or sessions are not independent held-out examples.
Repeated export of identical source and receipts is byte-identical.
Each row is checked against `owned_episode_export_record.schema.json` before
serialization; the manifest uses `owned_episode_export.schema.json`. These are
local development artifact contracts, not upstream trainer input formats.
An explicit [conversion/readiness step](OWNED_EPISODE_PREPARATION.md) maps reviewed
S1 choices to Example/Q and S2 plans to a distinct messages/target pair. Optional
real S1 CPU tokenizer verification still does not authorize training.

Revocation is append-only and blocks future export without silently deleting or
rewriting previous exports. Revocation can still be recorded if later source
inspection fails; the UI distinguishes recorded revocation from unavailable
source evidence. This is not physical secure erasure or automatic retention.

Private records live under the originating manager's `owned-episodes/` directory
(0700), with bounded canonical files (0600, single-link, no symlink traversal).
The role JSONL and manifest filenames begin with the export SHA-256. They may
contain private model inputs and must never be included in the source manifest,
example fixtures, logs or commits. No network upload is performed.

## API boundary

`POST /api/tasks/owned-skill-plan` also accepts version `1.1` with the existing
goal/control fields and the additional boolean `collect_learning`; old `1.0`
requests imply false. The checkbox resets after submission and control-scope
changes. The episode summary is returned by `GET /api/tasks`.

Authenticated `POST /api/tasks/owned-episode/{operation}` takes
`schema_version: "1.0"` and `episode_id`, with these exact additional fields:

| Operation | Additional fields |
| --- | --- |
| `inspect` | None |
| `review` | `role`, `decision`, `confirm_sha256`, `lease_id`, `generation` |
| `revoke` | `role`, `confirm_sha256`, `lease_id`, `generation` |
| `export` | `review_receipts`, `lease_id`, `generation` |

Writes require current idle AGENT control. Inspection does not require an idle
task. Unknown/duplicate fields and query parameters are rejected. Source and
schema failures return content-free errors. The authenticated host, not model
output, chooses these operations. Historical consent identity remains
provenance; it does not replace current write authority.

## Remaining product work

All candidates, receipts and exports retain `training_ready: false`. This slice
does not supply redaction/rights approval for a real application, independent
train/validation/test cohorts, full trainer admission, S2 adapter compatibility,
training, promotion or rollback. Those gates remain separate. A real target
application/account and independent result oracle are still required for W1–W6
product acceptance. See [WEB_APPLICATION_LEARNING](WEB_APPLICATION_LEARNING.md).
