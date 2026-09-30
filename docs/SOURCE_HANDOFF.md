# Source-only handoff and publication

AOS is being prepared for public source distribution and private forks. License selection is still pending; source availability alone is not an open-source license. Nothing in this workflow publishes to a remote repository or grants rights to redistribute model weights or company data.

## Build a reviewed source archive

From a reviewed checkout, with no concurrent source edits:

```sh
python3 scripts/update_manifest.py
.venv/bin/python scripts/validate_package.py
python3 scripts/package_handoff.py --output /tmp/aos-source.tar
```

The output must not exist. The packager uses a source allowlist and verified `MANIFEST.sha256`, not a recursive archive of the working directory. It rejects unsafe names, links, non-regular files, disallowed artifacts and changed hashes. The uncompressed tar contains relative source paths and the manifest; executable scripts retain executable permissions. Archive bytes are deterministic for the same source contents and executable modes.

Excluded content includes local model/adapter weights, private datasets, trajectories, screenshots, databases, tokens, runtime state, virtualenvs, node_modules and build output. The private-root README placeholders are included. Do not replace this command with `tar` of the whole working tree or `git add .` on the development host. Local agent configuration directories are ignored, not a publication allowlist.

This is an integrity and file-policy check, **not** a comprehensive secret scanner or a signature. Text source can still contain sensitive values. Before publishing, inspect the archive and review source/history for secrets, personal host identifiers, proprietary content and third-party licenses. Historical `docs/STATUS.md` describes one development machine, not a fresh-host guarantee; public connection guides use placeholders. Select the project license and review any notices before a public release. Do not copy private artifacts into a fork to reproduce historical evidence.

## New-host setup tiers

Extract only an archive you have reviewed into an empty directory. Commands below run inside that directory; keep the checkout because migrations, schemas and fixtures live outside the Python package. A standalone wheel is not the supported full-runtime installation.

### 1. Contracts only — no application or model

Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python scripts/validate_package.py
```

Dependencies may require network access to install; package validation itself does not run a model, browser, container or training job.

### 2. Fixture CLI and bounded core tests

```sh
.venv/bin/python -m pip install -e '.[dataset,browser,desktop]'
.venv/bin/agentctl hello --engine fixture
.venv/bin/python scripts/check_capabilities.py --profile core
```

The CLI uses an explicitly synthetic decision engine and writes local ignored workspace/trajectory data. It is not Decider acceptance. Core tests exclude the full local-app lifecycle module and enable only its safe subset; opt-in skips are not passing real-model tests. The extras satisfy test imports, but do not download a browser, build Docker images or prepare models. See [test pool scope](CAPABILITY_TEST_POOL.md).

### 3. Build the UI

With a compatible Node/pnpm environment:

```sh
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
```

This builds static assets, not a ready GPU backend, native installer or live Agent Computer.

### 4. Prepare an explicitly authorized real runtime

Review [development](DEVELOPMENT.md), [Decider/model constraints](MODELS.md), [Bonsai runtime](BONSAI_RUNTIME.md), [desktop runtime](DESKTOP_RUNTIME.md), [Playwright MCP](PLAYWRIGHT_MCP.md) and [first release](FIRST_RELEASE.md). Prepare host-specific Python/CUDA/runtime dependencies, reviewed model/code/tokenizer/projector artifacts and immutable manifests; build the pinned Agent Computer and install the required browser runtime. Keep inference environments separate when needed.

A fully reproducible fresh-host GPU installer and inference environment lock are **not** delivered by this archive. Existing download helpers do not by themselves establish all training/tokenizer/backend prerequisites. Do not reuse another host's manifest blindly or assume an arbitrary model/LoRA/QLoRA is compatible. Run doctor and explicitly selected acceptance only after setup and without interrupting other workloads; do not auto-download, train or promote as part of source verification.

### 5. Continue in a private fork

Follow [Claude handoff](CLAUDE_HANDOFF.md), [extension guide](EXTENDING_AOS.md) and [continuous improvement](CONTINUOUS_IMPROVEMENT.md). Keep company-specific profiles, credentials, corpora and adapters private and outside source distribution. The future SWAPP integration needs explicit network/account/data scope and independent real-site acceptance. Existing private-IP guards remain in force; a tunnel is not permission to bypass them.

## Delivery acceptance

- A fresh source manifest and package validation pass on the archive's extracted files.
- Source-only focused/core tests and UI build report their actual environment, skips and limits separately.
- Archive membership contains no local runtime artifacts; tampered paths, links and hashes are rejected by tests.
- No running session is restarted, no model is changed, no training occurs, and no remote publication is performed by packaging.
- License choice, fresh-host GPU setup, general learning/RAG/model migration and real-site acceptance remain explicit open items rather than completed percentages.

Executed results belong in [STATUS](STATUS.md). Passing the source handoff does not imply the product roadmap is finished.
