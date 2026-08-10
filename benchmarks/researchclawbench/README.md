# OpenEvo ResearchClawBench native-path adapter

This package is the adapter-only prototype for
`sequential_task_reflector_evolution_v0`. The active protocol treats every one
of the 17 Community tasks as an independent experiment: two fresh Candidate
passes, one current-task GT-supervised native evolution cycle, two independent
Judge operations, one paired result, and a complete active-state reset. The
plan contains 34 Candidate runs, 17 evolution cycles, and 51 artifact jobs.

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
native artifact registry, and atomic successor Project Head seed. The per-item
runner extends that native route with a fresh evolved Candidate, paired Judge
result, and task-local reset.

The package-local `recover-native-evolution` command remains a debug-only tool
for immutable historical evidence. The production per-item command does not
call or depend on it.

The R4.5 demo artifact boundary keeps only operational integrity as dispatch
authority: the native triple must be registered, readable, type-correct,
task-owned, and backed by valid lineage/content-admission receipts. Baseline
method, parameter, filename, output-name, generic-advice, token/stem, and
per-path retention measurements remain in the artifact-quality report as
diagnostics. Their result cannot terminate the lifecycle or block the fresh
evolved Candidate. Baseline equivalence is likewise diagnostic only. These
metrics observe evolution behavior; they do not predict Judge score.

No-model checks:

See `docs/runner-cli-demo.md` for the exact repository-managed protocol and
meeting commands. These commands are benchmark maintainer automation, not a
third OpenEvo user product or a replacement for the Daemon/Desktop contract.

This branch prepares only the Community per-item reset experiment. It does not
accumulate artifacts across tasks or prepare a frozen-composite/Official flow.
