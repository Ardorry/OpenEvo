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
executes methods and persists registry artifacts. The Life_005 engineering
validation proved the managed GPT-5.5 runtime, current-task GT attachment,
native artifact registry, and atomic successor Project Head seed. It stopped
before the evolved Candidate and Judge.

The original Life_005 successor exhausted its pre-job transition capacity. The
package-local `recover-native-evolution` maintainer command now freezes that
immutable Supervisor evidence and calls Core's existing source-resolution and
successor-recovery APIs. It does not implement a reflector, artifact registry,
runtime, provider, or fallback. Completed target operations are reconciled by
GET before any POST, so a CLI restart does not reauthorize an already durable
paid operation.

No-model checks:

See `docs/runner-cli-demo.md` for the exact repository-managed protocol and
meeting commands. These commands are benchmark maintainer automation, not a
third OpenEvo user product or a replacement for the Daemon/Desktop contract.

This is a Community training prototype, not an official leaderboard, Pass@1,
or Pass@5 run. The official-40 gate disables evolution, reflector, teacher,
task-local overlays, feedback release, and cross-task state changes.
