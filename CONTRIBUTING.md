# Contributing

AOS is an experimental local agent system under active development. Contributions should keep its permission boundaries visible and make claims no stronger than the evidence. AOS's original source and documentation use [Apache-2.0](LICENSE), selected with maintainer authorization. Submit only work you have the right to contribute and preserve applicable third-party notices. See [licensing scope](docs/LICENSING.md); do not change the license without explicit maintainer direction.

## Local setup

Use a supported Python 3 environment and Node/pnpm for the UI. A typical editable development setup is:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python -m pip install -e '.[dataset,browser,desktop]'
.venv/bin/python scripts/check_capabilities.py --profile core
```

Use Python 3.11 or newer. The extras cover eager imports in the core profile; a minimal editable install supports only limited fixture CLI work. `uv sync --locked --all-extras` is the alternative for locked application dependencies; install validation dependencies separately as described in `docs/DEVELOPMENT.md`. UI changes use the checked-in pnpm lock:

```sh
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
```

Do not install models or start a runtime merely to validate a documentation or pure-code change.

## Change expectations

- Find the existing typed contract and integration point before adding a new abstraction. Preserve fail-closed validation, immutable deployment pins, explicit user authorization, independent verification, and bounded cleanup.
- Keep schema files canonical. When changing persisted contracts, update the typed model, schema, synthetic example, validator/package check, relevant docs, and tests together. Add a new database migration rather than rewriting one already used by real data.
- Add negative tests for stale pins, malformed model output, unauthorized actions, source drift, cancellation, and cleanup when relevant. Label all synthetic fixtures explicitly.
- Keep local runtime data out of patches: model and adapter weights, trajectories, screenshots, tokens, private datasets, and databases must not enter Git.
- Update `docs/STATUS.md` only with evidence actually collected. Name the exact command/profile, distinguish real models from fixtures, and state skips, failures, and remaining limits.
- Do not describe the current integration seams as a stable third-party plugin API. Discuss substantial interface changes before expanding their authority.

## Test selection and risk

Prefer an individual test or a focused module. `scripts/check_capabilities.py --list` is a read-only way to inspect the named evidence pools; `--profile core` excludes the full port-affecting `test_local_app.py` module and selects the safe local-app tests. `ui` and `transport` require isolated browser/Docker setup. `real` can run Decider/Bonsai, Docker, visible browser, or CUDA work and must be explicitly scheduled away from live user tasks. Never assume that a test profile is safe merely because its name contains “test.” See [`docs/CONTINUOUS_IMPROVEMENT.md`](docs/CONTINUOUS_IMPROVEMENT.md) for the intended role-separated development cycle.

Do not run the entire `tests/test_local_app.py` against a host with a live local application. Its port lifecycle and supervisor tests can interfere with a running service. For the safe subset, use the capability pool or an explicit `LocalAppTests` unittest selection after checking its scope.

Before proposing a change, report what you tested and what you did not. Tests that skip, use fixture models, or simulate network responses do not establish real-model or real-site acceptance.
