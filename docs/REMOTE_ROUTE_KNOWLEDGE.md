# W2 remote route / manual draft candidate

`aos.remote_route_knowledge` is a read-only preview linking one exact immutable `SitePageDraft` to one route in two completed, separately approved `browser_remote_routes` runs. Both runs must share the selected registered profile, exact route plan and audited SQLite snapshot. The **selected** route's URL/title/H1 fingerprint must be unchanged between the two historical readbacks; unrelated planned routes may change without falsely invalidating this page. The draft must have the same profile, application, tenant, role, exact origin and non-templated route URL, and its claimed fingerprint must match the audited readback. If the manual draft claims outgoing page keys, its selected route additionally needs an equal, complete bounded browser link sample in both historical runs. The candidate binds the sample hash and each declared key to an observed authorized route index through a current same-scope draft; it does not prove the key's semantic meaning or equality of unregistered destinations. Each key requires one current same-scope, non-templated target draft whose URL and fingerprint match an observed planned route; `outgoing_route_indices` and `outgoing_knowledge_sha256` report the matched route indices and exact current target-draft checksums in key order. Missing or ambiguous targets and a changed, missing, truncated or legacy-unavailable sample block the candidate. A superseded profile or page revision fails closed.

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_route_knowledge \
  --database /path/to/private/store.sqlite \
  --before-run-id RUN_A --after-run-id RUN_B \
  --profiles /path/to/private/web-applications \
  --store /path/to/private/site-knowledge \
  --knowledge-sha256 PAGE_DRAFT_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256 \
  --route-index 1
```

`candidate_remote_profile_bound` means the draft's metadata matched those two historical readbacks, **not** that the draft is reviewed, current, or safe to retrieve in a task. `origin_verified`, `account_verified`, `site_outcome_verified`, `reviewed`, `task_retrieval_authorized`, `execution_authorized`, `collection_authorized` and `training_ready` remain false. This preview does not visit the site, write a DB/draft, promote a version, expose raw URL/content/run IDs, or grant model output authority. Hashes remain sensitive private metadata.

The contract and example are `schemas/remote_route_knowledge.schema.json` and `examples/remote_route_knowledge.json`; the example is explicitly synthetic. The owned test checks two stable synthetic TLS readbacks, changed readback rejection, wrong route rejection, CLI output, and stale draft rejection. Real target W2 acceptance still requires authorized data rights, account and origin verification, reviewed tenant/role-scoped knowledge, current-state checks and bounded task-time retrieval.
