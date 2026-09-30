# W2 private remote route metadata review and fresh-run reuse

`aos.remote_route_knowledge_review` turns an exact `candidate_remote_profile_bound` preview into an owner-only, content-addressed **metadata review record** after an explicit candidate-hash confirmation and `--acknowledge-metadata-only`. The acknowledgement records a local CLI assertion, not a verified reviewer identity, account, tenant access, data right or site outcome. The record contains exact historical run IDs and hashes but no page body, screenshot, Cookie or token; it belongs under ignored `data/` with `0700` directory and `0600` file permissions. Do not commit real review files.

First obtain a candidate from [remote route knowledge](REMOTE_ROUTE_KNOWLEDGE.md), inspect its scope, and compute the SHA-256 of its canonical JSON report. A registration example is:

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_route_knowledge_review register \
  --database data/<private-trajectory>.sqlite \
  --before-run-id RUN_A --after-run-id RUN_B \
  --profiles data/web-applications --site-store data/site-knowledge \
  --review-store data/remote-route-reviews \
  --knowledge-sha256 PAGE_DRAFT_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256 --route-index 1 \
  --confirm-candidate-sha256 CANDIDATE_REPORT_SHA256 \
  --acknowledge-metadata-only
```

The command re-audits both completed runs, exact profile/tenant/role/plan/route, approvals and independent readbacks, then writes a new immutable record. A wrong hash or missing acknowledgement writes nothing. Registration returns only a review hash, candidate hash and false authority flags. It does not fetch a site or change the running backend.

## Authenticated Tasks metadata review

In a **new** backend with a pinned route plan, the Tasks panel can carry a same-session two-run stable index through private draft seed, exact draft-hash registration, and **Preview metadata candidate**. `POST /api/tasks/page-knowledge-preview` uses only the server-pinned database/profile/plan/site store and the selected succeeded run IDs, route index and registered draft hash. It returns the canonical candidate plus its SHA-256; no URL or page body is returned. The operator must inspect the candidate's page key, revision and index, type its exact hash, and separately check **I acknowledge this is metadata review only** before **Record metadata review** becomes available. `POST /api/tasks/page-knowledge-review` re-audits the candidate and writes the same owner-only immutable review record as the CLI under `data/remote-route-reviews`. Both endpoints share the graph/seed bounded-audit concurrency guard; wrong session, changed source, wrong hash and missing acknowledgement fail closed. The response is the existing metadata-only review receipt. The wrapper contract and synthetic fixture are `schemas/remote_route_knowledge_ui_preview.schema.json` and `examples/remote_route_knowledge_ui_preview.json`.

The review hash is **not activated in the running session**. To opt into narrow task-time symbolic metadata later, deliberately start a new session with the four exact source/review pins below. Metadata review is a local operator acknowledgement, not verified reviewer identity, account access, data rights, semantic correctness or application success.

After a **distinct, completed run started after the review record** on the same exact plan, reuse can re-audit the historical pair and current run, then return only the current page key, revision, symbolic landmark/outgoing keys and hashes. Older second-resolution review records conservatively require a run starting in a later second:

```sh
PYTHONPATH=src .venv/bin/python -m aos.remote_route_knowledge_review retrieve \
  --database data/<private-trajectory>.sqlite --current-run-id RUN_C \
  --profiles data/web-applications --site-store data/site-knowledge \
  --review-store data/remote-route-reviews --review-sha256 REVIEW_SHA256 \
  --selected-profile-sha256 PROFILE_SHA256 \
  --selected-plan-sha256 PLAN_SHA256
```

Changed route fingerprints, a changed historical evidence chain, wrong profile/plan, corrupted/private-file permissions, a superseded page draft or a superseded profile fail closed. For a draft with manual outgoing page keys, a different, missing or truncated complete link sample in the fresh run also fails closed, even if URL/title/H1 stay unchanged. Offline reuse now also requires each mapped target route's fresh URL/title/H1 fingerprint to equal the reviewed target draft; an unchanged source page with a changed target fails closed. The link sample is a stale signal; the re-audited candidate separately binds each manual key to an observed authorized route index, without proving semantic meaning or current site state. The explicit review hash selects one record; there is no implicit “latest” or automatic activation. A later DB snapshot may differ after new runs, so retrieval compares the re-audited historical candidate **excluding only its snapshot hash**, while requiring the candidate and current-run audits to share the same new snapshot. This accepts append-only new runs without accepting changed historical evidence. A successor target-page draft changes `outgoing_knowledge_sha256` even when URL and fingerprint stay equal, so the old outgoing-key review fails closed and requires a new explicit candidate/review.

`reviewed_metadata_reused` is **offline metadata reuse after a completed run**, not task-time model-context retrieval, verified human review, a current logged-in account, a site-specific success oracle or W2 product acceptance. `task_retrieval_authorized`, execution, collection and training remain false. The site can change after the fresh readback; task execution must still enforce its own live policy and approvals. Contracts and synthetic fixture: `schemas/remote_route_knowledge_review.schema.json`, `schemas/remote_route_knowledge_review_receipt.schema.json`, `schemas/remote_route_knowledge_reuse.schema.json`, `examples/remote_route_knowledge_review.json`.

## Explicit task-time symbolic metadata opt-in

For a **new** managed `browser_remote_routes` session, supply the existing exact profile, private task and route plan plus all four explicit review/source options:

```sh
./scripts/aos-v1 preview-remote-route-review \
  --remote-entry-profile-sha256 PROFILE_SHA256 \
  --remote-entry-task-file data/<private-task>.json \
  --remote-routes-plan-file data/<private-routes>.json \
  --remote-route-review-source-database data/<historical-trajectory>.sqlite \
  --remote-route-review-site-store data/site-knowledge \
  --remote-route-review-store data/remote-route-reviews \
  --remote-route-review-sha256 REVIEW_SHA256
