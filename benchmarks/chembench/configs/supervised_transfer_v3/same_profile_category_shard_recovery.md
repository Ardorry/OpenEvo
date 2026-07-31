# V3 two-round same-profile category-shard recovery

`supervised_transfer_v3_two_round_one_evolution_category_shard_recovery` is an
explicit recovery amendment for the active two-round/one-evolution Train
profile. It is category-atomic: it is not a task, round, cycle, process,
artifact, context, or SQLite resume.

## Parent evidence boundary

For failed parent `stv3-one-update-online-20260730T165823Z`, the read-only audit
accepts the exact closed prefix:

1. `Name_Conversion`
2. `Property_Prediction`
3. `Mol2caption`
4. `Caption2mol`
5. `Product_Prediction`

Each accepted category must contain 50 closed tasks, 100 candidate
completions, 50 structured reflector calls, 150 successful Core jobs, 150
promoted typed artifacts, and 150 materialized contexts. Its checkpoint chain,
lineage, validators, promotions, context digests, event ledger, and immutable
SQLite integrity must all close with no active lease or staged artifact.

The parent `Retrosynthesis` category is discarded in full. Its eight closed
tasks, task 8 Round-0 completion, eight successful reflectors, failed ninth
reflector invocation, Core rows, artifacts, contexts, and paid usage remain
immutable accident evidence. None enters recovery context, accuracy,
learning-curve, target-growth, or final-result statistics.

## Fresh recovery execution

The recovery creates a new run, event ledger, Core stream, SQLite store,
artifact registry, and attempt namespace for category indices 5 through 8. Its
first model task is `Retrosynthesis` task 0 Round 0 with generation-zero
`text_memory`, `skill_bundle`, and `agent_system`. No parent database is
attached or copied, no parent artifact is registered or copied, and no parent
context or category head is injected.

Within every new category, the original successful-call semantics remain:

```text
Round 0
-> one structured reflector call
-> three independent Core jobs, validators, promotions, and context resolutions
-> Round 1 Final
```

Candidate calls still use `TaskRequest -> Rollout -> Gateway -> CodexHarness`
with the managed runtime. Reflectors still use the Core planned-job worker and
the managed Codex provider.

## Recovered timeout notice

Codex CLI 0.144.1 can emit an exact bounded notice shaped as
`Reconnecting... <1..5>/5 (request timed out)` and then successfully emit an
agent response and `turn.completed`. The transcript parser accepts this notice
only when the event has exactly `type` and `message`, the global recoverable
transport-event bound is respected, a non-empty completed agent message
exists, and the turn closes with a valid usage record. An incomplete timeout,
`turn.failed`, malformed schema, out-of-range attempt, or arbitrary error still
fails closed.

This compatibility changes transport-event classification only. It does not
change prompts, model, reasoning effort, evaluator, answer parser, supervised
packet, reflector prompt, target schema, validator, admission/promotion,
context resolution, category order, task order, or round/cycle semantics.

## Composition and accounting

After the four new categories close, the composition receipt binds five parent
closed shards and four recovery closed shards. All nine entries must share the
same config, split, model, executor identity, managed Codex digest, and
category-internal protocol. Source commits are recorded separately because the
recovery source includes this transport fix and recovery orchestrator.

The receipt is labeled:

```text
CATEGORY_SHARD_RECOVERY
FIVE_PARENT_CLOSED_SHARDS_REUSED
PARTIAL_PARENT_SHARD_FULLY_DISCARDED
NOT_A_SINGLE_UNINTERRUPTED_RUN
```

The remaining Train plan contains 200 tasks, 400 candidate answers, 200
reflectors, and 600 Core jobs. Parent discarded usage remains visible in cost
accounting but is excluded from the 1,350 answer/reflector calls in the composed
full Train result.
