# OpenEvo ResearchClawBench native-path adapter

This package is the adapter-only prototype for
`sequential_task_reflector_evolution_v0`. It freezes the 17 Community tasks in
the protocol order, three independent attempts per task, and two completed-task
evolution cycles per task. The resulting plan contains 51 candidate runs, 34
reflector cycles, and 102 artifact-specific evolution requests.

Candidate execution is owned by OpenEvo Core: an opaque workspace handoff and
runtime-context binding enter a real `TaskRequest`, `AgentSpec(harness="codex")`
selects `CodexHarness`, and the managed subscription runtime executes
`/opt/codex/bin/codex` version 0.144.1 with transcript capture. This package has
no Codex subprocess, credential reader, candidate Docker runner, timeout loop,
or transcript parser.

Public `task_info.json` data declarations may name either a regular file or a
directory below the task's `data/` root. Directory declarations are expanded
to a deterministic regular-file inventory. Symlinks, special files, path
escapes, and any `target_study` path fail closed before workspace creation.

Reflector work is submitted as three independent native plan-bound jobs using
`agent_system_gepa_reflector`, `text_memory_expel_reflector`, and
`skill_bundle_reflector`. A verified OpenEvo evolution worker, not this adapter,
executes methods and persists registry artifacts. Adapter-side scanning is an
additional promotion/admission gate; it cannot create an OpenEvo revision.

The current OpenEvo commit does not reproducibly pin the evolution worker's
`codex_cli` binary to the candidate managed-runtime 0.144.1 binary, and it does
not expose a native post-run authority for attaching task-local hard-GT feedback
to the immutable completed-session dataset. Therefore model execution remains
fail-closed with `BLOCKED_BY_OPENEVO_NATIVE_CAPABILITY_GAP`.

No-model checks:

```bash
python -m pytest tests -q
python -m openevo_researchclawbench.cli contamination-audit --protocol /absolute/protocol.yaml
python -m openevo_researchclawbench.cli static-audit --protocol /absolute/protocol.yaml
python -m openevo_researchclawbench.cli readiness --protocol /absolute/protocol.yaml
```

This is a Community training prototype, not an official leaderboard, Pass@1,
or Pass@5 run. The official-40 gate disables evolution, reflector, teacher,
task-local overlays, feedback release, and cross-task state changes.
