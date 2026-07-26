# ChemBench supervised transfer current-state audit

Audit time: 2026-07-27 Asia/Shanghai
Repository: `/home/lhy-h/work/compare2`
Protocol: `chembench_supervised_transfer_v1`
Decision: `MECHANISM_MISMATCH_FOUND`

This report is content-free. It does not include questions, options, targets,
completions, feedback, memory text, auth material, or proxy values.

## Pause evidence

Before the pause, the relevant process tree contained:

- PID/PGID 15196: run-05 supervised-transfer runner.
- PID 19369, PGID 15196: supervised reflector launcher.
- PID 19370 and PID 19371: the two Bubblewrap layers under that launcher.
- During TERM convergence, PID/PGID 19503 and child PID 19514 were observed as
  one task-model Codex invocation. They exited before the follow-up TERM could
  be delivered.

PGID 15196 received TERM. No KILL was required. A post-pause process scan found
zero supervised-transfer runners, Codex task calls, reflectors, Probe/Train/Test
workers, or benchmark monitors. The unified runner session exited with status 1.
Both run directories were preserved.

## Repository identity

- Branch: `chembench-supervised-transfer-v1`.
- HEAD: `a9642717fa878bd039630885193c969eda227834`.
- run-04 recorded source commit: the same HEAD.
- run-05 recorded source commit: the same HEAD.
- No source commit or tracked benchmark source changed between run-04 and the
  pause of run-05.
- `git diff -- src/openevo` and the staged equivalent are empty.
- Worktree status before this report contained only untracked `data/`,
  `results/`, and `state/` runtime trees.
- The current and old repositories have different `.git` inode identities.
- The old repository remains at
  `d795ee3d6d654ab175ab8b3c9929c4db318180c5`; its `src/openevo` diff is empty.
  Its pre-existing untracked `results/` tree was not modified by this audit.

## Frozen identities

- Dataset revision: `f8ad41a980170f4c5d0cc97e57722d06887c8f53`.
- Dataset combined SHA-256:
  `cb6c17c54d4c0cf103b38f12e9ce05663515b05bfdc342d83e3245d39f3a4b3a`.
- Dataset manifest SHA-256:
  `76593f7c1cb32ab9a477412d070434798b1e421d98f133a1914d995fe75cb1c4`.
- Split summary SHA-256:
  `50968e95944e982b2513fdd832ba9b88f405ba987b53d452e7bd6975010257cf`.
- Split isolation receipt SHA-256:
  `a3a315e6a3a13d1ba070a215092cb7399de797de4fc99c4b5208c0fec8a5baf5`.
- Split-generation code SHA-256:
  `69de1bc3f148e15b2f75db0182b127ff9af5a2034d9bee462ef2182b49d76301`.
- Supervised config file SHA-256:
  `441f47bb30c80727b7ac7218d4920c5821ccef0a5793d0b11c9f613cbe9eb13b`.
- Task executor policy SHA-256:
  `bcd6a464d352186e26b7309c91d87a32ac8070631a1e7681b8cdbc533f46d377`.
- Verified Core registry digest:
  `8cf10d038b27674d5d823be0e93fec6ddb978b6a8ace725f0f60cafc3dbfaf34`.
- Train/Probe/Primary Test/Recovery Test/Reserve counts:
  450/90/450/450/2569.
- Every private Train/Probe/Test manifest remains mode `0600`.
- Pairwise split intersections remain zero. Probe and both Test manifests have
  zero historical actual exposure.
- The immutable V1 blocked receipt remains SHA-256
  `43e64dce6a4c357b5811042140b7a908820fa13e9fe0aefc0f9eab4da11ab18e`.

The non-paid `verify` command recomputed these identities byte-for-byte with
`model_calls_made=0` and returned PASS.

## Access and isolation evidence

Across runs 01 through 05:

- Primary Test task-model attempts: 0.
- Recovery Test task-model attempts: 0.
- Test evolution jobs/reflector events: 0.
- Probe reflector events: 0.
- Every `REFLECTOR_SUPERVISED` UID intersects Train and does not intersect
  Probe, either Test manifest, or Reserve.
