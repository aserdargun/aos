# W3 synthetic skill source rehearsal — request-bound, not validated

`aos.site_skill_rehearsal` combines three **explicit** selections: one registered immutable symbolic skill draft, one canonical synthetic structural-variation plan, and one audited synthetic trajectory snapshot. It checks the existing skill source-ID inspection and plan's role/profile/page/task/parameter structure together. First obtain the sorted `source_request_sha256` list from the private [source inspection](SITE_SKILL_PROVENANCE.md) and place it in the private plan's `source_variant_sha256`; rehearsal requires exact equality with those audited model-call request hashes and rechecks the source snapshot before reporting. Noncanonical or oversized requests and caller-claimed hashes that do not match those exact requests fail closed. The report says `status=source_request_bound_parameter_unbound`: this binds source **requests**, not skill parameter values, a skill invocation, or held-out case inputs. It performs **no skill execution** or independent varied-case outcome verification.

```sh
.venv/bin/python -m aos.site_skill_rehearsal \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --profiles /path/to/private-profiles --pages /path/to/private-pages \
  --skills /path/to/private-skills --plan /path/to/private-plan.json \
  --skill-sha256 SKILL_SHA256 --plan-sha256 PLAN_SHA256 \
  --snapshot-sha256 AUDITED_SNAPSHOT_SHA256
```

No latest/default skill, run, plan or snapshot is selected. The audited run must retain a known synthetic policy; an unclassified policy cannot be labeled `synthetic=true`. Wrong role/scope, changed audited source, invalid private plan, duplicate or source-overlapping declared held-out variants fail closed. `source_request_bound=true` means only exact request-byte provenance after canonicalization; `source_variant_bound=false` remains because no parameter-key/value extraction or actual skill call is verified. `held_out_separated_structurally=true` means only that the **declared hashes** are disjoint; a whole-request hash cannot establish parameter-semantic independence or generalization success. All execution, outcome, review, skill validation, activation and training flags are false. The CLI prints no raw run ID or page content and returns a fixed error on unsafe input. Keep its selected private paths and source artifacts out of the repository.

`schemas/site_skill_rehearsal_report.schema.json` is the canonical report contract and `examples/site_skill_rehearsal_report.json` is explicitly synthetic: its source request is `{}` in an invented trajectory fixture, not a real Decider/Bonsai inference. The separate [case-input binder](SITE_SKILL_CASE_BINDING.md) checks private synthetic values against declared plan hashes, but does not associate them with these audited source requests. Actual acceptance is in [STATUS](STATUS.md). W3 still needs parameter-key binding to runtime source and skill execution, a typed executable skill version, authorized varied parameter cases on the target site, independent outcomes, reviewer decision, explicit activation and rollback. This rehearsal does not satisfy those gates.
