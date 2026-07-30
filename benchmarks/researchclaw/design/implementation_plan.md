# ResearchClawBench × OpenEvo Implementation Plan

Status: `PHASE_0_DESIGN_FREEZE`

No phase transition is automatic. Every phase produces immutable receipts and must meet its acceptance gate before the next begins. “Rollback” means abandoning a new namespace and selecting the last accepted manifest; it never means rewriting or deleting completed evidence.

## Phase 0: Static design and protocol freeze

**State:** completed by this design set, subject to final parse/hash verification.

- **Inputs:** pinned OpenEvo/ResearchClawBench source, existing reports, community dataset manifests, public community task metadata.
- **Outputs:** fixed 11/6 split, split manifest, architecture/isolation/feedback/validator/canary designs, feedback and protocol schemas, example frozen manifest, maintainer questions/status, this phased plan.
- **Modification scope:** project-root `design/`, `configs/`, `schemas/`, and necessary `manifests/` only.
- **Acceptance:** all files parse where applicable; split has no overlap, covers exactly 17 community tasks, excludes all 40 official tasks; source repositories retain their expected Git states; every new file has SHA-256.
- **Stop conditions:** unexpected tracked source change; hidden target/checklist/score used for design or split; malformed schema/config; write outside allowed directories.
- **Model calls:** no.
- **Scorer calls:** no.
- **Evolution:** no.
- **Estimated resources:** minutes of low-CPU static analysis; negligible additional storage.
- **Rollback:** create a new versioned design/split only before any downstream result exists. Once accepted, never edit the split in response to scores; supersede by a separately approved protocol rather than overwrite.

## Phase 1: Minimal Adapter Skeleton

- **Inputs:** accepted Phase 0 documents and exact source commits.
- **Outputs:** independently installable `benchmarks/researchclawbench` package skeleton; closed models; task loader; official workspace constructor; artifact validator; package-boundary and static tests.
- **Modification scope:** new benchmark package and its tests/docs only. No `src/openevo/**`, `desktop/**`, Terminal Bench, or ResearchClawBench tracked-source change.
- **Acceptance:** imports installed OpenEvo contracts without vendoring; can list frozen task IDs and construct/validate synthetic empty workspaces; no task execution entry can launch a model; validator unit corpus covers safe/unsafe paths, PNGs, placeholders, traceability, and status consistency.
- **Stop conditions:** implementation requires a Core contract change; hidden paths enter a candidate-facing module; source pins drift; package imports evaluator at candidate startup; tests would require a real benchmark/model/scorer.
- **Model calls:** no.
- **Scorer calls:** no.
- **Evolution:** no.
- **Estimated resources:** low CPU, under 1 GiB temporary test data, no GPU, no network except already authorized lightweight dependency resolution if separately approved.
- **Rollback:** discard only the new unaccepted package namespace or select its previous tagged skeleton; never touch Core/RCB source or completed run evidence.

## Phase 2: Workspace Smoke Test

- **Inputs:** Phase 1 skeleton, pinned runtime rootfs/image, public community tasks, fixed canary list.
- **Outputs:** construction and isolation receipts for `Life_005`, `Neuroscience_004`, and `Earth_004`; deterministic prompt/input hashes; negative-path and mount-policy evidence.
- **Modification scope:** project-local adapter test/run directories only; no source changes except focused fixes in the benchmark package under a new protocol attempt.
- **Acceptance:** `design/community_canary_protocol.md` Stage 1 passes for all three tasks; read-only inputs and writable outputs are enforced; forbidden paths/environment are unavailable; namespaces tear down cleanly.
- **Stop conditions:** model/agent would be invoked; isolation falls back to `cwd`; input is writable; hidden/host path visible; bwrap/Docker policy unavailable; unexpected resource contention.
- **Model calls:** no.
- **Scorer calls:** no.
- **Evolution:** no.
- **Estimated resources:** 1 CPU, 2 GiB memory, at most 5 minutes/task, sequential, no GPU/network.
- **Rollback:** keep failed receipt, fix only the adapter package, and retry with a fresh run ID. Never reuse or mutate the failed workspace.