- Probe items were evaluated only in Probe smoke/checkpoint-0 sessions; their
  answers were not projected into a supervised packet.

## Failed run-04

- Run ID: `st-v1-run-20260726-04`.
- Status/stage: `FAIL_CLOSED` / `CONTROL_TRAIN`.
- Total completed sessions: 360.
- Pre-formal sessions: 253.
- Formal Control sessions: 107/1350.
- Complete formal Control tasks: 35; the next task had rounds 0 and 1 only.
- Failure code/stage: `EXECUTOR_MODEL_TRANSPORT_FAILED` / `MODEL_TRANSPORT`.
- Failed invocation completion observed: false.
- Private events contain 361 attempts and 360 private evaluations, confirming
  that the failed item produced no accepted completion or evaluation.
- Cleanup complete: true; auth removed: true; residual roots: 0; tool events: 0.
- Executor-level diagnostic metadata classified a replacement attempt as
  infrastructure-safe, but the benchmark run itself is terminal and must not
  be resumed. Run-level resume permission is therefore false.
- Its 35 complete Control tasks and partial next task are excluded from every
  future formal result; no cross-run continuation is permitted.

## Paused run-05

- Run ID: `st-v1-run-20260727-05`.
- It has distinct result/state directories and a fresh run ID.
- Last persisted stage: `ONLINE_CANARY`.
- Completed sessions/reflector jobs/artifacts: 20/14/14.
- Private attempts: 21; private evaluations: 20. The paused invocation did not
  enter the public completed-event stream.
- Current persisted status is still `INITIALIZED`, because external TERM does
  not produce an immutable paused/fail-closed receipt. The run is nevertheless
  preserved and is administratively non-resumable.
- Primary and recovery Test attempts remain zero.
- An exclusive administrative pause receipt was added without rewriting
  `run_state.json`. Its SHA-256 is
  `e215d7c614b206f3497c744420cc5397ee85770fe7e8d22213ff9b2686853a29`;
  it records `PAUSED_NONRESUMABLE`, zero active model calls, and binds the
  pre-existing public/private event-stream and run-state digests.

## Answers to required audit questions

1. HEAD is the expected source commit: yes.
2. Source changed after run-04: no, before creation of these audit notes.
3. Split remained frozen: yes.
4. Test was never model-accessed: yes.
5. Probe remained no-evolution: yes.
6. run-04 is strict fail-closed: yes.
7. The failed run-04 item had no completion: yes.
8. run-04 formal Control output is excluded from future formal results: yes by
   policy; the current runner also has no resume path, but this must be made an
   explicit formal-run gate.
9. run-05 is a fresh run ID and directory: yes.
10. run-05 entered online canary because `run()` unconditionally executes all
    preflight stages for every fresh run, not because a receipt identity check
    found an invalid canary.
11. There are no canonical smoke/canary reuse receipts binding source, config,
    split, model, executor policy, Codex version, and Core registry together.
    The underlying event and success evidence is strong, but is not a reusable
    stage authority under the requested rules.
12. run-04 checkpoint-0 has an internally consistent memory checkpoint receipt
    and 180 paired Probe completions, but the checkpoint receipt does not bind
    the complete reusable execution identity. It is not portable as-is.
13. Correct action: fix orchestration and receipt authority, then rerun only the
    newly invalidated minimum preflight suite and begin a separate formal run at
    Control task 0.
14. The existing full-run state machine does not skip Control Train.
15. It does not enter formal Online Train before Control Train.
16. It does repeat checkpoint-0 Probe for every fresh run and lacks a global
    cross-run primary-Test use ledger. Both are protocol risks.
17. A fresh existing full run would start Control task 0 after its redundant
    preflights; the corrected formal-only entry point must make that explicit.
18. Current code does not load run-04 Control checkpoints, so accidental splice
    is absent; an explicit no-cross-run formal gate is still required.
19. Observed Train memory contamination of Probe/Test is absent. The packet,
    bridge, and public lineage constrain reflector inputs to Train.
20. Core Train/Probe/Test mechanics substantially match the design, but stage
    reuse, pause terminal evidence, and global Test single-use enforcement do
    not. Overall conformance is therefore not established.
