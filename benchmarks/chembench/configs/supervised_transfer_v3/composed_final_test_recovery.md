# Composed Final Test suffix recovery v3

`composed-final-test-recover` is an explicit protocol amendment for recovering
an infrastructure-interrupted, evolved-only composed Final Test. It is not a
resume of the failed process and is not reported as one uninterrupted run.

The controller accepts only a unique ordered completion prefix from a
`FAIL_CLOSED` parent whose failure code is `EXECUTOR_STALLED`. It validates the
parent run tree, append-only ledger, frozen 27-target receipt, Test UID order,
completion digests, zero-finding state, and the first incomplete UID. Parent
state, results, events, and ledger are opened read-only and their hashes must
remain unchanged throughout recovery.

The recovery run has a fresh run ID, result/state namespace, attempt IDs, and
suffix ledger. It re-audits the nine closed Train shards and re-materializes
their exact final contexts. The resulting artifact-set digest must equal the
parent frozen artifact-set digest. Only the suffix beginning at the first UID
without a parent completion is executed:

```text
accepted parent completions 0..N-1
-> discard all no-completion attempts for N as infrastructure evidence
-> fresh recovery attempt for N
-> execute N..449 once to valid completion
-> explicit prefix/suffix composition receipt
```

The candidate path remains `TaskRequest -> Rollout -> Gateway -> CodexHarness`
with the managed Codex binary. Test feedback, reflectors, Core jobs, artifact
updates, score-driven retries, and re-execution of accepted parent completions
remain forbidden. Infrastructure retries are allowed only before a completion,
under the existing frozen stall window.

Source compatibility is fail-closed. The recovery commit may only add this
orchestration, its CLI dispatch, tests, documentation, and regenerated source
manifest. Dataset, split, Test order, prompt, model, reasoning effort, harness,
evaluator, parser, frozen artifacts, and single-pass semantics must be byte-for-
byte or digest compatible with the parent execution.

Final reports must carry all four markers:

```text
AUDITED_FINAL_TEST_SUFFIX_RECOVERY
PARENT_COMPLETION_PREFIX_REUSED
INCOMPLETE_PARENT_TASK_REEXECUTED
NOT_A_SINGLE_UNINTERRUPTED_RUN
```
