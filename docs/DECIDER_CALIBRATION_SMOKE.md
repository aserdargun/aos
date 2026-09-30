# Transient Decider calibration diagnostic — not fine-tuning

`aos.decider_calibration_smoke` is an **opt-in, synthetic-only optimizer-plumbing diagnostic**. The existing curated fixture dataset has one S1 train example and **no validation/test example**; it cannot support a dataset-bound held-out optimizer claim. Instead, this command selects two distinct reviewed synthetic source fixtures (`s1-write` for the single train example and `s1-inventory` for the single held-out example). It verifies their source review hashes, synthetic rights, distinct runs, groups, task families and near-duplicate clusters before launching a child. These are two toy examples, not a statistically meaningful evaluation set or a replacement for the curated dataset/readiness gates.

```sh
.venv/bin/python -m aos.decider_calibration_smoke \
  --manifest models/decider-manifest.json \
  --model-python /home/cachyos/.venv/bin/python \
  --include-synthetic --optimizer-smoke
```

Both flags are required. The bounded offline child verifies pinned model/code/dependencies and at least 8 GiB free CUDA memory. It loads a **separate** frozen Decider copy, obtains detached logits for the two fixed examples, and applies exactly one CPU SGD step to a disposable scalar log-temperature using only the train example. It checks a finite scalar gradient and reports train and held-out NLL before and after that step. The held-out logits are never used for backward or optimizer updates; their after-loss reuses the same frozen logits with the new scalar. Held-out loss may worsen and is not a success oracle.

No Decider weight receives a gradient or optimizer step. The child rechecks model parameter versions, gradient absence, pinned files and manifest; it exits without a checkpoint, adapter, promotion or model activation. The parent verifies exact source/input/runner/manifest/weight identities before emitting a metadata-only report. A child report is a local check, not adversarial attestation. No logits, prompts, source text or model weights enter the report. On failure, the CLI prints a generic error and no partial report; it does not stop the active desktop session or another model service. The diagnostic does not change training readiness, authorize real-site data or satisfy W5 fine-tune/evaluation/promotion/rollback acceptance.

`schemas/decider_calibration_smoke.schema.json` and `examples/decider_calibration_smoke.json` are the canonical report contract and **illustrative synthetic fixture**, not a measured model result. `test_decider_calibration_smoke.py` covers source and identity binding, opt-in, held-out separation, schema/authority flags and mocked worker failures. The real pinned-model test is opt-in with `AOS_CALIBRATION_TESTS=1 AOS_MODEL_PYTHON=/home/cachyos/.venv/bin/python`; run it only when the GPU is free and record any actual result separately in `docs/STATUS.md`.
