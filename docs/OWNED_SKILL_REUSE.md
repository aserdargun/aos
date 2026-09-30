# Selected owned skill reuse in a new session

This is an opt-in, synthetic owned-form development path. It reuses a reviewed and selected S1 recipe in a **new** managed desktop session after the previous managed session has verifiably stopped. It is not crash recovery, task resumption, autonomous permission, production activation, training, or real target-site acceptance. See [STATUS](STATUS.md) for executed acceptance evidence; an implemented path is not automatically verified.

## Operator flow

In Tasks → **Load version catalog**, use **Prepare new-session reuse** for the selected family to prepare the terminal commands. This explicitly re-reads the catalog and current source; it does not bind a task, stop the session, execute a command, or authorize reuse. Only an unchanged selection with an accepted review gets instructions. Save the instructions **before** stopping, because this UI belongs to the session being stopped. The commands apply only to a source managed by `./scripts/aos-v1`; an isolated test backend is not the ordinary manager's source. Busy/reserved state or a new catalog operation removes the displayed instructions. A displayed command remains an informational snapshot: the CLI must inspect the stopped source again.

The start template already contains the selected release and selection hashes. Replace its literal `PREVIEW_SHA256` with the exact checksum returned by the separate stopped-source preview; the template is not runnable authorization. Do not stop an unrelated session, delete `current.json`, omit the confirmation, or start a fresh baseline between stopping the source and reusing it. The existing macOS port-8765 tunnel stays the same; obtain the new token after a successful start.

1. In an owned-v1 session, complete the demonstration, save a candidate, execute and audit fresh development evidence, accept its review, then publish and explicitly select a [skill release](OWNED_SKILL_RELEASES.md).
2. Stop that owned session cleanly. Do not stop an unrelated ordinary session to use this workflow. A live, failed, unresolved, or uninspectable previous session is rejected; there is no automatic orphan cleanup.
3. On CachyOS, obtain a read-only preview using the exact release and selection hashes:

   ```sh
   ./scripts/aos-v1 preview-owned-skill-reuse \
     --owned-skill-release-sha256 RELEASE_SHA256 \
     --owned-skill-selection-sha256 SELECTION_SHA256
   ```

4. Inspect the preview. Start only that exact preview in a new session:

   ```sh
   ./scripts/aos-v1 start \
     --owned-skill-release-sha256 RELEASE_SHA256 \
     --owned-skill-selection-sha256 SELECTION_SHA256 \
     --owned-skill-reuse-confirm-sha256 PREVIEW_SHA256
   ```

5. Sign in using the **new** session token. In Tasks, load the version catalog and explicitly bind the selected version. Enter a fresh development case/value, preview, confirm, and start. Six fresh manual action approvals are still required. The original demonstration is not replayed, and opening the session does not start any task.
6. After completion, request the separate execution audit. Its reuse admission must identify the new manager, desktop session and runtime while retaining the original candidate, recipe, review, release and selection.

The public manager still uses port 8765. The source fixture must reacquire its exact previous loopback TLS port; a conflict fails rather than changing the origin. This feature does not change the macOS SSH tunnel command.

## Source and authority boundaries

The manager derives the source from its immediately previous stopped state under the same managed base. A prior reuse session retains a pointer to the original source session. Caller-selected source directories, copied databases, restored leases, restored approvals and automatic fallback to another release are not supported.

The source's owner-private `owned-form` directory, immutable artifacts, selection history, revocations and trajectory SQLite database remain in place. The new manager has its own workspace, logs, token, lifecycle journal, runtime and controller. Database identity stays pinned while new trajectory rows are appended. Old tasks are not assigned as the new scheduler's current task.

A descriptor-pinned exclusive workspace lock passes from launcher to supervisor to backend and stays held for their lifetime. Closing the launcher's copy does not unlock a child process's copy. A competing owner, replaced path or lost lock invalidates admission.

Before new controller creation, the offline preview requires real source and review-evidence audits, the exact current selection and accepted review, verified shutdown journals/process observations, terminal previous sessions/jobs/runs and no unresolved actions, approvals or queued inputs. A preview grants no execution or training permission. Startup rechecks its canonical files and source under the inherited lock before opening the shared store for use.

After the new controller exists, admission binds that exact preview to its new manager/session/runtime/lease/generation. Each selected run and pre-action gate rechecks admission and existing source/review/release protections. Pending approval cannot bypass a review revocation. The baseline demonstration fixture closes before the API is exposed; candidate runs acquire their own bounded fixtures.

The reused session does not enable the managed metadata-retention collector. Its shared-source lifetime needs a separate retention contract before such collection can be enabled here.

## Contracts and evidence

- `owned_skill_reuse_preview`: canonical, non-authorizing stopped-source preview and exact source identity/audit/selection pins.
- `owned_skill_reuse_admission`: canonical preview plus fresh runtime/controller identity, persisted privately in the new manager directory.
- Candidate execution schema `1.3`: all `1.2` review/release/selection pins plus `reuse_admission_sha256`. Older execution modes cannot bypass the new-session admission requirement.
- `skill.owned_reuse_admission`: pre-action observation joining the execution to the new admission; an offline audit must independently verify its persisted bundle and run/session/runtime links.

Consumed preview history is restored separately from per-session start limits. Complete bundles burn previews even without completion records; malformed or partial history blocks new starts. Deleting an entire historical bundle is not detected by a cryptographic completeness ledger; this existing [replay inventory limitation](OWNED_CANDIDATE_EXECUTION.md) remains explicit.

The managed acceptance test uses two real supervisor subprocesses on ephemeral API ports, not a supervisor thread whose process remains live after shutdown. It must prove the original process exited, source DB identity stayed unchanged, no old work started on reopen, fresh approvals were required, and selected execution ran under a different desktop/runtime. Synthetic contract tests and mocked manager handoff tests do not provide this runtime evidence.

```sh
PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_OWNED_SKILL_REUSE_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_owned_skill_reuse_managed.OwnedSkillReuseManagedTests -v
```

This test uses owned synthetic values only, requires the installed pinned GPU/browser runtimes, and intentionally executes six separately approved actions per successful run. Failed acceptance preserves private diagnostic artifacts under the isolated test base; screenshots remain outside the source package.
