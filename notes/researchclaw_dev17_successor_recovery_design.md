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
