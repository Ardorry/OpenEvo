# Source and licensing notice

The six adapter modules are copied from the local OpenEvo experimental workspace under its Apache-2.0 license. This is a file-hashed research snapshot, not a claim that the files belong to an upstream published OpenEvo release. The full OpenEvo product and unrelated workspace changes are not included.

Harbor 0.21.0 is an external dependency, not vendored. Codex and uv binaries are external runtime assets, not included in this source repository. Their upstream licensing and notices apply when distributing those binaries. Benchmark task definitions, tests and solutions are not vendored; obtain the pinned dataset from its publisher.

The frozen AGENTS files contain task-local research observations generated from earlier trajectories and grading feedback. Their provenance is disclosed in artifacts/manifest.json and docs/METHOD.md. They are not represented as generic instructions trained without evaluation-task feedback.

The portable launcher, delivery documentation and result tables were assembled for this handoff. The original executed launcher is retained in the private evidence bundle; source hashes identify the adapter modules shared by both.
