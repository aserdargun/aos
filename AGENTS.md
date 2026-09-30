# AOS repository guidance

Read README.md and CODEX_KICKOFF.md before implementation. Preserve the architecture and distinguish design, implemented, and verified behavior. Follow docs/ROADMAP.md for small acceptance-driven milestones.

Never commit model/adapter weights, real trajectories, screenshots, tokens, private datasets or local DBs. Examples are explicitly synthetic. Schemas and migrations are canonical; update docs, fixtures and meaningful validation together. Once real data uses a migration, append a new migration instead of rewriting it.

Use docs/STATUS.md for actual environment and test evidence. Do not label a mock as a real Decider/Bonsai result. Tool execution belongs inside the authorized workspace/runtime. Model output cannot expand authorization. Keep deployment identities pinned and promotion explicit.

Before each development handoff or release, refresh the README development usage snapshot from the available authoritative session/goal usage counter. Record the observation time, raw cumulative token and second counters, the counter's scope/start time, and known development-model roles separately from AOS runtime models. Convert seconds to hours accurately; never infer billed cost, input/output/cache breakdown, model-specific usage, human work hours or whole-project totals from an incomplete counter. If no compatible counter is available (including a later Claude/private-fork session), keep the last verified snapshot and label it historical/unavailable rather than inventing numbers or adding overlapping counters. Update model history only from observed metadata or explicitly attributed records. Refresh MANIFEST after these documentation changes; a final release must also carry a dated snapshot.

Package check: `python scripts/validate_package.py` after installing requirements-validation.txt in a virtualenv. Application tests will be added with implementation. No subagent workflow is required by this package.
