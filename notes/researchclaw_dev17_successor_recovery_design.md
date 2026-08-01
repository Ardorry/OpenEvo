# ResearchClawBench Dev17 successor recovery design

## Scope and evidence boundary

- Repository: `/home/lhy-h/work/researchclaw_openevo/OpenEvo`
- Source formal namespace: `rcb_oe_v0_community17_fresh_efficiency_v38`
- Failed unit: `Energy_004`, attempt `a0`, successor transition
  `successor-42ad0867f71760911d57f807ac649e7c`
- The v38 database and Core records are immutable source evidence. This change
  does not edit that namespace or claim it completed.
- Recovery may only use Core's append-only successor recovery protocol. It may
  not replay Candidate, Judge, or the source transition.

## Observed call chain

```text
CommunityTrainingSupervisor(EVOLUTION_RUNNING)
  -> ProductionTrainingOperations.evolve_artifacts
  -> CoreSuccessorPort.recover / execute
  -> GET /v2/internal/training-successors/{transition}
  -> POST /v2/transitions/{transition}/retry (stable supervisor key)
  -> CoreScienceTaskOwnerV2._execute_successor_transition
  -> ProductionScienceSuccessorPreparerV2.run_methods
  -> Evolution plan-bound job creation
  -> validate/materialize/commit/handoff (not reached for Energy_004 a0)
```

## Durable findings

- Candidate completed and was sealed. The public validator rejected the
  artifact, so Judge was correctly skipped. The evaluator-only feedback
  attachment was sealed before the supervisor requested evolution.
- Core made two successor attempts. Attempt 1 was retryable and attempt 2 was
  terminal. Both failed during `running_methods`, before validation,
  materialization, commit, or project-head activation.
- The Evolution database has no plan-bound job for this transition, no
  Reflector inference reservation, no active lease, no retry receipt, no
  output artifact, and no staged artifact owned by this transition.
- Core has no successor commit and the active project head is still the exact
  predecessor. This rules out a lost commit response and rules out reuse of a
  staged successor.
- The source transition exhausted its retry authority. Its smallest safe
  continuation is a new-generation, append-only Core recovery; retrying the
  source transition or rerunning Candidate/Judge is forbidden.

## Defect

`CoreSuccessorPort` correctly reconciles a committed successor before issuing
a retry and reuses the supervisor idempotency key. However, after Core proves
that the transition is terminal without a commit, the adapter raises only a
generic `CoreControlError`. `ProductionTrainingOperations` consequently leaves
no immutable operation-level failure receipt. A restarted runner must query
the exhausted external transition again and an operator cannot distinguish a
recovery-required terminal from an ambiguous transport failure using adapter
receipts alone.

## Minimal change

1. Add a typed `SuccessorRecoveryRequired` error carrying a closed, sanitized
   checkpoint.
2. Build the checkpoint only from a generation-fenced Core authority that
   proves all of the following:
   - transition state is `failed`;
   - the latest attempt is terminal and non-retryable;
   - commit is absent;
   - successor artifacts are absent;
   - transition, predecessor head, attachment, Core generation, Core release,
     and the stable supervisor idempotency key are bound in the receipt.
3. Persist that checkpoint as a `FAILED_TERMINAL` operation receipt with
   `FailureClass.AUTHORITY` before re-raising. Never complete the supervisor
   side effect from this receipt.
4. On restart, replay the same typed failure from the local immutable receipt
   without another POST or model-producing action.
5. Treat contradictory terminal evidence (for example, failed plus a commit or
   successor artifacts) as an integrity error and do not mint a recovery
   checkpoint.

## Recovery boundary

The new checkpoint is diagnostic and routing authority, not a successor
receipt. It explicitly forbids source mutation and Candidate/Judge/Reflector
replay. Actual recovery still requires Core's existing authenticated,
generation-changing `/v2/internal/science-successor-recoveries` workflow,
including source re-resolution, one target at a time, atomic project seeding,
and a fresh continuation namespace.

