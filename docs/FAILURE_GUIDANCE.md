# Reviewed guidance in a fresh decision

## Implemented scope

Tasks can separately authorize two finite, reviewed instructions for a new fixed Hello task: use the fresh observation, or inspect the current result before retrying. The selected instruction comes from an accepted, current [failure review](FAILURE_IMPROVEMENT.md). A new [follow-up](FAILURE_FOLLOWUP.md) retains the source and task bindings and requires fresh manual action approval.

The instruction is appended to the new task's actual observation before its DECIDE snapshot and model request. The text is defined by `hello-guidance-v1`; users and models cannot supply arbitrary correction text through this API. `request_clarification` and `repair_environment` remain unavailable for this application path. The original failure and original request are preserved.

## User flow

1. Preview, separately save and accept a failed/cancelled Hello task's metadata review. Choose **Refresh the observation** or **Inspect before retrying**.
2. Open **Use reviewed guidance** and request a preview while the session is idle and AGENT-controlled. Preview creates no task or private intent.
3. Give the separate guidance-use consent and exact preview confirmation. Start creates a fresh linked task under the current control lease. The existing ordinary follow-up without guidance remains available separately.
4. Inspect and manually approve the fresh effect. Conflicting existing file content remains a human-resolution case; guidance cannot permit overwrite or alter paths, content, options, tools or policy thresholds.
5. Inspect **Guidance evidence**. Context preparation, successful real-model request evidence, current source/review and independently verified task outcome have separate fields.

Old backends advertise no guidance capability and receive no guidance requests from the updated UI. Starting a new backend is necessary to expose this new Python capability; rebuilding static UI does not upgrade a running process.

## Persisted evidence

Separate immutable private intent files under `failure-guidance/` bind consent, nested follow-up preview, finite code and versioned context hash. This directory lives beside the session database, is Git-ignored and excluded from source packages. Guidance and ordinary follow-up admissions are written in the same job transaction. No existing migration is rewritten.

The context observation binds source/review hashes, exact intent, job/run/step, DECIDE snapshot/state hash, unchanged options and exact decision-request hash. It is committed with the model-call request before invocation. Source/review/configuration/control are guarded before context preparation, before recording and at manual-approval/effect boundaries. A changed or revoked source stops further execution.

Inspection reads one frozen SQLite snapshot for both guidance and nested follow-up evidence. `context_binding_verified` checks the exact versioned text and state/request/decision association. `model_request_verified` additionally requires a successful real S1 call whose deployment, recorded request and returned prediction match the linked decision. `guidance_applied` is true only when both fields are true. A fixture engine can verify context preparation but cannot claim real-model application. The ordinary schema-1.0 follow-up report retains its original false guidance flag; application evidence belongs to this separate report.

Source revocation after completion does not erase historical context or outcome evidence. It prevents a new guided task and is shown separately as current review validity. Missing/tampered context evidence fails closed even when the ordinary task result still verifies.

## API and schemas

Authenticated exact-body POST operations under `/api/tasks/failure-guidance/`:

- `preview`: schema version, source job, candidate and accepted receipt hashes.
- `start`: exact returned preview, separate `consent: true`, confirmation hash and current lease/generation.
- `inspect`: schema version and linked job selection; read-only.

Canonical responses: [preview](../schemas/failure_guidance_preview.schema.json), [start](../schemas/failure_guidance_start.schema.json), [report](../schemas/failure_guidance_report.schema.json). Matching examples are synthetic. Extra/duplicate fields, query scope, foreign/stale identities and arbitrary correction content are rejected.

## Remaining acceptance

The finite instruction can also be explicitly published and selected through [same-session Hello guidance reuse](HELLO_GUIDANCE_REUSE.md). This separate permission does not turn the original failure into a corrected training label or a generally portable skill.

Model-input application and a later verified outcome do not establish that the instruction caused an improvement. `causality_verified`, `gold` and `training_ready` remain false. Controlled correction/regression comparisons, reusable skill/RAG items, role-specific labels, prior-consent continuous capture and I1–I5 completion still need their own evidence. This fixed local contract does not satisfy private-site W1–W6 acceptance. Executed tests are recorded in [STATUS](STATUS.md).
