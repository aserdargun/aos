# Private JSON-bundle page draft from audited runs

In a **new v2-plan backend session**, the authenticated Tasks panel can seed a private `SitePageDraft` from two distinct, completed, same-session JSON-bundle runs. Both runs are re-audited in one frozen trajectory snapshot. The exact profile, task and plan, consumed manual approvals, independent browser readbacks, entry HTML, JS/CSS, JSON responses, title and H1 must be unchanged. A changed JSON body rejects the seed even when the visible title remains unchanged.

The operator supplies a symbolic page key. The server derives the exact entry origin, literal route and versioned transport fingerprint from its pinned sources. It does **not** infer page meaning or outgoing links. The initial draft has empty landmark/outgoing keys and is written as an owner-only `0600` file under `data/json-page-seeds/<PROFILE_SHA256>/<PAGE_KEY>.json`. That private file can be inspected before registration; sorted symbolic landmark keys may be edited, but outgoing-page claims are rejected because this worker does not sample links. Recompute the canonical draft SHA-256 after any permitted edit.

For an edited private draft, calculate the canonical confirmation hash locally without printing its content:

```sh
PYTHONPATH=src .venv/bin/python -c 'import sys; from pathlib import Path; from aos.contracts import digest; from aos.site_knowledge import SitePageDraft; page = SitePageDraft.model_validate_json(Path(sys.argv[1]).read_bytes()); print(digest(page.model_dump()))' data/json-page-seeds/PROFILE_SHA256/PAGE_KEY.json
```

`POST /api/tasks/json-page-draft-seed` writes only that private, unregistered draft and returns its hash. `POST /api/tasks/json-page-draft-register` requires the exact private-file hash typed by the operator, re-audits the two runs and all immutable source fields, then registers the unreviewed draft under `data/site-knowledge`. The API takes no caller-supplied source path, rejects changed or wrong-session runs, and does not overwrite an existing seed or page key. The UI fills the registered draft hash into the adjacent [candidate/review workflow](REMOTE_READONLY_DATA_KNOWLEDGE_REVIEW.md). An existing manually registered draft hash remains usable.

Seeding or registration does not contact a site, start a task, verify account/tenant access, establish data rights, semantically review the page, or authorize retrieval, execution, collection or training. The current managed backend is not hot-upgraded by a UI bundle; a deliberate new v2-plan session is needed. Canonical reports and illustrative **synthetic** fixtures: `schemas/remote_readonly_data_page_draft_seed.schema.json`, `schemas/remote_readonly_data_page_draft_registration.schema.json`, `examples/remote_readonly_data_page_draft_seed.json`, and `examples/remote_readonly_data_page_draft_registration.json`.
