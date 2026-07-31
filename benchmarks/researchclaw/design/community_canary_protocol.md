# Community Canary Protocol

Status: `DESIGN_ONLY_NOT_EXECUTED`

The canary uses only frozen community Dev tasks. Selection used task ID, domain, public `task_info.json` data metadata, and candidate-visible byte size. No checklist, target paper/image, score, or leaderboard result was used.

## Preselected tasks

| Role | Task | Domain | Candidate-visible size | Public data format | Selection basis |
| --- | --- | --- | ---: | --- | --- |
| Small | `Life_005` | Life | 10,326,184 bytes (9.848 MiB) | 7 CSV paths | Small public footprint and ordinary tabular I/O; first end-to-end gate |
| Medium | `Neuroscience_004` | Neuroscience | 33,618,401 bytes (32.061 MiB) | 3 NPY paths | Medium footprint, binary-array loading, distinct single-task domain |
| Cross-domain stress | `Earth_004` | Earth | 199,645,415 bytes (190.397 MiB) | 64 NetCDF paths | Third domain, multi-file scientific format, largest candidate-visible community input |

All three are in `configs/community_split.yaml.dev_tasks`. Validation tasks are deliberately excluded so canary debugging cannot train on the selection set.

“Cross-domain” here means a third, materially different scientific domain and file modality used to test adapter generality. It does not claim that the task's scientific content itself is interdisciplinary.

## Common prerequisites

Before any canary stage:

- OpenEvo and ResearchClawBench commits match the frozen source identities and tracked source is clean;
- community split and dataset revision hashes match;
- no `target_study` path is read or mounted by candidate-side code;
- the candidate/evaluator isolation smoke probes pass;
- runtime image/rootfs, dependency lock, environment, tools, network, model, and budget fields are concrete—no pending placeholder for a field needed by that stage;
- every run uses a new run ID and task-private namespaces/caches;
- no GPU is exposed.

The task progression order is fixed: `Life_005`, then `Neuroscience_004`, then `Earth_004`. A later task cannot be substituted after seeing a result.

## Stage 1: Workspace construction smoke test

Purpose: validate source selection, official prompt rendering, copied public inputs, standard directories, mount policy, and candidate-negative probes without launching any model or agent.

Operations:

1. Construct a fresh workspace for each of the three tasks.
2. Verify `INSTRUCTIONS.md`, `data/`, `related_work/`, `code/`, `outputs/`, `report/`, and `report/images/`.
3. Compare copied public input hashes with source inventory.
4. Build but do not execute the candidate mount specification.
5. Run only a harmless isolation probe in the empty runtime that checks read/write policy and forbidden-path non-visibility. The probe is not given the scientific instruction and cannot invoke a model.
6. Destroy the probe namespace and retain receipts.

Pass criteria:

- all expected public input paths exist and match hashes;
- official prompt bytes are deterministic across two construction attempts;
- `data/`, `related_work/`, and instruction are read-only in the probe;
- `code/`, `outputs/`, `report/`, and private `/tmp` are writable;
- benchmark root, `target_study`, host home, `/mnt`, sibling workspaces, Docker socket, Codex auth, and judge variables are absent;
- no model, scorer, judge, evolution, or network call occurs.

Limits:

- 1 CPU;
- 2 GiB memory;
- 5 minutes per task;
- no network;
- no GPU;
- one sequential task at a time.

Failure criteria:

- any hash/path/mount/environment mismatch;
- any forbidden path visible or read-only input writable;
- any unexpected source modification;
- probe timeout or ambiguous teardown.

Retry: one fresh construction retry is allowed only for a deterministic, recorded filesystem race before any candidate/model call. A second failure stops the canary. Do not repair a published workspace in place.

Gate: all three Stage 1 workspaces must pass before Stage 2.

## Stage 2: Fixed baseline agent run

Purpose: prove the full candidate lifecycle and artifact contract with a fixed, non-evolving baseline. The baseline has no OpenEvo `agent_system` artifact and no cross-task memory.

Execution order and limits:

