# W2 foundation — private site page drafts

`aos.site_knowledge` stores a manually authored **draft** page description under one exact immutable web-application profile revision. It is a durable, profile/tenant/role-scoped foundation for future site memory, not discovered or verified site knowledge. It does not open a browser, collect data, run a skill, authorize an action, or train a model.

The canonical `schemas/site_page_draft.schema.json` and explicitly synthetic `examples/site_page_draft.json` define one page key, exact allowed origin, symbolic route template, claimed page fingerprint and UTC draft time, symbolic landmark keys and outgoing page keys. They contain no raw DOM, selectors, screenshots, form values or credentials. A static route segment can still contain sensitive text: the validator is not a PII detector. Do not enter real customer identifiers or secrets. The fingerprint, draft time and any current fingerprint supplied for inspection are **caller assertions**, not browser attestation.

To try the fixture, first register the matching synthetic profile as described in [WEB_APPLICATION_PROFILE](WEB_APPLICATION_PROFILE.md). Extract only `page` from the example into a private source file, then use the exact preview hash:

```sh
umask 077
.venv/bin/python -c 'import json; from pathlib import Path; value=json.loads(Path("examples/site_page_draft.json").read_text())["page"]; Path("data/synthetic-site-page.json").open("x").write(json.dumps(value))'
.venv/bin/python -m aos.site_knowledge preview --page data/synthetic-site-page.json
.venv/bin/python -m aos.site_knowledge register --page data/synthetic-site-page.json --confirm-sha256 <reviewed-sha256>
.venv/bin/python -m aos.site_knowledge list --profile-sha256 <profile-sha256> --page-key record-list
.venv/bin/python -m aos.site_knowledge inspect --sha256 <reviewed-sha256> --selected-profile-sha256 <profile-sha256> --current-fingerprint-sha256 <claimed-current-fingerprint>
```

The private store defaults to ignored `data/site-knowledge`; `--profiles` and `--store` precede the subcommand if different roots are needed. The store requires a registered matching profile and exact confirmation. Files are content-addressed, owner-only and immutable; reads reject symlinks, hardlinks, altered bytes, broad permissions, broken lineage and changed profile scope. A later page revision names the exact prior hash and retains the same profile revision, application, tenant, role and page key. A new profile revision never silently imports an old page. Repeated registration of the same bytes is idempotent. `list` returns only bounded metadata for one exact profile, optionally one page key; it returns every matching revision, **never chooses a latest/active revision**, and rejects unexpected store files.

`inspect` reports only `draft_match` or `stale`, always with execution/collection/training false. It checks the selected profile and caller-supplied fingerprint, then scans the bounded private inventory: a registered descendant makes the selected exact revision `stale` even if its fingerprint is unchanged. Competing roots, forked revisions or corrupt inventory fail closed instead of choosing a branch. This does not designate the remaining tip as active or atomically freeze concurrent registrations. `draft_match` does **not** mean the page was seen, reviewed or is safe to use. An observed mismatch must cause a future retriever or skill to stop and request fresh review; there is no active retriever yet. The CLI omits routes and landmarks from its reports and uses a fixed error message. A publication crash between hard-link creation and temporary-name removal can leave a hardlink; reads fail closed rather than repairing it automatically.

There is no trajectory/run relationship or SQL table in this slice, so no migration was added. W2 still needs authorized source capture, evidence references, review, bounded retrieval, verified invalidation after actual page changes and live-task acceptance. W1 network/account scope and collection rights remain prerequisites for any real site use. Tests: `.venv/bin/python -m unittest discover -s tests -p test_site_knowledge.py -v`.

The separate [W2b local-page evidence projection](SITE_PAGE_EVIDENCE.md) can derive a synthetic semantic fingerprint from independently verified MCP navigation, but it does not import that evidence into drafts or establish real-site scope.
