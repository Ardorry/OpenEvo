# Supervised Transfer v3 composed Control Test baseline

## Purpose

This protocol amendment adds a paired, no-evolution Test baseline for the
completed supervised-transfer v3 experiment.  It consumes the exact frozen Test
partition and task order used by the completed evolved Final Test, while
deliberately injecting no `text_memory`, `skill_bundle`, or `agent_system`
artifact.

The baseline is a separate immutable run.  It does not modify, resume, or copy
the completed evolved Test run and it does not describe the pair as one
uninterrupted process.

## Candidate execution path

Every baseline item follows the existing official execution path:

```text
ChemBench v3 baseline controller
  -> SupervisedAgentRequestV2(arm=control, resolved_context=None)
  -> TaskRequest
  -> Rollout
  -> Gateway
  -> CodexHarness
  -> managed_science runtime
  -> managed Codex subscription transcript
  -> private evaluator
```

The control request uses the same public prompt renderer, model, reasoning
effort, timeout, managed image, managed Codex executable, transcript capture,
zero-tool policy, and private evaluator as the evolved Test.  The only intended
candidate-visible difference is the absence of the three evolved targets.

No parent artifact workspace is materialized or uploaded.  The candidate sees
neither Test targets nor evaluator state.  The private evaluator runs only after
the candidate completion has been sealed in the append-only Test ledger.

## Frozen invariants

- Test contains 450 tasks, 50 from each of the nine frozen categories.
- Test UID order must equal the completed evolved Final Test receipt.
- Each task admits at most one valid control completion.  Infrastructure
  attempts with no completion may be retried and remain auditable.
- Full Rollout/Gateway health and schedulability are required before a new
  candidate submission.  Once Rollout has sealed a terminal result, postflight
  checks verify the exact process and managed-container identity without using
  transient free-capacity or heartbeat state to discard that completion.
- A bounded preflight health outage is converted to an auditable
  no-completion infrastructure retry.  Source, process, container, image, or
  executable identity drift remains a hard fail-closed condition.
- `reflector_calls`, Core evolution jobs, context resolutions, and all three
  artifact counts remain zero.
- The control context binding contains no target or artifact IDs.
- Candidate execution must use the expected managed Codex SHA-256
  `a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902`.
- The completed evolved Test evidence remains read-only and is used only after
  control completion to compute aggregate paired metrics.

## Outputs

The new namespace writes:

- `ControlFinalTestBaselineAmendmentV3`
- `ControlFinalTestBaselinePaidExecutionPlanV3`
- `ControlFinalTestBaselineSourceCompatibilityReceiptV3`
- an append-only private control consumption ledger
- `ControlFinalTestBaselineCompletionReceiptV3`
- aggregate category and paired correctness statistics

The paired report records baseline-only correct, evolved-only correct, both
correct, and both wrong counts.  It may estimate a paired difference, but a
single stochastic sample per arm is still an exploratory comparison rather than
a leaderboard claim.

## Validation

Before paid execution, focused tests prove that the dry run is Test-only, the
control request has no context or artifact IDs, the official managed harness is
still selected, no adapter-level `codex exec` path exists, the completed evolved
run is unchanged, and the default v3 evolved-only path remains unchanged.
