# W3 foundation — private symbolic skill drafts

`aos.site_skill` stores **manual, non-executable candidates** for Decider/S1 and Bonsai/S2 separately. Each candidate pins one immutable web profile revision, application, tenant, account role, declared task, registered page draft, model role and skill identity. It contains bounded symbolic parameter, precondition, step and expected-outcome keys, plus explicit learning-event and optional verification ID claims. It contains no selectors, code, tool calls, raw DOM, screenshots, credentials or form values.

`schemas/site_skill_draft.schema.json` is the canonical v1 contract; `examples/site_skill_draft.json` is explicitly synthetic and references the synthetic profile, page draft and one synthetic S1 learning event. IDs and page fingerprint are **caller assertions**, not attested evidence. Registration does not inspect the source trajectory or establish data rights, review, independent success, generalization or training readiness. S2 drafts may have no direct verification ID; that absence is not silently upgraded to a successful plan.

Use the Python API with a private workspace, registered profile and registered page draft:

```python
from aos.site_knowledge import SiteKnowledgeStore
from aos.site_skill import SiteSkillDraft, SiteSkillStore
from aos.web_application import WebApplicationProfiles

profiles = WebApplicationProfiles(profile_root)
pages = SiteKnowledgeStore(page_root, profiles)
store = SiteSkillStore(skill_root, profiles, pages)
draft = SiteSkillDraft.model_validate(candidate_data)
preview = store.preview(draft)
store.register(draft, confirm_sha256=preview['skill_sha256'])
store.list(profile_sha256=draft.profile_sha256, model_role=draft.model_role)
```

The metadata-only CLI supports the same operations. Its source file and containing directory must be owner-only (`0600` and `0700` respectively). Register the matching synthetic profile and page draft first, place only the `skill` object from the synthetic example in a private source file, then review the preview hash before registering:

```sh
umask 077
.venv/bin/python -c 'import json; from pathlib import Path; value=json.loads(Path("examples/site_skill_draft.json").read_text())["skill"]; Path("data/synthetic-site-skill.json").open("x").write(json.dumps(value))'
.venv/bin/python -m aos.site_skill_cli preview --skill data/synthetic-site-skill.json
.venv/bin/python -m aos.site_skill_cli register --skill data/synthetic-site-skill.json --confirm-sha256 <reviewed-sha256>
.venv/bin/python -m aos.site_skill_cli inspect --sha256 <reviewed-sha256> --selected-profile-sha256 <profile-sha256> --selected-page-sha256 <page-sha256>
.venv/bin/python -m aos.site_skill_cli list --profile-sha256 <profile-sha256> --model-role system1
```

The CLI defaults to ignored private `data/web-applications`, `data/site-knowledge` and `data/site-skills`; pass `--profiles`, `--pages` or `--store` before a subcommand to choose other roots. It rejects symlink/hardlink, broad-permission, changed or duplicate-key source files. `inspect` reports `draft_match` only when **caller-selected** profile and page hashes equal the stored draft; it is not a live page attestation. CLI output omits symbolic steps and provenance IDs; invalid arguments and operational failures use a fixed message.

`preview` returns bounded metadata and a content hash, not step or source content. `register` needs exact hash confirmation, writes owner-only immutable content-addressed bytes, and is idempotent for identical bytes. Later revisions name the exact prior hash and retain the same profile/tenant/role/task/page/skill/model-role identity. Reads validate file ownership, mode, link count, bytes and the full lineage. `list` requires an exact profile and model role, returns all matching revisions and never chooses an active/latest version. No draft can authorize execution, collection, activation or training; even a future activated skill would still need a separately authorized task and fresh action approval.

There is no SQL table, live skill extractor, executor, review queue or promotion pathway in this slice, so no migration was added. W3 acceptance still needs genuine authorized provenance binding, independent parameter-varied tests, wrong-scope rejection, explicit review/activation and rollback for each role. W1 real-site/account/data-rights and W2 observed/reviewed site knowledge remain open. Tests: `.venv/bin/python -m unittest discover -s tests -p 'test_site_skill*.py' -v`.
