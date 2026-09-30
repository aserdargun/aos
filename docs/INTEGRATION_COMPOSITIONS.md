# Composable applications and specialist agents

## Core versus private fork

AOS is the reusable core, not a product tied to a particular company application or agent framework. One intended private fork combines **SWAPP**, a company-specific web application on a private intranet, with **AI-Scientist**. Other adopters may combine different applications, models and specialist agents.

This document is a target integration contract. It does not claim those integrations, a stable plugin SDK or a multi-agent execution service already exist. The exact AI-Scientist implementation/version/API has not been selected here; do not infer a particular upstream repository, license, execution model or network service from the name.

| Public AOS core | Private deployment/fork |
|---|---|
| Typed task/state/tool boundaries, policy, deterministic execution, verification | Authorized target hosts, tenants, roles, accounts and task definitions |
| Model/runtime interfaces and pinned deployment identities | Selected S1/S2 models, optional specialist implementations and resource budgets |
| Canonical learning records, skill contracts, evaluation and extension tests | Application-specific skills, reviewed knowledge/RAG, private datasets and adapters |
| Synthetic reference fixtures, package/test tooling and generic docs | Credentials, intranet topology, company content, trajectories and screenshots |

Company-specific material stays outside the public source archive even when maintained alongside a private fork. Upstream contributions should use generic interfaces and explicitly synthetic tests, not proprietary application details.

## Preserve the two-role execution core

System-1 remains the finite-choice decision role; System-2 remains the planning/recovery role. Both can be replaced through the evaluated model-upgrade contract. Additional agents do not require replacing those two roles or giving every agent its own route to mouse, keyboard, shell, network or production data.

A specialist may propose a task, analysis, experiment, knowledge candidate or reviewed artifact through a future typed integration boundary. AOS validates its output, scope and provenance before an authorized workflow can use it. Returned code, tool calls, research plans and model-generated recommendations are untrusted data, not execution permissions. A specialist's own internal agents do not inherit AOS privileges.

The existing runtime has a single active input owner/writer. Adding specialist agents does not authorize concurrent control of one desktop. Future parallel execution requires distinct admitted runtimes, bounded resource scheduling and explicit ownership/cancellation rules; sequential isolated use is the initial integration target.

## Admission requirements for each integration

Before implementing a connector, define and test:

1. **Identity and contract:** integration/version, reviewed implementation and license, local or remote transport, request/response schema and supported capabilities. Fail closed on unsupported versions or missing capabilities.
2. **Authority:** exact runtime/workspace/network/application/tenant/role scope, permitted operations and data recipients. An agent response cannot expand these grants or delegate them to another agent.
3. **Lifecycle:** timeout, resource and retry budgets, correlation/idempotency keys, cancellation, owned-process cleanup and handling of uncertain effects. Agent failure must not replay a submitted application action.
4. **Data:** minimum permitted input, redaction, provenance, retention and separate collection/training/export consent. External inference or uploads need their own data-flow authorization; private intranet data is not automatically shareable with an AI service.
5. **Verification:** an independently checkable result or an explicit unverified proposal status. A specialist cannot grade its own unsupported assertion as task success.
6. **Version changes:** compatibility tests, immutable pins, explicit activation and a known-good fallback. One failing optional integration must not silently replace another or broaden execution scope.

Use the [current extension seams](EXTENDING_AOS.md) rather than adding an unrestricted generic tool runner. No new configuration or API schema is asserted by this document; implement it with canonical schemas, synthetic fixtures and negative tests when a bounded integration is selected.

## SWAPP + AI-Scientist: future private acceptance

The intended composition is: authorized SWAPP observation/result → scoped specialist analysis or experiment proposal → validated AOS task proposal → separately authorized execution → independent application verification → opt-in learning candidates. Not every specialist output should become a browser action, skill, training target or deployment.

Begin with an offline synthetic specialist stub that is explicitly not a real AI-Scientist result. Then accept the selected real specialist in isolation, with no company access. Only after the company authorizes the intranet connection, target/account scope and data flow should SWAPP be connected. A private-IP connector needs its own threat model and narrowly pinned admission; do not disable the existing public-HTTPS guard, TLS checks or browser isolation. Tunnel connectivity alone is not authorization.

Acceptance must include malformed/overbroad specialist output, cross-tenant retrieval, revoked grants, unavailable agents, timeouts, cancellation, repeat delivery, source drift and an independently verified successful task. General arbitrary-system support is not inferred from one private integration. SWAPP real-site W1–W6 remains the final deferred integration gate.

## Learning stays local to the authorized composition

Capture which core model and specialist produced each candidate. Keep S1 decisions, S2 plans and specialist-derived artifacts distinguishable; one agent's answer is not another role's gold label. Application knowledge, skills and eligible canonical data may survive a model replacement, while tokenizer-specific inputs, embeddings and adapters require compatibility checks or rebuilding.

Follow [continuous improvement](CONTINUOUS_IMPROVEMENT.md) for failure review, skill/RAG reuse, separate training, held-out evaluation and explicit promotion/rollback. Public source sharing never implies sharing a company's learned corpus or weights.
