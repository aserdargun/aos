# W4 — Read-only remote learning source audit

`aos.remote_learning_source` inspects one **completed, successful** `browser_remote_routes` run against an exact registered profile and ordered plan. It uses one frozen trajectory snapshot, rechecks every route's private profile/plan binding, separate human approval, browser readback and passed transport verification, then selects only actual pinned model-call metadata tied to those route decisions. A requested role with no matching call has an empty list; no Bonsai result is invented from Decider success.

```sh
.venv/bin/python -m aos.remote_learning_source \
  --database data/<private-trajectory.sqlite> \
  --run-id <completed-run-id> \
  --profiles data/web-applications \
  --selected-profile-sha256 <exact-profile-sha256> \
  --selected-plan-sha256 <exact-plan-sha256>
```

The canonical [report schema](../schemas/remote_learning_source.schema.json) contains only source event IDs, hashes, requested role names and closed authority flags. The [example](../examples/remote_learning_source.json) is explicitly synthetic and illustrative. The command makes no network request, does not write an outbox or dataset, and never exports URLs, DOM, model prompts/responses, credentials or screenshots. The output is still sensitive metadata and should stay private.

This is **not** the W4 live collector. It requires a settled run rather than polling during the task. A profile's `data_rights_ref` is only a request label, not reviewed rights. The report therefore fixes `rights_reviewed=false`, `redaction_reviewed=false`, `collection_authorized=false` and `training_ready=false`; transport readback is not an independent application outcome or account identity. S1 events are not gold labels, and S2 appears only when a real linked supervisor escalation exists. Future work needs an explicit rights/scope grant, role-specific incremental outbox, redaction/review, retention and reproducible held-out datasets before W4 acceptance.

Tests cover schema/unsafe source, owned two-route Chromium/MCP fixture with zero fake model events, real pinned Decider with two S1 events and zero S2 events, wrong plan and changed approval rejection. Neither owned fixture nor synthetic profile is the user's target site.
