# W4 foundation — blocked learning-candidate gap review

`aos.learning_candidate` summarizes the existing audited `learning_event_v2` projection for one explicit run into separate S1 and S2 **metadata-only gap rows**. It never materializes a dataset example, label, skill, image, prompt or model input. Every row is `blocked`, `label_ready=false` and `training_ready=false`, even when S1 has an independently verified action or S2 has capture/scene and downstream Decider verification references. These references are evidence of a bounded synthetic task, not a Bonsai gold label or permission to train.

```bash
.venv/bin/python -m aos.learning_candidate --database /path/to/private-trajectory.sqlite --run-id RUN_ID
```

The CLI inherits the bounded, read-only SQLite audit, pinned model-call checks and deterministic event IDs from `aos.learning_events`. It prints no raw trajectory content and does not write to the source. The canonical report schema is `schemas/learning_candidate_report.schema.json`; `examples/learning_candidate_report.json` is explicitly synthetic. S1 and S2 counts describe recorded calls only. An absent S2 call stays absent; it is not fabricated to fill a dataset.

The summary helper also rejects malformed source review envelopes, authority flags, count mismatches and duplicate event IDs before reporting gaps; it cannot turn a caller-supplied altered review into an apparently clean candidate inventory.

This is a gap inventory, **not** an incremental collector, a review receipt, a skill candidate, a canonical fine-tune dataset or a trainer readiness pass. It does not resolve collection rights, redaction, site/tenant/role scope, independent gold labeling, group split, held-out evaluation, training authorization or promotion. W4 real data work remains blocked without an authorized target application and reviewed data rights. Focused tests: `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p test_learning_candidate.py -v`.
