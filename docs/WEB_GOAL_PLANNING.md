# Free-text goals against a scoped web skill catalog

## Implemented boundary

`BonsaiWebGoalPlanner` is a separate, proposal-only System-2 adapter. It accepts free-text goals rather than the two exact owned-save-message forms. One immutable catalog identifies one application/tenant/role/profile and up to eight skills. Each skill carries exact skill/task/recipe/release/selection hashes, a description and up to eight bounded text parameters, optionally restricted to enumerated values.

The host creates a per-catalog response grammar: select one catalog skill and every required parameter, or return `needs_human`. Unknown skills/parameters, foreign catalog hashes, extra fields, missing/true/nonboolean permission flags, invalid bounds, duplicate JSON keys and unsupported parameter values are rejected. Unicode and quoted text are not normalized. Descriptions and goals cannot add a URL/tool/action field or broaden authorization.

This adapter does **not** yet read live profile/release registries, authorize a site/account, compile an executable task, expose a console API or start System-1. Catalog hashes supplied in the synthetic example are placeholders, not reviewed source admission. `scope_authorization_verified`, execution/activation/training flags remain false. The current owned selected-skill executor and its exact grammars stay unchanged.

## Model and lifecycle

The separate deployment pins protocol `aos-web-goal-plan-v1`, canonical response schema, exact system-prompt digest and a 1,024-token output bound. Existing recovery and owned-skill deployment identities do not change. Native inference reuses the pinned Bonsai process/cleanup path. Prompt/schema drift is rejected before inference; a changed prompt requires a different deployment identity.

Calling `plan` requires exact inference consent and a host `current_catalog` provider. The provider must return a typed current catalog equal to the frozen request snapshot before scheduling, immediately before dispatch and after response. The adapter composes the pre-existing dispatch hook first and restores its exact presence/value after success, failure or cancellation. Concurrent use of one planner is rejected. The supplied evidence is copied before await so another caller cannot replace the dispatched catalog.

This freshness callback is a seam for host-owned source validation, not a built-in external ACL or source attestation. The caller must implement registry/review/source checks before admitting a real catalog. A model proposal may contain valid parameter text that is nevertheless semantically wrong; separate host preview, fresh execution consent and independent effect verification are still required.

## Validation

`examples/web_goal_planning.json` defines two explicitly synthetic catalogs (contact notes/priorities and inventory notes/status) and five independent expected proposal cases, including TR Unicode and a negated task. These are model-interface oracles, not browser tasks, held-out quality or real-site success.

CPU tests: `PYTHONPATH=src:tests .venv/bin/python -m unittest test_web_goal_planner`. Native tests require an explicitly reserved idle GPU and `AOS_WEB_GOAL_PLANNER_REAL_TESTS=1`; private request/response records remain under ignored `data/`. The capability runner selects this opt-in only in `real_tasks`. Exact executed evidence and any model failures are recorded in [STATUS](STATUS.md).

## Remaining integration

Build catalogs from validated profile/task/skill/review/release registries, not request-supplied self-claims. Bind proposals to current runtime/control/source identity, expose consent/review in the console, compile parameters to a supported deterministic executor without letting the model choose URLs/tools, and verify two different synthetic applications end-to-end with real S1 and independent oracles. Preserve cancel/stale-source/uncertain-POST zero-replay behavior. Only that broader acceptance can close [remaining work item 3](REMAINING_WORK.md); this proposal adapter does not close it.
