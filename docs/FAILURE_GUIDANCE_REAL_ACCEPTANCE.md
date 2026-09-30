# Private pinned-model guidance acceptance

This opt-in test checks the implemented `hello-guidance-v1` contract with the actual pinned Decider and a fresh isolated Docker workspace. A successful run proves exact guidance input binding and a separately approved fixed Hello outcome. It does not prove causal improvement, a reusable skill/RAG result, a gold label, training readiness, promotion or private-site acceptance. Execution evidence belongs in [STATUS](STATUS.md); skipped tests do not establish real-model acceptance.

## Prerequisites and session safety

Use the prepared repository virtualenv and desktop dependencies from [DEVELOPMENT](DEVELOPMENT.md), the existing verified `models/decider-manifest.json`, the compatible inference Python at `~/.venv/bin/python`, and the prepared `models/desktop-manifest.json`/Docker image from [DESKTOP_RUNTIME](DESKTOP_RUNTIME.md). The test loads the prepared model; it does not download weights or prepare a runtime.

Run only during an explicitly authorized idle GPU window with no competing Decider/Bonsai/training workload. Direct unittest does not acquire the capability pool's advisory GPU lock. A fresh test backend still shares the host GPU with the user's session. Do not stop, restart or take control of a live user session to make this test pass; if that session is busy, defer real acceptance. Use the commands below for this single module, rather than unrestricted desktop test discovery.

## Commands

From the repository root, verify the default opt-in boundary without Docker or inference:

```bash
env -u AOS_DESKTOP_TESTS -u AOS_FAILURE_GUIDANCE_REAL_TESTS \
  .venv/bin/python -m unittest discover \
  -s tests -p test_failure_guidance_real.py -v
```

Expected: one skipped test. During the authorized idle window, explicitly enable both flags:

```bash
AOS_DESKTOP_TESTS=1 AOS_FAILURE_GUIDANCE_REAL_TESTS=1 \
  .venv/bin/python -m unittest discover \
  -s tests -p test_failure_guidance_real.py -v
```

The harness starts a new loopback backend on an ephemeral port and its own Docker runtime. It authenticates using that backend's private local token, submits exact action approval/rejection through the API, and closes its own backend/container on exit. These are authenticated harness approvals, not observed human UI interaction. No existing user session is selected or restarted.

## Required evidence

- Reject the original real-model Hello write before any action/file effect, then separately save and accept `refresh_observation` metadata.
- Reject missing consent, incorrect confirmation/context hashes and unsupported guidance before admitting a task or creating guidance intent files.
- Start a distinct guided task with fresh manual approval, zero actions/file effects before approval, and no automatic approval. The old rejected approval cannot authorize the new task.
- Verify one successful S1 call per run against the prepared Decider deployment digest. Check exact admission/session, context marker, run/step/DECIDE snapshot/state, unchanged options, request hash/body, call identity and returned prediction/decision bindings. There is no S2 call in this Hello case.
- Approve the new write, verify exact file content by independent readback, and retain separate `context_binding_verified`, `model_request_verified`, `guidance_applied` and outcome evidence. `causality_verified`, `gold` and `training_ready` stay false.
- Revoke the review after completion: historical application/binding and verified outcome remain, current review validity becomes false, and a new preview is denied without creating another task.
- Temporarily replace only the new test's private guidance intent with malformed JSON and an empty object: inspection must reject both. Restore the original bytes/mode and require the previous historical report again.

## Artifact boundary

The test retains a unique mode-0700 `data/failure-guidance-real-*` directory containing its private SQLite trajectory, logs, workspace and metadata intents/reviews. The final JSON summary prints only its evidence directory, job/deployment identifiers, flags and elapsed seconds. Keep raw requests, responses, authentication tokens, DBs and any local model artifacts private and outside Git/source packages. The deliberately malformed intent bytes are restored even if an assertion fails.

Record the dated command, pass/skip/failure result and limited scope in [STATUS](STATUS.md) after an actual authorized run. A successful synthetic Hello with real inference is not a controlled correction comparison or a general learning acceptance result. On failure, inspect only the new private evidence directory; do not repair the user's session as an implicit test side effect.
