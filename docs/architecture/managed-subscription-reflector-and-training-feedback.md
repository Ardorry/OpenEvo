# Managed Subscription Reflector And Post-run Training Feedback

Status: Core capability note for benchmark automation. Durable feedback
transport and the production Science successor hook are implemented. This note
does not claim that every benchmark package has a production training driver.

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
without modifying its `openevo.session_completed` event or source manifest.
Evolution holds the non-serializable `EvaluatorAuthority`; an authenticated
Core-control request is forwarded with the generation-bound Evolution service
identity. Callers cannot choose or mint an authority ID. The service verifies
that the source dataset represents exactly one completed and sealed session,
that session/task/revision identities match, and that the attachment content
hash remains immutable.

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

Attachments and resolved views are stored in a private SQLite database using
WAL, `synchronous=FULL`, transactional uniqueness, and immutable fsynced JSON
mirrors. Idempotency binds session, dataset revision, producer, authority and
content; conflicting reuse fails closed. A restarted Evolution service can
read and verify the same attachment and resolved-view hashes.

The private Core-control surface is bearer protected and intentionally absent
from the public v2 OpenAPI document. It exposes create/get/list/resolve and
forwards only through the current generation-bound Evolution binding.
`ProductionScienceSuccessorPreparerV2` can require an attachment per project,
resolve the immutable completed dataset plus referenced attachments, submit
native method jobs against that resolved dataset, and record attachment IDs,
hashes, and resolved-view hash in the successor receipt. Missing or drifting
authority is retryable and fail-closed. Official-frozen mode rejects feedback.

The ResearchClawBench benchmark package has a durable transition store and
synthetic crash recovery tests, but its CLI must remain blocked until a
concrete production `TrainingOperations` driver binds candidate workspace
construction, Core v2 task ownership, evaluator-only scoring, feedback
transport, native successor evolution, admission, and fresh next-attempt
workspace creation. The inspection-only driver deliberately refuses every
mutating command.

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
isolation, failure receipts, model-control-plane validation, cross-process and
restart-safe evaluator attachments, sealed-session binding, feedback class
separation, source dataset immutability, private Core-control transport,
successor resolved-view binding, admission update/keep/reject, and native
run-owner result validation. A benchmark-specific formal launch additionally
requires its production operations driver and clean-host release gates.
