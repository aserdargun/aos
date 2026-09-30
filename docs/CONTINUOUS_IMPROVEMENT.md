# Continuous improvement and replaceable models

## Product contract — target, not completed capability

Applications and optional specialist agents are separate composition choices: the future SWAPP + AI-Scientist private fork is one example, not a dependency of the public core. Additional agents must remain behind typed, scoped boundaries; they do not share unrestricted control of the desktop. See [integration compositions](INTEGRATION_COMPOSITIONS.md).

AOS is intended to help adopters operate their own authorized systems and improve from experience. It is not a SWAPP-specific product. SWAPP is a later private-fork integration; general compatibility with arbitrary systems is not established. System-1 (Operator decision model) and System-2 (Supervisor planning/recovery model) are roles, not permanent dependencies on Decider and Bonsai.

The goal is a controlled learning loop, not an agent that rewrites its own permissions or silently retrains itself. Execution, experience capture, retrieval, training, evaluation and deployment each have distinct authorization and evidence. An execution-level “Approve all” never grants data collection, training, downloads, or promotion rights.

## When a task fails

1. Stop or use the existing bounded recovery budget. Preserve the failure, uncertainty and independently observed outcome; do not replay an uncertain external effect.
2. With prior scoped collection consent, create a private candidate linked to task, application/tenant/role, state, exact deployments and evidence. Record a concise diagnosis, not hidden chain-of-thought. Minimize secrets and personal data before reuse.
3. Separate the failed proposal from a reviewed correction. A later success does not make every earlier decision correct. Missing observations, rejected unsafe actions and permission failures are not positive examples to imitate.
4. Choose the smallest improvement: fix a deterministic integration defect, update stale knowledge, create a bounded skill, or nominate training data. Not every failure requires fine-tuning.
5. Test the correction against the original failure and separate regression cases. Learning must not turn a denied action into an allowed action or erase the historical failure.

Automatic capture here means capture under an existing explicit, revocable scope and budget, not collecting every user's activity by default. This general failure-to-improvement workflow remains a development target; the implemented owned-episode paths have narrower source/admission requirements.

The first implemented diagnostic slice is [failed-task improvement review](FAILURE_IMPROVEMENT.md): an ordinary current-session failed/cancelled task can be explicitly previewed, saved under retrospective metadata-only consent, and separately accepted/rejected/revoked with finite guidance. It copies no raw task/model content and cannot grant training authority. A [separately authorized follow-up](FAILURE_FOLLOWUP.md) creates a fresh, manually approved task with exact source/review/configuration bindings and an independent persisted-outcome audit. The separate [Hello guidance lane](FAILURE_GUIDANCE.md) binds two reviewed finite instructions to the fresh DECIDE observation and actual model request. Context preparation, successful real-model request evidence and outcome are audited separately. Prior-consent continuous collection and causal correction evaluation remain open.

## Three reusable outputs

| Output | Intended content and use | Required boundary |
|---|---|---|
| Skill | Versioned typed procedure, parameters, preconditions, tools and independent success checks | Review before activation; fresh state and task-specific authorization every execution; revocation and previous-version fallback |
| Retrieval / RAG | Reviewed application knowledge with provenance, version, freshness and application/tenant/role scope | Filter access before retrieval; bounded context; treat retrieved text as untrusted data, never instructions that grant authority |
| Training dataset | Separate canonical S1 choices/corrections and S2 plans/diagnoses/recovery examples with source and verified labels | Rights, redaction, review, deduplication and leakage-group splits before model-specific conversion |

Retrieval is for changeable facts and procedures; skills are executable contracts; training changes model behavior. Keep them separate. A changed embedding model requires a new versioned index and retrieval evaluation, not silent reuse of incompatible vectors. Revoking source access must prevent retrieval and future dataset builds; existing trained artifacts require separate withdrawal/retraining assessment, not a claim of automatic unlearning.

## LoRA, QLoRA and fine-tuning

Keep canonical examples independent of tokenizer IDs and model-specific prompt formatting. A selected backend then produces its own tokenizer/template/label-mask inputs and pins that conversion. S1 and S2 datasets, recipes and evaluations remain separate even when their evidence comes from the same episode.

LoRA, quantized-base LoRA training (QLoRA), and other fine-tuning methods are optional backend capabilities, not universal switches. Each needs a supported trainable checkpoint, reviewed code/license, exact base and tokenizer revisions, adapter target modules, quantization/runtime compatibility and a measured memory/time budget. A quantized inference file or successful inference does not establish training support. The current Bonsai GGUF must not be assumed trainable by these paths.

Training runs outside the live execution loop after explicit admission and resource coordination. Publish an immutable candidate and report; independently reload it, evaluate held-out tasks and safety regressions, then request explicit deployment promotion. Never upgrade from a single successful training example or same-case base/adapter comparison.

## Replace either model without discarding experience

The portable layer is canonical task/state/evidence, reviewed knowledge and skills, role-separated datasets, and evaluation suites. Runtime connectors, tokenizer inputs, calibrated decision thresholds, vision projectors and adapter weights are model-specific. Old adapters are **not** presumed portable to a different base model, even within a similarly named family.

