# Source delivery and continuation guide

**Dated source-handoff scope: 3 October 2026.** This is a guide to receiving,
checking and continuing AOS, not a production release, runtime authorization or
claim that the complete product works. License selection remains pending:
publicly readable source is not an approved open-source license.

The next development phase should complete a bounded AI-Scientist and authorized
web-application workflow **inside AOS**, using its Tasks/control surface and
independent outcome checks. Company-intranet SWAPP integration comes last.
Neither this document nor a passing source test authorizes external access,
model execution, training, maintenance or deployment.

## 1. Read the delivery evidence correctly

Use the [canonical release acceptance record](release_acceptance.json) and its
[interpretation guide](RELEASE_ACCEPTANCE.md) for the current acceptance state.
[STATUS](STATUS.md) contains dated environment and execution evidence. This
document is not a second status database, percentage or replacement roadmap.

| Boundary | Delivered or evidenced | Not established by that evidence |
|---|---|---|
| Source contracts | Typed schemas, migrations, fixtures, package integrity checks and source packaging | Production readiness, license clearance or absence of every secret in source/history |
| Execution core | Finite Operator choices, deterministic authorized tools, trajectory storage, independent verification; dated narrow native-model results | Arbitrary-task reliability or fresh-host GPU acceptance |
| Console | Authenticated control center, task/control surfaces and historical versus current evidence separation; dated local UI checks | Current remote-client acceptance or delivery of every later source change to the running UI |
| Scientist integration | Typed Lab requests, durable intent/status/result paths, scoped host preparation, CPU-tested composition, real bounded CPU/API study, pipeline-boundary stop/repeat/terminal recovery and exact-owned CPU cleanup | Active-Scorer interruption, current shared GPU/fairness acceptance or sustained native GPU exclusion |
| Shared runtime preparation | Explicit plan/provision, scoped paths, identity checks and no-replay launch intent; an inert private workspace was prepared | A running shared service, current broker generation, activation or GPU rights |
| Native handover | Preview, inhibit primitive, staged entry, maintenance, concrete exclusion reader and prelaunch verifier source with bounded CPU tests; unapplied shared-only source candidate | Executed maintenance, integrated trusted activation driver, actual worker coexistence or verified physical handover |
| Learning and specialization | Narrow reviewed knowledge/skill/episode paths, dataset tooling and dated experimental adapter evidence | General continuous learning, arbitrary-site specialization, accepted QLoRA or production model promotion |
| Agent orchestration | Opt-in durable runner and bounded CPU/synthetic adapter tests | A generally delivered live multi-agent product or arbitrary third-party agent compatibility |

Read each result with its source/configuration identity, date, environment and
scope. A mock is not an actual Scientist/model result. A historical success does
not validate changed source, a new boot or another deployment. A skip is not a
pass. A passing package check does not run a desktop, model or training job.

## 2. Receive and verify source safely

Use a reviewed checkout or the allowlisted source archive described in
[SOURCE_HANDOFF](SOURCE_HANDOFF.md). Keep the complete checkout: schemas,
migrations, fixtures and scripts are needed outside the Python package. A wheel
alone is not the supported full-runtime distribution.

Do not copy the development host's private state to reproduce its results.
Models/adapters, datasets, trajectories, screenshots, tokens, databases and
runtime receipts are not part of the source delivery. Private evidence paths in
historical documentation may not exist on the recipient's machine.

Development-checkpoint tests validate public source references by default.
Private `data/` evidence is deliberately absent from a source-only delivery;
its separate existence check is skipped unless `AOS_PRIVATE_EVIDENCE_TESTS=1`
is explicitly set on the original evidence-owning host. Do not copy private
logs into a public package merely to make that local-evidence check pass.

Run the following in a **fresh disposable Linux checkout**, not a running
installation. Choose one installation route; do not create `.venv` manually and
then ask the fresh-environment installer to reuse it.

### Application dependencies, without runtime startup

Install Python 3.11+ and `uv` through your normal system tooling. For a console
build, use the Node/pnpm versions declared in `ui/package.json`.

```sh
python3 -m scripts.setup_local --prerequisites
python3 -m scripts.setup_local
python3 -m scripts.setup_local --install
.venv/bin/python scripts/validate_package.py
.venv/bin/agentctl hello --engine fixture
.venv/bin/python scripts/check_capabilities.py --profile core
```

