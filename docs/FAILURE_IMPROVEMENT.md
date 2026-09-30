# Failed-task improvement review

## Scope

This workflow starts from an actual recorded failed or cancelled task in the current desktop session, not from an invented training example. Tasks offers a deliberate preview, explicit permission to save minimized metadata, a separate human review with finite improvement guidance, and revocation. It does not retry the task or call a model.

This is the first diagnostic-review slice of [continuous improvement](CONTINUOUS_IMPROVEMENT.md), not a completed autonomous learning loop. It is **retrospective opt-in**: a user requests review after failure. It does not claim that prior content-collection consent existed or continuously collect every failed task. The separate owned-episode successful-data exporter and its stricter prior consent remain unchanged.

## User flow

1. In **Tasks**, select a failed/cancelled job and request its improvement preview. No candidate file is created merely by opening Tasks or previewing.
2. Inspect the failure code, actually recorded S1/S2 call counts, and approval/action disposition. These facts are not proof that a model caused the failure; an environment, verifier or policy can be responsible.
3. Explicitly consent to local metadata use and save the exact candidate hash. The candidate is private, immutable and deduplicated. Approve-all task execution does not grant this permission.
4. Choose separate guidance: refresh observation, inspect before retry, request clarification, or repair environment. Accept that guidance or reject the candidate using the server's exact selection confirmation. Accepting guidance is not approval to perform it.
5. Revoke a stored review when appropriate. An exact stored receipt can be revoked even if the original evidence is no longer current; revocation grants no new permission and does not erase the historical source.

Mutating operations require an idle, running AGENT-controlled session with a fresh lease and generation. Restart/quiesce, human control, an active/reserved task or a stale UI selection prevent writes. The available job list is bounded by the existing Tasks view and this session; this is not a cross-session learning-library browser.

## Data and evidence

The source is read through a frozen trajectory snapshot. The service binds the job to its session, run, terminal typed state, runtime and model deployment identities; source fingerprints also bind the desktop job and approvals. Normal unrelated tasks/control changes must not rewrite an old incident. Changed source, malformed identity, missing evidence, a foreign session or mismatched hashes fail closed.

Only typed metadata is projected: source/scope hashes, task kind, terminal outcome/code, actual role call-status counts and action/approval counts. Goals, raw requests/responses, exception detail, option labels, URLs, form values, screenshots, credentials and hidden reasoning are not copied to the review artifact or returned by this API. No S2 call is invented when only S1 ran. Fixture-model evidence stays fixture evidence.

The initial source audit requires settled actions. In-flight/uncertain effects and `waiting_human` runs are not accepted as terminal review sources; inspect/reconcile them through the existing authorized workflow, never auto-retry to make a candidate. A job that failed before a valid run/state was stored can also be unavailable. “Unavailable” is not a successful correction or a reason to weaken source checks.

Artifacts live under the selected session database's parent in the private `failure-improvements/` directory, outside the public source archive. This directory name is also Git-ignored, including for a custom database location inside a checkout. Candidate/review/revocation files use bounded, hash-checked, private storage. This metadata consent does not authorize future prompt capture, company data export, dataset creation or training. Keep private records out of Git even in a fork.

## API and contracts

Authenticated, exact-body POST operations are under `/api/tasks/failure-improvement/`:

- `preview`: schema version and current-session job selection; read-only.
- `save`: exact candidate confirmation and `consent: true`, plus current control identity.
- `review`: saved candidate, accept/reject, finite guidance and exact review confirmation, plus current control identity.
- `revoke`: saved candidate and exact review receipt, plus current control identity.

Query scope, extra or duplicate fields and stale control are rejected. Errors omit private paths and raw source details. The canonical response is [failure_improvement.schema.json](../schemas/failure_improvement.schema.json); the [example](../examples/failure_improvement.json) is explicitly synthetic. The backend advertises `failure_improvement_available`; older backends do not silently gain this capability from a new static UI.

`gold`, `training_ready`, `execution_authorized` and `failure_attribution_verified` remain false. The review is separate from dataset accept/revoke receipts, successful owned episodes, skill activation and deployment promotion. It does not produce a correct-option training label or automatically mark a suggested fix verified. A separately authorized [follow-up task](FAILURE_FOLLOWUP.md) can now bind this review to fresh execution and independently audit its persisted outcome. Guidance application/causality, role-specific corrected targets, skill/RAG integration and training conversion remain open.

An already running backend is not upgraded by rebuilding the static UI. Use a new authorized session with the updated backend to exercise this capability; never restart a live user task merely to expose the panel. A legacy backend shows an unavailable message and receives no improvement write requests.

Executed tests and limitations are recorded in [STATUS](STATUS.md). This feature does not satisfy real-site W1–W6, general RAG, fine-tuning or model-upgrade acceptance.