1. Implement the replacement behind `DecisionEngine` for S1 or `Supervisor` and any required vision interface for S2. A generic chat model is not automatically a finite-choice decision engine; typed outputs and option/probability validation still apply. These are internal integration seams, not a stable third-party plugin SDK.
2. Register a new immutable experimental deployment: model/code/tokenizer/template/schema/backend/config hashes, role, context and option limits, supported tools/vision, license and measured resources. Keep the other role pinned.
3. Rebuild model-specific training/retrieval inputs where needed. Reuse eligible canonical data and task suites, not incompatible token IDs, vectors, adapters or unreviewed labels.
4. Compare the current and candidate role on the same authorized workload and independent held-out groups. Measure verified task success, invalid outputs, unsafe-action denials, latency, memory and recovery; recalibrate thresholds instead of assuming confidence scores are comparable.
5. Check the complete S1–S2 pair: handoff, structured planning, recovery, cancellation, stale state, vision if claimed, and resource contention. A better isolated score is insufficient.
6. Explicitly promote only after gates pass. Drain the affected lane, verify loaded identity, atomically change the role pointer with audit, and retain a tested known-good rollback. Pin in-flight runs; no mid-action swap. Use a controlled restart when hot-swap is unsupported.

This is the migration contract, not a claim that today's UI can install or hot-swap arbitrary models. Registry metadata alone is not runtime compatibility or permission to activate a deployment. See [registries](REGISTRIES.md) and [extension seams](EXTENDING_AOS.md).

## Implemented versus open

| Area | Evidence available today | Still open |
|---|---|---|
| Experience and skills | Narrow synthetic/owned source audits, versioned skill reuse, opt-in S1/S2 episode review/export; failed/cancelled metadata review, authorized outcome-audited follow-up, finite Hello decision context and explicitly published same-session guidance entries | Prior-consent continuous failure capture, evaluated causal correction and arbitrary-system skill generation |
| Knowledge | Scoped page metadata, selected symbolic reuse, [document ingestion/review and lexical retrieval](DOCUMENT_KNOWLEDGE.md), separately consented [Bonsai extractive answers](DOCUMENT_KNOWLEDGE_ANSWERS.md), [bounded S1 context for one ordinary task](TASK_KNOWLEDGE.md) | Embedding/vector retrieval, S2 task-planning context, arbitrary goals and independently evaluated free-form grounded generation |
| S1 training | Experimental rank-4 adapter update, separate reload and same-case task comparison on pinned Decider | General continuous trainer, independently demonstrated quality gains, production rollout |
| S2 training | Separate plan/diagnosis candidate and dataset conversion paths | Verified tokenizer/loss-mask/trainable-checkpoint/backend compatibility and trainer |
| QLoRA | A desired optional training capability | Implemented and accepted QLoRA backend for either role |
| Model upgrades | Typed internal interfaces, pinned identities and experimental registry records | General backend capability negotiation, alternative-model acceptance, production promotion/drain/rollback service |

Detailed executed evidence is in [STATUS](STATUS.md), [episode learning](OWNED_EPISODE_LEARNING.md), [episode adaptation](OWNED_EPISODE_ADAPTATION.md) and [paired evaluation](OWNED_ADAPTER_EVALUATION.md). None of these satisfies real-site W1–W6 acceptance by itself.

## Small acceptance milestones

These remain open, not completed percentages. I1 has metadata review, separately authorized follow-up/outcome and finite Hello guidance-input slices; full correction/regression acceptance is still open. I2 has a finite [same-session Hello guidance entry](HELLO_GUIDANCE_REUSE.md), reviewed document retrieval and pinned Bonsai extractive consumer, not full evaluated RAG or arbitrary-system skill generation. Implement one bounded vertical slice at a time; update canonical schemas, fixtures, migrations when needed, docs and negative tests together.

- **I1 — Failure improvement candidate:** one authorized failed task produces a minimized, deduplicated, role-scoped candidate and separately reviewed correction; no consent/revoked consent produces no collection, and no effect is replayed.
- **I2 — Knowledge and skill reuse:** one reviewed correction becomes a bounded skill or retrieval item; a fresh permitted task uses it, while cross-tenant, stale, revoked and prompt-injection cases cannot gain authority.
- **I3 — Reproducible training:** immutable reviewed dataset and leak-free held-out groups feed one supported S1 backend; report reload, quality and resource evidence. Add S2 and QLoRA only after separate compatibility acceptance, not by renaming S1 output.
- **I4 — Independent role replacement:** replace one role behind the existing interface, keep the other unchanged, and run pair-level regression plus negative contract tests. An unsupported adapter or missing capability fails before execution.
- **I5 — Promotion and rollback:** explicit admission, drained lane, pinned loaded identity, auditable activation and restart/crash-safe rollback to a tested deployment. Historical evidence never silently becomes current-source evidence.

These milestones can progress on explicitly synthetic/local systems while company-intranet access remains deferred. They do not authorize external targets, training on company data or publishing private artifacts.