## Phase 3: Community single-task Baseline Canary

- **Inputs:** accepted Phase 2, concrete frozen canary manifest, fixed baseline agent, `Life_005`.
- **Outputs:** one complete standard baseline run folder, transcript/process/isolation receipts, artifact inventory, validator result. Community scoring remains disabled for the first run.
- **Modification scope:** project-local canary run/log directories; no benchmark/Core tracked source changes during the run.
- **Acceptance:** small-task Stage 2 criteria pass: exact source/request pins, isolated candidate, bounded resources, terminal transcript/exit, validator `PASS`, namespace gone, complete hashes, zero cross-task state.
- **Stop conditions:** any model/config/budget field pending; isolation/credential proof fails; timeout/nonzero/invalid output; source drift; candidate sees hidden/host state; a retry would depend on result/score.
- **Model calls:** yes, exactly one fixed baseline canary attempt after explicit phase authorization.
- **Scorer calls:** no for the initial gate; an optional later community-only scorer smoke requires a separate frozen amendment and evaluator-isolation acceptance.
- **Evolution:** no.
- **Estimated resources:** up to 2 CPU, 8 GiB memory, 10 GiB writable storage, 2-hour wall limit, no GPU, network disabled.
- **Rollback:** retain failure, use a fresh run ID after an allowed source-independent fix. Never resume an agent-started run.

## Phase 4: Community OpenEvo Dev

- **Inputs:** accepted single-task baseline; remaining preselected medium/cross-domain baseline gates; all 11 community Dev tasks; fixed feedback schema; one or more declared OpenEvo methods targeting `agent_system`.
- **Outputs:** baseline expansion receipts, Dev-only trajectories/datasets, versioned candidate `agent_system` artifacts with lineage, operational/score feedback envelopes, cost/runtime ledger.
- **Modification scope:** standalone adapter package fixes made before a run namespace; project-local Dev/evolution stores and artifacts. Core and ResearchClawBench tracked source remain frozen.
- **Acceptance:** before evolution, `Neuroscience_004` and `Earth_004` baseline canaries pass the predeclared Stage 2 gates. During evolution, every input task is in Dev; every feedback record validates as `community_dev_feedback`; each artifact applies only to a later fresh task/session; budgets and candidate set/round rules are predeclared; no Validation/official data enters an evolution dataset.
- **Stop conditions:** task outside Dev; raw/per-item judge content reaches reflector; artifact lineage ambiguous; cross-task state exceeds declared Dev policy; repeated infrastructure failure; budget cap reached; Core change required.
- **Model calls:** yes, only fixed candidate and reflector calls declared for community Dev.
- **Scorer calls:** yes, community Dev only, after each candidate run is sealed and evaluator-isolated.
- **Evolution:** yes, community Dev only.
- **Estimated resources:** moderate sequential CPU/I/O; no GPU by default; per-task and total token/time/cost caps must be concrete before launch; concurrency defaults to 1.
- **Rollback:** stop at the last immutable accepted artifact; do not rewrite the evolution DB/artifact. A code/protocol fix starts a new Dev run namespace and cannot silently combine incompatible evidence.

## Phase 5: Community Validation and Artifact Freeze

- **Inputs:** predeclared finite candidate artifact set from Phase 4; all 6 frozen Validation tasks; selection rule; identical baseline/treatment runtime.
- **Outputs:** sealed per-candidate Validation runs, one aggregate selection-only summary per candidate, exactly one final frozen artifact and content hash, finalized protocol draft.
- **Modification scope:** project-local Validation run/evaluator folders and artifact-freeze receipts. No adapter changes after the first Validation result is visible.
- **Acceptance:** no Validation feedback reaches reflector; summaries validate as `community_validation_summary` with `released_to_reflector: false`; predeclared tie-break selects exactly one artifact; artifact is copied/materialized read-only, hashed, and never updated; baseline/treatment manifests differ only in allowed artifact fields.
- **Stop conditions:** candidate set/rule changes after results; per-task Validation detail used for mutation; artifact rewritten; environment/model/budget drift; any task outside Validation; scorer/evaluator boundary failure.
- **Model calls:** yes, fixed Validation candidate runs only.
- **Scorer calls:** yes, Validation only, after each run is sealed.
- **Evolution:** no; selection is not evolution.
- **Estimated resources:** 6 tasks × predeclared candidates, sequential by default; concrete total budget required; no GPU unless separately frozen and justified.
- **Rollback:** reject the entire Validation attempt and start a new protocol version from pre-result evidence only. Never tune the artifact from failed/low Validation scores.

