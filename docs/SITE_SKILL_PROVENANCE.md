# W3b — manual skill source provenance inspection

`aos.site_skill_provenance` inspects **one explicitly selected, immutable manual skill draft** against **one explicitly selected, audited trajectory run**. It first reads the draft through `SiteSkillStore.get`, including its profile/page/tenant/role scope and content-addressed lineage checks. It then recomputes `learning_event_v2` metadata from a bounded SQLite snapshot rather than trusting the draft's caller-supplied source IDs.

```bash
.venv/bin/python -m aos.site_skill_provenance \
  --database /path/to/private-trajectory.sqlite --run-id RUN_ID \
  --profiles /path/to/private-web-applications \
  --pages /path/to/private-site-knowledge --skills /path/to/private-site-skills \
  --skill-sha256 SKILL_SHA256
```

Every claimed source event must exist in that single run, have the skill's exact S1 or S2 role, and share one run-pinned model deployment identity. The same frozen SQLite snapshot checks each selected call's canonical, bounded JSON request and returns sorted unique `source_request_sha256` values so a private W3 rehearsal plan can pin its source without copying the request. A wrong/noncanonical/oversized request fails closed. For S1, each claimed verification ID must be among independently attributed direct verification IDs on those selected events; no claim is reported as `not_claimed`. S2 has **no direct outcome verification** in this projection: its status is always `absent_for_system2`, and a draft claiming a direct verification ID is rejected even if a downstream Decider verification exists. Downstream verification is shown only as a separate boolean, never as Bonsai gold or plan success. Missing event, wrong role/deployment, mismatched verification, corrupt source, or invalid selection fails closed without partial output.

The result contains only hashes, source IDs, role/model identity, counts and booleans. It omits skill steps, prompts, raw observations, URLs, screenshots and credentials. A request hash is not redaction of a low-entropy private input; keep the report private and do not publish or commit real output. A run-pinned deployment identity is an audited local trajectory claim, not remote attestation. Crucially, the trajectory run is **not bound to the draft's profile, tenant, account, page or task**, and a request hash does not identify a skill parameter value. `profile_bound`, `reviewed`, execution, collection, activation and training flags remain false even when all claimed IDs match. No live collector, skill execution, review, dataset, model call, migration or promotion is added. The canonical report schema is `schemas/site_skill_provenance.schema.json`; `examples/site_skill_provenance.json` is explicitly synthetic.

For a separately audited HTTPS route run, [remote S1 source binding](REMOTE_SITE_SKILL_PROVENANCE.md) can additionally check the manual draft against the run's exact profile/task/plan and selected route readback. That separate report does **not** turn transport success into application outcome, verified skill behavior or authorization.

Focused synthetic-trajectory tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_site_skill_provenance.py -v`. This is a provenance consistency check, not W3 skill validation or real-site mastery.
