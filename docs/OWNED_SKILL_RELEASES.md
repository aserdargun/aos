# Owned skill versions and development selection

This flow gives saved, reviewed synthetic skill/recipe candidates immutable versions and an explicit persistent selection used by subsequent executions. It is not production activation, independent skill validation, a model deployment, or permission to train. Implementation and measured acceptance must be distinguished; consult [STATUS](STATUS.md) for tests actually run.

## User flow

1. Complete the owned-v1 demonstration and [candidate development/review flow](OWNED_CANDIDATE_REVIEW.md). An accepted review must remain valid against its candidate and successful development evidence.
2. In Tasks, **Load version catalog**. Choose **First version**, or a matching latest **Parent version**, then **Preview version**. Inspect the skill, revision, recipe and exact release hash.
3. Check **I confirm publishing this exact version** and **Publish version**. Publication alone neither selects a version nor starts a task.
4. Choose **Preview selecting this version**, inspect the proposed selection event, and confirm **Save development selection**. This compare-and-swap operation uses the exact previously observed selection hash; a stale tab cannot silently overwrite a newer selection.
5. Choose **Bind selected version to a new run**. The existing execution panel pins the candidate, accepted review, release and selection event together. Supply a fresh development case/value, preview and explicitly start. The normal six separate manual action approvals still apply.
6. After success, use the separate execution audit. It verifies source/execution evidence and both review and release admission. A version catalog entry or successful publication is not execution evidence.
7. For a prior version, **Preview rollback to this version** is a separate operation. It requires a previous successful selected execution that can be re-audited and a review that remains accepted. Confirming rollback appends a new selection event; it does not alter prior events, undo site effects or repeat an old task.

Catalog loading after a page reload reads the persistent choice but does not start a task or automatically bind a new execution. Review revocation or changed source artifacts prevent use; the system must not silently fall back to another version. Direct development and review-only execution remain explicitly available as separate modes, not error recovery fallbacks.

## Scope and persistence

The server derives a family from the exact profile, application, tenant, account role, task key/hash, page draft, skill key, S1 role and field-binding hash. Browser requests cannot invent or broaden that scope. Each release pins its immutable candidate, skill, recipe, accepted review, source provenance and development evidence. A new revision has the previous release as its parent and different candidate and recipe hashes; it does not rewrite the embedded original draft revision.

Private files live below the owned-form root in an owner-only catalog. Content-addressed release and selection records are immutable; a separately controlled head records the current event. Mutations use a lock, exact expected-head comparison and durable writes. Invalid inventories, changed files, symlinks and incomplete histories must fail closed. No source artifacts, review receipts, old runs or model registry pointers are overwritten.

If an in-process selection write fails before head replacement, cleanup removes only that attempt's unchanged, unpublished event and leaves the previous selection readable. If head replacement already succeeded, its referenced event is retained even when the following sync fails. Unexpected orphan records or interrupted-process histories require inspection; there is no automatic recovery or power-loss durability acceptance claim. Catalog enumeration uses a fresh descriptor anchored to the pinned family directory so first publication also sees the newly created lock on the workspace's Btrfs filesystem.

Selection/publication require an idle scheduler. A running selected execution pins its event and checks that selection again before actions; a selection change cannot authorize queued input. A successful historical run stays distinguishable from current eligibility: its selection may be `superseded`, while its current review may be `revoked`.

The existing direct/review-only path remains limited to four candidate starts per session. Release-bound execution has a separate limit of four starts, available only after validated release/selection admission. Each start still needs an exact preview confirmation and the original bounded task permissions. An identical consumed preview cannot be replayed. These are development session limits, not general autonomous execution grants.

Persistent replay history is reloaded separately from these session counters. A full execution bundle without a completion record also burns its preview; corrupt or partial history blocks further starts instead of silently disappearing from the replay set. The bounded scan and its missing-inventory/deletion limits are described in [candidate execution](OWNED_CANDIDATE_EXECUTION.md). It does not reopen a stopped learning workspace by itself.

The separate [new-session reuse path](OWNED_SKILL_REUSE.md) adds an explicit stopped-source preview and fresh runtime admission. It retains the original source database and selection history rather than copying them or replaying the original task. Its implementation and runtime acceptance are recorded separately in STATUS; catalog persistence alone does not prove new-session execution.

## API and evidence

Authenticated, origin-controlled routes under `/api/tasks/owned-form-candidate/`:

- `GET release-catalog`: server-configured scope; no caller query or body.
- `POST release-preview`, `release-publish`: schema `1.0`, `review_sha256`, nullable `parent_release_sha256`; publishing additionally requires `confirm_sha256` equal to the preview's release hash.
- `POST selection-preview`, `selection-commit`: schema `1.0`, `release_sha256`, nullable `expected_selection_sha256`, and `operation` (`select` or `rollback`); committing adds exact selection-hash confirmation.
- Existing execution preview/start: schema `1.2` requires the review, release and selection hashes together. Existing `1.0` and `1.1` contracts remain separate.

While an execution is `previewed`, task status includes its exact `preview_sha256`. The UI preserves confirmation across polling only for its own matching preview; a different preview, source, runtime or control identity invalidates confirmation.

Selected bundles and a pre-action `skill.owned_release_selection_admission` observation pin the exact version/event. Audit verifies admission against the run rather than treating a later selection as retroactive approval. Release-aware audit returns a `1.2` envelope; a base-only standalone audit must not silently discard its release/review requirements.

## Acceptance target

The end-to-end acceptance is source → evidence/review A → publish/select A → execute A → separate evidence/review B → publish/select B → execute B → explicit rollback A → execute A with restored release/recipe pins and a fresh event/run. It also checks stale heads, busy mutations, changed/revoked sources, missing admission, persistence in a new network-forbidden process, and EN/TR rendering.

```bash
PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_OWNED_SKILL_RELEASE_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest test_owned_skill_release_managed -v
```

The synthetic versions use the supported six-operation form template; distinct recipe hashes do not prove improved behavior or independent generalization. Actual target-site/account/oracle acceptance, independent validation, S2 skills, production activation/rollback and S1/S2 training remain the [full release gates](WEB_APPLICATION_LEARNING.md).
