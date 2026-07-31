# ResearchClawBench × OpenEvo Adapter Architecture Freeze

Status: `DESIGN_FROZEN_FOR_PHASE_0`

Basis: OpenEvo `b7baa85b6699ed797c61a3e2166fa0b5ad2a0284`; ResearchClawBench `53ee262a265d47e47e94cbb4c2249a0478dfdbfc`; Hugging Face dataset `db5d2f4d9494e83c6f0ea0839b00f2b9c1ab66a4`.

## 1. Design goals

The adapter must preserve ResearchClawBench's official task prompt, workspace deliverables, scorer, and run-folder contract while using OpenEvo only for candidate execution, transcript capture, and pre-test evolution of a typed `agent_system` artifact. It must make hidden evaluator inputs structurally unavailable to the candidate, keep the official 40 tasks frozen, and produce complete, hash-addressed run folders suitable for later maintainer review.

The design is fail-closed. A missing pin, ambiguous workspace, unavailable isolation primitive, artifact mismatch, invalid report, or scorer/candidate boundary violation ends that run without a score-derived retry.

## 2. Adapter-only boundary

The future implementation belongs in a standalone benchmark package, following the Terminal Bench precedent:

```text
OpenEvo/benchmarks/researchclawbench/
├── pyproject.toml
├── README.md
├── src/openevo_researchclawbench/
│   ├── __init__.py
│   ├── models.py
│   ├── task_loader.py
│   ├── workspace.py
│   ├── request_builder.py
│   ├── candidate_supervisor.py
│   ├── agent_entrypoint.py
│   ├── artifact_validator.py
│   ├── feedback_filter.py
│   ├── official_scorer.py
│   ├── result_writer.py
│   └── submission_packager.py
└── tests/
```

The package imports installed OpenEvo contracts. OpenEvo Core and Desktop must never import the benchmark package. The initial local implementation may be developed as an independently installable checkout, but the module and import boundary above is canonical.

The adapter does not implement an evolution algorithm, a generic scheduler, a replacement scorer, or a second Codex harness. It composes OpenEvo's existing `TaskRequest`, `AgentSpec`, Codex harness, transcript builder, artifact contracts, and runtime interfaces.

## 3. Paths that are prohibited from modification

The adapter must not change any of the following OpenEvo paths:

- `OpenEvo/src/openevo/**`
- `OpenEvo/desktop/**`
- `OpenEvo/docs/maintainer/productization/spec.md`
- existing OpenEvo runtime, gateway, rollout, trajectory, evolution, store, registry, or artifact contracts
- `OpenEvo/benchmarks/terminal_bench/**`

It must not change ResearchClawBench tracked task or evaluator code, including:

- `ResearchClawBench/evaluation/**`
- `ResearchClawBench/tasks/**` tracked files
- `ResearchClawBench/README.md` or `CONTRIBUTING.md`

An eventual upstream agent-preset PR to `evaluation/agents.json` is a separate deliverable and is not required for the adapter to execute locally.

## 4. Source-derived contracts and limitations

- `TaskRequest` is a closed Pydantic request with task identity, instruction, timeout, runtime, agent, builder, optional evaluator, metadata, and optional workspace/runtime-context bindings (`OpenEvo/src/openevo/rollout/models.py:67`). The adapter uses `num_samples: 1` and canonicalizes every request before admission.
- `AgentSpec` requires exactly one of `harness` or `import_path` (`OpenEvo/src/openevo/harness/models.py:34`). The first implementation uses `harness: codex`; it does not create another Codex launcher.
- Codex runs non-interactively with JSON output and an ephemeral session; the current harness uses `codex exec`, `--json`, `--ephemeral`, explicit model selection, `bash -o pipefail`, and a transcript file (`OpenEvo/src/openevo/harness/presets/codex.py:278`). Subscription runs additionally require the existing credential-isolation readiness receipt.
- Pure-text capture is the correct mode for subscription Codex. The builder produces a tokenless trajectory with `capture_mode: transcript` and `token_level_metrics_available: false` (`OpenEvo/src/openevo/trajectory/builder/agent_transcript.py`). No token-level RL claim is permitted.
- OpenEvo artifacts are registered through `ArtifactRegisterRequest` with type, URI, manifest, lineage, compatibility, scores, tags, and promotion status (`OpenEvo/src/openevo/evolution/models.py:130`). The treatment artifact type is `agent_system`.
- OpenEvo's registry `freeze()` freezes a verified descriptor graph, not a benchmark result. The immutable revision/admission models bind project/workspace/context/execution snapshots (`OpenEvo/src/openevo/evolution/revisions.py:454`), but the repository explicitly states that production issuance and end-to-end cross-session run-owner activation are unfinished. The adapter therefore maintains its own frozen protocol manifest and must not claim that Core's internal ledger already protects the formal run.
- ResearchClawBench `TaskRunner.setup_workspace()` copies only `data/` and `related_work/`, creates `code/`, `outputs/`, `report/`, and `report/images/`, renders the official template, and initializes `_meta.json` (`ResearchClawBench/evaluation/run_task.py:32`).
- ResearchClawBench `TaskRunner.run()` is not a security boundary: it inherits the entire host environment, relies on `cwd`, and executes a shell command with `shell=True` (`ResearchClawBench/evaluation/run_task.py:93`). It must not be used as the formal candidate launcher.
- The official scorer reads hidden checklist and target images and writes `_score.json` (`ResearchClawBench/evaluation/score.py:183`). It belongs only to the evaluator process after candidate termination.

