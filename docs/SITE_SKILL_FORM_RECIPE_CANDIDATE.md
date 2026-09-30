# Trajectory-derived executable recipe candidates

This opt-in development path connects a verified synthetic form trajectory to a reusable recipe. It is not automatic semantic learning, a reviewed skill, real-site acceptance, or a training export.

## What is derived

`derive_site_skill_form_recipe_candidate` reads one frozen, completed six-stage form run. It binds the actual ordered actions, predecision states and real S1 decision events, consumed human approvals, and independent receipt/state readbacks. The source is a no-Cookie `.invalid` synthetic form with no invented S2 events. Candidate schema `1.0` preserves the original unbound-source contract and hashes. Schema `1.1` additionally accepts an exact owned fixed-template v1 source through its explicit private manifest/source directory and reconstructed invocation. V2 recipe runs and candidate descendants are not demonstration sources. Failed outcomes and mismatched evidence cannot seed a candidate.

The operator explicitly supplies the page/task/skill names, operation-to-symbolic-step mapping, parameter-to-field mapping, precondition names and expected-outcome name. Mapping order is not execution order: the audited source determines that. Semantic declarations remain manual; `SiteSkillDraft.source_kind=manual_candidate` is intentional. The encompassing candidate records `derivation_kind=audited_form_trajectory` and embeds the resulting draft, executable recipe and source lineage.

Private source parameter values must match the source form's exact body hash/length and declared field mapping. Values are used transiently, not saved in the candidate. Their domain-separated variant hash, the body hash and source group remain pinned. These hashes are integrity bindings, not anonymization guarantees; candidates remain private.

## Persistence and revalidation

`persist_site_skill_form_recipe_candidate(directory, candidate, confirm_sha256=...)` publishes one canonical content-addressed, owner-only file. Repeating the same confirmed publication is idempotent, not a new skill version. The embedded draft and recipe cannot be half-published as separate candidate files.

`load_site_skill_form_recipe_candidate` reads the exact private file and rederives its contents from the current source database, profile, page and source parameters. Source-specific fingerprints permit unrelated/new task rows to be appended while changed source evidence invalidates the candidate. Stored metadata alone never substitutes for a fresh audit.

Registering the embedded draft in `SiteSkillStore` is a separate explicit hash-confirmed action. It still creates only an unreviewed draft, not an active deployment. A recipe can then be compiled with the existing exact task, form/state plans and fresh case inputs.

The local CLI separates preview, confirmed publication and reinspection. The annotation file contains an annotation object; the parameter file contains only the explicit parameter-key/value object. Both must use AOS canonical JSON bytes and owner-only private-file permissions, not committed examples or real-site credentials. Replace the example private paths/run identifier with the selected synthetic source:

```bash
common=(--database data/private-source.sqlite \
  --profiles data/web-applications --pages data/site-knowledge \
  --source-parameters-file data/private-source-parameters.json)
PYTHONPATH=src .venv/bin/python -m aos.site_skill_form_recipe_candidate preview \
  "${common[@]}" --run-id RUN_ID --annotation-file data/private-annotation.json
```

After inspecting the preview, repeat it with `publish`, the same source arguments, `--directory data/site-skill-candidates` and `--confirm-sha256` set to the exact preview candidate hash. Recheck a stored candidate with:

```bash
PYTHONPATH=src .venv/bin/python -m aos.site_skill_form_recipe_candidate inspect \
  "${common[@]}" --directory data/site-skill-candidates --sha256 CANDIDATE_SHA256
```

The CLI neither starts a task nor materializes/activates the embedded skill. Source parameters must remain available privately for later revalidation; the candidate file is not a raw-value backup.

## Managed Tasks demonstration

Only a fresh `./scripts/aos-v1 start --owned-synthetic-form-invocation` session exposes this path. It does not upgrade or restart an existing session. Complete the six manually approved actions, then explicitly audit the invocation. In Tasks, load the candidate source context, edit the semantic names and field mappings, declare them, and preview. Preview writes nothing. Saving requires a separate confirmation of the exact shown candidate hash. Stored candidates remain unreviewed; neither saving nor reinspection starts a task, activates a skill or trains a model.

The browser sends only the fixed source reference/invocation pin, the bounded semantic annotation, and the preview/inspection hash. It cannot select a database, source directory, profile, run identifier, URL or raw field value. Preview/publication select the current completed, audited, idle owned-v1 run. The inspect API can also revalidate a stored candidate beyond the current audited lifecycle, but only from the same pinned owned-source manifest; this does not revive the source task. Every preview/publication/reinspection revalidates the original source. The UI remains scoped to the current audited run, discards results if runtime, source or job identity changes, and invalidates the preview when declarations change. V2 recipe sessions do not expose this demonstration panel.