The prerequisites command reports presence, not model integrity or runtime
readiness. The next command prints a plan. `--install` creates a new `.venv`,
uses locked application dependencies, installs validation requirements and
checks the CLI. It refuses an existing destination. Dependency installation may
use network access; it does not download a Python interpreter or model, start a
service/container, admit Scientist, train or promote anything.

The fixture command is explicitly synthetic and writes ignored local
workspace/trajectory data. The core profile reports its actual tests and skips;
it is not real-model acceptance. These commands describe the ordinary source
checkout. The unapplied shared-only promotion candidate deliberately disables
native entrypoints, including ordinary CLI execution; do not bypass that policy
after promotion to reproduce an earlier fixture command.

### Optional isolated console build

As an alternative fresh install, use new, nonoverlapping destinations:

```sh
python3 -m scripts.setup_local --install --build-ui \
  --environment data/handoff-check/environment \
  --ui-directory data/handoff-check/console
data/handoff-check/environment/bin/python scripts/validate_package.py
```

The build is staged under the specified directory, not the live `ui/dist`.
This environment does not replace the `.venv` expected by `scripts/aos-v1`.
`--offline` uses existing caches only. Partial failed output is retained for
inspection; do not silently delete, adopt or retry it. Manifest/source drift is
a failure to resolve, not a reason to regenerate a manifest over unknown bytes.
See [LOCAL_SETUP](LOCAL_SETUP.md) for exact receipt and destination semantics.

### Contracts-only alternative

