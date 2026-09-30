# Claude Code Handoff

This guide is for a maintainer who later forks AOS and uses Claude Code on a company workstation. It does not authorize access to company systems. AOS should remain useful as an independently developed, local agent project while any future intranet integration is deferred.

## Before a work session

1. Confirm the checkout, branch, and intended task. Read the root `CLAUDE.md`, `README.md`, `CODEX_KICKOFF.md`, `docs/ROADMAP.md`, and relevant `docs/STATUS.md` entries.
2. Review the exact files and tests in scope. Ask Claude to summarize the current behavior and its verified limits before making changes.
3. Keep the change independent of proprietary SWAPP details unless an explicitly authorized task says otherwise. Do not paste company content, internal URLs, employee/account data, tokens, or screenshots into prompts, source files, fixtures, or logs.

## Work safely

- Start with pure contracts and synthetic fixtures; run the smallest relevant tests.
- Keep authorization in host code and human approvals. Claude/model output may propose data but cannot broaden permissions or choose a new target.
- Keep pin checks, TLS/SSRF guards, workspace confinement, and data minimization intact. Do not add an intranet exception by disabling a general safety gate.
- Do not ask Claude to download weights, use CUDA, launch a live browser session, or exercise an internal service unless that exact action is authorized and the test scope is isolated.
- Record what is designed, implemented, and verified separately. Update status only from actual results.

## Improvement data and model changes

AOS is intended to improve from opt-in task experience, including diagnosed failures, but a failed or uncertain model output is not a gold answer. Preserve the original input/output and verifier evidence; any correction must be a separate, explicitly human-reviewed or independently verified target linked to its source. Do not promote an unverified correction or silently relabel a prediction.

Prefer the versioned, role-separated AOS candidate/export contracts as the portable record. They retain source, review, and deployment provenance; they are not automatically trainer-ready. Existing conversion is model-specific: S1 choices map to the pinned Decider Example/Q format and tokenizer, while S2 plans preserve the Bonsai planner messages and typed target. Never discard those pins or assume a record can be fed unchanged to a different model.

S1 (decision/action selection) and S2 (planning) are separate roles and can be evaluated or replaced independently in principle, not guaranteed plug-compatible. A better checkpoint or backend needs its own pinned identity, request/schema/tokenizer or chat-template adapter, regression and independent held-out evaluation, followed by explicit promotion with a known rollback path. Current work includes narrow experimental S1 adapter loops; it is not a general fine-tuning service. QLoRA, general continuous training, and S2 training are not established features. Retrieval records are scoped evidence, not permission; a general RAG pipeline is not established.

For the current implemented/open stages of that improvement loop, see [`docs/CONTINUOUS_IMPROVEMENT.md`](CONTINUOUS_IMPROVEMENT.md).

The current [reviewed document foundation](DOCUMENT_KNOWLEDGE.md) supports explicitly submitted UTF-8 text, private immutable revisions/reviews and exact application/tenant/role lexical retrieval. The Knowledge UI verifies source/chunk hashes and review state. These local corpus labels are not external account ACLs; records remain untrusted, not automatic model context or training targets. Start regression with `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_knowledge*.py'`; rendered fixture acceptance additionally requires `AOS_DESKTOP_TESTS=1 AOS_UI_TESTS=1` and an isolated reviewed local runtime. Do not import company documents into a public fixture corpus.

## Source-only fork setup

Knowledge also has a separately consented [native Bonsai extractive answer consumer](DOCUMENT_KNOWLEDGE_ANSWERS.md). Preserve request-dispatch source/control guards, exact actual-request binding, untrusted quote semantics and scheduler reservation/cancellation. Do not turn a source quote or the three synthetic acceptance cases into a gold label, broad held-out quality claim or automatic task authority.

[Task-bound document context](TASK_KNOWLEDGE.md) now admits local sources before immutable S1 DECIDE snapshots for ordinary tasks. Preserve fresh one-task inference/private-trajectory consent, manual effects, actual dispatch/request/decision joins and source/configuration/lease revalidation. Fixture preparation is not model use; local labels are not external ACLs. A separate [selected-skill S2 document context](OWNED_SKILL_KNOWLEDGE.md) lane exists for new owned-reuse sessions; native inference and a separate real managed EN/TR S2-to-S1 chain passed on a synthetic site/corpus. Preserve separate bind/start, six fresh approvals, independent readback, revocation-before-fill and replay rejection. Inference-only reports keep downstream proof false; the execution audit is separate. Arbitrary goals and combined learning remain open. SWAPP private intranet acceptance stays deferred to the private fork.

The task document audit now has an explicit-refresh EN/TR UI, with historical/current validity, citations and independent outcome. Required successful-model proofs include exact canonical validated response text/hash, avoiding JavaScript/Python float-format divergence. The S2 lane uses wire envelopes 1.1 and raw canonical preview/pins/intent/request strings, retaining historical inner contracts and native `0.0` identities. Never normalize model pins or rewrite stored evidence to satisfy JavaScript. Preserve parent-runtime binding and distinguish synthetic report tests from actual pin-backed authenticated report acceptance. Development lists four separately verified knowledge milestones (56/65 dated checklist), while real-site W1–W6 remains 0/6; checklist arithmetic is not project-completion percentage. Follow [REMAINING_WORK](REMAINING_WORK.md) for the acceptance-driven queue.

The intended company fork combines the private-intranet SWAPP web application with a selected AI-Scientist integration; other deployments may choose other applications and specialist agents. Keep those choices out of the generic core. Follow [integration compositions](INTEGRATION_COMPOSITIONS.md) for typed agent boundaries, explicit data flows and future acceptance. The AI-Scientist implementation/API is not selected or implemented by this handoff.

The source checkout intentionally excludes machine-local model files, databases, trajectories, tokens, and runtime state. A fork onto another host therefore needs its own reviewed setup, dependency environment, model artifacts with verified upstream revisions/hashes, and host-specific acceptance. Do not copy another machine's private `data/` tree or pretend that a source checkout alone reproduces its runtime.

## Intranet work is a later stage

Any future SWAPP/company-intranet work needs a separate authorization decision, host and account scope, documented data rights, private-IP and TLS threat review, an independently verifiable outcome, and isolated real-site tests. Use synthetic/local development until those prerequisites exist. AOS currently demonstrates selected local and synthetic workflows; it does not claim support for arbitrary systems or company applications.
