# Owned episode experimental System-1 adaptation

This is a separately authorized local GPU experiment on reviewed, converted
owned synthetic episode records. Collection consent, accepted role reviews and
CPU tokenizer checks do **not** authorize training. Implementation and measured
acceptance are distinguished in [STATUS](STATUS.md).

## Operator flow

In a newly configured owned-reuse backend, complete the opted-in task, review
both roles, export, convert and run the real S1 tokenizer check. Tasks then offers:

1. **Preview training budget**: revalidate existing sources and show the exact
   authorization hash, example count and fixed budget. No file or GPU job starts.
2. Review the original inputs/targets for rights and sensitive content, and check
   that separate attestation. This is an operator declaration, not automated
   detection or independent rights certification.
3. Separately authorize this exact local experimental training budget. Both
   checkboxes default off and reset on control/source scope changes.
4. **Train experimental adapter** persists a one-use authorization before any
   worker starts. A private candidate is trained, then independently loaded in
   a second CUDA process for base/candidate comparison.
5. **Verify saved experiment** revalidates current source, receipts, code,
   deployment, artifact and completed report without model execution.

Pause/Stop drains the owned experiment before releasing its reservation. While
it runs, task admission and episode writes, including revoke, are unavailable.
Pause first, then revoke the source review; there is no automatic retry. Revoked
or changed source cannot verify a completed report. Failed/cancelled attempt
files are private diagnostics, not completed candidates; report publication is
the completion marker. Existing ordinary sessions are not silently restarted.

## Numerical scope

All 1–32 S1 records are used, not a selected subset. They retain the single
`development_only` fixture-family group. No fake train/validation/test split is
constructed. A rank-4 adapter targets the last MLP down projection of the pinned
Decider base: 32,768 float32 adapter parameters, seed 42, SGD learning rate 0.1,
one step, gradient clip 1.0, microbatch size one and a 1536-token ceiling.
Per-example loss divided by cohort size is accumulated before the single step.
The frozen base stays in evaluation mode; only adapter parameters receive
gradients. The worker checks finite nonzero gradients, changed adapter parameters
and unchanged base parameter versions/no base gradients.

The second independent process reloads the pinned base and the exact private
adapter bytes. It evaluates **every same-cohort record** with and without the
adapter, reporting mean NLL and finite-choice accuracy. This is resubstitution,
not held-out quality or a claim of improvement. Worse or unchanged results are
reported honestly. Parameter-version checks are not hostile-worker attestation.

Each worker requires at least 8 GiB free CUDA memory, limits input to 1 MiB and
stdout to 64 KiB, and has a 180-second deadline. Only the owning backend's reusable
Decider engine may be released first. Other services/processes are never stopped
to obtain VRAM. No dependencies/models are downloaded, no network export occurs,
and the active model/adapter registry is not changed.

## Source and lifecycle binding

Authorization binds episode, conversion, tokenizer report, source execution,
current review-derived memberships, all ordered S1 examples, deployment raw
manifest hash, runner/source/schema hashes, fixed budget, attempt number and
fresh control lease/generation. At most four starts per backend are allowed;
existing attempt directories are never overwritten or replayed after restart.

An async scheduler reservation is held through train/replay child cleanup.
Control reads remain responsive. Only the exact owning task can re-audit its
bound execution while holding that reservation; generic task admission stays
closed. Source and control are revalidated before workers and publication.
Persisted authorization and anchored attempt-directory identity are checked
before each worker and before final publication. CPU tokenizer and GPU adaptation
cannot start concurrently through the console.

Artifacts remain inside the private episode's `adapter-<authorization hash>`
directory, owner-only with no symlink traversal. The binary header binds exact
authorization, examples and deployment. The report is written last, after replay
and source revalidation. Missing/altered candidates and source files are not
recreated during read-only verification. No real artifact, input, screenshot or
DB belongs in the source manifest; `examples/owned_episode_adaptation.json` is
an explicitly synthetic schema illustration, never execution evidence.

## API

Authenticated POSTs under `/api/tasks/owned-episode/` include `schema_version:
"1.0"` and `episode_id`:

| Operation | Additional exact fields |
| --- | --- |
| `adaptation-preview` | `conversion_sha256`, current `lease_id`, `generation` |
| `adaptation-start` | same selection/control, exact `confirm_sha256`, boolean `rights_redaction_reviewed: true`, `experimental_training_authorized: true` |
| `adaptation-inspect` | `authorization_sha256` |

Preview requires idle current AGENT control but writes nothing. Start returns
HTTP 202; `/api/tasks` publishes content-free training/evaluating/verified/failed/
cancelled status. Inspection is read-only and requires no current lease.

## Not established

This experiment does not grant general `training_ready`, independent validation,
production training, inference deployment, promotion/rollback, S2 trainer
compatibility or W5 product acceptance. Real-site rights/account/oracle and
independent evaluation groups remain required for the full release.