## Verification map

- Adapter tests: terminal checkpoint construction, contradictory evidence,
  lost POST response followed by commit reconciliation, receipt replay after
  runner exit, stable idempotency identity, and no additional external/model
  call on repeated recovery.
- Existing Core tests: pre-job recovery authority, staged/paid job adoption,
  response-loss adoption, repeated recovery idempotence, generation change,
  immutable supersession, atomic seed, and tampered receipt rejection.
- Real validation: a fresh isolated `Energy_004` gate using the formal model,
  reasoning, adapter, and protocol, followed by formal Dev17 continuation only
  after the gate passes.

## Exact formal Core predecessor admission

The first repaired release could not reach the mutation path. The host service
observer rejected the still-healthy formal Core before `ensure_core_service`
because v0.1.10 recognized only one published v0.1.9 daemon profile at
lifecycle 16. The v38 service is a second, independently frozen v0.1.9 release
at lifecycle 85. Its running ledger, authenticated `/version` response, and
`/v2/system/status` response agree, but none matched the single hard-coded
profile.

The deployment repair adds the v38 predecessor as a second exact profile. It
does not admit v0.1.9 generically. Observation and equal-lifecycle replacement
require every frozen release, registry, framework-lock, source, daemon bundle,
canonical manifest, build, OpenAPI, event-schema, feature-set, and runtime
contract digest to match the profile. Any drift remains fail-closed. The
existing lifecycle-16 profile continues to use ordinary monotonic 16-to-85
replacement; only the exact lifecycle-85 formal profile receives the bounded
v0.1.10 equal-lifecycle replacement authority.

## Dev17 v40 post-method reconciliation

### New durable evidence

The isolated Energy gate passed, but formal v40 failed later at
`Energy_004/a0` in transition
`successor-889c006c8e8eec11bf5653c832e07cac`. This failure is materially
different from v38:

- the sealed dataset and evaluator-only feedback are complete;
- all three plan-bound jobs (`agent_system`, `skill_bundle`, and
  `text_memory`) succeeded exactly once and have completed inference
  reservations;
- Core admission selected one sealed and promoted artifact for each target;
- the transition reached `materializing` (`progress_completed=4/6`), but no
  materialized-context row, successor commit, project-head activation, or
  workspace handoff exists;
- the predecessor remains the active project head, all leases are closed, and
  the transition has no pending or orphan model job;
- exact offline projection and materialization with the deployed v0.1.10 wheel,
  framework lock, request identity, and selected payloads pass.

The terminal source is therefore neither a pre-job failure nor a failed paid
job. The existing cross-generation successor-recovery contract intentionally
cannot encode it. A clean formal restart would replay already closed paid
calls and is not an admissible recovery.

### Narrow recovery contract

Add an authenticated internal `completed-methods reconciliation` operation for
one failed successor transition. The operation is authorized only if all of
the following read-only checks pass before the Core ledger changes:

1. the transition is failed, uncommitted, at `progress_completed=4`, and its
   exact predecessor is still active;
2. its latest failed attempt and transition digests match the caller's frozen
   checkpoint;
3. Evolution returns an exact, bounded plan-bound job for every enabled target
   and no other transition-bound job;
4. every job is `succeeded`, attempt count is one or otherwise unchanged, and
   its terminal result digest matches the read-only plan authority;
5. every target has exactly one Core-selected, sealed, promoted UPDATE output
   with matching admission and content-admission receipts;
6. no API in this path can create or retry an Evolution job, and the request
   declares `model_execution_allowed=false`.

After that proof, Core appends a new `reconciliation_only` transition attempt.
It reconstructs the existing `ScienceMethodOutputV2` receipts from read-only
Evolution authorities, then runs only the ordinary validation,
materialization, workspace-capture, and atomic-commit tail. The failed attempt
is never rewritten. A stable reconciliation idempotency key makes a lost HTTP
response safe: repeated calls read the same attempt or committed successor.

### Supervisor receipt reconciliation

