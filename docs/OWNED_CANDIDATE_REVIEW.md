# Evidence-bound candidate review

This explicit human-review path applies only to saved owned-v1 synthetic candidates and successful source-bound development executions. It does not mark a skill validated or active, authorize training, or provide independent held-out evidence. Implemented and measured behavior is recorded separately in [STATUS](STATUS.md).

## User flow

1. Finish and audit an owned-v1 demonstration, save its candidate, then [execute and audit](OWNED_CANDIDATE_EXECUTION.md) that candidate with a fresh development value.
2. In Tasks, choose **Review the latest audited execution**. The server reloads the candidate, original source, execution bundle and completed run. Preview creates no review, task, model call or action.
3. Inspect the candidate's skill/outcome names, parameter-to-field mapping, ordered steps and evidence case/run. These names are operator declarations, not automatically verified semantics. Check the separate confirmation and choose **Record review** for the exact receipt hash.
4. The saved hash can be copied or manually re-entered after a reload using **Reinspect review receipt**. Acceptance does not automatically start a task or bind subsequent runs. For a new value, explicitly select **Bind this run to the selected review; stop if revoked**. Preview/start must preserve that exact review hash and still require the normal six action approvals.
5. **Revoke review** requires its own exact confirmation. It is available during a review-bound run. Revoking the matching pending run invalidates its approval and fixture authority before further actions; an already-submitted effect cannot be undone. Revocation is permanent for that receipt and does not erase historical success.

The existing unreviewed development path remains explicitly available. Selecting review-bound execution cannot silently downgrade to that path after an error, changed receipt or revocation. An acceptance made after a run does not retroactively make that run review-bound.

## Persistence and authority

The receipt is content-addressed and immutable. It binds the candidate, source group/fingerprint, profile, skill/recipe, original invocation and one successful development execution's stable case/run/invocation/parameter pins. The displayed semantic summary is inside the hashed receipt. The reviewer is the server's fixed authenticated-local-user label, never a browser-supplied identity. This is local-console authorization, not external identity verification.

Receipts exclude timestamps and changing whole-database snapshot/report hashes so unrelated subsequent runs do not change what was accepted. Current evidence must still revalidate before acceptance, inspection or a new review-bound action. A separate append-only revocation takes precedence; re-accepting the same logical review cannot reopen it. A changed source may block use or positive reinspection but cannot prevent revocation of an intact saved receipt.

Reviewed execution uses schema-1.1 request/preview/bundle metadata with `review_sha256`; unreviewed schema-1.0 content stays compatible. A distinct review admission observation binds the receipt before the first action. Audit verifies this binding separately from the existing source-bound execution report. Historical execution success and current review eligibility (`accepted`, `revoked`, `unavailable`) are different facts.

The scheduler audits the completed task, base execution and review-admission timing in one frozen database snapshot. Admission must lie between the initial CREATED state and the next state, before the first action, and match the selected receipt. Historical audit also binds the receipt's candidate/source/recipe/profile pins, including after revocation. The standalone base-only persisted auditor rejects schema-1.1 bundles rather than returning an apparently unreviewed report; use the review-aware scheduler envelope for reviewed execution. Offline receipt inspection separately revalidates accepted evidence without a running fixture or network.

Private receipt/revocation files are not datasets, model deployments or public source artifacts. Do not put secrets, screenshots, real trajectories or private databases into the source manifest. No active model/skill pointer changes, no permissions expand and no training runs as a review side effect.

## HTTP contract

Authenticated, origin-controlled endpoints under `/api/tasks/owned-form-candidate/`:

- `review-preview`: exact `candidate_sha256`, `source_run_ref`, `source_invocation_sha256`, `candidate_execution_sha256`.
- `review-accept`: the same selection plus matching `review_sha256` and `confirm_sha256`; the server rederives the proposal instead of trusting cached UI data.
- `review-inspect`: `review_sha256` only.
- `review-revoke`: matching `review_sha256` and `confirm_sha256`; valid while busy, and no source revalidation prerequisite for revocation.

Paths, URLs, actor identity, activation or training claims are rejected. Review-bound execution preview/start use `schema_version: "1.1"` and an explicit `review_sha256`; absence requires the existing `"1.0"` contract. A `"1.1"` audit wrapper keeps the base execution report unchanged and separately exposes `review_sha256`, `review_admission_verified` and `review_status`.

## Acceptance

```bash
PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_OWNED_CANDIDATE_REVIEW_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest test_owned_candidate_review_managed -v
```

Managed acceptance covers source → candidate → development evidence → explicit UI acceptance → new review-bound development run → audit → revoke at another run's pending fill → no POST, plus source/hash/downgrade/reopen/persistence negatives. Consult STATUS for the measured results and the distinction between real-model runs and mocked UI/unit checks. Real target/account/oracle, independent skill validation, activation/rollback and S1/S2 training remain open [release gates](WEB_APPLICATION_LEARNING.md).