| Task | Wall timeout | CPU | Memory | Writable storage | Network |
| --- | ---: | ---: | ---: | ---: | --- |
| `Life_005` | 2 hours | 2 | 8 GiB | 10 GiB | disabled |
| `Neuroscience_004` | 4 hours | 4 | 16 GiB | 20 GiB | disabled |
| `Earth_004` | 6 hours | 4 | 24 GiB | 30 GiB | disabled |

The model, Codex CLI, reasoning level, token/cost caps, tools, runtime image, and dependency lock must come from one concrete frozen canary manifest. Package installation during the run is forbidden; dependencies must be prebuilt. If the chosen baseline cannot operate without network, stop and revise the canary protocol before execution rather than enabling network after observing task behavior.

Pass criteria per task:

- isolation readiness receipt passes before task context is installed;
- candidate is the only owned process tree and terminates within limits;
- stdout/stderr and exact exit code are captured;
- candidate namespace is gone before validation;
- artifact validator returns `PASS`;
- complete standard run folder and hash receipt are present;
- no evaluator/hidden data enters the candidate or transcript;
- no state is shared with the next task.

The first Stage 2 run is `Life_005`. Only after it passes may `Neuroscience_004` run; only after that passes may `Earth_004` run. This bounds cost and makes each gate interpretable.

Scoring is disabled for the initial baseline canary because adapter/artifact correctness is the gate. A separate community scorer check may be added only after evaluator isolation is proven and before results are observed; it must use the Dev feedback filter and cannot change the selected canary tasks or runtime.

Failure criteria:

- timeout, nonzero/ambiguous exit, invalid transcript, validator failure, namespace leak, source mutation, feedback-policy violation, or resource-cap breach;
- report exists but process/validator status disagrees;
- any hidden/evaluator path or credential exposure.

Retry: no in-place resume. A pre-model isolation/construction failure may use one fresh run ID. Once a model request occurs, the outcome is retained and not retried as the same canary attempt. Any proposed model-run retry requires a predeclared, score-blind infrastructure category and protocol version update.

Gate: all three baseline runs must pass artifact validation before OpenEvo Dev or Stage 3 is considered.

## Stage 3: Frozen OpenEvo artifact treatment run

Current status: `BLOCKED_BY_MISSING_FROZEN_ARTIFACT`, intentionally not executable in Phase 0.

Future purpose: repeat Stage 2 with one final `agent_system` artifact produced only from community Dev and frozen before treatment canary launch.

Additional prerequisites:

- artifact ID, type, content SHA-256, lineage, compatibility, and source Dev tasks are fixed;
- community Validation has selected this artifact without releasing Validation detail to the reflector;
- paired baseline/treatment manifests compare equal after removing only `condition`, `artifact_id`, and `artifact_sha256`;
- treatment does not add memory, skills, tools, network, packages, budgets, retries, or cross-task state.

Pass criteria:

- every Stage 2 criterion passes;
- the exact artifact is mounted read-only and its pre/post hash matches;
- transcript metadata identifies pure-text capture and no token-level metric claim;
- no score is used to update the artifact;
- all three treatment runs are retained, regardless of whether their scientific scores improve.

Failure and retry policy is identical to Stage 2. A worse result is not an infrastructure failure and does not permit a rerun or artifact edit.

## Logs and artifacts

Future canary outputs must be kept under a protocol-scoped, project-local run root, for example:

```text
runs/community_canary/<protocol_id>/
├── manifests/
├── stage1/<task_id>/<run_id>/
├── stage2/<task_id>/<run_id>/
├── stage3/<task_id>/<run_id>/
└── logs/<task_id>/<run_id>/
```

Each run retains source/prompt/request/mount/network/artifact hashes, stdout/stderr, process and namespace receipts, validator receipt, and complete run folder. No log contains secrets or hidden content.

## Stop conditions

Stop the complete canary sequence when:

- a source, split, artifact, image, dependency, or protocol pin drifts;
- isolation cannot be proven;
- the candidate sees a forbidden path/secret;
- a task requires an unapproved resource/network change;
- two attempts hit the same pre-model infrastructure failure;
- a model-run failure occurs without a predeclared retry category;
- any operation would modify OpenEvo Core or ResearchClawBench tracked source;
- a pending maintainer decision is required for the next phase.

Passing Stage 3 would authorize only the next community phase in `design/implementation_plan.md`; it would not authorize the official 40 tasks.
