# Reviewed manual parameter skill releases

An independently accepted [manual bootstrap candidate](OWNED_PARAMETER_SKILL.md)
can become a private immutable **development release**. Explicit selection and
rollback append records to a shared catalog. These records preserve manual,
synthetic provenance: they do not invent model learning events, start a task,
authorize execution, activate a runtime, train a model, or verify native/GPU or
real-site behavior. [Separately authorized new-parameter execution](OWNED_PARAMETER_SKILL_REUSE.md)
now has a CPU-fixture host/API path; release and selection alone still grant
no execution authority.

## Original evidence and durable history

Every target release is bound to its exact original project, candidate, accepted
review, audited bootstrap and database identity. Targeted read, publication and
selection freshly verify these sources. A revoked review cannot be selected;
there is no automatic fallback to an older release.

Canonical migration **0022** adds append-only release and selection history to
the original trajectory database. The history is authorized before its private
file is written. Selection compares the exact expected previous selection hash;
rollback requires an earlier release that was actually selected in the same
family. Family identity includes application/tenant/role scope, task and field
binding semantics. Every revision retains its own exact source pins.

Two versions must have independently audited original bootstrap records in the
same original database and share one private release directory. Supply the
target version's original project, candidate and review paths for each operation;
to roll back to A, supply A's source paths. Changing the project or copying a
database does not transfer review authority. Catalog inspection checks immutable
history and hashes but does not freshly audit every historical source. Its
`source_current_verified=false` explicitly distinguishes inventory from target
admission.

The Tasks host uses only its currently configured original project. It cannot
switch to another project's source through a release hash or a request payload.
The CLI can supply the original target project explicitly. Multi-version backend
tests exercise those separate trusted source contexts; they do not demonstrate
a managed GUI source switch or deployment of this workflow to the live session.

Missing, changed or unanchored release/selection files fail closed even after a
fresh process starts. An authorized database append followed by a failed file
write also fails closed. Explicit `recovery-preview`/`recover` now restore only
an exact already-authorized missing release or selection file; they never append
history, change selection, replay a task or activate a skill. Never delete
history or retry an uncertain operation blindly. Inspect
the catalog and exact release after a lost acknowledgement. A coordinated
privileged rollback of both the database and filesystem remains outside the
local trusted-history guarantee.

An older database without migration 0022 is rejected with
`owned_parameter_skill_release_history_migration_required`. Read/preview do not
migrate or adopt it. Existing migration files are unchanged; ordinary
`TrajectoryStore` initialization applies new canonical migrations only when that
database is explicitly opened for application startup. This feature does not
upgrade the running user's database or restart their application.

## Public command

Run from the development checkout with its existing virtual environment:

```sh
.venv/bin/python -m aos.owned_parameter_skill_release_cli --help
```

The following shell helper avoids repeating original-source flags. Paths and
uppercase SHA values are placeholders for already verified private artifacts;
they are not bundled data. The release directory's existing parent must be
private and owned by the current user. Keep project, candidate, review and release
artifacts outside the checkout and the execution workspace.

```sh
release_cli() {
  .venv/bin/python -m aos.owned_parameter_skill_release_cli "$@" \
    --project-directory /tmp/aos-private/project-a \
    --project-manifest-sha256 EXACT_MANIFEST_SHA256 \
    --database /tmp/aos-private-runtime/trajectory.sqlite \
    --bootstrap-journal-directory /tmp/aos-private-runtime/bootstrap-a \
    --candidate-directory /tmp/aos-private/candidates-a \
    --review-directory /tmp/aos-private/reviews-a \
    --release-directory /tmp/aos-private/manual-skill-release-catalog
}

release_cli preview --review-sha256 EXACT_ACCEPTED_REVIEW_SHA256
release_cli release --review-sha256 EXACT_ACCEPTED_REVIEW_SHA256 \
  --confirm-release-sha256 EXACT_PREVIEWED_RELEASE_SHA256 \
  --human-confirmation RELEASE_MANUAL_SKILL

release_cli read --release-sha256 EXACT_RELEASE_SHA256
release_cli select-preview --release-sha256 EXACT_RELEASE_SHA256
release_cli select --release-sha256 EXACT_RELEASE_SHA256 \
  --confirm-selection-sha256 EXACT_PREVIEWED_SELECTION_SHA256 \
  --human-confirmation SELECT_MANUAL_SKILL
release_cli catalog
release_cli recovery-preview --record-sha256 EXACT_MISSING_RECORD_SHA256
release_cli recover --record-sha256 EXACT_MISSING_RECORD_SHA256 \
  --confirm-recovery-sha256 EXACT_PREVIEWED_RECOVERY_SHA256 \
  --human-confirmation RESTORE_ANCHORED_RELEASE_RECORD
```

