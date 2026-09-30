# Executable synthetic S1 form recipes

`aos.site_skill_form_recipe` adds an ordered executable artifact separate from the existing skill draft. Draft `step_keys` remain a sorted set of symbolic declarations; they are not interpreted as execution order. A recipe maps every declared step to one bounded operation, binds parameter-to-field mappings, and declares where its preconditions and outcome are checked. No recipe supplies URLs, shell commands, JavaScript, extra requests or authorization.

## Execution

The two supported schedules are:

1. Entry → baseline GET → fill → one POST → receipt → after-state GET.
2. Entry → fill → baseline GET → one POST → receipt → after-state GET.

The second schedule checks the server baseline immediately before submission rather than before local editing. The operator derives its actual action order from the recipe, and the MCP runtime independently enforces that order. Form availability is checked by the existing form-controls validation at fill, not inferred from entry-page metadata. Both schedules require the declared baseline before POST and independent receipt/state readbacks afterward. Failure never authorizes resubmission.

The recipe binds the exact skill/profile/page and task scope but excludes case values and case-specific form/state plans. `compile_site_skill_form_recipe_invocation` binds one selected private parameter case, its exact body and readback plans, the recipe digest and ordered steps into a version-2 invocation. The same recipe can therefore execute distinct values with distinct invocation hashes. This is a bounded synthetic form executor, not arbitrary web workflow execution.

The invocation digest is recorded in the initial run state and retained through every state transition. Before each model decision, a durable observation records the recipe digest, symbolic step, operation and ordinal. Existing scheduler revalidation rereads private sources before job creation, run binding and each action. Every stage still requires its own fresh human approval; recipe declarations do not authorize actions, collection, training or activation.

## Direct opt-in

Alongside the existing [private skill invocation inputs](SITE_SKILL_FORM_INVOCATION.md), pass `--remote-form-skill-recipe-file data/<private-recipe>.json` and `--remote-form-skill-recipe-file-sha256 <exact-file-hash>` to `scripts/serve_desktop.py`. Supply the version-2 compiled invocation hash through `--remote-form-skill-invocation-sha256`. The recipe must be canonical, owner-only and under private `data/`; missing pins, changed bytes and mixed public/Cookie scope are rejected. These files do not install public `.invalid` DNS/TLS routing.

The existing `aos-v1 start --owned-synthetic-form-invocation` remains version 1 and does **not** enable recipes. The current live backend is not upgraded by these changes.

## Managed opt-in and Tasks

For a fresh real-model session, use `./scripts/aos-v1 start --owned-synthetic-form-recipe`. Do not run this to replace a session with an active task: inspect and safely stop that session first. This mode cannot be combined with the version-1 owned invocation, synthetic staging/learning, public remote plans or Cookie scope.

The manager provisions a private version-2 manifest with 12 exact source hashes, a pinned recipe and a capability-protected TLS listener. It uses schedule 2 above. The existing version-1 manifest retains its original 11-source contract. Neither mode changes host DNS or the system trust store; only the owned fixture transport can reach its pinned synthetic endpoint.

In **Tasks → HTTPS form**, the executable-skill panel shows the recipe digest and six ordered symbolic steps. Explicitly opt in and start the single-use task. Each pending approval highlights its matching step; this is a pending-action indicator, not fabricated completion evidence. All six actions require separate approval and Approve all stays disabled. After success, the separate read-only audit button verifies current sources and the frozen run evidence. A changed recipe or mismatched mode/run/hash removes the verified claim; a second start is rejected.

The version-2 report explicitly means synthetic recipe execution, unlike the version-1 fixed-template report. English and Turkish presentation share these limits. No URL, path, source or value editor is exposed by this bounded trial. Test evidence and the distinction between isolated managed-supervisor acceptance and the public port-8765 launcher are recorded in [STATUS](STATUS.md).

## Evidence and boundaries

An additional opt-in path derives the draft's actual source references and recipe order from an audited completed trajectory, then binds fresh development executions to that persisted candidate. Semantic names remain explicitly manual. See [trajectory-derived candidates](SITE_SKILL_FORM_RECIPE_CANDIDATE.md); ordinary recipe execution alone does not prove this provenance.

`audit_site_skill_form_recipe_execution` in `aos.site_skill_form_recipe_audit` recompiles the current exact sources, verifies the frozen trajectory snapshot, all 32 ordered states, six consumed approvals, one POST and independent receipt/state readbacks. It additionally requires exactly six canonical step observations in their matching OBSERVE→DECIDE intervals. Wrong, duplicated, missing or moved step evidence fails closed. The version-1 invocation auditor continues rejecting version 2, even when both use the same action order.

`executable_recipe_executed=true` and `skill_executed=true` mean only that the six explicitly mapped synthetic recipe steps ran and their declared readbacks passed. They do not prove source-learning provenance, semantic held-out independence, application/account outcome, skill validation, review, activation or training. Those claims remain false. No Bonsai call is invented to populate the second role.

Opt-in acceptance: `PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_FORM_RECIPE_TESTS=1 AOS_FORM_RECIPE_REAL_DECIDER_TESTS=1 .venv/bin/python -m unittest test_site_skill_form_recipe_execution -v`. The isolated harness uses real Ubuntu Chromium/Playwright MCP and pinned Decider against a private synthetic TLS server; DNS/address resolution is redirected inside the test process. It does not contact a real target site. The suite executes both schedules, reuses one recipe across three values, and rejects an incorrect after-state without repeating the POST. See [STATUS](STATUS.md) for measured results; real-site W1–W6 is still open.
