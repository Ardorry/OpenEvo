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

In a release-owned Docker user container, reflector credentials use the same
verified daemon-host mapping contract as candidate sessions. The Core service
supervisor issues a distinct, generation-bound
`ManagedReflectorMountAuthority` to the evolution worker through a sealed
inherited descriptor. The authority binds the release install and registry,
framework lock, signed managed-runtime release digest and profile, worker launch ID, runtime
identity, and the exact `DockerHostPathSpec`; it never exposes the host Codex
home and is not persisted in argv, ordinary environment values, or ledgers.
The worker creates independent session and credential roots below the held
mapped `sessions/` directory, so their translated bind sources are visible to
the Docker daemon even when Core itself runs in a WSL/Docker user container.
The offline OCI archive's local Docker `loaded_image_id` remains separate: it
is verified and used for inspect/create, but it is canonicalized to the signed
`trusted_digest` before mount authority, registration expectation, readiness,
or execution identity leaves the runtime boundary.

Before registering or claiming any evolution job, the worker performs a
no-model Docker adoption canary. It verifies the fixed
`/openevo/credentials/codex` target, read-only auth mount, container-user
readability and UID/GID, exact generation/release/runtime/mapping identities,
and owned-root cleanup. The receipt asserts that neither Codex CLI nor a model
was started. The evolution backend receives a separate non-secret exact
registration expectation from the supervisor and rejects missing or drifting
receipts before worker registration; the supervisor repeats the exact check in
its health probe. Consequently a failed canary cannot race ahead and claim a
job. Readiness consumers must re-read this current worker receipt before
persisting an evolution intent and fail closed with
`REFLECTOR_CREDENTIAL_MOUNT_NOT_READY` on any mismatch.

The adoption canary's host Docker CLI remains in the evolution worker's process
group. Its otherwise closed environment therefore preserves the supervisor's
validated, non-secret process-ownership digest so group health cannot mistake
the in-flight Docker child for a foreign process. No credential, user Docker
configuration, or host home is inherited, and the ownership marker is not
passed into the managed container. This is part of the common managed-runtime
process contract rather than a benchmark-specific reflector bypass.

The Core readiness provider first asks the service supervisor for a strict
current-generation adoption. A pristine supervisor proceeds through normal
managed startup. An existing generation is never silently restarted by this
readiness path: it must be fully healthy already or be the single verified
worker startup-tail shape documented by the Core service supervisor. This
closes the response-loss window in which Docker adoption and worker
registration complete after the initiating health observation failed, without
reissuing a credential snapshot or changing generation. The path remains a
no-model operation and produces no evolution intent.

Candidate readiness uses the same Docker credential authority and executes the
same deterministic isolation command as `CodexHarness.setup()`. The command is
POSIX `/bin/sh` syntax because the managed runtime does not promise Bash as its
command interpreter. A failed probe emits one closed, non-secret terminal JSON
frame before Gateway exits; the service supervisor drains that frame before it
falls back to a generic health error. The frame may expose only a stable failure
code, phase, generation/release identities, and boolean model/secret state.
Credential paths, credential bytes, exception text, and host environment values
remain excluded.

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
Strict recovery contracts retain tuple-backed closed inventories in Python,
while their JSON wire representation is an array. Private recovery endpoints
therefore validate the canonical request bytes in Pydantic JSON mode before
calling a resolver. They do not relax strict model validation or accept extra
fields; this only prevents the HTTP decoder's intermediate Python `list` from
being mistaken for an invalid wire representation.
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

Normal Science successor work registers every native method output as sealed
and unpromoted. The Core admission policy receives the complete, closed target
proposal inventory; auxiliary outputs such as the GEPA candidate archive stay
outside that inventory. GEPA may therefore emit several agent-system
proposals, while text memory and skill bundles may emit one, without changing
the promotion contract. A decision either promotes exactly one target
proposal or records an explicit keep/reject that inherits the active parent.
The predecessor remains promoted as immutable historical authority until the
Science project-head transaction chooses the new composition; uniqueness is
enforced within the new job's proposal set, not by invalidating an older
project head before its successor commit.
The decision ID is deterministic, so replay after a lost response reads the
same atomic registry receipt rather than submitting another reflector job.

Before mutation, Core scans every verified UTF-8 proposal payload. The private
denylist binds the task/evaluator supplement plus exact literals derived from
the sealed dataset payload, including task IDs, data filenames, entities,
result-like values, DOI strings, report sentences, evaluator terms, and
absolute paths. The public `content_admission` receipt contains only category
counts, proposal IDs, the declared-basis digest, and the sealed source payload
digest; it never copies a protected literal. All three global artifact types
carry this receipt in their decision and successor authority. The generic
text-memory, ExpeL, skill-bundle, and agent-system workers also apply the same
source-record denylist before registration. Missing source authority,
unbounded/non-text payloads, pre-promoted proposals, or ambiguous proposal
inventories fail closed.

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
