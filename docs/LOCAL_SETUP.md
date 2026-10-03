# Fresh local source setup

This setup command prepares application dependencies and an optional isolated
console build. It does not prepare GPU models, start a desktop/container/server,
modify a running installation, admit Scientist, train, deploy or choose a license.
Scientist runtime coordination remains a separate gate.

For the separate reviewed, fixture-only Scientist console, see
[SCIENTIST_CPU_PANEL](SCIENTIST_CPU_PANEL.md). Its portable command defaults to
local validation, requires exact source/config/private-UI pins and never starts
the backend without `--start`. Existing pilot and frozen CPU preparation are
not automatically upgraded.

## New Linux checkout

Before installation, an existing checkout can inspect tool/artifact presence:

```sh
python3 -m scripts.setup_local --prerequisites
```

This path is read-only even when source manifests are stale or environments
already exist. It reads bounded nofollow local preparation manifests, lists
missing/malformed artifacts and tools, and performs no version/process/model
probes or weight hashing. `presence_complete` is only presence: Docker image,
pin/hash integrity, GPU readiness, inference and task acceptance remain unverified.
Use the existing `aos-v1 doctor` for its full preparation checks; that command can
hash large trees and is not implicitly run by this lightweight report.

Install Python 3.11+, `uv`, and optionally the Node/pnpm versions named in
`ui/package.json` using your normal system tooling. No root package changes are
performed by AOS. Inspect the dated source manifest first.

```sh
python3 -m scripts.setup_local
python3 -m scripts.setup_local --install
```

The first command only prints the plan. The second creates a **new** `.venv`
using `uv sync --locked` with dataset/desktop/browser extras, installs the pinned
validation requirements, and verifies that the installed CLI imports and exposes
its help. It refuses an existing environment rather than updating or deleting it.
Dependency registry/cache access is installation, not model download. No Python
interpreter is downloaded. Supply `--python /path/to/python` if required.

For an existing checkout, use an explicit fresh environment instead:

```sh
python3 -m scripts.setup_local --install \
  --environment data/my-setup/environment --python /path/to/python
```

This environment does not replace the `.venv` used by `scripts/aos-v1`.
`--offline` uses existing caches only and fails if required packages are absent.
On failure, partial output stays for inspection. Choose a different fresh path
after inspecting the cause; the command never cleans it up or retries for you.
Manifest mismatch/concurrent source edits prevent successful setup reporting.

Completed receipts now include the observation time, exact input/manifest hashes,
observed uv/Node/pnpm versions, installed Python/distribution versions and editable
AOS source binding. Staged builds record each bounded regular artifact's hash.
The editable source can change later; the receipt describes its observed snapshot,
not a permanently frozen installed application. Failed command stages emit a
failed JSON receipt with `installed: false`, `ui_staged: false`, completed-stage
history and inspection guidance. This is a setup receipt, not runtime admission.

## Optional console staging

```sh
python3 -m scripts.setup_local --install --build-ui \
  --environment data/my-setup/environment \
  --ui-directory data/my-setup/console
```

UI source/config/lockfiles are copied into the fresh staging directory; pnpm uses
the frozen lockfile with install scripts disabled. The build lives in that
directory's `dist`, **not** the live `ui/dist`. Existing `node_modules`, UI builds,
environments, model manifests and services are untouched. Environment/UI paths
cannot overlap or traverse symlink/parent components. Explicit UI promotion and
runtime activation require a separate reviewed idle/fresh checkout procedure;
this tool never promotes a build.

## What still must be prepared

Use [DEVELOPMENT](DEVELOPMENT.md), [BONSAI_RUNTIME](BONSAI_RUNTIME.md)
and [FIRST_RELEASE](FIRST_RELEASE.md) for the
model/browser/desktop pins and actual authorized pilot. Models, adapters, real
trajectories, private tokens and company data are not distributed by setup.
Prepared CPU dependencies/compiled UI are not native/GPU or clean-machine runtime
acceptance. A separate [Scientist handoff](SCIENTIST_HANDOFF.md) describes the
unconfirmed joint capability and sole Scientist-runner GPU acceptance.