Candidates are private canonical files in `OWNED_SOURCE/site-skill-recipe-candidates/`. Their schema-1.1 `source_context` contains only the fixed-template kind, manifest hash and invocation hash. Reinspection reconstructs the original source from its pinned private bundle; it needs no running fixture, browser or model. After shutdown, an explicit host can inspect using:

```bash
PYTHONPATH=src .venv/bin/python -m aos.site_skill_form_recipe_candidate inspect \
  --database SESSION/store.sqlite \
  --profiles SESSION/owned-form/profiles \
  --pages SESSION/owned-form/site-knowledge \
  --owned-source-directory SESSION/owned-form \
  --owned-source-manifest-sha256 MANIFEST_SHA256 \
  --directory SESSION/owned-form/site-skill-recipe-candidates \
  --sha256 CANDIDATE_SHA256
```

Here `SESSION`, `MANIFEST_SHA256` and `CANDIDATE_SHA256` refer to the actual private session and inspected pins, not committed examples. This owned path rejects an additional source-parameters file and alternate profile/page/candidate directories. Preserve the original private bundle and trajectory database: an artifact alone cannot pass source reinspection.

## Source-bound development execution

`prepare_candidate_execution` pins the candidate, source group, new parameter variant and version-2 invocation in a `CandidateExecutionAdmission`. It accepts only `development_variation` and a declared development case. Source variants, source bodies, source-run reuse and requests for validation/test/independent-held-out classification are rejected. Structural validation plans still contain their existing reserved held-out slots; this candidate path cannot execute those slots as held-out evidence.

Pass the admission and a callback using `revalidate_candidate_execution` to the scheduler. The callback rechecks the private candidate and source before actions; all six manual approvals and ordinary authorization checks remain mandatory. The scheduler persists a unique admission observation after CREATED and before the first OBSERVE state. This distinguishes source-bound execution from merely matching a recipe after the run.

`audit_candidate_execution` composes current source rederivation, admission timing, six distinct S1 execution events and the existing recipe execution audit in one frozen snapshot. Its report says only `source_bound_development_execution`. Source and descendants share a lineage group even with new values and run IDs. The existing dataset exporter is unchanged: this gate does not claim global train/test split enforcement, and `dataset_ingestion_authorized`, `held_out_independence_verified`, `training_ready`, `skill_validated` and activation remain false.

## Acceptance

The opt-in test is:

```bash
PYTHONPATH=src:tests AOS_DESKTOP_TESTS=1 AOS_FORM_RECIPE_CANDIDATE_TESTS=1 \
  .venv/bin/python -m unittest test_site_skill_form_recipe_candidate_execution -v
```

It uses actual pinned Decider and Ubuntu Chromium/Playwright MCP with a private TLS fixture. DNS/address routing is redirected inside the test process, not installed on the host. It derives from the first run, reloads the persisted candidate, then executes the same recipe with two fresh development values. Source/admission tampering, held-out requests, failed source outcomes and changed-candidate rejection are separate negative checks. Measured results are recorded in [STATUS](STATUS.md); test intent alone is not acceptance evidence.

Managed UI acceptance is separately opt-in through `test_owned_form_invocation_managed`, with `AOS_DESKTOP_TESTS=1 AOS_OWNED_FORM_INVOCATION_TESTS=1`; include `AOS_OWNED_FORM_RECIPE_TESTS=1` to cover v2 isolation. Route-injected bilingual UI tests are fixture evidence, not model acceptance. Current measured results are in STATUS.

Saved schema-1.1 candidates also have a separate [managed development execution](OWNED_CANDIDATE_EXECUTION.md) flow in Tasks: new bounded input, exact preview/start confirmation, six manual approvals and a source-bound audit. Its opt-in test is `test_owned_candidate_execution_managed` with `AOS_DESKTOP_TESTS=1 AOS_OWNED_CANDIDATE_EXECUTION_TESTS=1`. Private execution bundles allow subsequent offline reconstruction without an in-memory scheduler; measured results belong in STATUS.

This path is not connected to a production target application. Review/activation/rollback, genuinely independent variations, actual-site provenance and S1/S2 training remain separate gates. See [recipe execution](SITE_SKILL_FORM_RECIPE.md) and [first-web-release requirements](WEB_APPLICATION_LEARNING.md).
