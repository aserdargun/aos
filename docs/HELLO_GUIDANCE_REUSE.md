# Reusing reviewed Hello guidance

## Scope

A reviewed failure instruction can be published as an immutable, content-addressed entry and explicitly selected for another fresh Hello task. Version 1 supports only the developer-defined `refresh_observation` and `inspect_before_retry` instructions from [failure guidance](FAILURE_GUIDANCE.md). It is not arbitrary skill generation, vector retrieval, general RAG, cross-session portability or an independently verified improvement.

The entry is bound to its accepted failure candidate and review, exact task contract, System-1 deployment, session, runtime/image, configuration and descriptor-backed workspace identity. It contains finite instruction metadata, not raw screenshots, secrets or corrected training answers. Publication alone authorizes no execution.

## Separate permissions

1. Save and accept a supported failed Hello metadata review.
2. Request a publication preview. This reads evidence without writing an entry or starting a task.
3. Confirm the publication hash with separate consent under the current control lease. The private entry is immutable and idempotently published by its content hash.
4. Select that exact saved entry and request a fresh reuse preview. Source, review and scope must still match; reuse creates a new attempt, not a replay of the failed action.
5. Separately consent to execution with the new preview hash and current lease. The fresh job has its own guidance/follow-up/reuse admission and requires manual effect approval. Approve-all cannot replace that permission.
6. Inspect the reuse report. Entry/admission binding, current entry validity, actual guidance request evidence and independently verified fresh-task outcome are separate claims.

The same entry may be selected again, but each execution requires a new preview, consent and approval. A successful Hello file result does not demonstrate that the instruction caused improvement. `causality_verified`, `gold` and `training_ready` remain false. Fixture models cannot establish real model application.

## Scope and revocation

Another session, workspace, runtime, role or deployment cannot adopt the entry silently. Configuration drift, changed source evidence, revoked review, unsupported text and altered private records fail closed. The entry is checked at admission and the observation, model and effect boundaries; model output cannot broaden authority.

Revocation prevents future reuse and further effects. It does not erase independently established historical application or outcome evidence. The report shows historical binding separately from current validity. Source revocation is not a claim of automatic model unlearning.

Private records live under `hello-guidance-reuse/` beside the session database. Keep them, trajectories and runtime artifacts outside source packages. No weights or datasets are produced, no active deployment changes, and no existing migration is rewritten.

## API

Authenticated exact-body POST operations under `/api/tasks/hello-guidance-reuse/`:

- `publish-preview`: exact failed-job, candidate and accepted receipt selection.
- `publish`: exact publication preview, hash, consent and current lease/generation.
- `inspect`: exact saved entry hash; read-only current validity inspection.
- `reuse-preview`: exact saved entry hash; fresh bounded execution preview.
- `start`: exact reuse preview, hash, separate consent and current lease/generation.
- `report`: exact linked fresh job ID; read-only evidence inspection.

Canonical schemas and explicitly synthetic examples use `hello_guidance_entry` and the `hello_reuse_*` prefix. A missing backend capability is not support: a running old backend needs a separately controlled restart before exposing the new Python feature. Static UI rebuilds do not hot-upgrade it.

## Remaining acceptance

This is one finite, same-session I2 slice. General knowledge ingestion/search, cross-system skills, evaluated retrieval relevance, continuous capture, controlled causal correction and real-model reuse acceptance remain separate work. See [STATUS](STATUS.md) for executed evidence, [continuous improvement](CONTINUOUS_IMPROVEMENT.md) for I1–I5 and [private guidance acceptance](FAILURE_GUIDANCE_REAL_ACCEPTANCE.md) for the real-model test boundary. It does not satisfy private-site W1–W6 acceptance.
