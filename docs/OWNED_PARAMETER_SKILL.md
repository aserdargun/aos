# Audited manual bootstrap skill candidates

## Tasks interface

In an explicitly configured parameter-project session, the EN/TR Tasks card
offers candidate preview after the original bootstrap is independently accepted.
Review the exact candidate and its SHA, check the separate human acknowledgement,
then publish. Preview and publication never rerun the task. Publication requires
the current AGENT lease/generation and an idle scheduler; Human mode retains
read-only access. An uncertain publication acknowledgement disables blind retry;
read the already known exact candidate SHA instead. A malformed, unresolved or
unconfigured bootstrap cannot open this workflow.

The authenticated endpoints are
`/api/tasks/parameter-project-skill/preview`, `publish` and `read`. These are AOS
local API routes, not Scientist broker endpoints. The card is source-implemented;
an existing live session is not silently restarted or updated to expose it.

`scripts/aos-parameter-skill` provides a private, offline preview → exact human
publication → read workflow for an original accepted
[owned parameter project bootstrap](OWNED_PARAMETER_PROJECT.md). The candidate
is typed `audited_manual_bootstrap_candidate`, awaiting manual review. It records
the reviewed synthetic recipe and parameters alongside the original bootstrap
intent, run binding, independently audited trajectory and whole-record receipt.
It is not a released or activated skill, real Decider learning event, trained
model, native-model acceptance, real-site/account verification or GPU release.

All commands require the original private project directory and its independently
retained manifest SHA, original private SQLite database, original bootstrap
journal and a private candidate destination outside the checkout and execution
workspace. Use the exact original intent SHA from the bootstrap receipt. Copied
or changed sources, wrong databases/journals, unresolved bootstrap records and
missing or inconsistent verified receipts fail closed. There is no arbitrary
source upload, listener, model invocation or new task execution path.

The destination parent must already exist, be owned by the current user and have
mode `0700`. Source and evidence paths must satisfy the existing private-file
and directory checks; symlinks and unsafe permissions are rejected. Publication
stores the private candidate with mode `0600`.

The original managed database and bootstrap journal may live under the ignored
`data/` directory inside the checkout, including `data/local-app-v1/session`.
This narrow runtime-evidence exception does not allow tracked source-tree paths:
the project and candidate destination must still remain outside the checkout.
Use the original managed paths; moving or copying a journal is not an accepted
replacement for its pinned identity.

Run from an installed development checkout with `.venv`. The wrapper forwards
to `.venv/bin/python -m aos.owned_parameter_skill_cli`. The example paths below
are placeholders for original private artifacts, not bundled acceptance data.
Keep these artifacts and the candidate out of Git.

```sh
scripts/aos-parameter-skill preview \
  --project-directory /tmp/aos-parameter-review/crm-project \
  --project-manifest-sha256 EXACT_ORIGINAL_MANIFEST_SHA256 \
  --database /tmp/aos-private-runtime/trajectory.sqlite3 \
  --bootstrap-journal-directory /tmp/aos-private-runtime/bootstrap-journal \
  --candidate-directory /tmp/aos-private-candidates/manual-crm \
  --intent-sha256 EXACT_ORIGINAL_BOOTSTRAP_INTENT_SHA256
```

Preview validates the existing evidence without writing a candidate or changing
the source, database or journal. Output contains scope and hashes, including the
exact `candidate_sha256`; private parameter values, credentials, raw trajectory
and absolute private paths are omitted. Review the original private inputs and
receipt before explicitly publishing that exact candidate:

