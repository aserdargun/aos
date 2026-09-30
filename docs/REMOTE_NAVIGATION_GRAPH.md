# W2 observed HTTPS navigation graph candidate

`aos.remote_navigation_graph` derives a content-free route-index graph from two explicitly selected completed `browser_remote_routes` runs. It reuses the exact selected registered profile, route plan, ordering, separate approvals, browser readbacks and final single-snapshot source audit required by [remote route change](REMOTE_ROUTE_CHANGE.md). It does not fetch pages or mutate a database.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_navigation_graph \
  --database /path/to/private/store.sqlite \
  --before-run-id RUN_A --after-run-id RUN_B \
  --profiles /path/to/private/web-applications \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

`stable_pages` contains planned routes whose versioned URL/title/H1 fingerprint is identical in both runs. `observed_edges` contains only a source route's planned link index when its complete bounded link sample is identical in both runs **and** the target route is stable. `changed_route_indices` and `incomplete_link_sample_indices` expose why a planned route or its outgoing edges are omitted. The graph can be empty. Its canonical contract and explicitly synthetic example are `schemas/remote_navigation_graph.schema.json` and `examples/remote_navigation_graph.json`.

This is a **historical observation candidate**, not a site map or a `SitePageDraft`. It does not infer semantic page keys or link text; an observed anchor to a planned route is not proof of navigation outcome, current destination, account/tenant rights, or entire-site coverage. The sampled inventory is bounded to 64 anchors; unknown destinations are not exposed. The report does not review, activate, retrieve, collect, execute or train anything. Hashes may still be sensitive and should remain private. W2 real-site acceptance remains open.

Owned synthetic TLS/Ubuntu/Chromium/MCP tests cover a stable two-page observed edge, a changed target that removes the edge, a changed source link sample that removes the edge, CLI output, missing source rejection, and a single audited snapshot. No external site is contacted.

When the managed backend is started with a pinned ordered HTTPS route plan, the authenticated Tasks panel lets the operator explicitly select two succeeded route jobs from the **current session** and request the same read-only graph. `POST /api/tasks/navigation-graph` accepts only those two run IDs in a duplicate-free JSON body; it pins the already configured profile/plan and audits the private trajectory database in a worker thread. At most one graph audit runs per console; another concurrent request receives `429` rather than starting another bounded SQLite backup. It rejects unauthenticated, missing, unfinished, wrong-session, reversed, changed or unsafe sources without opening another route. The EN/TR panel displays only route indices and counts, not raw URLs or hash values. It does not turn the graph into reviewed site knowledge.

A selected stable index can be sent to the separate [private page draft seed](REMOTE_PAGE_DRAFT_SEED.md) action with a human-chosen semantic page key. That action re-audits the same exact sources and first writes only a private, unregistered draft. A second explicit SHA-256 confirmation can register it as an unreviewed draft; neither action proves a semantic page identity or grants task-time authority.
