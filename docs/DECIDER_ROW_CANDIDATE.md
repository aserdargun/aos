# Synthetic Decider option-row candidate — non-deployable diagnostic

`aos.decider_row_candidate` is an **explicitly opt-in, synthetic-only** model-weight optimizer smoke. The current curated dataset has one S1 train row and no validation/test rows, so this command instead uses two distinct, reviewed source fixtures: `s1-write` for train and `s1-inventory` for held-out. Their exact review hashes, synthetic usage rights, run/group/family/near-duplicate separation and converted input hash are checked. One held-out example is not a meaningful benchmark or license for training on real trajectories.

```sh
.venv/bin/python -m aos.decider_row_candidate \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --include-synthetic --train-candidate
```

The output directory defaults to ignored `data/decider-row-candidates/` and must be owner-only (`0700`); an explicit `--output-root` may select only an immediate child of ignored `data/`. The parent creates a private empty temporary file and removes it on any failed child/report/artifact check. A successful run publishes one content-addressed, owner-only (`0600`) `.aosrows` file. Repeated identical output is idempotent. No path from a model response is accepted. The `aos-option-rows-v1` binary contains an eight-byte magic, 32-byte source/input/deployment-manifest SHA-256 values, two little-endian option-token IDs and two `float32` rows of 2,048 values each, exactly 16,496 bytes. The parent checks those embedded identities against the report and pinned inputs. It is **not** a deployable model, standard adapter or full checkpoint; only an isolated candidate diagnostic. Keep it private and delete it explicitly when no longer needed. Never add it to the source package.

The bounded offline child verifies pinned Decider weights/code/dependencies and at least 8 GiB free CUDA memory. It loads a separate frozen base model, extracts train and held-out answer-slot hidden vectors, identifies the exact A/B option-token IDs in the pinned tokenizer and clones only their two tied output-head rows. One CPU SGD step updates **only the cloned candidate row weights** from the train example; a finite nonzero gradient is required and clipped. Held-out labels are never used for backward or update. Before/after held-out NLL is evaluated with the same frozen held-out vector and the two candidate row states; a decrease on one synthetic example is not generalization evidence. The base model parameters, tied input embeddings and active deployment are never updated. The child rechecks base parameter versions, absent base gradients, pinned files and manifest before writing its temporary artifact.

The parent checks source/input/runner/model identity, report flags, exact artifact hash, binary shape, token IDs, finite row values and owner-only file metadata before publication. Runtime is capped at 180 seconds with bounded I/O; failure returns no report or published artifact. The report contains only hashes, token IDs, aggregate NLL/gradient numbers and fixed safety flags, never prompts, logits or weight rows. `schemas/decider_row_candidate.schema.json` and `examples/decider_row_candidate.json` define the canonical contract; the fixture numbers are illustrative, **not** a model result. A separate read-only replay diagnostic below can reload this row file; there is still no deployable adapter, whole-model checkpoint, promotion, rollback or W5 acceptance. Training readiness and promotion authority remain false.

## Read-only candidate replay

After an explicitly opted-in candidate smoke, select its exact private `.aosrows` path:

```sh
.venv/bin/python -m aos.decider_row_replay \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --artifact data/decider-row-candidates/<exact-artifact-sha256>.aosrows \
  --include-synthetic
```

The artifact must be a private `0600`, single-link file in an immediate owner-only child of ignored `data/`. The parent rechecks its source/input/manifest header, token IDs, finite rows and SHA-256 before and after a bounded worker. The worker revalidates the pinned Decider files/dependencies, loads the **frozen base model** in an isolated process, extracts the same held-out fixture's hidden vector, then scores it separately with base and reloaded candidate rows. It does **not** modify the live model, its tied embeddings, active deployment, or any private dataset. Output contains only aggregate held-out NLL/choice and identity hashes; no model weights or raw text. One synthetic held-out example is a persistence/compatibility smoke, not evidence of generalization or an authorization to train, activate or promote. The binary header alone does not prove optimizer provenance; keep the originating candidate report and artifact hash together for review. `schemas/decider_row_replay.schema.json` and `examples/decider_row_replay.json` define the canonical report and an explicitly illustrative fixture.

Unit tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_decider_row_candidate.py -v`. The real pinned-model test uses `AOS_ROW_CANDIDATE_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python`; it writes under an ignored temporary directory and verifies that directory is removed afterward. Run only with GPU headroom and record actual measurements separately in `docs/STATUS.md`.

Replay tests: `.venv/bin/python -W error::ResourceWarning -m unittest tests.test_decider_row_replay -v`. The real reload/held-out check additionally requires `AOS_ROW_REPLAY_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python`; it creates and deletes its candidate under ignored private `data/` and never changes a deployment.
