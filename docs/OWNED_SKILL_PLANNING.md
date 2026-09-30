# Owned selected-skill goal planning

## Implemented boundary

This implements goal → Bonsai proposal → explicit bind → execution preview →
explicit start → reviewed S1 skill execution. Generating a proposal alone never
creates a task, action, approval, training sample, review, or activation. The
plan-bound execution uses schema 1.4 and still requires six fresh manual action
approvals. Existing manual schema-1.3 execution remains separate; a bound plan
must be explicitly discarded before switching to that manual path.

Only a fresh managed [owned skill reuse](OWNED_SKILL_REUSE.md) backend configures
this planner. The ordinary running application is not restarted or upgraded.
The planner uses the pinned native Bonsai runtime, a distinct protocol/deployment
identity and a 768-token output limit; it does not download models or train.

The host recognizes exactly these two case-sensitive templates:

```text
Save message "gamma"
Mesaj alanına "gamma" kaydet
```

There must be one literal of 1–128 ASCII letters, digits, spaces, `_`, `.`, or
`-`, without leading/trailing whitespace. Extra instructions, negation, multiple
literals and other grammars return `needs_human` without a model call. This is
not general natural-language understanding. The host derives the case key and
revalidates the admitted candidate, review, release, selection, original source,
runtime, lease and generation. The model cannot select another skill, tool,
URL, value, case key, or authority; it may propose the exact admitted steps or
abstain. Existing development/held-out/source-value restrictions still apply.

## Authenticated API

The default `POST /api/tasks/owned-skill-plan` request is:

```json
{"schema_version":"1.0","goal":"Save message \"gamma\"","lease_id":"current-lease","generation":0}
```

The illustrative lease must be replaced by the actual current control identity.
Version `1.1` additionally requires a boolean `collect_learning`; true persists
separate episode consent before the model task starts. Version `1.0` remains
opt-out. [Episode content and review contract](OWNED_EPISODE_LEARNING.md).
The asynchronous response is HTTP 202. Poll `GET /api/tasks/owned-skill-plan` for
`pending`, `ready`, `needs_human`, `cancelled` or `failed`; idle/unavailable are
reported before any proposal or on an unconfigured backend. Status carries no
goal, literal, raw model text or private path. `model_called` and `real_model`
distinguish a rejected host input, a fixture and an attempted actual model call;
only `ready` means a validated proposal, not verified execution. Explicit binding
changes it to `bound`; the first start attempt consumes it, including a failed
attempt. No proposal or failed execution is automatically retried.

At most eight recognized planning attempts run per backend session. A pending
native call reserves task admission and first releases the reusable Decider
worker. The long await is outside the console control lock. Pause, Stop,
Take Control, logout and shutdown invalidate the result and cancel the call;
reservation is retained until cancellation drains. Completion rechecks current
control and source pins before publishing. No GPU concurrency or automatic
task start is introduced.

## Explicit UI and execution binding

In a newly admitted owned-reuse session, open **Tasks**, select **HTTPS form**,
and use **Plan with Bonsai, execute with Decider**:

1. Enter a supported goal and select **Generate proposal**.
2. Select **Confirm proposal and preview execution** for this proposal.
3. Inspect the six ordered operations and preview hash, check the exact-plan
   confirmation, then select **Start with manual approvals**.
4. Approve the six actions separately. After success, select **Audit plan-bound
   result**. A successful job alone is not the provenance audit.

The panel is bilingual EN/TR. Scope changes clear local confirmation; reload
does not silently reclaim a bound proposal. **Discard proposal** releases it.
There is no automatic downgrade to manual execution when planning fails.

Authenticated `POST /api/tasks/owned-skill-plan/{bind,start,discard,audit}`
requires `schema_version: "1.4"`. Bind/discard accept only
`planning_bundle_sha256`, matching `confirm_plan_sha256`, and current
`lease_id`/`generation`. Start additionally requires `preview_sha256` and its
matching `confirm_sha256`. Audit accepts only `candidate_execution_sha256`.
Candidate, case, value, source, release and selection come from the private
proposal, never caller overrides. Versions 1.0–1.3 cannot replace the planned
route, and planned audit cannot return a successful legacy/base-only report.

The preview and execution identities both pin `planning_bundle_sha256`.
The execution directory contains immutable `planning-bundle.json` alongside
the existing review/release/selection/reuse provenance. Persist, load and each
pre-action recheck join the original source/candidate/recipe, exact goal literal,
case and ordered steps, manager/session/runtime/lease/generation. A one-shot
internal start context ties execution to the explicit bind/consume transition.

Before the first action, a content-free `skill.owned_planning_admission`
observation pins the proposal and preview. Historical audit requires exactly
one matching observation after initial CREATED state and before the first
action, plus all existing candidate, review, release, reuse and SQL joins.
It works without a live planner, native model startup or current lease; the
current known protocol and canonical schema are still required. Future protocol
compatibility is not assumed. Missing or altered provenance is unavailable,
not a weaker successful audit.

## Private record and remaining scope

The manager's `owned-skill-plans/` directory is owner-only 0700; immutable
content-addressed JSON files are 0600, single-link, canonical and bounded.
They retain the request, structured evidence, exact model request, **parsed
canonical model response** (not the raw HTTP envelope), deployment/pins,
bounded metrics and authority snapshot. There is no fabricated `model_calls`
row for a run that does not exist. All execution, activation, training and
downstream-verification flags remain false. Examples are explicitly synthetic.

Still required for the actual product: an authorized real application and account,
independent result oracle, broader goal/skill coverage, separately consented S1/S2
real-application learning records, independent held-out evaluation and explicit training and
promotion. This synthetic selected-skill path does not close a real-site W1–W6
gate. Implemented behavior, actual test evidence and failed diagnostic attempts
are recorded separately in [STATUS](STATUS.md).