```sh
scripts/aos-parameter-skill publish \
  --project-directory /tmp/aos-parameter-review/crm-project \
  --project-manifest-sha256 EXACT_ORIGINAL_MANIFEST_SHA256 \
  --database /tmp/aos-private-runtime/trajectory.sqlite3 \
  --bootstrap-journal-directory /tmp/aos-private-runtime/bootstrap-journal \
  --candidate-directory /tmp/aos-private-candidates/manual-crm \
  --intent-sha256 EXACT_ORIGINAL_BOOTSTRAP_INTENT_SHA256 \
  --confirm-candidate-sha256 EXACT_PREVIEWED_CANDIDATE_SHA256 \
  --human-confirmation PUBLISH_MANUAL_CANDIDATE
```

The full literal and both exact hashes are required. Publication revalidates
evidence and creates only a private immutable candidate. Repeating the same exact
publication is idempotent; a changed candidate or conflicting destination is
rejected. Publication does not accept a review, admit execution, replay the
bootstrap, activate a skill, train or promote a model.

```sh
scripts/aos-parameter-skill read \
  --project-directory /tmp/aos-parameter-review/crm-project \
  --project-manifest-sha256 EXACT_ORIGINAL_MANIFEST_SHA256 \
  --database /tmp/aos-private-runtime/trajectory.sqlite3 \
  --bootstrap-journal-directory /tmp/aos-private-runtime/bootstrap-journal \
  --candidate-directory /tmp/aos-private-candidates/manual-crm \
  --candidate-sha256 EXACT_PUBLISHED_CANDIDATE_SHA256
```

Read returns the bounded metadata summary after exact candidate and source
validation. It is read-only. All commands print canonical JSON, return 0 on
success, 1 on rejected evidence/filesystem checks, and 2 on invalid CLI arguments.
Rejections omit raw exceptions and private values.

## Independent development review and revocation

An immutable published candidate keeps its original `awaiting_manual_review`
status. The CLI can record a separate, exact human development review of that
candidate and later append its revocation. Acceptance changes the independent
review's status to `accepted`; revocation changes its current status to
`revoked`. Neither rewrites the candidate or original bootstrap evidence.
This review is bounded to the audited synthetic manual recipe. It does not
release or activate a skill, permit execution/replay, verify native inference,
grant training readiness or prove real-site/account or held-out acceptance.

All five review commands require the same original source arguments used above
and an additional `--review-directory`. Its parent must already be private
(`0700`); the review destination must remain outside the checkout and execution
workspace and separate from source, candidate and bootstrap-journal stores.
Review and revocation records are immutable private `0600` files. Each explicit
acceptance or revocation first appends its original human authorization and exact
record to review history in the original private trajectory database, then writes
the matching file. Task, model and original source records remain unchanged;
the database gains review metadata. Previews and
readbacks create no review files and start no listeners, tasks or models. Output
contains bounded hashes, scope, review status and explicit authority flags;
private parameter values and raw paths are omitted.

For concise commands, set these shell variables to your original private paths
and independently retained hashes:

```sh
project_directory=/tmp/aos-parameter-review/crm-project
manifest_sha256=EXACT_ORIGINAL_MANIFEST_SHA256
database=/tmp/aos-private-runtime/trajectory.sqlite3
bootstrap_journal_directory=/tmp/aos-private-runtime/bootstrap-journal
candidate_directory=/tmp/aos-private-candidates/manual-crm
review_directory=/tmp/aos-private-candidates/manual-crm-review
candidate_sha256=EXACT_PUBLISHED_CANDIDATE_SHA256

manual_skill() {
  scripts/aos-parameter-skill "$@" \
    --project-directory "$project_directory" \
    --project-manifest-sha256 "$manifest_sha256" \
    --database "$database" \
    --bootstrap-journal-directory "$bootstrap_journal_directory" \
    --candidate-directory "$candidate_directory" \
    --review-directory "$review_directory"
}

manual_skill review-preview --candidate-sha256 "$candidate_sha256"
```

Review the original private candidate, source inputs and independently verified
receipt. Copy the exact `review_sha256` from the preview only after that review:

