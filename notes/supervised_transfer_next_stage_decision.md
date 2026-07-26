# ChemBench supervised transfer next-stage decision

Decision: `MECHANISM_MISMATCH_FOUND`
Real model calls: paused

## Why run-05 must not continue

run-05 is a fresh directory, but it entered online canary only because the
monolithic `run()` method always starts from preflight. It was not authorized
by a receipt invalidation decision, and it is now externally interrupted with
no terminal pause receipt. It must remain preserved and non-resumable.

## Required benchmark-local correction

Only `benchmarks/chembench/**` and audit documentation may change.
`src/openevo/**`, frozen manifests, split receipts, old runs, and the old
repository remain untouched.

1. Introduce a closed paid stage mode with separate entry points:
   `run-preflight` and `run-formal`.
2. `run-preflight` executes only update smoke, online canary, control canary,
   Probe smoke, and checkpoint-0 Probe. It ends terminally as
   `PREFLIGHT_COMPLETED`; it never enters Control Train.
3. Produce `PreflightAuthorityReceiptV1` with exact source/config/split/model,
   Codex, executor-policy, Core-registry, event-stream, success-receipt,
   cleanup, stage-count, zero-finding, and Probe-no-evolution bindings.
4. Include aggregate-only checkpoint-0 paired rows in that authority so the
   final learning curve can reuse checkpoint 0 without copying per-item private
   outcomes into the formal run.
5. `run-formal` requires one exact current-source authority and starts its first
   paid task session at `CONTROL_TRAIN`, task ordinal 0, round 0. It must not
   execute any smoke, canary, or checkpoint-0 model call.
6. A formal run may reference the preflight authority but may not read or merge
   any Control/Online Train result from another run.
7. Add a protocol-global, atomic Test-manifest use ledger under the private
   state root. It must reject a second primary use and permit a recovery Test
   only after an immutable source-bug invalidation receipt. Performance cannot
   authorize switching.
8. Add a closed transition table for preflight and formal modes, plus an
   immutable externally-created pause receipt helper for orderly operator
   pauses.
9. Update reporting to consume only the authority's aggregate checkpoint-0
   rows and the formal run's checkpoints 10-50/private Train/Test rows.

## Required tests

- A preflight run cannot enter Control Train.
- A formal run cannot start without an exact current-source authority.
- The first paid formal call is Control task 0 round 0.
- A formal run never reruns smoke/canary/checkpoint-0 Probe.
- No Control/Online Train row from a failed or preflight run is imported.
- Stage transitions reject skips, rollback, and arbitrary names.
- Probe authority contains aggregates only and no UID, target, prediction, or
  private completion fields.
- A primary Test manifest can be claimed once across all run IDs.
- Recovery requires a source-bug invalidation and cannot be score-triggered.
- Paused/failed runs remain immutable and non-resumable.
- Existing packet, category-chain, Probe-no-evolution, Test-freeze, split, and
  exposure counterfactual tests continue to pass.

## Execution after the fix

Because the source commit will change, prior preflight evidence will no longer
match the new source identity. Run exactly one new minimum preflight suite with
a fresh run ID; do not mechanically rerun unrelated tests or any formal Train.
After its authority validates and a second audit returns
`EXPECTED_MECHANISM_CONFIRMED`, start a separate fresh formal run ID at Control
Train task 0. Intentionally skip all stages covered by the new authority.

The old run-04 Control output and all run-05 output remain excluded. Primary
Test remains unused and frozen.

## Current gate state after implementation

The mechanism fixes are implemented and their relevant regression, Ruff,
shell-syntax, frozen-identity verify, and complete zero-call dry-run gates pass.
The source has not yet been committed, so no old paid receipt can match the
eventual repaired source identity. The current decision remains
`MECHANISM_MISMATCH_FOUND` until the source commit is created and one fresh
preflight authority validates against that exact commit. No real call is
allowed before that point.

Exact next sequence:

1. stage only the benchmark-local source/tests/docs and these audit notes;
2. create a local commit without data/results/state/private manifests;
3. re-run frozen identity and source cleanliness checks;
4. run one fresh `run-preflight` ID only;
5. verify its terminal authority and repeat the protocol audit;
6. if and only if the second decision is `EXPECTED_MECHANISM_CONFIRMED`, start
   a distinct `run-formal` ID whose first paid session is Control task 0 round
   0.

## Preflight run-06 fail-closed debug history

`st-v1-preflight-20260727-06` completed update smoke, online canary, and
control canary, then failed before the first Probe-smoke attempt with
`SESSION_ARM_STAGE_INVALID`. It has 55 completed task sessions, 19 reflector
jobs/artifacts, zero infrastructure/security/context/artifact findings, zero
Test access, and no active residual process. The run is terminal, preserved,
and excluded from reuse.

Root cause: the new admission guard treated `PROBE_SMOKE` as both the explicit
smoke stage and a generic `PROBE_*` checkpoint. The checkpoint arm set
overwrote `control_probe_smoke`/`online_probe_smoke`. The condition is narrowed
to `PROBE_CHECKPOINT_*`; a regression now proves both smoke arms are admitted
and checkpoint arms are rejected at `PROBE_SMOKE`. The fix passed 8 focused
tests, Ruff, dry-run with zero calls, and source-diff checks. A new source
commit and fresh preflight run ID are required; run-06 is never resumed.

## Preflight run-07 fail-closed debug history

`st-v1-preflight-20260727-07` completed the supervised update smoke and reached
the 9-category online canary. It then failed closed on Yield_Prediction update
2 with `TASKWISE_ARTIFACT_VALIDATION_FAILED`. It has 21 completed task
sessions and 14 reflector jobs/artifacts, zero infrastructure, security, or
context findings, one artifact finding, zero Test access, complete cleanup,
and no residual process. The failed artifact and run remain preserved and are
not reusable.

The content-free validation receipt identifies
`memory_required_sections_invalid`. A private heading-only inspection showed
all required headings but one exact duplicate `Provisional Principles`
heading. The model-facing contract already required exactly eleven unique
headings, so another prompt-only reminder would not provide adequate
reliability for 900 formal updates. The benchmark-local reflector boundary now
performs one closed structural normalization: only when all eleven exact
allowed headings first occur in the required order and the sole structural
defect is one or more duplicate allowed headings, it merges those section
bodies without changing their text. Missing, unknown, or reordered headings
remain unchanged and fail the existing validator. Leakage, provenance, rule
schema, evidence, capacity, and answer-map checks remain downstream and are
not weakened.

The private reflector receipt schema records both source and Core-consumed
output digests, the normalization ID, and whether it was applied; receipt
loading recomputes the transformation from the immutable private event stream.
Thus the normalized bytes still enter the registered Core method and typed
artifact lifecycle, while the original model output remains audit-bound. A
fresh source commit and new preflight run ID are required; run-07 is never
resumed.
