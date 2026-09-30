# Audited synthetic staging S1 skill candidate

`aos.staging_skill_candidate` derives one **content-free, read-only, unreviewed** S1 candidate from an explicitly selected completed `browser_staging_workflow` run. It does not create a site skill draft, activate a skill, authorize execution, collect site data, issue a gold label, or prepare trainer input. It does not infer a Bonsai/S2 result. The source application and message are fixed synthetic fixtures, not a user-provided website.

```bash
.venv/bin/python -m aos.staging_skill_candidate \
  --database data/private/store.sqlite --run-id RUN_ID
```

To pin a previously inspected database snapshot, also pass `--snapshot-sha256 HASH`. A changed snapshot is rejected rather than silently re-derived. The database is opened through the existing frozen, query-only audit snapshot; the command does not migrate, reconcile, update eligibility or write a candidate file. Store its output only in a private ignored location if needed. The `run_ref`, per-action references and SHA-256 hashes are **linkable**, not anonymous or cryptographically signed.

## Required source

The selected run and desktop job must both be succeeded; the run must have passed outcome and the staging policy. The recorded model identity must be a pinned, real S1 Decider/Laya identity, and the saved environment must attest the owned Docker/Chromium/Playwright MCP staging guard. A fixture decision-engine run is intentionally rejected, even if its browser outcome passed.

Exactly four ordered S1 calls, decisions, manually consumed approvals, effectful actions and separate snapshot readbacks are required. The derivation recomputes each decision request from its hashed `DECIDE` state and finite options, compares the recorded prediction, checks the exact action envelope and fresh pre-action symbolic reference, matches one `local_authenticated_user` approval to that envelope, and checks each verification against an independently recorded snapshot observation. Only the final receipt outcome with `submissions=1` is accepted; no extra action, call, approval, verification, intervention or S2 escalation is allowed. The worker's fixed request guard admits one exact local POST; the trajectory alone is not a network packet trace or a transferable proof of real-site behavior.

The canonical output is `schemas/staging_skill_candidate.schema.json`; `examples/staging_skill_candidate.json` has placeholder hashes and is synthetic. It contains only a run/job/snapshot/source/deployment/workflow fingerprint and four ordered stage hashes/references. It contains no raw URL, page text, form value, model prompt, response, approval envelope or credentials. All profile, review, collection, activation, execution and training authority flags remain false. No migration is added because this projection stores nothing.

## Acceptance and limits

`tests/test_staging_skill_candidate.py` validates schema/fixture and no-create CLI failure by default. With `AOS_DESKTOP_TESTS=1 AOS_DESKTOP_MCP_TESTS=1 AOS_REAL_BROWSER_TASK_TESTS=1`, it runs an isolated owned Ubuntu/Chromium/MCP task with the pinned real Decider and four separate approvals, checks deterministic content-free output and rejects modified model input, approval, actor, verification and independent readback. Tests use only synthetic local content. Existing site-skill lifecycle and held-out verification remain separate; a single repeated trajectory is not a validated reusable skill.
