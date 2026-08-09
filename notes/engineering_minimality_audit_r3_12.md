# Engineering Minimality Audit — Preservation-First R3.12

- `R3_PRE_CHANGE_BASE`: `abde9d853ff740a5862c757689547e805bd079e4`
- audited head: `6238ed1af307920bb78bce2c4a9de472283d0bf7`
- scope: R3 preservation, successor recovery, failure diagnostics, and provenance admission

## Classification

### A. Correct mechanism required

- `baseline_success_trace.py`: deterministic Candidate trajectory extraction.
- `baseline_evidence_capsule.py`: Candidate provenance and achievement ledger.
- `evaluation_feedback.py`: the existing sanitized boundary plus one composition step.
- `task_specific_artifact_quality.py`: per-artifact preservation admission.
- `baseline_equivalence.py`: pre-Judge capability-equivalence admission.
- Core task-local preservation scope, native planned jobs, registry admission, and fresh successor materialization.

### B. Tests required

- preservation-first projection, artifact fidelity, GT-isolation, native prompt/output capture,
  provenance, reset, and baseline-equivalence regression tests.

### C. Generic diagnostics retained

- `3096be3e1`: retains only a closed, non-secret successor failure class. This is useful
  production recovery authority, not task-specific instrumentation.
- failed-successor safe abandonment/reset remains because it proves the exact Core transition
  is terminal, non-retryable, artifact-free, and owned before benchmark state is cleared.

### D/G. One-shot or recovery-only behavior removed or narrowed

- Removed new-call authority for `R3_CONTENT_ADMISSION_SCOPE_CONFLICT`. New closures use
  `CORE_SUCCESSOR_TERMINAL_FAILURE`; the old label is accepted only to verify immutable R3.11
  evidence.
- Replaced four overlapping Reflector views (`prompt_contract`, `actionable_candidate_evolution`,
  embedded sanitized feedback/capsule, and fresh-workspace guidance) with one bounded
  `BalancedEvolutionContext`. The full sanitized feedback and capsule remain separate durable
  authorities and are not duplicated inside the model-visible view.
- No production `r3_successor_reconciliation_capacity_exhausted` branch exists. The historical
  label was not made authoritative.

### E/F. Superseded or duplicate abstraction

- The prior compact prompt-contract helper is superseded by `BalancedEvolutionContext`.
- Task-local agent-system generation no longer reuses the long generic multi-round handbook
  prompt. It receives only the balanced feedback and a short submission-constraint contract.

### H. Unrelated code

- No unrelated Core, Desktop, scorer, Judge, GT, Community batch, or Official path is changed.

## Diagnostic commits

- `3096be3e1`: retain. It solves observability of fail-closed successor errors without exposing
  exception text or changing retry semantics.
- `6238ed1af`: retain as the monotonic release identity corresponding to that behavior. The next
  behavior-changing managed release must advance the lifecycle again; it must not reuse 105.

## Resulting authority map

- GT/content literal collision scan: filtered scan-source set.
- artifact provenance: complete sealed source artifact set and canonical source-payload digest.
- Reflector input: one balanced composition of Candidate success plus admitted feedback.
- artifact admission: per-artifact role and preservation gate.
- Judge precondition: Candidate/public-only baseline-equivalence gate.

One structure now owns each semantic; no parallel recovery semantics or task-specific answer
logic is introduced.

## Pre-live R3.14 closure

- A real R3.13 replay showed that a one-script/one-achievement ledger still allowed five
  baseline figures to collapse to two. The ledger now binds each report-linked scientific
  evidence role and output class, excludes inventory/manifest/access-status bookkeeping, and
  preserves a bounded maximum of twelve required evidence chains.
- Baseline equivalence now binds an evidence role to its concrete output class and report
  reference, plus a baseline output-count floor. A figure can no longer stand in for a missing
  numeric table, and unrelated files cannot satisfy an achievement.
- The native renderer's existing 2,000-character whole-feedback bound was proven to truncate a
  real 11-achievement balanced context before strengths, diagnoses, and SuccessTrace. Adapter
  composition cannot split one authoritative feedback attachment without parallel semantics.
  Core therefore retains the 2,000-character default and uses a closed 16,000-character bound
  only for already-authorized `task_local_preservation` jobs. No target ID, benchmark name, or
  answer-specific branch was added.
- The baseline-equivalence invalidation transition is retained as one narrow terminal lifecycle,
  because it was required to archive the unjudged R3.13 Candidate and clear exact task-local
  ownership without deleting immutable evidence. It does not retry a model or authorize a new
  run.
