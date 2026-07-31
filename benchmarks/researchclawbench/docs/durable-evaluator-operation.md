# Durable Community evaluator operation

`DurableEvaluatorOperation` is the evaluator-only exactly-once boundary for a
billed ResearchClawBench Judge call. It is deliberately separate from the
training supervisor state database:

1. validate the request, protocol, model, provider, API base and tracked scorer
   tree identities;
2. commit `INVOCATION_COMMITTED` to a `synchronous=FULL` SQLite journal;
3. invoke the official scorer exactly once;
4. commit the raw private response, private feedback layers and closed public
   receipt in one transaction;
5. materialize immutable JSON mirrors from the committed database row.

If a process exits after step 2 but before step 4, recovery raises
`AmbiguousJudgeInvocation`. It never calls the Judge again. If step 4 completed
but the Supervisor lost the response, recovery returns the same receipt without
another billed request.

## Production construction

The production evaluator port can be constructed without reading a secret:

```python
from openevo_researchclawbench.community_evaluator import (
    DurableCommunityEvaluatorPort,
    build_production_community_evaluator,
)

bundle = build_production_community_evaluator(
    config=config,
    authority_root=run_root / "evaluator_private" / "durable_authority",
)
evaluation_port = DurableCommunityEvaluatorPort(bundle)
```

The builder verifies that the protocol scorer commit equals the
ResearchClawBench `HEAD`, that the tracked `evaluation/` tree is clean, and
hashes its Git tree identity. It selects `secrets/judge.env` only after checking
directory mode `0700` and file mode `0600`; only the evaluator child reads the
file. The OpenRouter model slug and API base are checked inside that child.

`DurableCommunityEvaluatorPort` implements the `recover(request, key)` and
`execute(request, key)` shape used by `ProductionOperationPort`. The attachment
port obtains private feedback through
`read_feedback_for_attachment(idempotency_key=..., expected_sha256=...)`; raw
Judge reasoning is never returned in the public evaluator receipt.

The official scorer currently does not expose exact provider request count,
token usage or cost. These fields are recorded as the literal `"unavailable"`.
They must not be replaced with a guessed checklist length, `request_count=1`, or
`cost_total_usd=0`.

## Frozen official 40-task authority

`DurableOfficialUnifiedScorerAuthority` reuses the same per-task invocation
fence and private response store. It accepts only the protocol-ordered set of
40 sealed manifests, one attempt per task, and the exact frozen no-feedback
policy. Invalid candidate artifacts receive a terminal zero without a Judge
call; valid artifacts are scored once. The public aggregate contains no
per-task feedback, while its evaluator-private receipt is hash-bound. A lost
aggregate response recovers already completed task operations and never calls
the Judge twice. The separate v11 official budget is 40 candidate calls, at
most 40 task scoring operations, one unified aggregation, and per-operation
runtime caps; it does not reuse the Community wall-time allowance.