## Phase 6: Maintainer protocol confirmation

- **Inputs:** frozen artifact and protocol draft; English questions in `design/maintainer_protocol_questions.md`; complete community evidence summary without hidden content.
- **Outputs:** sent correspondence and a durable maintainer response reference; updated confirmation-status file in a new authorized task; finalized manifest with no execution-critical pending fields.
- **Modification scope:** configuration/status and correspondence evidence only. No task execution/source changes.
- **Acceptance:** explicit answers cover external submission, offline evolution, single-task iteration, cross-task state, judge feedback, Pass@5 independence, same agent/version/config, network/tools/budgets, scorer/judge, run-folder/trajectory requirements, and retry policy. Required status fields remain false unless the response clearly authorizes them.
- **Stop conditions:** ambiguous/no response; required field pending; reply conflicts with public code/rules; proposed change would invalidate community comparison; user has not authorized sending.
- **Model calls:** no candidate or reflector calls.
- **Scorer calls:** no.
- **Evolution:** no.
- **Estimated resources:** negligible compute/storage; human review time.
- **Rollback:** status remains `NOT_SENT` or becomes an explicit non-authorized state. Do not infer permission.

## Phase 7: Official 40-task formal run

- **Inputs:** maintainer-confirmed manifest; one immutable artifact; exact official 40 task order; clean pinned source; validated isolation/runtime/dependencies; approved baseline/treatment/Pass@K policy.
- **Outputs:** complete baseline/treatment run folders, process/isolation/validator/transcript manifests, deferred official scores, paired comparison, submission package.
- **Modification scope:** fresh formal run/evaluator/submission directories only. Adapter, Core, ResearchClawBench source, artifact, split, configuration, and dependencies are immutable.
- **Acceptance:** every candidate task uses fresh state and the same artifact/config; no official feedback is released; all candidate runs are sealed before scorer invocation; scorer/judge identity matches confirmation; run folders pass validator/hash checks; paired manifests differ only in `agent_system`; complete submission package passes local audit.
- **Stop conditions:** any confirmation/status false; source/artifact/config drift; isolation or credential failure; hidden data exposure; result-driven retry/change; unapproved network/Pass@5/cross-task behavior; budget exhaustion; scorer started early; unexpected tracked-source modification.
- **Model calls:** yes, only frozen candidate configuration and approved number of attempts.
- **Scorer calls:** yes, only after all official candidate runs are sealed and only if `official_judge_allowed: true`.
- **Evolution:** no—before, during, and after formal runs using official results.
- **Estimated resources:** high model cost and substantial wall time/storage; exact totals must be computed and approved from the final task/attempt count and budgets. Concurrency defaults to 1 unless explicitly confirmed.
- **Rollback:** none for scientific outcomes. Infrastructure failures follow only the preconfirmed fresh-run retry rule. Otherwise preserve evidence, stop, and seek maintainer/user direction; never repair or rerun based on scores.

## Global gates

The next phase is blocked if any of these is false:

- current phase acceptance receipt exists and hashes match;
- both tracked source trees match their pins;
- all previous run folders are immutable evidence;
- requested operation is allowed by the phase's model/scorer/evolution row;
- no hidden data has crossed a boundary;
- resource budget and isolation primitives are available without affecting other projects/processes;
- no required maintainer confirmation is pending.
