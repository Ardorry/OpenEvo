# Per-item supervised one-step evolution protocol

## Scope

This branch measures per-item supervised evolution gain on the canonical 17
Community tasks. It does not use accumulated successor state, a frozen
composite, or an Official split.

For every task, the benchmark creates an independent Supervisor namespace:

```text
generation-zero
  -> fresh Candidate A
  -> sealed baseline
  -> independent baseline Judge
  -> baseline + current-task GT
  -> one native evolution cycle
  -> memory + skill + agent-system
  -> fresh Candidate B on the same original task
  -> sealed evolved result
  -> independent evolved Judge
  -> paired result
  -> active-state reset
```

## Candidate boundary

Both Candidate passes use `CoreV2CandidatePort`, Core `/v2/tasks`, a real
`TaskRequest`, `CodexHarness`, and the managed GPT-5.5 runtime. A pass always
creates a fresh task, attempt, harness session, and workspace. The production
route has no host Codex fallback.

Baseline starts from artifact-empty generation zero. Evolved starts from a new
public-task workspace whose Project Head contains exactly the three admitted
same-task artifacts. The baseline workspace and Candidate conversation are not
continued.

## Supervision and Judge boundary

Raw GT is loaded only after the baseline Judge completes. The GT attachment is
private input to the existing Core native successor path. Its task-local
overlay is not activated for either Candidate. The admitted content scanner
must pass before the three artifacts can be rebound to Candidate B.

Judge receipts are statistical outputs. The evolution request contains
`judge_feedback=None`; Judge score, reasoning, and raw response are excluded.
Candidate and reflector environments do not receive Judge credentials.

## Same-task artifact binding

Core's successor Project Head correctly owns the evolved artifact set but also
pins the immutable baseline workspace. `PerItemEvolvedWorkspacePort` therefore
creates an unused generation-zero destination from the same public task and
uses Core's existing historical-restore authority to bind only that task's
three admitted artifacts to the clean workspace. This Core operation is
same-task only and cannot seed the next benchmark task.

## Reset and Community loop

An item closes only after two sealed/scored attempts, one evolution receipt,
three artifact registrations, and one paired result. `ITEM_CLOSED -> ITEM_RESET`
clears all active Project Head, composite, artifact, workspace, Candidate,
reflector, and GT references while preserving immutable evidence.

The Community command creates a separate `DurableTrainingControl` for each
task. Before dispatch it writes an isolation receipt showing an empty artifact
set, no active head, no prior workspace/GT/session, and no previous-task fork.
No object returned by one task is passed to the next task's control.

## Failure semantics

No score-based retry or second evolution is allowed. A failed task retains its
evidence and stops the Community command without presenting a complete paired
sample. A fresh task namespace is required for any authorized engineering
retry. Archived evidence is never used as active context.
