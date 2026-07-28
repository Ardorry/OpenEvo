# Managed Subscription Reflector And Post-run Training Feedback

Status: Core capability note for benchmark automation; this does not add a
release-facing CLI or claim that the complete Daemon successor workflow is
implemented.

## Managed Codex reflector

Pure-text evolution methods may request a `codex_cli` reflector with an
explicit `ManagedReflectorRuntimeConfig`. In managed mode the configuration is
closed over the Core-owned runtime profile, trusted image digest,
`/opt/codex/bin/codex`, exact CLI version, subscription authentication,
transcript capture, and `path_fallback_allowed=false`. Missing or drifting
identity is rejected during validation; managed execution never falls back to
the host `PATH`.

`ManagedCodexReflectorService` stages a prepared credential snapshot into a
fresh private credential root, creates a fresh runtime session, verifies the
CLI version, runs one inference, records an execution receipt, then stops the
runtime and removes only the session and credential roots it owns. Candidate
and reflector sessions do not share a runtime, process, temporary directory, or
credential view. A failed CLI operation retains private stdout/stderr evidence
and publishes a hash-only failure receipt before cleanup.

The managed container has two distinct network policies:

- `allow_internet=false` keeps candidate or reflector tools offline;
- `allow_model_control_plane_network=true` permits only the managed Codex
  subscription process to reach its model control plane.

Core accepts that split only for a managed runtime. It is rejected for ordinary
host execution and must not be interpreted as general task network access.

## Immutable post-run attachment contract

`TrainingFeedbackAttachment` supplements a completed transcript dataset
without modifying its `openevo.session_completed` event or source manifest. A
process-local `EvaluatorAuthority` capability is required to create an
attachment. The service verifies that the source dataset represents exactly
one completed and sealed session, that session/task identities match, and that
the attachment content hash remains immutable.

The supported classes are:

- `HARD_GT`: private task-local truth only;
- `SOFT_JUDGE`: a closed global allowlist of completion, validity, aggregate
  score, generic failure tags, runtime bucket, cost, and artifact hash;
- `MIXED`: the same global allowlist plus a separate task-local mapping.

Raw judge reasoning, checklist/rubric fields, target-study content, per-item
scores, prompts, responses, keywords, and weights are rejected. The derived
evolution dataset contains only global feedback. Task-local feedback is
available through a separate overlay call that requires the same task scope;
cross-task access fails closed.

The current capability is intentionally process-local. It has been exercised
with an `EvolutionStore` and native workers, but it is not yet transported by
the production Evolution HTTP service nor inserted into
`ProductionScienceSuccessorPreparerV2` between dataset sealing and method
execution. Benchmark automation must not claim formal daemon training
readiness until that internal, generation-bound transport and durable retry
semantics exist.

## Native admission and run ownership

`NativeArtifactAdmissionService` receives proposals already persisted by a
native plan-bound worker. `update` promotes exactly one successor, `keep`
inherits the active parent, and `reject` leaves the active parent unchanged.
Decision receipts bind the job, proposal set, selected/rejected artifacts,
validator evidence, active artifact, and a canonical hash. Adapter code may
apply benchmark-specific static checks, but it cannot create a replacement
artifact or registry revision.

`NativeTaskRunOwner` owns submit/poll/cancel semantics for a canonical
`TaskRequest` and returns one validated `SessionResult`. Workspace handoff,
runtime-context binding, credential staging, `CodexHarness`, transcript
construction, and result publication remain Core responsibilities. A benchmark
adapter may prepare a closed workspace and consume the result; it must not run
Codex directly or parse CLI JSONL as the authoritative session result.

## Verification

Focused tests cover managed identity drift, PATH fallback rejection, host-home
isolation, failure receipts, model-control-plane validation, evaluator
authority, sealed-session binding, feedback class separation, source dataset
immutability, admission update/keep/reject, and native run-owner result
validation. A complete release claim additionally requires the production
Daemon successor path and clean-host release gates defined by the canonical
product specification.
