# Explicit JSON-bundle metadata review and fresh-run recheck

The JSON-bundle [page draft candidate](REMOTE_READONLY_DATA_KNOWLEDGE.md) is historical evidence only. An operator may record a separate, immutable, owner-only metadata review for its exact candidate hash. A later, distinct, completed v2 JSON-bundle run can then be compared with the reviewed source. Neither operation fetches a site, starts a task, grants execution, or enables task-time retrieval.

## Review an exact candidate

Use the full canonical SHA-256 digest of the candidate JSON emitted by `aos.remote_readonly_data_knowledge`. Its historical snapshot hash is part of this exact confirmation; registration re-audits the source before writing anything.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_readonly_data_knowledge_review register \
  --database data/PRIVATE_SESSION/store.sqlite \
  --before-run-id BEFORE_RUN_ID --after-run-id AFTER_RUN_ID \
  --profiles data/web-applications --site-store data/site-knowledge \
  --review-store data/json-knowledge-reviews \
  --knowledge-sha256 PAGE_DRAFT_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256 \
  --confirm-candidate-sha256 CANDIDATE_SHA256 \
  --acknowledge-metadata-only
```

Paths and IDs are placeholders. Missing acknowledgement, wrong hash, changed source, successor profile/page, or unsafe files fail closed. The review file contains hashes and symbolic IDs only, is `0600`, and lives under the private `data/` tree. It does not store JSON bodies, DOM text, URLs, screenshots, or model output.

## Authenticated Tasks workflow

In a **new backend session pinned to a v2 JSON plan**, Tasks shows the JSON page metadata review panel. It accepts two successful JSON-bundle run IDs from that session and the exact SHA-256 of an already registered manual `SitePageDraft`. The adjacent [private draft workflow](REMOTE_READONLY_DATA_PAGE_DRAFT.md) can now seed and register that unreviewed draft from stable runs with separate exact-hash confirmation; it does not infer page meaning. `POST /api/tasks/json-knowledge-preview` returns the content-free canonical candidate and its digest. The operator must type that exact digest and explicitly acknowledge metadata-only review before `POST /api/tasks/json-knowledge-review` writes the private record. The panel can later select a distinct completed run and exact review hash for `POST /api/tasks/json-knowledge-recheck`. A failed or drifted recheck returns no page keys. The panel never runs commands, contacts a site, or activates the review in the current task.

The API pins the database, profile, plan, page store, and private review store on the server; callers cannot supply paths or broaden scope. It rejects unauthenticated requests, wrong-session or incomplete runs, duplicate/extra body fields, wrong hashes, missing acknowledgement, changed sources, and concurrent audits. The browser displays only symbolic keys, hashes, and status; the existing Tasks scope panel separately shows the exact URLs already required for action approval. An older running backend is not hot-upgraded by a new UI build. The wrapper contract and synthetic fixture are `schemas/remote_readonly_data_knowledge_ui_preview.schema.json` and `examples/remote_readonly_data_knowledge_ui_preview.json`.

## Explicit live readback binding in a new v2 session

To bind an existing private review to a **new** managed v2 JSON-bundle session, first run the offline manager preview with the exact profile, task, plan, historical source database, page store, review store, and review SHA-256. These paths are placeholders and must refer to owner-only sources under `data/`. The task and plan files must be the same exact v2 scope as the review.

```sh
./scripts/aos-v1 preview-remote-json-review \
  --remote-entry-profile-sha256 PROFILE_SHA256 \
  --remote-entry-task-file data/PRIVATE_TASK.json \
  --remote-readonly-data-plan-file data/PRIVATE_V2_PLAN.json \
  --remote-json-review-source-database data/PRIVATE_SESSION/store.sqlite \
  --remote-json-review-site-store data/site-knowledge \
  --remote-json-review-store data/json-knowledge-reviews \
  --remote-json-review-sha256 REVIEW_SHA256
```

Inspect the current session, stop it safely, then use `./scripts/aos-v1 start` with **the same seven options and values**. Preview is offline and never launches a task. Manager rechecks the historical audit and private source identity before child launch; the direct backend additionally accepts an optional exact source snapshot SHA-256 and rejects a mismatch before serving. A running session is not upgraded. `restart` refuses this opt-in scope, so a later session must be started with the exact options again.

After the new task's separate manual approval, independent JSON/asset/page readback and historical source recheck, the scheduler records a content-free `browser.remote_json_knowledge` observation. An exact fingerprint match reports `matched` with the symbolic page key, revision, and landmark keys; a changed fingerprint reports `stale` **without** those keys. Authenticated Tasks displays the latest job's matched/stale summary only when this review is pinned; it never displays raw JSON or DOM in that status. This is post-open reporting, not a hint passed to the Decider decision, a real-site outcome, semantic page review, or execution/collection/training authority. Contracts and synthetic fixture: `schemas/remote_readonly_data_knowledge_live_pin.schema.json`, `schemas/remote_readonly_data_knowledge_live_event.schema.json`, `examples/remote_readonly_data_knowledge_live.json`.

## Recheck a fresh completed run

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_readonly_data_knowledge_review recheck \
  --database data/PRIVATE_SESSION/store.sqlite \
  --current-run-id FRESH_RUN_ID \
  --profiles data/web-applications --site-store data/site-knowledge \
  --review-store data/json-knowledge-reviews \
  --review-sha256 REVIEW_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

The recheck re-audits both historical runs and the fresh run in one frozen SQLite snapshot. It requires the same profile and plan, a distinct run **started after the review was recorded**, consumed human approval, independent readback, and an unchanged versioned fingerprint across entry HTML, JS/CSS, JSON responses, title, and H1. Older second-resolution review records conservatively require a run starting in a later second. It also checks that the manual page draft is still the latest matching revision. On match, it returns only symbolic page/landmark keys and hashes; on any drift it returns no reusable page metadata. Later appended runs may change the DB snapshot hash without invalidating unchanged historical source evidence.

This is a **post-run, read-only metadata check**. It cannot prove the page key or landmarks are semantically correct, account/tenant identity at the site, external data rights, live page freshness between runs, or application outcome. Task-time retrieval and real-site W2 acceptance remain open. No collection, skill activation, training, or promotion authority is created. Schemas and synthetic fixture: `schemas/remote_readonly_data_knowledge_review.schema.json`, `schemas/remote_readonly_data_knowledge_review_receipt.schema.json`, `schemas/remote_readonly_data_knowledge_recheck.schema.json`, `examples/remote_readonly_data_knowledge_review.json`.