The v40 adapter operation receipt is already `FAILED_TERMINAL` and remains
immutable. On replay, `ProductionTrainingOperations` may call the new Core
operation only when that receipt contains the exact typed recovery checkpoint.
The successful result is persisted under a separate, deterministic
reconciliation receipt identity which binds the original terminal receipt,
request identity, Core generation/release, transition, and final successor
authority. Subsequent supervisor recovery reads that append-only receipt and
returns `RECOVERED`; it never overwrites the failure or reissues a model call.

Any missing job, foreign plan/transition, non-succeeded terminal, missing
admission, KEEP/REJECT output, receipt digest mismatch, project-head drift,
Core generation drift, or contradictory commit evidence remains fail-closed.

### Regression obligations

- reconciliation after all method jobs succeeded does not call job create or
  job retry;
- response loss after commit returns the same successor;
- repeated reconciliation creates neither a new transition attempt nor a new
  adapter side effect;
- Core restart can resume a `reconciliation_only` attempt from its durable
  dataset without entering normal `run_methods`;
- missing/failed/extra jobs, generation drift, receipt drift, and contradictory
  commit evidence are rejected;
- model/job/reservation counters remain unchanged across reconciliation.

## Dev17 completed-prefix continuation

### Why v40 cannot resume as an ordinary runner

The v40 Supervisor database is correctly bound to the protocol, Core source,
and adapter source that created it. Deploying the reconciliation implementation
necessarily creates a new immutable Core release and adapter identity. Rebinding
the existing database in place would erase that provenance; starting a normal
fresh namespace would instead repeat thirteen already-closed Candidate and
Judge operations. Neither action is admissible.

The repair is therefore split at an explicit authority boundary. A bounded
repair executor first closes only the existing v40 Energy successor tail and
advances the source to `NEXT_ATTEMPT_READY`. It authenticates the persisted
source identity separately from the new executor identity. It may reconcile the
terminal evolution receipt, admit the resulting composite, and persist the
workspace handoff, but cannot start a Candidate, Judge, or Reflector operation.
The source identity columns and its earlier receipts remain unchanged.

### Append-only prefix namespace

A new formal namespace can then import the completed prefix by typed reference.
Its protocol records the source namespace, state and database digests, all three
source identity digests, the repaired successor authority, cursor, and exact
consumed operation counts. The only accepted boundary is a closed
`NEXT_ATTEMPT_READY` state with:

- contiguous attempts for every completed task and the current task;
- exactly two evolution cycles per completed task and one per already-closed
  non-final attempt of the current task;
- a closed three-job receipt for every evolution cycle;
- no planned or failed source side effect, active resource, or incomplete
  transaction;
- budget reservations equal to the referenced Candidate, Judge, and Reflector
  prefix, including the protocol's five Reflector-call units per cycle;
- one active Core project head, composite, and successor workspace authority
  that agree with the repaired commit.

Initialization is resumable but fail-closed. The destination first remains in
an import-pending state, writes content-addressed references to attempts,
completed side effects, and budget reservations with stable destination keys,
then performs one durable transition to `NEXT_ATTEMPT_READY`. `run_next` cannot
leave an incomplete prefix import. Repeating initialization must reproduce the
same rows and transition receipt or fail.

No model artifact is copied into a new result. The local composite journal is
deterministically reconstructed from the already-admitted evolution receipts,
and every reconstructed composite must equal its source authority. The one
sanitized successor workspace needed by the next same-task attempt is copied
through the deterministic workspace-archive contract into the destination
namespace; both archive declarations are verified and the rebased authority is
content-addressed. Core project heads and promoted artifacts remain native Core
authorities in the persistent Core database.

The destination budget includes the imported reservations, so its remaining
allowance is the original Dev17 allowance minus the exact source prefix. Final
verification still requires all 51 attempts, 34 evolution cycles, 102 native
jobs, 17 task selections, zero active resources, and zero pending or failed
side effects across the combined referenced prefix and new suffix.
