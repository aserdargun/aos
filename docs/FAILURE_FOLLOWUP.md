# Separately authorized failed-task follow-up

## Implemented scope

Tasks can link an accepted, non-revoked [failure metadata review](FAILURE_IMPROVEMENT.md) to a fresh task of the same supported kind and exact semantic contract in the current session. This is a new execution with separate permission, not continuation of the failed run. The finite guidance is displayed as provenance; it is not injected into model input or automatically applied. `guidance_applied`, `causality_verified`, `gold` and `training_ready` remain false even when the new outcome verifies.

## User flow

1. Preview, save and separately accept a failed/cancelled task's metadata candidate.
2. While the session is idle, running and AGENT-controlled, request **Explicit follow-up task** preview. Inspect the source/review hashes, task kind, deployment and semantic-contract bindings.
3. Check the separate execution consent and enter the exact preview hash. Start creates a new job under the current lease/generation. Every effect requires its ordinary manual approval; approve-all and metadata collection are disabled for this lane.
4. Inspect a linked task's report explicitly. Historical admission, current source validity, current review validity and independent outcome evidence are separate fields. Source/review revocation does not rewrite a historical admission or erase an outcome.

The source and accepted receipt, exact configuration, session, lease, generation and task contract are rechecked at admission, run creation, model calls and approval/consume boundaries. Revocation or source/configuration drift prevents further execution. Paused follow-ups cannot resume through the normal resume path; use a separately authorized fresh attempt. An uncertain external effect remains excluded by the failure-source audit and is never replayed by this feature.

## Evidence and storage

Private immutable intent files live in `failure-followups/` beside the session database. One-shot admission is recorded in existing desktop events, with a run-creation observation before calls/actions; no migration is rewritten. The new run preserves the original failure and its trajectory. API responses contain hashes and typed metadata, not raw goals, model prompts, URL/form values, credentials or exception details. Execution itself still uses the existing authorized task configuration.

The read-only outcome auditor requires a settled successful run, contiguous hash-bound typed state history, decision/action envelopes, actual readback observations and verifier records. Scheduler effects additionally require exact consumed manual approval and its authenticated human audit. Fixed hello, form, canvas, local navigation and staging use their fixed outcome contracts. Remote entry/routes/static/data/form use their narrower transport/readback contracts; a form's declared state readback does not establish an arbitrary application's business result. Malformed, missing, changed or excessive evidence fails closed; unsupported policy contracts are explicit.

## API and canonical contracts

Authenticated exact-body POST operations under `/api/tasks/failure-followup/` are `preview`, `start` and `inspect`. Query parameters, duplicate/extra fields and stale control fail closed. Schemas: [preview](../schemas/failure_followup_preview.schema.json), [start](../schemas/failure_followup_start.schema.json), [report](../schemas/failure_followup_report.schema.json). Matching examples are explicitly synthetic. The backend advertises `failure_followup_available`; static UI does not infer support from an old backend.

The separate [reviewed-guidance application](FAILURE_GUIDANCE.md) lane can add a finite instruction to a fresh Hello decision with its own consent and evidence; the ordinary follow-up contract remains unchanged.

This acceptance slice does not prove that guidance repaired a defect, generate corrected role-specific labels, enable skill/RAG reuse or train/promote a model. Prior-consent continuous failure capture and the full I1–I5 learning loop remain open. Test execution and its fixture/real-model boundaries are recorded in [STATUS](STATUS.md).

## Real-model acceptance

The pinned Decider acceptance starts its own loopback backend and Docker workspace, rejects the original hello write before any effect, accepts metadata guidance, separately authorizes a new attempt, and approves its fresh write through the authenticated API. It checks real S1 call identities, exact consumed approval and independent filesystem readback. Revoking the review afterwards preserves the historical binding/outcome and prevents new follow-up preview. Approvals are driven by the test harness, not an observed human interaction. The target is the fixed local hello task, not a private site or a held-out correction benchmark.

After the explicitly prepared local model/Docker environment is available:

```sh
AOS_DESKTOP_TESTS=1 AOS_FAILURE_FOLLOWUP_REAL_TESTS=1 \
  .venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s tests -p test_failure_followup_real.py -v
```

The existing `real_tasks` capability pool also selects this opt-in test. Direct unittest does not acquire that pool's advisory GPU lock; run only when no other real-model workload is active. No model download, training or promotion is performed.