```sh
review_sha256=EXACT_PREVIEWED_REVIEW_SHA256
manual_skill review-accept \
  --candidate-sha256 "$candidate_sha256" \
  --confirm-review-sha256 "$review_sha256" \
  --human-confirmation ACCEPT_MANUAL_REVIEW
manual_skill review-read --review-sha256 "$review_sha256"
```

Acceptance binds the exact candidate and review destination, revalidates current
original evidence, and requires the full literal `ACCEPT_MANUAL_REVIEW`.
Repeating the same exact acceptance is idempotent while the review is current.
An altered candidate, confirmation or source is rejected. A revoked review cannot
be accepted again while the original trusted database history is intact.

Revocation requires its own read-only preview, exact hash and separate literal:

```sh
manual_skill revoke-preview --review-sha256 "$review_sha256"
revocation_sha256=EXACT_PREVIEWED_REVOCATION_SHA256
manual_skill revoke \
  --review-sha256 "$review_sha256" \
  --confirm-revocation-sha256 "$revocation_sha256" \
  --human-confirmation REVOKE_MANUAL_REVIEW
manual_skill review-read --review-sha256 "$review_sha256"
```

Readback reports the current independent review status and exact revocation
hash. Repeated exact revocation is idempotent. A revoked review remains available
for audit. Candidate `read`
continues to report the original awaiting-review artifact. Source tampering
fails current provenance readback rather than producing a current accepted
review. Every process checks the private files against original database review
history and fresh source evidence. A missing, altered, extra or unanchored record
fails closed after restart too. Deleting a revocation file cannot turn its review
back into an accepted one. Coordinated privileged rollback of both the original
database and private files cannot be proven without a separate trusted integrity
anchor. This manual review grants no release or execution authority; future
execution admission still requires a separate authorization contract.

Review history requires canonical migration `0021`. Supported source/store
initialization includes it; these CLI commands never migrate an old or live
database or adopt earlier unanchored review files. A missing migration produces
the bounded error `owned_parameter_skill_review_history_migration_required` and
`migration_required: true`. Use a supported newly initialized source/runtime
store for this workflow; a pre-existing live session is not silently upgraded.

## Exact missing-record recovery

An authorization may reach the original database before its immutable file is
written, or an anchored file may later be removed. Normal read, acceptance and
revocation remain blocked until separate exact recovery restores that originally
authorized record. Recovery uses the existing private review directory and its
identity, current original source and exact durable history. It grants no new
review, execution, release or model authority and does not rerun a task.

Set `record_sha256` to the known missing review or revocation SHA. Using the
`manual_skill` shell helper above:

```sh
record_sha256=EXACT_MISSING_REVIEW_OR_REVOCATION_SHA256
manual_skill recovery-preview --record-sha256 "$record_sha256"
recovery_sha256=EXACT_PREVIEWED_RECOVERY_SHA256
manual_skill recover \
  --record-sha256 "$record_sha256" \
  --confirm-recovery-sha256 "$recovery_sha256" \
  --human-confirmation RESTORE_ANCHORED_REVIEW_RECORD
manual_skill review-read --review-sha256 "$review_sha256"
```

Preview is read-only. Recovery requires the full literal and exact recovery hash
and recreates only the missing original immutable record from database history.
It adds no authorization/history row and never overwrites an existing corrupted
record. A changed source, destination or confirmation fails closed. Restoring a
revocation keeps the review revoked. After an uncertain recovery response, read
the exact review status before taking any further action; do not blindly retry.

Targeted CPU CLI checks:

```sh
.venv/bin/python -m unittest discover -s tests -p test_owned_parameter_skill_cli.py
```

Synthetic CPU acceptance verifies this manual provenance and development-review
path only. Native two-application acceptance, skill release and execution
admission remain open. The CLI checks also exercise exact review/revocation and
anchored missing-record recovery, independent readback,
fresh-process public commands and genuine original evidence created under a
fresh ignored `data/` runtime directory, with the private project and candidate
outside the checkout, and reject other source-tree paths.
