# Extending AOS

This is an implementation guide, not a promise of a stable plugin API. AOS is a typed application with narrow seams; third-party extension compatibility is not yet versioned. Start by identifying the product behavior, permission boundary, failure modes, and evidence needed for acceptance.

## Current integration seams

- `src/aos/contracts.py` defines canonical state, options, predictions, actions, error types, and serialization helpers. Persisted changes require coordinated model/schema/example/validator updates and, when necessary, a new migration.
- `src/aos/supervisor.py` exposes the `Supervisor` protocol (`plan(problem, evidence)`) and a pinned Bonsai implementation. A supervisor proposes plans; it does not directly execute tools.
- `src/aos/decision.py` defines `DecisionEngine.decide(state, options)`. Keep options finite and host-generated. Parse model output into typed predictions and reject missing, extra, or invalid choices.
- `src/aos/operator.py` owns the state/observe/decide/execute/verify loop. `src/aos/computer.py` defines `ComputerRuntime`; `src/aos/desktop.py` and the gateway/controller layer provide bounded runtimes and authenticated control.
- `src/aos/desktop_tasks.py` and `src/aos/desktop_console.py` implement a specific scheduler and HTTP surface, not a general-purpose job or plugin framework. New actions need server-side authorization, persisted intent, stale-state checks, bounded execution, approval, and independent verification.
- Dataset, learning, and owned-skill modules use explicit schemas, pins, consent/review records, and private artifacts. They do not imply a general automatic training pipeline or deployment promotion.

Treat these seams as internal until a versioned extension contract is deliberately introduced. Avoid framework replacement or broad refactoring when a small typed integration suffices.

## Adding a runtime or model integration

1. Define the smallest typed input/output and stable identity fields. Do not let model output select paths, hosts, credentials, permissions, or arbitrary tools.
2. Create a separate adapter or runtime instead of changing an existing backend's behavior implicitly. Pin model/code/artifact hashes and runtime configuration; fail closed if they drift.
3. Put every effect behind an allowlisted gateway/runtime operation. Bound input/output sizes, time, retries, request count, process lifetime, network targets, and filesystem scope.
4. Persist intent before an effect. Validate the current state, lease, runtime, and approval again immediately before execution. On cancellation, drain children and revoke capabilities.
5. Verify outcomes independently of model confidence and exit status. A successful API response is not automatically proof that a user-visible task succeeded.

## Learning and continued improvement

The intended direction is opt-in experience capture → minimized/redacted role-separated S1/S2 candidates → explicit review → immutable, versioned data → separately budgeted training → held-out evaluation → explicit promotion and rollback. These are separate stages and require data rights and evidence. The longer-term aim is to use verified failures to improve a user's own system; it does not mean that raw failed traces can be converted automatically into training targets.

### Portable records, model-specific consumers

Canonical AOS schemas are the portable, versioned record boundary: examples include `schemas/system1_choice.schema.json`, `schemas/system2_supervisor.schema.json`, role-specific owned episode candidates/exports, and versioned skill/recipe records. Portability means the record has an explicit AOS schema and provenance; it does **not** mean that different tokenizers, prompts, action vocabularies, or trainers consume it identically.

The current reviewed-episode converter makes this distinction explicit. System-1 choice records convert to the pinned Decider Example/Q shape and have a separate tokenizer preflight. System-2 records preserve the original pinned Bonsai system/user messages and structured plan target in a distinct format. The exported conversion still marks `trainer_compatible: false`; model/template/tokenizer changes need a new converter identity and their own tests. See `src/aos/owned_episode_conversion.py`, `docs/OWNED_EPISODE_PREPARATION.md`, and `docs/DATASET_PIPELINE.md`.

Existing scoped page/route/skill knowledge can provide bounded retrieval evidence for supported workflows. It is not a general, continuously refreshed RAG corpus. Retrieved text remains untrusted evidence: retrieval, citation, or a high similarity score does not grant an action, host, account, or data-use permission. A future RAG adapter needs source rights, freshness/provenance checks, bounded retrieval and independent evaluation.

The [document knowledge backend](DOCUMENT_KNOWLEDGE.md) now provides explicit publication/review, immutable source/chunk provenance and deterministic scoped lexical retrieval. Reuse its canonical schemas and pre-ranking validity filters rather than bypassing review in a connector. Model-context consumers, embedding backends and answer-quality evaluation remain separate additions; corpus partitions do not replace target-system authorization.

### Failure-derived candidates

Keep the source model request, prediction, selected action, actual result, and verifier evidence linked but distinct. `needs_human`, timeout, invalid output, rejected action, or failed verification is evidence of a problem—not an answer label. To learn from it, add a separate correction record with a source reference, corrected target, correction author/method, and independent verification/review status. Never overwrite the original event or infer a corrected target from model confidence alone. Until that correction contract exists for a workflow, preserve the failure for diagnosis and exclude it from supervised targets.

Some experimental synthetic/local paths exist: S1/S2 capture and review/export, S1 tokenizer/adaptation experiments, per-task adapter runtimes, and same-case comparisons. They do not establish a general continuous trainer, arbitrary-system capability, independent held-out quality, or real-site acceptance. The narrow rank-4 S1 adapter experiment is not a general fine-tuning pipeline; QLoRA is not established, and the S2 plan data does not currently imply S2 training compatibility. Follow `docs/STATUS.md` and the relevant `docs/OWNED_*` guides for actual evidence.

S1 and S2 are separate roles and may be upgraded independently only through a new pinned model/deployment identity and compatible adapter/converter. Validate each candidate against its own canonical request/response contract and role-specific regression suite, then evaluate on leakage-separated held-out data. Promotion must be a separate, explicit, reversible operation; keep the previous known-good deployment available for rollback. No current path silently replaces the active model. The intended end-to-end improvement loop and its implemented/open stages are summarized in [`docs/CONTINUOUS_IMPROVEMENT.md`](CONTINUOUS_IMPROVEMENT.md).

## Verification and contribution

Add focused tests for valid behavior, malformed output, stale pins, denied actions, source drift, cancellation, cleanup, and compatibility. Synthetic examples must say they are synthetic. Inspect `scripts/check_capabilities.py --list`; use `--profile core` for broad non-GPU contracts. UI/transport profiles need their isolated setup. Real profiles can use CUDA, Docker, and browsers and are opt-in; schedule them only with explicit authorization and no conflicting live work. Avoid the full `test_local_app.py` module on a host with a running service. The contributor setup uses Python 3.11+ and installs `.[dataset,browser,desktop]` plus validation requirements before the core profile; see `CONTRIBUTING.md`.

Update `docs/STATUS.md` only with executed evidence. Keep fixture, real-model, real-browser, and real-site claims distinct. See `CONTRIBUTING.md` for setup and patch expectations.