## 5. Module responsibilities

### `models.py`

Closed, versioned data models for `TaskSnapshot`, `WorkspaceReceipt`, `CandidateRunReceipt`, `ValidationReceipt`, `FeedbackEnvelope`, `FrozenArtifactRef`, and `SubmissionManifest`. Unknown fields are rejected. Secret values and hidden evaluator content are not valid fields.

### `task_loader.py`

Privileged supervisor-only loader. It accepts an exact task ID from the frozen split, verifies the task directory and `task_info.json`, rejects traversal and symlinks, records file identities/hashes, and exposes only public task metadata to the workspace builder. It never exposes `target_study` to candidate-facing code.

### `workspace.py`

Constructs a new opaque run ID and standard run folder. It invokes only the official setup semantics, preferably by creating a `TaskRunner`, overriding its workspace paths as the official CLI does, and calling `setup_workspace()`—never `TaskRunner.run()`. It then verifies that copied input paths match `task_info.json`, creates the candidate mount plan, and records the prompt SHA-256.

### `request_builder.py`

Builds and canonicalizes one OpenEvo `TaskRequest`:

- `task_id`: exact frozen task ID plus protocol-scoped run identity;
- `instruction`: exact bytes of generated `INSTRUCTIONS.md`;
- `num_samples`: `1`;
- `runtime`: frozen isolated runtime profile;
- `agent`: frozen Codex `AgentSpec`;
- `builder`: `agent_transcript`;
- `evaluator`: `null` during candidate execution;
- `metadata`: secret-free hashes and protocol IDs only.

The canonical request SHA-256 is stored before launch.

### `candidate_supervisor.py`

Owns the isolation lifecycle, starts exactly one candidate process group, enforces wall time and resource limits, captures stdout and stderr separately, records the exact exit code, terminates only its own proven child process group on timeout, and tears down the namespace before evaluator access. It never searches for or signals processes by generic name.

### `agent_entrypoint.py`

Optional ResearchClawBench agent-preset target. It accepts the already generated prompt/workspace identifiers, validates them against the supervisor receipt, and delegates to `candidate_supervisor`. It is not allowed to launch the candidate on the host merely because `cwd` is correct.

### `artifact_validator.py`

Validates candidate deliverables and produces the JSON receipt specified in `design/artifact_validator_spec.md`. Validation happens after candidate termination and before scoring. It has no access to the checklist or target study.

### `feedback_filter.py`

The only path from evaluator/operational results to OpenEvo. It validates records against `schemas/evolution_feedback.schema.json`, applies the phase policy, strips non-allowlisted data, and rejects official-test feedback categorically.

### `official_scorer.py`

Evaluator-only wrapper. It imports the pinned `evaluation.score.score_workspace`, verifies the ResearchClawBench commit and run receipt, injects judge credentials only into the evaluator process, and invokes the scorer only when the phase policy permits. For official tests it runs only after all formal candidate runs are sealed.

### `result_writer.py`

Atomically publishes `_meta.json`, `_agent_output.jsonl`, separated stderr/audit logs, validation receipt, and content hashes. It maps terminal state and exit code without converting an invalid artifact into `completed`.