For source/schema verification without application dependencies, use another
fresh checkout or choose this instead of the installer above:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.txt
.venv/bin/python scripts/validate_package.py
```

Do not infer application, browser or GPU readiness from this tier.

### Runtime preparation is a separate decision

Review [DEVELOPMENT](DEVELOPMENT.md), [MODELS](MODELS.md),
[BONSAI_RUNTIME](BONSAI_RUNTIME.md), [DESKTOP_RUNTIME](DESKTOP_RUNTIME.md) and
[FIRST_RELEASE](FIRST_RELEASE.md) before preparing a real host. The archive does
not provide a fully reproduced clean-machine GPU installer. Model code,
checkpoint, tokenizer, projector, interpreter and native dependencies require
host-specific reviewed pins. Do not copy another machine's deployment identity,
silently repin drift, auto-download large artifacts or restart existing work.

## 3. Finish Scientist inside AOS before expanding targets

Scientist remains the **single shared GPU allocation/execution authority**.
AOS may submit bounded, explicitly approved work; it must not introduce a second
allocator, native fallback or synthetic successful cleanup proof. See
[SCIENTIST_RUNTIME_INTEGRATION](SCIENTIST_RUNTIME_INTEGRATION.md),
[SCIENTIST_HANDOFF](SCIENTIST_HANDOFF.md) and
[SHARED_DESKTOP_MANAGER](SHARED_DESKTOP_MANAGER.md).

The prepared workspace is inert. Old source/configuration acknowledgments and
process/socket observations are historical when their selected bytes or
generation change. Review the final AOS/Scientist source pair and exact scoped
configuration together before actual admission.

The [shared-only candidate](../scripts/shared-only-runtime-v1/README.md) is an
unapplied, reviewed source patch: supported repository-managed native paths are
denied while Scientist broker paths remain available. It is not an OS sandbox
against a trusted owner executing another checkout or arbitrary Python code.
CPU tokenizer/data preparation is not confused with GPU model execution.

The maintenance implementation is source-deliverable, **not live-tested**. Its
intended boundary is explicit finite consent, exact old identities and idle
readback, temporary quiesce, exact process termination, independent physical
cleanup and pinned source promotion with durable partial-failure records.
Temporary restart quiesce alone is releasable and cannot prove sustainable
native exclusion. No automatic native restart, reverse patch or expiry-driven
reopening is permitted; restoring native execution requires separate explicit
authorization. Existing [handover preview](NATIVE_HANDOVER.md),
[inhibit primitive](NATIVE_INHIBIT.md) and
[staged entry](NATIVE_DECIDER_ENTRY.md) are not substitutes for completed coverage.

The native-exclusion module now supplies a concrete `NativeExclusionReader`
and separate `NativeExclusionPrelaunchVerifier`, verified in bounded CPU tests.
The production shared-manager driver and joint trusted activation/cleanup
composition remain open; source implementation does not establish live worker
coexistence or an accepted handover. Required live
evidence must distinguish retired legacy identities from the actual new shared
unit/process generation and bind boot, plan, source/configuration, sustainable
admission closure, independent worker absence and original finite validity.
Audit history is not current authority; expiration must not reopen native work.

The next runtime acceptance must use the actual AOS task/UI path: explicit
approval → dispatch → status/events → result → independent readback, then a
separate subsequent task and cancellation/drain/cleanup cases. Exercise resource
contention, stale generation and uncertain outcomes without replay. Scientist
alone performs authorized GPU acceptance. Historical experiments or a manual
script demonstration do not close this gate.

Separate real CPU/API acceptance now includes a completed study and a controlled
one-start/two-approved-stop terminal recovery run. The latter exited zero in
115.2553 seconds, but peer SQL places Scorer completion at 14:52:36.404943 UTC,
before the trigger at .408127 and stop at .958495. Its scope is pipeline/baseline-
boundary stop and repeated-stop recovery; active-Scorer interruption is unproven.
The helper's broader success flag is not authoritative. Scientist subsequently
closed the exact-owned isolated API and PostgreSQL container; AOS independently
read back API unit/PID/cgroup absence, the pinned database container exited with
PID zero, closed ports and the retained volume. Worker absence was checked for
both runs. SQL queue and sandbox observations remain peer evidence. This is CPU
cleanup, not physical GPU-release proof, and no additional run is implied.

The isolated source now forwards optional [field intent and selected experience](SCIENTIST_TASK_CONTEXT.md)
through the typed proposal, exact approval/intent body and structured EN/TR UI.
Absent new fields retain legacy serialization. References are manually selected;
the server still owns dataset/provider authorization and history eligibility.
This is not deployed default-UI support or a new actual Scientist acceptance.

## 4. Specialize for a particular web application

Specialization is not a single training button. Use the smallest reviewed
improvement that solves the target problem, retaining the existing architecture:
S1 Operator selects finite typed actions, S2 Supervisor plans/diagnoses/recovers,
and deterministic tools execute within independently checked authorization.

### A. Establish one authorized task and baseline

Record the application origin, tenant/account role, allowed data, permitted
reads/writes, task parameters, recovery budget and independent success checks.
Obtain separate permission for network access, external effects and learning
collection. Run a bounded baseline through AOS; preserve failures and uncertain
effects instead of retrying them blindly. Two owned test applications are an
acceptance starting point, not proof of compatibility with every website.

Use [ISOLATED_LEARNING_PROJECTS](ISOLATED_LEARNING_PROJECTS.md) and
[OWNED_PARAMETER_PROJECT](OWNED_PARAMETER_PROJECT.md) for narrow synthetic
preparation/activation paths. Their fixture results do not establish native
inference, a real account or target-site acceptance.

### B. Separate knowledge, retrieval and executable skills

- **Knowledge/RAG:** ingest only authorized, minimized content; bind provenance,
  application/tenant/role, version, review and revocation. Filter access before
  retrieval. Retrieved text is untrusted data, never permission or executable
  instructions. Current document retrieval is a bounded lexical path, not an
  accepted general vector-RAG platform.
- **Skill:** describe typed parameters, preconditions, allowed tools and an
  independent outcome check. Review/release the exact version before selection;
  acquire fresh task authority and state on every reuse. A stored skill does not
  authorize new domains, accounts or writes.
- **Review:** test changed pages, stale knowledge, revoked access, wrong tenant,
  ambiguous results and prompt injection before promoting reusable artifacts.
  Knowledge/skill release is separate from model deployment.

Start with [DOCUMENT_KNOWLEDGE](DOCUMENT_KNOWLEDGE.md),
[TASK_KNOWLEDGE](TASK_KNOWLEDGE.md), [SITE_KNOWLEDGE](SITE_KNOWLEDGE.md),
[SITE_SKILL](SITE_SKILL.md) and [OWNED_SKILL_RELEASES](OWNED_SKILL_RELEASES.md).
Each describes a bounded implemented scope, not arbitrary automatic skill
generation or unconstrained grounded answering.

### C. Build a reviewed dataset only when justified

Collection consent, retention/redaction, dataset reuse and training permission
are distinct. Keep raw evidence private. Export only eligible reviewed records
with provenance and independently observed outcomes; do not turn rejected or
failed actions into positive labels. Record concise diagnoses, not hidden
chain-of-thought. Keep S1 choice/correction and S2 plan/recovery formats separate.

Perform deduplication, leakage-group splitting, held-out selection and canonical
schema validation before model-specific conversion. Synthetic examples remain
explicitly synthetic and excluded from real-data claims. See
[DATASET_PIPELINE](DATASET_PIPELINE.md), [DATASET_AUDIT](DATASET_AUDIT.md),
[DATASET_READINESS](DATASET_READINESS.md) and
[OWNED_EPISODE_PREPARATION](OWNED_EPISODE_PREPARATION.md).

### D. Train, evaluate and promote as separate gates

Tokenizer/preflight success is not training compatibility. Review checkpoint and
data rights, exact base/tokenizer/backend, adapter targets, budget and measured
resources. Inference GGUF support does not establish a trainable S2 checkpoint;
QLoRA is not an accepted universal capability. Obtain explicit training admission
through Scientist before any shared GPU work.

Publish an immutable experimental artifact/report, reload independently and
compare against the pinned base on held-out tasks and safety regressions. A
same-case improvement or successful loss update is not demonstrated general
quality. Promote only with explicit authorization, pinned loaded identity,
appropriate drain and an independently reviewed rollback procedure. Never
silently replace an active deployment or change an in-flight task's model.

See [CONTINUOUS_IMPROVEMENT](CONTINUOUS_IMPROVEMENT.md),
[OWNED_EPISODE_ADAPTATION](OWNED_EPISODE_ADAPTATION.md),
[OWNED_ADAPTER_EVALUATION](OWNED_ADAPTER_EVALUATION.md) and
[OWNED_ADAPTER_RUNTIME](OWNED_ADAPTER_RUNTIME.md). General S2 training, accepted
QLoRA, causal improvement evaluation and production model promotion remain open.

## 5. Continue with new agents and private targets

Start with [AGENTS.md](../AGENTS.md), [CODEX_KICKOFF](../CODEX_KICKOFF.md),
[ARCHITECTURE](ARCHITECTURE.md), [SECURITY](SECURITY.md), the canonical acceptance
record and current dated evidence. For another development assistant/private
fork, use [CLAUDE_HANDOFF](CLAUDE_HANDOFF.md) and
[EXTENDING_AOS](EXTENDING_AOS.md). Preserve architecture and source identities;
do not replace the execution core with a new general agent framework.

For runtime agents, follow [AGENT_ORCHESTRATION](AGENT_ORCHESTRATION.md): typed
identity/capabilities, bounded authorization/budget, durable intent, dependency
checks, status/events/results, job-specific cancellation and independent cleanup.
An agent proposes artifacts and actions; model output cannot expand authority.
Keep a single desktop input owner and independently authorize any parallel
isolated instance. Existing CPU adapter evidence is not a stable arbitrary-agent
plugin SDK or real-agent compatibility claim.

Keep the next slice small: complete one authorized Scientist plus web-application
workflow through AOS, record its actual gaps, then repeat with new inputs and
reviewed reuse. Do not restart finished source work or add helpers in place of
closing the concrete acceptance gate.

SWAPP/company intranet integration is last. Obtain explicit network/account/data
scope, approved target tasks, private-data handling and real-site acceptance
before connecting. A tunnel or authenticated UI is not intranet authorization;
do not bypass private-network guards or publish company-specific configuration,
corpora, trajectories or adapters.

## 6. Handoff acceptance and remaining release blockers

### Requested release version

The requested first release is **v0.1.0**, a **source/prototype delivery** to be
versioned after the source-delivery checks below finish. It is not conditional
on completing all six future runtime/product stages: those remain open in the
canonical acceptance record, including the explicitly deferred SWAPP stage.
This distinction neither redefines the full goal nor promotes runtime authority.
Python, web UI, Cargo and Tauri package metadata
already declare `0.1.0`; this does not mean a Git release exists or acceptance
has passed. At the 3 October review no `v0.1.0` Git tag existed. Do not tag the
old HEAD while the tested implementation remains in uncommitted/untracked files.
First freeze and review the exact source set, rerun the required checks, record
known limitations and usage, refresh the manifest, and commit that coherent
reviewed tree. Only then create the requested version tag and publish within
the separately authorized remote/branch scope, without force push. Do not call
this a completed production/GPU release or an approved open-source license.
The [planned release note](releases/v0.1.0.md) records the bounded source scope;
it is not evidence that the tag has been created.
License selection remains an explicit gate before public versioned release,
as required by [SOURCE_HANDOFF](SOURCE_HANDOFF.md). The v0.1.0 tag is deferred
pending the owner's license decision and final review. Previously authorized
reviewed source-checkpoint publication is not a release/tag or license approval;
no intentionally unlicensed release policy is inferred here.

**Acceptable source handoff:** reviewed source and exact manifest; validation and
focused/core test evidence with skips/environment; checked archive membership;
documented reproducible setup tiers and known limitations; no secret/runtime
artifacts; no implied live change. The recipient can inspect and develop the
source without being told unperformed GPU tests passed.

Published source baseline `101f51a` has a checked 1,634-member source archive:
archive SHA256 `2901f91c33e9f8c9bcd22821ce5cf9b93984926d0b9df822ba2c0cf508653bb2`,
manifest SHA256 `ccdb87273df8a0c8e6d0db7a41aea81c503aa908353486d71b2a9f1c4e12b153`.
Root validation passed 5,642 package checks and 99 focused tests/3.227 seconds;
extracted source passed 5,642 checks and 98 tests/1 private-evidence skip/3.263
seconds. These are exact baseline results; this documentation update still
requires final review and a refreshed manifest before the planned tag.

**Still blocks the corresponding runtime/product release:** real joint
Scientist/native handover and producer/consumer acceptance; fresh current-source
GPU/task/cancellation/cleanup evidence; target-user UI delivery; authorized web
workflow and specialization acceptance; clean-host runtime reproducibility;
license decision and applicable third-party/model rights. SWAPP remains deferred,
not silently removed from the full goal.

**Regression closure, 3 October:** the seven failures from the initial handoff
are fixed. Migration assertions now track the supported current schema while
preserving exact legacy rows, trigger SQL and foreign-key checks; the downgrade
fixture removes version 28 completely. The backup canonical schema accepts 28,
and an actual synthetic current-schema backup validates without permitting
version relabeling. The receipt test broker now survives empty authentication
probes and still rejects truncated frames; production authentication/framing did
not change. The full 3426-instance core run completed with 3122 passes, 304
skips and no failures/errors. Skips keep the official outcome `partial`, not
real runtime acceptance. Five ignored SQLite finalizer ResourceWarnings remain
in the log; their allocation origin is not established by the current test name.
The console also warns about its approximately 1 MB minified main JavaScript
chunk. Exact commands and evidence are in dated [STATUS](STATUS.md).

Before packaging, the release owner refreshes the README's authoritative
development usage snapshot and dated release record, then the manifest. Follow
[USAGE_ACCOUNTING](USAGE_ACCOUNTING.md): development counters and runtime API use
are separate. Preserve scope/start/observation time and raw counters; never infer
model/provider bills, subscription cost, human hours or whole-project totals from
an incomplete aggregate. Historical or unavailable counters remain labeled.
Never publish raw private token/session logs.

Only after source review is complete and concurrent edits have stopped:

```sh
python3 scripts/update_manifest.py
.venv/bin/python scripts/validate_package.py
python3 scripts/package_handoff.py --output /tmp/aos-source.tar
```

The output must not already exist. Verify the extracted source too; record the
actual result in the release evidence. The archive allowlist/integrity checks do
not replace privacy, source/history or licensing review. Do not archive the
whole working tree or blindly stage all dirty files. Scheduled commit/push is a
separate explicitly reviewed automation boundary, not a side effect of these
commands, and must not publish private artifacts or unreviewed changes.

**Administrative progress and remaining work:** the private hourly usage timer
was explicitly installed and activated on 3 October, 13:14 UTC. Actual readback
showed enabled/active-waiting; its first service run succeeded and produced a
private snapshot. [USAGE_TIMER](USAGE_TIMER.md) preserves the earlier failure and
corrected activation evidence. This is accounting-service acceptance, not an
AOS runtime or GPU change. Reviewed automatic Git commit/push is still not
installed, so the broader automation request is not counted as complete.
Do not replace it with
blind staging of the development working tree. It needs an approved exact file
set, source/privacy checks, clean-index and branch/remote checks, drift handling,
non-force publication and visible failure reporting. This is separate from the
runtime/GPU acceptance path and does not block inspecting the source archive.
