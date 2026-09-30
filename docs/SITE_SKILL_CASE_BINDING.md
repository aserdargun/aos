# W3 synthetic skill case-input binding

`aos.site_skill_case_binding` checks that explicitly synthetic, private parameter values reproduce the declared case-variant hashes in one exact structural skill plan. It first reruns the registered draft/plan structural gate, then requires matching plan hash, ordered case keys, exact parameter-key sets and domain-separated `site_skill_parameter_variant_v1` hashes. S1 and S2 remain separate draft roles. The report contains hashes and counts, not parameter values.

Canonical contracts are `schemas/site_skill_case_inputs.schema.json` and `schemas/site_skill_case_report.schema.json`; `examples/site_skill_case_binding.json` is invented, public synthetic data only. The input file can contain sensitive values in future, so keep it out of the repository and use only an authorized private runtime. Its directory must be owner-only `0700`; the single-link regular canonical JSON file must be owner-only `0600`, at most 16 KiB, stable while read, and free of duplicate keys. No default plan or input is selected.

After registering the matching synthetic profile, page and skill draft, save the fixture's `plan` and `case_inputs` objects separately as canonical JSON in a private directory, then run:

```sh
.venv/bin/python -m aos.site_skill_case_binding \
  --plan /private/canonical-plan.json --plan-sha256 PLAN_SHA256 \
  --cases /private/canonical-case-inputs.json --cases-sha256 CASE_INPUTS_SHA256 \
  --skill-sha256 REGISTERED_SKILL_SHA256
```

Hashes are SHA-256 over canonical JSON (sorted keys, compact separators). Optional `--profiles`, `--pages` and `--store` select already registered private roots. The command only prints a canonical report or a fixed error; it does not write a report, start a browser, invoke a model or execute a skill.

`case_inputs_bound=true` means only that the supplied private values match the plan's caller-declared variant hashes. `source_parameters_bound=false` and `held_out_independence_verified=false` remain explicit: the audited model-call requests are **not** parsed into parameter values, and different hashes cannot prove semantic independence. Execution, outcome verification, skill validation, review, activation and training readiness are all false. W3 still needs an authorized target, source-to-parameter provenance, executable skill version, independent varied executions and outcomes, reviewer decision, explicit activation and rollback. Test: `.venv/bin/python -m unittest -v tests.test_site_skill_case_binding`.

## Exact HTTPS form-plan bridge

`bind_site_skill_form_case` is a read-only Python API for one selected **S1** synthetic case. The caller supplies an already registered skill, the exact structural plan and private case inputs, an exact `WebTaskContract`, an existing `WebHTTPSFormPlan`, and an ordered one-to-one mapping from symbolic parameter keys to concrete form field names. The bridge uses the same URL encoding as the existing form transport, checks the resulting body SHA-256 and byte count against the form plan, and revalidates that plan against the registered profile and task. The mapping order is the intended form DOM order; it cannot add a field or change the plan's URLs. The canonical `schemas/site_skill_form_field_binding.schema.json` and `schemas/site_skill_form_case_report.schema.json` plus `examples/site_skill_form_case_binding.json` cover an invented `record-query` → `message` case.

`parameter_values_bound_to_plan=true` means only that the private synthetic values would produce the exact already-declared POST body. It is **not** evidence that the form was opened or submitted, that the source model request carried those values, that the page readback matches the skill page, or that any application outcome succeeded. The report keeps all those flags false and provides no activation or training authority. It contains hashes of potentially low-entropy values via the plan/input digests; keep it private and do not treat a hash as redaction. A future executable skill needs fresh policy/lease/approval on every action and audited run/outcome binding, not this function alone.

The separate [synthetic form execution audit](SITE_SKILL_FORM_EXECUTION.md) now checks one development case against an approved completed `.invalid` form run and the actual pinned S1 submit-decision event. It does not change this plan-only report or claim that the skill itself ran.