Recovery requires the original retained catalog directory, unchanged original
database anchor and a still-accepted freshly audited target review/source. It
does not recreate a lost directory, overwrite a changed file, accept an
unanchored record or reactivate a revoked review. Preview and execution use the
same catalog/review locks and exact current directory identity; stale confirmation
is rejected. Other missing anchored records may remain, so restore each explicitly
before ordinary inventory/admission resumes. The canonical recovery proposal
schema is `schemas/owned_parameter_skill_release_recovery.schema.json`; the full
proposal is ephemeral and no new recovery metadata is inserted into migration0022.

### Explicit missing-record restoration in Tasks

The configured parameter-project Tasks panel also supports the same retained
catalog recovery. Enter the exact missing release or selection record hash,
request a recovery preview, inspect its canonical bytes/hash, then confirm the
separate restoration checkbox before restoring. This recreates only the already
anchored original bytes. It does not create a new release, selection, execution,
training permission or model update. Changed/revoked source and lost directories
remain rejected. A stale control lease/generation requires fresh inspection;
failed or ambiguous writes are never automatically resent.

Authenticated POSTs under `/api/tasks/parameter-project-skill-release/`:

| Operation | Exact request fields in addition to `schema_version: "1.0"` |
| --- | --- |
| `recovery-preview` | `record_sha256` |
| `recover` | `record_sha256`, `confirm_recovery_sha256`, `human_confirmation: "RESTORE_ANCHORED_RELEASE_RECORD"`, current `lease_id`, `generation` |

The response carries an ephemeral `recovery`, `recovery_canonical` and
`recovery_sha256`, plus the existing false native-model/execution/activation/
training/GPU-release flags. A recovery preview grants no write authority. The
restore needs idle current AGENT control and the original accepted source.
This source capability does not upgrade the running live console automatically.
Executed validation and deployment boundaries remain in [STATUS](STATUS.md).

For the first release/selection, omitted expected hashes mean that no prior
record is expected. For a later version, both release preview and publication
must include `--expected-parent-release-sha256` with the current latest release.
For a later selection, both preview and write must include
`--expected-selection-sha256` with the exact current selection. A stale head is
rejected rather than silently replaced.

After independently publishing and selecting B, use A's original source paths
and the current B selection hash to explicitly restore the prior A selection:

```sh
release_cli rollback-preview --release-sha256 EXACT_RELEASE_A_SHA256 \
  --expected-selection-sha256 EXACT_CURRENT_B_SELECTION_SHA256
release_cli rollback --release-sha256 EXACT_RELEASE_A_SHA256 \
  --expected-selection-sha256 EXACT_CURRENT_B_SELECTION_SHA256 \
  --confirm-selection-sha256 EXACT_PREVIEWED_ROLLBACK_SHA256 \
  --human-confirmation ROLLBACK_MANUAL_SKILL
```

CLI output is canonical JSON containing bounded scope/hash metadata. Private
parameter values, absolute private paths and raw trajectories are omitted.
Exit status is 0 on success, 1 for rejected source/storage checks and 2 for invalid
arguments. A successful selection records development metadata only; bootstrap
actions, approval records and model-call counts are unchanged.

## Verification boundary

Targeted tests use actual temporary trajectory stores, finite CPU operators and
owned loopback TLS/readback, with scripted decisions and a mock browser backend.
They are separate from native Decider/Bonsai acceptance. Current test results,
UI integration and deployment status are recorded in [STATUS](STATUS.md).