### `submission_packager.py`

Checks every run folder against the frozen protocol, builds a non-secret `submission_manifest.json`, lists every file and SHA-256, records omissions explicitly, and creates an archive without following links. Packaging never reruns, repairs, or scores a task.

## 6. Input and output interfaces

Candidate-facing input is exactly:

```text
/workspace/INSTRUCTIONS.md              read-only
/workspace/data/                        read-only
/workspace/related_work/                read-only
/openevo/session/evolution/agent_system.md  read-only, treatment only
```

Candidate-writable output is exactly:

```text
/workspace/code/
/workspace/outputs/
/workspace/report/
/tmp/                                   task-private ephemeral
```

Supervisor-only state includes `_meta.json`, `_agent_output.jsonl`, stderr, receipts, hashes, runtime logs, frozen configuration, and credentials. Evaluator-only input additionally includes the task's `target_study` and judge credentials.

The standard final run folder retains at minimum:

```text
<run>/
├── _meta.json
├── _agent_output.jsonl
├── INSTRUCTIONS.md
├── data/
├── related_work/
├── code/
├── outputs/
├── report/report.md
├── report/images/
├── _score.json                 evaluator-created, when scoring is authorized
└── audit/                      adapter receipts, hashes, stderr, mount evidence
```

`target_study` is never copied into a run folder.

## 7. Prompt transmission

The official `INSTRUCTIONS_TEMPLATE` is rendered from pinned `task_info.json`, and the rendered bytes are written once and hashed. The same bytes become `TaskRequest.instruction`. The command must pass the instruction as an argument/stdin through the structured OpenEvo harness; it must not use ResearchClawBench's `$(cat ...)` shell substitution.

Treatment may prepend only the frozen `agent_system` through OpenEvo's typed context injection. It may not edit `INSTRUCTIONS.md`. Baseline uses the identical request/runtime with no evolved `agent_system` content. The difference is declared in the paired manifest.

## 8. Workspace lifecycle

1. Resolve task from the frozen split and verify source identities.
2. Allocate a new, non-reused run ID and private staging folder.
3. Construct the official workspace and prompt outside the candidate namespace.
4. Reject unsafe paths, links, devices, sockets, hard-link anomalies, or unexpected files.
5. Record input inventory and hashes; construct read-only/read-write mounts.
6. Materialize and verify the frozen `agent_system` for treatment.
7. Launch one isolated candidate and capture its transcript.
8. Stop the candidate namespace; no candidate process may remain.
9. Validate artifacts, hash all outputs, and atomically freeze the run folder read-only to candidate code.
10. In Dev/Validation only, optionally invoke the separate evaluator and feedback filter according to phase policy.
11. For official tasks, seal all 40 candidate runs before any scorer call.
12. Package immutable run folders and submission manifest.

## 9. Codex CLI invocation

Use the installed OpenEvo Codex preset via `AgentSpec(harness="codex")`, with exact model, provider/auth mode, reasoning effort, capture mode, and native-memory policy pinned. Subscription mode requires `settings.capture_mode: transcript`, the existing managed credential isolation surface, no caller-controlled MCP server, and no arbitrary agent environment.

The effective command identity, CLI version, configuration overrides, and credential-isolation receipt are recorded. The adapter must not construct a second, divergent `codex exec --full-auto` command. A ResearchClawBench preset invokes the adapter entrypoint, not Codex directly.

## 10. Stdout, stderr, and exit code

- Preserve raw Codex JSON stdout in bounded append-only capture and derive `_agent_output.jsonl` without dropping malformed lines.
- Preserve stderr separately; do not merge it into score inputs.
- Use pipefail semantics and record the actual Codex exit code plus any capture/validator exit code.
- `completed` requires candidate success and artifact-validator success. A syntactically completed Codex event does not override a missing or invalid deliverable.
- Timeout maps to a terminal `TIMEOUT` receipt. Signal identity and timeout source are recorded.
- Capture truncation, undecodable output, or missing terminal event is an explicit validation error, never silent success.

## 11. Artifact validation

Validation is independent of the official scorer and contains no target-derived checks. It verifies structure, safe paths, report substance, PNG integrity and references, code/output presence, traceability evidence, status consistency, and a complete hash manifest. Exact rules and response format are frozen in `design/artifact_validator_spec.md`.

