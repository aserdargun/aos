# Audited synthetic canvas S2 skill candidate

`aos.vision_skill_candidate` derives one **content-free, read-only, unreviewed** Bonsai/System-2 scene candidate from an explicitly selected completed synthetic `vision_canvas` run. It requires a real pinned Bonsai call, matching capture artifact and scene observation, a real pinned Decider decision bound to that scene, one manual action approval, and a separate successful `vision.verify` readback. It does not execute or activate a skill, create a gold S2 label, collect site data, or authorize training.

```sh
.venv/bin/python -m aos.vision_skill_candidate \
  --database data/private/store.sqlite --run-id RUN_ID
```

An optional `--snapshot-sha256 HASH` pins a previously inspected database snapshot; source changes are rejected. The command uses the existing bounded read-only audit snapshot and never migrates or writes to the trajectory. Output contains only hashes and references to the capture, scene, Bonsai call, Decider decision, approved click and **downstream Decider verification**. These references are linkable metadata, not anonymization or signed proof. Do not add real outputs, captures, trajectories or local databases to the source package.

For the same selected run, `--dual-role` returns a separate `operator`/`supervisor` pair in one canonical report. The operator projection has `outcome_verified=true` only for its independently read-back SAVE action; it is **not** a reviewed gold label or trainer example. The supervisor projection is the unchanged S2 candidate with `supervisor_outcome_verified=false`. Both share one audited snapshot and source fingerprint, so a Bonsai scene cannot inherit the Decider result. Its contract and illustrative fixture are `schemas/vision_dual_role_candidate.schema.json` and `examples/vision_dual_role_candidate.json`.

```sh
.venv/bin/python -m aos.vision_skill_candidate \
  --database data/private/store.sqlite --run-id RUN_ID \
  --snapshot-sha256 HASH --dual-role
```

The canonical contract is `schemas/vision_skill_candidate.schema.json`; `examples/vision_skill_candidate.json` is an illustrative synthetic placeholder, not a model run. `supervisor_outcome_verified=false` is deliberate: the independent SAVE outcome verifies the downstream S1-driven action, **not** Bonsai's scene as a gold vision label. All review, profile, execution, collection, activation and training flags remain false. No migration or active deployment change is made.

The opt-in real test starts a separate owned Ubuntu/Chromium synthetic canvas with pinned Bonsai and Decider, approves one click, checks separate S2/S1 metadata and derives both the S2 candidate and dual-role report. Derivation checks the verifier identity and criterion, a single evidence reference to the separate `vision.verify` action, its recorded SAVE result and `vision.outcome` observation. Changed Bonsai request, scene observation, approval actor, verifier, evidence, readback or click arguments must fail closed. The trajectory still cannot prove the recorded screenshot bytes or network behavior from metadata alone. This does not validate a real site, a reusable visual skill, changed layouts or generalization; those remain W3/W4 acceptance gates.
