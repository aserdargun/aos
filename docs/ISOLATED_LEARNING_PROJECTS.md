# Named isolated learning projects — implemented, CPU verified

The public manager accepts a paired project name and loopback port on every
supported command. This selects a separate private manager root without changing
the ordinary `data/local-app-v1` instance or its port8765. It is not a second GPU
scheduler, automatic execution grant, or proof of parallel model residency.

```sh
./scripts/aos-v1 prepare-ui --project learning-demo --project-port 18766
./scripts/aos-v1 doctor --fixture --project learning-demo --project-port 18766
./scripts/aos-v1 status --project learning-demo --project-port 18766
./scripts/aos-v1 start --fixture --project learning-demo --project-port 18766
./scripts/aos-v1 token --project learning-demo --project-port 18766
./scripts/aos-v1 open --project learning-demo --project-port 18766
./scripts/aos-v1 restart --project learning-demo --project-port 18766 --expected-session APP_SESSION
./scripts/aos-v1 stop --project learning-demo --project-port 18766 --expected-session APP_SESSION
```

Replace `APP_SESSION` with the exact session returned by that project's status.
`prepare-ui` uses installed local Node/Vite offline, with bounded output and a
30-second build deadline. It builds in a private staging directory and publishes
only after source/state/asset checks, without replacing an existing UI. It never
installs dependencies, starts a runtime or grants execution authority. A previous
session must have verified cleanup and both processes absent, not merely an idle
flag. Failed staging stays private for inspection. If its UI already exists,
inspect it rather than overwrite it; do not rebuild shared `ui/dist`. Named
preflight/doctor and backend require this exact derived UI directory, reject
missing/stale/symlinked assets, and never fall back to shared UI. Doctor is
read-only and does not build, start a runtime or grant inference authority.

Commands above are usage examples. Public `prepare-ui` plus three actual CPU
fixture sessions passed in11.742s. A separate browser-enabled repetition passed
in12.562s, verifying actual token login, default Development, EN/TR and mobile
layout with GPU-disabled Chromium. No real inference or learning was performed.
A separate earlier unique project completed two
actual public-CLI CPU fixture manager/backend/container sessions in7.374s;
authentication, scope readback, idempotent start and exact-session shutdown passed.
Shared UI/default manager snapshots stayed unchanged; both owned processes,
containers, tokens and listener cleaned up. This is CPU lifecycle acceptance,
not a model/GPU/learning result. Named owned-v1 learning startup uses the existing explicit
`--owned-synthetic-form-invocation` mode instead of `--fixture`; it still requires
the independently reviewed runtime/model/scheduler prerequisites. No task starts
automatically and no additional GPU rights are conferred.

Names match `[a-z0-9][a-z0-9-]{0,47}`. Ports must be1024–65535 and must not be8765.
The private root is `data/local-app-project-NAME`; arbitrary public `--base` is
still rejected. The persisted project name and URL must match the selected scope
before credential reads, requests, signals or child launch. The typed immutable
scope is context-local, not mutation of process-wide default configuration.

Existing source provisioning, lifetime lock, selected-skill readmission and
private original data are reused. Reuse preview/start must carry the same project
pair, original release/selection hashes and explicit preview confirmation. The
lowest source-path check also binds the persisted project to the exact base name;
copying a state under another project does not adopt it. Backend roots and
listener are scoped; ordinary retention and automatic local login are not enabled.

Authenticated Tasks metadata exposes only validated project/port. Its EN/TR skill
guide preserves scope in preview/start/token commands. Named project cookies are
separate from the legacy default cookie, since browsers do not isolate cookies by
port. Shared-cookie-jar tests exercise login/logout coexistence on one host.

Supported public commands: prepare-ui, doctor, start, status, token, stop, open, restart,
release-restart, recover-clean-exit, recover-reboot and selected-skill reuse preview.
Named stop/restart/release/recovery require the exact current `--expected-session`.
Restart is only for a verified plain idle or cleanly stopped session; retained
owned-learning sessions still require explicit stop/reuse/start with original pins.
An interrupted named session is never recovered automatically by restart.

After inspection, a current named restart admission lock can be explicitly
released through `release-restart` with the same project/port/session. Development
shows this scope-bound command only when the manager session is available.
Recovery commands retain physical process/workspace/container/journal proofs;
they do not kill arbitrary orphans, replay old work or create a new GPU lease.
Failed cleanup refuses successor launch. Original predecessor identity is checked
again inside the start lock, so a concurrent replacement cannot be adopted.

Remote-plan startup, standalone owned recipe and other synthetic modes remain
unsupported. Existing explicitly pinned CPU parameter-project activation is
supported as described in [OWNED_PARAMETER_PROJECT](OWNED_PARAMETER_PROJECT.md).
It requires the complete source directory/manifest/fixture-engine tuple, fresh
one-use activation and exact external source profile/listener/workspace locks.
It is not plain `--fixture` mode, native inference or restart continuation.
Named remote-form guidance does not offer an unrelated
default command. Real reboot/crash recovery was not executed in acceptance.

Evidence includes CPU/mock/private owned-file, isolated GPU-disabled Chromium,
and the actual public CPU fixture lifecycle described above. Real model/learning
multi-project execution, model inference, held-out quality and shared-GPU
acceptance remain unrun. Do not infer product completion,
learning quality, training/promotion or private SWAPP acceptance from isolation.