## 12. Evaluator invocation

The OpenEvo `TaskRequest.evaluator` stays `null`; otherwise evaluation metadata would be merged into the same trajectory. The adapter instead calls the pinned ResearchClawBench scorer from an evaluator-only process after the candidate namespace is gone.

The evaluator receives the sealed run folder read-only, the pinned ResearchClawBench task tree read-only, and judge credentials. It does not receive OpenEvo state, candidate credentials, unrelated task workspaces, or host home. `_score.json` is written to a dedicated evaluator output overlay and then atomically attached to the run folder by the supervisor.

## 13. Run-folder and submission manifest

Every run records:

- task/run/protocol IDs and phase;
- baseline or treatment condition;
- source commits, dataset revision, split hash, artifact ID/SHA-256;
- canonical `TaskRequest` SHA-256 and prompt SHA-256;
- runtime image/rootfs digest, mount policy digest, network policy, environment variable names only;
- Codex/model/reasoning/tool/budget identities;
- start/end time, wall duration, exit state, validation receipt;
- transcript and output file hashes;
- scorer identity and score hash only after authorized scoring;
- retry lineage, if any, without overwriting the failed predecessor.

The package-level submission manifest enumerates all run manifests and file hashes, states whether trajectories/code/cost data are included, and contains no secret values or hidden rubric content.

## 14. Error handling

Suggested stable error families:

- `SOURCE_IDENTITY_MISMATCH`
- `TASK_NOT_IN_FROZEN_SPLIT`
- `WORKSPACE_PATH_UNSAFE`
- `WORKSPACE_SOURCE_CHANGED`
- `ISOLATION_UNAVAILABLE`
- `MOUNT_POLICY_MISMATCH`
- `CREDENTIAL_ISOLATION_UNPROVEN`
- `ARTIFACT_IDENTITY_MISMATCH`
- `CANDIDATE_TIMEOUT`
- `CANDIDATE_NONZERO_EXIT`
- `TRANSCRIPT_INVALID`
- `ARTIFACT_VALIDATION_FAILED`
- `FEEDBACK_POLICY_VIOLATION`
- `SCORER_NOT_AUTHORIZED`
- `EVALUATOR_FAILED`
- `SUBMISSION_INCOMPLETE`

Errors are recorded with bounded, redacted detail. No exception path copies evaluator inputs into candidate logs.

## 15. Restart and resume

- Workspace construction may be retried only before candidate launch, using the same source snapshot but a fresh staging path.
- A launched run folder is immutable evidence. Failure, timeout, or invalid output is never resumed or repaired in place.
- A retry gets a new run ID and records `predecessor_run_id` plus a non-score reason.
- Formal official runs may be retried only for documented infrastructure failures under a predeclared rule and without inspecting scorer output; ambiguous failures require maintainer approval.
- OpenEvo evolution databases and candidate native memory are never shared across formal task runs unless the frozen cross-task policy explicitly permits it. The recommended official protocol forbids such sharing.
- Packaging and evaluator operations are idempotent by input manifest digest.

## 16. Baseline and treatment control

Baseline and treatment must share task order, source bytes, prompt bytes, model, provider, Codex version, reasoning level, tools, network, runtime image, dependency lock, time/token/cost limits, seed policy, candidate isolation, validation rules, and scoring schedule.

The sole intended variable is the `agent_system` selection:

- baseline: no evolved `agent_system` artifact;
- treatment: one frozen, hash-addressed `agent_system` artifact.

No treatment-only memory, skills, MCP tools, network access, retries, or package installation is allowed. Pairing is proven by a machine comparison of the two manifests after removing only condition and artifact fields.

## 17. Minimal implementation order

1. Package skeleton, closed models, and source-identity checks.
2. Public task loader and official workspace construction with no model call.
3. Artifact validator and hash/freeze receipts.
4. Candidate isolation profile and mount-evidence smoke tests with a harmless shell probe, not a benchmark.
5. OpenEvo request builder using existing Codex/transcript contracts.
6. Result writer and immutable run-folder lifecycle.
7. Feedback filter and schema enforcement.
8. Evaluator-only official scorer wrapper.
9. Submission packager and paired-manifest comparison.
10. Community canary, evolution, freeze, maintainer confirmation, then—and only then—official execution.

No implementation begins until this Phase 0 design set is reviewed and accepted.