```

The preview runs the same private-source and historical review admission checks as `start`, but prints only the selected profile/plan/review hashes, audited source snapshot hash and false authority flags. It does not start, stop or alter a session, make an HTTPS request, or activate the review. After inspecting the result, deliberately replace `preview-remote-route-review` with `start` using the same exact options; `start` rechecks the sources and can still reject a changed snapshot. A preview is not a real-site, account, data-rights or outcome acceptance.

The existing session is never upgraded in place: inspect and stop it deliberately before a new start. The direct `scripts/serve_desktop.py` entry point accepts the same four review/source options. Sources must remain private under ignored `data/`; the historical SQLite source must be an owner-only regular file. The manager records the review hash and a digest of exact source paths plus the audited source snapshot in its private state, rechecks the snapshot in its supervisor, and passes the expected snapshot to the backend's admission guard. A changed source between start and backend admission fails closed. The review hash is explicit, never selected as latest.

Historical two-run comparison and page-draft preview now share **one frozen, integrity-checked SQLite snapshot per call** rather than repeatedly backing up the same database. Both runs still receive their full action, approval, verification and source checks against that same image. The live review admission path consequently needs one source snapshot for its candidate instead of four; later task decisions intentionally re-audit to catch source or draft changes. An owned test asserts the one-snapshot boundary, but the full browser test is dominated by runtime startup and does not establish an end-to-end latency improvement.

At admission and during the task, the backend re-audits the historical two-run candidate, current profile, plan and latest immutable draft. After its own separately approved exact GET and independent browser readback, it compares the selected route's live URL/title/H1 fingerprint. If the draft has outgoing keys, the complete bounded link sample must also match both historical readbacks at opening and fresh readback. A source match writes a contentless `browser.remote_route_knowledge` observation but does **not** pass symbolic keys to the next Decider choice while any mapped outgoing target remains unopened. Each mapped target must have its own separately approved GET and independent readback matching the pinned fingerprint. Only after **all** mapped targets match can the review hash, page key, revision and bounded symbolic landmark/outgoing keys reach a later Decider choice. A target mismatch writes a target-specific `stale` event and no symbolic context is passed. A source mismatch likewise writes `stale` and passes no context. In a two-route source→target plan there is no later choice after the target, so no model-context reuse occurs in that run. An owned synthetic source→target→later-route task verifies both paths with the real pinned Decider: the first two choices lack reviewed metadata; the third sees only symbolic keys after a matched target's separate approval and readback, or no keys after a changed target and a recorded `stale` event. Other route URLs, tool options, approvals, execution and training rights do not change. A changed historical source before any context-bearing decision fails closed. Site content can still change after the last readback; this bounded task-time context is not authority granted by the review record (`task_retrieval_authorized=false`) or proof of account/outcome.

The synthetic TLS fixture covers a distinct trajectory DB/desktop session, matched reuse and changed-route stale handling. A separate direct backend process also accepts the exact source snapshot and advertises the configured review hash; a wrong snapshot fails at admission. None contacts an external site or proves a real managed process, real target, reviewer identity, authenticated tenant, independent site outcome or W2 product acceptance. Additional contracts: `schemas/remote_route_knowledge_live_pin.schema.json` and `schemas/remote_route_knowledge_live_event.schema.json`.

The EN/TR Tasks panel displays the explicitly configured review hash only when present, alongside the fixed route plan and separate-approval warning. After a live readback event, the latest route job additionally exposes only its matched/stale status, route index and bounded stale reason through the authenticated task-status API. The panel distinguishes a source match from an unverified outgoing target and identifies a later target fingerprint change without exposing URLs, page content or raw hashes. It shows no live claim before an event, for an unrelated latest job, or without a configured review pin. Neither the pin nor this display turns metadata into verified site knowledge or a site outcome.
