# Composed Control Test baseline suffix recovery v3

## Purpose and evidence status

`composed-control-baseline-recover` is a narrow recovery amendment for the
infrastructure-interrupted, no-evolution Control Test baseline. It does not
resume or edit a failed process. It opens the failed parent and all earlier
baseline namespaces read-only, creates a fresh run namespace, and executes only
the frozen Test suffix whose UIDs have no accepted completion in the selected
parent.

The selected parent is
`stv3-control-final-test-baseline-dockerfix-20260801T190638Z`. Selection is fixed
without reading correctness: it is the unique longest contiguous completion
prefix, it uses the latest reviewed runtime-only source transition, and all
earlier completed prefixes are strict subsets of it. The parent contributes
ordinals `0..143`; recovery begins at ordinal `144` and plans 306 candidate
calls.

This amendment cannot erase the historical fact that earlier failed namespaces
produced 90 candidate-attempt events: 43 overlapping completions and 47
no-completion attempts, including 45 public infrastructure-retry events. The 43
completions comprise 27 in the first baseline namespace and 16 in the reconnect
namespace. Those rows are excluded by run identity, never selected by
correctness, and disclosed in a separate immutable receipt. The final report
must distinguish:

- zero duplicate completions inside the composed 450-item sample;
- 43 excluded historical completion events covering 27 unique Test UIDs; and
- 90 excluded historical attempt events, including 47 no-completion attempts;
- a descriptive audited recovery, not a strict single-execution baseline.

## Admission and immutable-parent audit

The recovery audit fails closed unless all of the following hold:

- the selected parent is `FAIL_CLOSED` in `CONTROL_TEST` with exact failure code
  `RUNTIME_SERVICE_PROCESS_IDENTITY_INVALID`;
- security, context, and artifact findings are zero;
- model, reasoning effort, managed image, candidate executable, config, split,
  prompt order, evaluator, parser, generation-zero context, and no-evolution
  counters match the frozen baseline authority;
- private and public events contain one ordered completion for every ordinal
  `0..143`, no completion for ordinal `144`, and no later UID;
- the append-only parent ledger has the same identity and sequence, with exactly
  one sealed completion for each accepted UID and only no-completion attempts at
  the recovery boundary;
- each earlier baseline namespace has the same Test order and no feedback or
  evolution; its private attempt events, append-only ledger, public completion
  and retry events, run counters, failure code, and terminal event agree; every
  earlier completion UID is contained in the selected prefix; and
- the selected parent is the unique longest prefix. Ties or any sibling
  completion beyond it are terminal compliance failures.

The audit records canonical tree and ledger hashes for the selected parent and
tree hashes for every excluded namespace. Recovery rechecks these identities
before each new candidate submission and before final composition. It never
attaches, copies, or modifies a parent database, ledger, event file, result, or
state file.

## Candidate and evaluator boundary

Every new suffix item preserves the existing execution path:

```text
Control baseline suffix controller
  -> SupervisedAgentRequestV2(arm=control, resolved_context=None)
  -> TaskRequest
  -> Rollout
  -> Gateway
  -> CodexHarness
  -> managed_science runtime
  -> sealed transcript/completion
  -> append-only suffix ledger
  -> private evaluator
```

The candidate receives no Test target, evaluator state, prior completion,
correctness, aggregate score, evolved artifact, or recovery-history content.
The controller never uses correctness to choose a parent, retry an item, or
decide whether to continue. Infrastructure retry remains legal only when the
official terminal evidence proves that no completion exists. A completion,
tool/security event, identity mismatch, source drift, or ambiguous terminal
state is never retried.

## Fresh namespace and outputs

The recovery creates a new run ID, state/result roots, event streams, attempt
IDs, and `control_test_suffix_ledger_v3.jsonl`. It emits:

- `AcceptedControlBaselinePrefixReceiptV3`;
- `DiscardedControlBaselineIncompleteTaskReceiptV3`;
- `ExcludedHistoricalControlBaselineRunsReceiptV3`;
- `ControlBaselineSuffixRecoveryPlanV3`;
- a source-compatibility receipt;
- a suffix-ledger receipt; and
- `ControlBaselinePrefixSuffixCompositionReceiptV3`.

The final composition contains exactly one selected completion for each frozen
Test UID in order. It carries the classifications:

```text
AUDITED_CONTROL_BASELINE_SUFFIX_RECOVERY
PARENT_COMPLETION_PREFIX_REUSED
HISTORICAL_OVERLAPPING_COMPLETIONS_DISCLOSED
NO_EVOLUTION_ARTIFACTS
NOT_A_SINGLE_UNINTERRUPTED_RUN
DESCRIPTIVE_RECOVERY_NOT_STRICT_SINGLE_EXECUTION
```

This implementation accepts only the named original parent. Chaining from a
failed recovery run is not implicit; it requires another evidence-specific
amendment and fresh namespace.

The original `composed-control-baseline-run` source-compatibility gate remains
closed at this recovery commit. It cannot be used to silently execute all 450
items again; only the recovery-specific entry point admits this source change.

## Source compatibility and validation

The recovery source change is limited to this document, recovery orchestration,
CLI dispatch, focused tests (including the original full-run fail-closed gate),
and the regenerated source manifest. Dataset,
split, Test order, prompt, model, reasoning effort, harness, executor,
runtime-service identity checks, evaluator, parser, baseline controller, and
single-completion ledger semantics must remain byte-identical to the selected
parent commit.

Before paid execution, zero-model validation must prove the 144/306 boundary,
parent and sibling immutability, exact overlap accounting, generation-zero
context, zero evolution calls/artifacts, no direct Codex or database bypass,
source-manifest closure, runtime-service health, and a fresh run ID.
