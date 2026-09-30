# W3 synthetic skill variation plan

`aos.site_skill_validation` checks the **structure** of a bounded synthetic parameter-variation plan against one immutable private skill draft. It does not execute a skill, open a browser, read a trajectory, observe an outcome, review evidence, activate a candidate or authorize training. The report deliberately says `structure_only` and all outcome/authority flags are false.

The canonical `schemas/site_skill_validation_plan.schema.json` and explicitly synthetic `examples/site_skill_validation_plan.json` define one exact skill/profile/page/task/model-role binding, declared source-variant hashes and 3–16 synthetic cases. The result has a separate canonical `schemas/site_skill_validation_report.schema.json` and synthetic `examples/site_skill_validation_report.json`. Every case supplies a unique symbolic case key, the exact draft parameter-key set, an opaque parameter-variant hash, an explicit expected symbolic outcome and a development or held-out cohort. The checker requires a nonempty draft parameter set, at least one development and two held-out cases, unique variants across all cases, and no overlap with the **declared** source variants. A wrong profile, page, task, model role, parameter contract or expected outcome fails closed.

Example API after registering the matching synthetic profile, page and skill draft:

```python
import json
from pathlib import Path

from aos.site_skill_validation import SiteSkillValidationPlan, preview_skill_validation

value = json.loads(Path('examples/site_skill_validation_plan.json').read_text())['plan']
plan = SiteSkillValidationPlan.model_validate(value)
report = preview_skill_validation(store, plan)
assert report['status'] == 'structure_only'
```

The case-variant hashes and source exclusion list are caller claims in this structural preview, not derived or attested split provenance. A hash disjointness check cannot prove that two private inputs are semantically independent; held-out means **declared structural cohort** only. Never put real identifiers, parameter values or private source data in this fixture or repository. S1 and S2 plans run through the same structural gate but bind separate role-specific draft hashes; one role never validates the other. A separate [synthetic source rehearsal](SITE_SKILL_REHEARSAL.md) compares source hashes with audited canonical model-call requests. The [private case-input binder](SITE_SKILL_CASE_BINDING.md) can additionally derive declared case hashes from supplied synthetic parameter values; neither binds audited source requests to parameter-key values or proves skill success. There is no report persistence, SQL migration, live skill test, generalization score or promotion path. W3 still needs authorized real-source binding, actual independent executions with varied parameters and wrong-scope negatives, reviewer acceptance and explicit activation/rollback. Test: `.venv/bin/python -m unittest discover -s tests -p test_site_skill_validation.py -v`.

## Read-only private CLI

`aos.site_skill_validation_cli` runs the same structural preview against an **already registered exact skill hash**. It requires the caller to select both the skill hash and the canonical plan hash; it cannot register a skill, save a report, execute a case or turn structural checks into verified outcomes. The plan file is a single canonical JSON `plan` object, not the repository example's synthetic wrapper. Its containing directory must be owner-only (`0700`), and the regular file must be owner-only (`0600`), single-link, stable and at most 16 KiB; symlinks, duplicate JSON keys, noncanonical bytes and changed files fail closed. Operational and argument failures print one fixed message without echoing paths or input content.

For the synthetic example only, after registering its matching profile, page and skill drafts, create a private canonical plan and note its digest:

```sh
umask 077
.venv/bin/python - <<'PY'
import json
from pathlib import Path
from aos.contracts import canonical, digest

plan = json.loads(Path('examples/site_skill_validation_plan.json').read_text())['plan']
Path('data/synthetic-site-skill-validation-plan.json').open('x').write(canonical(plan))
print(digest(plan))
PY
.venv/bin/python -m aos.site_skill_validation_cli \
  --plan data/synthetic-site-skill-validation-plan.json \
  --skill-sha256 <registered-skill-sha256> --plan-sha256 <printed-plan-sha256>
```

The default private roots are `data/web-applications`, `data/site-knowledge` and `data/site-skills`; use `--profiles`, `--pages` and `--store` for other registered roots. The output is the canonical structure-only report, with no case inputs or source variants. A passing preview is **not** skill validation, live site evidence, review, activation or training readiness. CLI tests: `.venv/bin/python -m unittest discover -s tests -p 'test_site_skill_validation*.py' -v`.
