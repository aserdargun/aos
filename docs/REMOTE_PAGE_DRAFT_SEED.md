# W2 audited HTTPS route draft seed

`aos.remote_page_draft_seed` prepares one private `SitePageDraft` from two completed, ordered, exact-profile/plan HTTPS route runs. It reuses the full independent approval/readback audit and one frozen SQLite snapshot from `aos.remote_route_change`. The selected route must have the same URL/title/H1 fingerprint in both runs. The exact URL comes from the audited plan in that snapshot, not a caller-supplied URL. An invalid or already-used page key, superseded profile, changed route, unsupported symbolic path, or unsafe output fails closed.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_page_draft_seed \
  --database /path/to/private/trajectory.sqlite \
  --before-run-id RUN_A --after-run-id RUN_B \
  --profiles /path/to/private/web-applications \
  --store /path/to/private/site-knowledge \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256 \
  --route-index 0 --page-key entry \
  --output /home/cachyos/aos/data/private-seeds/entry.json
```

Create the output directory with mode `0700` first. The output must be a **new** file under repository `data/`; it is written `0600` and contains the exact route origin/path, so keep it private. The command does not register the file. Its stdout contains only hashes, selected index/key, and explicit false authority flags; hashes themselves may be sensitive. The report contract and explicitly synthetic example are `schemas/remote_page_draft_seed.schema.json` and `examples/remote_page_draft_seed.json`.

The human-selected `page_key` is still a manual semantic assertion. Empty landmarks/outgoing keys are not inferred from the page. Review the private draft, edit semantic keys if appropriate, then use `aos.site_knowledge preview` and explicit `register --confirm-sha256` if desired. Registration is a draft, not activation. Neither this command nor the two historical observations prove the current page, account, tenant, rights, application outcome or full site coverage. No new browser request, task retrieval, execution, collection or training authorization is granted. W2 real-site acceptance remains open.

## Authenticated Tasks UI

With a pinned route plan in a **new** backend session, the Tasks panel lets the operator compare two succeeded route jobs from that same session and select one stable index. Entering a symbolic page key and clicking **Save private page draft** invokes `POST /api/tasks/page-draft-seed`; the server chooses the already pinned database/profile/plan and re-audits both runs. The client cannot supply a URL, output path, profile, plan or store. The server creates `data/site-page-seeds/PROFILE_SHA256/PAGE_KEY.json` as a new private `0600` file inside `0700` directories, then returns only the metadata report. A repeat save, changed source, invalid key or competing audit fails closed. The authenticated page never receives the URL or page body.

The operator must inspect that private file. To register it in the immutable draft store, enter its **exact SHA-256** in the separate confirmation field and click **Register confirmed draft**. `POST /api/tasks/page-draft-register` requires the same current-session run IDs and server-pinned source scope, re-audits both historical runs, reads an owner-only canonical file without following symlinks, checks its exact confirmed hash and unchanged profile/URL/fingerprint/revision/authority fields, then registers it. Landmarks and outgoing keys may be edited as explicit manual semantic assertions, not inferred evidence. Changed bytes, weak file permissions, a changed page fingerprint, stale profile or already registered page key fail closed. Its report contract and synthetic-only fixture are `schemas/remote_page_draft_registration.schema.json` and `examples/remote_page_draft_registration.json`. Registration is still an **unreviewed draft**: it does not activate knowledge or authorize task retrieval, browser execution, collection or training. The CLI `aos.site_knowledge preview/register` remains an alternative for a private file.

The next explicit Tasks step is [metadata candidate preview and hash-confirmed review](REMOTE_ROUTE_KNOWLEDGE_REVIEW.md). Registering a draft alone does not perform that review or enable it in the current session.
