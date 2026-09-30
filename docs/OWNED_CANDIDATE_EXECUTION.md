# Managed source-bound candidate execution

This development path reuses a saved schema-1.1 candidate from the existing owned-v1 synthetic demonstration. It does not promote the candidate, claim independent held-out evidence, or authorize a production site. Implementation and measured acceptance are tracked separately in [STATUS](STATUS.md).

## User flow

In a fresh `--owned-synthetic-form-invocation` session, finish and audit the demonstration, then explicitly preview and save its candidate. Tasks offers a separate saved-candidate execution panel. Enter the candidate hash, a development case key and a new synthetic value. The field accepts 1–128 ASCII letters, digits, spaces, underscores, dots and hyphens, without surrounding whitespace. Do not enter credentials, secrets or personal information. Case keys use lowercase letters, digits and hyphens, starting with a letter.

Preview performs no network requests, model calls or task actions. It reloads the candidate/source and compiles exact new form/state plans, case inputs, a v2 recipe invocation and a candidate admission. Inspect the shown execution hash and ordered steps. A separate checkbox and Start confirm that exact preview and the current desktop control lease/generation. The normal six manual action approvals still apply; this is not Approve all.

Completion alone is not a source-bound execution claim. Use the separate execution audit button to verify the source, admission recorded before actions, disjoint S1 events, actual recipe sequence, receipt and declared-state readback. Only then does the UI display `source_bound_development_execution`. Another development case requires a new preview and explicit start confirmation. Original demonstration status and candidate execution status are distinct.

The development session allows at most four accepted candidate starts. A consumed preview cannot be replayed, including after a failed run. This bounded experiment is not an unattended production runner.

Before starting, the scheduler also reloads the private execution inventory against the audited source run, invocation and manifest pins. A complete persisted bundle burns its preview even without a completion record, including an attempt that failed to bind its fixture port. Direct/review-only and selected-version replay histories remain distinct from their separate four-start session counters; historical attempts do not consume a new session's counter. A failed job creation after the counter is consumed does not refund it.

The scan reads at most 256 bundles and rejects partial, malformed, changed, foreign-source or non-private entries as a whole. A missing inventory is permitted only for a verified successful demonstration belonging to the current controller session with no local execution history. It is not inferred from empty process memory alone. Nothing automatically deletes or repairs invalid history. The scan cannot prove that an entire historical bundle was never deleted; cross-session reopening still needs independent source/trajectory reconciliation and explicit new-session admission. This change does not implement reopening or revive an old lease, approval or task.

## Source and runtime boundaries

- The original source manifest, profile, page, task, values and invocation remain unchanged. The candidate retains its source lineage group; a descendant cannot become a fresh independent demonstration.
- The execution recipe and profile/origin pins remain the candidate's pins. New values produce new exact body/state hashes, parameter variants and invocation/admission bindings.
- A confirmed execution uses a **new** private fixture capability at the exact original loopback port with the original pinned certificate. Revoked targets are never reset. An occupied port must fail closed, not switch ports or adopt another listener.
- The browser cannot supply paths, URLs, source parameters, profiles, a source database or a target runtime. It can select only the saved candidate hash/source pins and the bounded development input.
- Candidate/source changes must be rejected during preparation and before every action. The existing scheduler, gateway, manual approvals and deterministic independent verification remain authoritative.
- Execution bundles are private artifacts separate from the source bundle. They are not training exports or public examples. Readback hashes are integrity evidence, not anonymization.

The canonical [bundle schema](../schemas/owned_candidate_execution_bundle.schema.json) binds all eleven execution artifacts, the source and preview identities, and the execution admission. Each action reloads those artifacts against the saved manifest pin. A separate append-once [completion](../schemas/owned_candidate_execution_completion.schema.json) binds the successful job/run. Offline auditing reconstructs the report from these private artifacts and the original source/database; an in-memory scheduler result is not required. Missing or changed artifacts fail closed.

## HTTP surface

All routes require the existing local session authentication and origin controls. Under `/api/tasks/owned-form-candidate/`:

- `POST execution-preview`: `schema_version`, `candidate_sha256`, `source_run_ref`, `source_invocation_sha256`, `case_key`, `development_value`.
- `POST execution-start`: the same selection plus `preview_sha256`, identical `confirm_sha256`, and current `lease_id`/`generation`. The server rederives instead of trusting the preview response.
- `POST execution-audit`: exact `candidate_execution_sha256`. An unavailable report cannot be presented as success.

`GET /api/tasks` exposes `owned_form_candidate_execution` separately from the original `owned_form_invocation`. It contains hash-only source/execution identities, the case key, ordered steps and lifecycle, not the entered value. The frontend invalidates previews on edits/control changes, and discards asynchronous results from a different source/runtime/execution.

## Acceptance boundary

The explicit managed real-model test is:

```bash
PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_OWNED_CANDIDATE_EXECUTION_TESTS=1 \
  .venv/bin/python -m unittest test_owned_candidate_execution_managed -v
```

The test must cover a real demonstration and saved candidate, two fresh inputs through Tasks and six approvals each, unchanged original source bytes, source-bound audit, and candidate mutation before POST. Route-injected UI tests support malformed/stale-response and bilingual checks; they are not real model acceptance. Results, failures and unverified scope belong in STATUS, not inferred from this command.

Real target account/oracle, semantic generalization, independent held-out quality, reviewer authority, activation/rollback and S1/S2 training remain separate [first-web-release gates](WEB_APPLICATION_LEARNING.md).
