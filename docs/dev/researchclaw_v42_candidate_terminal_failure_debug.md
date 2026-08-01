# ResearchClaw V42 candidate terminal-failure debugging

## Scope and evidence boundary

- Workspace: `/home/lhy-h/work/researchclaw_openevo`
- Immutable failed namespace: `rcb_oe_v0_community17_dev17_fresh_v42`
- Failed unit: `Earth_004`, attempt `a0`
- The V42 supervisor, Core databases, receipts, and model-call evidence remain
  immutable. This repair does not resume or rewrite that run.
- Private benchmark instructions, evaluator evidence, credentials, and model
  output are outside this document.

## Observed call chain

```text
CommunityTrainingSupervisor(CANDIDATE_RUNNING)
  -> ProductionTrainingOperations.ensure_candidate
  -> CoreScienceTaskOwnerV2 executes one immutable Attempt
  -> ScienceAttemptExecutorV2 polls Rollout
  -> Rollout returns one terminal ERROR SessionResult
  -> _wait_for_terminal_result raises ScienceAttemptExecutionV2Error
     with a closed failure_authority
  -> ScienceAttemptExecutorV2 exception cleanup calls cancel_task
  -> Rollout rejects DELETE because the task is already terminal
  -> HTTPStatusError masks the typed terminal failure
  -> Core persists attempt_execution_internal_error without failure_authority
  -> adapter rejects the failed lifecycle authority
  -> supervisor stops at CANDIDATE_OPERATION_FAILED_BEFORE_SEAL
```

## Durable findings

- Core, workspace, project-head, runtime-isolation, and source identities were
  valid immediately before Candidate execution.
- The Candidate SessionResult is terminal `ERROR` with a nonzero first-step
  exit. Its closed execution metadata proves model and benchmark execution had
  started, but the retained result contains no transcript trace.
- The Core service log records an untyped `HTTPStatusError`, while the Attempt
  row records `attempt_execution_internal_error` and has no
  `failure_authority`.
- Rollout's terminal DELETE contract returns conflict for an already-terminal
  task. The executor's cleanup therefore masks the original typed failure.
- Historical network or login causation cannot be proven from the retained
  redacted evidence. The same Core generation and credentials completed the
  preceding attempts, so restarting network or login services is not justified
  by current evidence.
- A zero-model diagnosis after the failure confirmed that the owned release
  host was still running and the actual `openevo` service account's Codex login
  status was ready. No login or service restart was performed.

## Minimal repair

Treat a `ScienceAttemptExecutionV2Error` carrying a validated
`failure_authority` as proof that Rollout terminal evidence has already been
received. Exception cleanup must not issue cancellation in that case. Failures
without terminal authority keep the existing cancellation requirement.

This preserves both sides of the contract:

1. nonterminal or ambiguous failures still require owned Rollout cancellation;
2. terminal Session failures retain their original closed failure authority;
3. cleanup cannot replace a scientifically meaningful failure with a transport
   exception from an invalid terminal DELETE;
4. no Candidate, Judge, Reflector, or other model call is introduced.

The repair advances the managed Daemon identity to lifecycle 95. The deployment
ledger rejects a different release at the same lifecycle, so the increment is
required for a future fixed bundle to replace lifecycle 94 without weakening
downgrade or same-lifecycle replacement protection.

## Verification plan

- Make the existing terminal-error fixture reject cancellation exactly like the
  production Rollout API and reproduce the current masking failure.
- Apply the minimal executor correction and rerun that focused test.
- Run adjacent Science execution/owner tests and relevant ResearchClaw
  production-operation tests.
- Run `git diff --check` and inspect the final diff.
- Any future live validation must use a fresh namespace and explicit paid-run
  authorization; V42 is not resumable evidence.
