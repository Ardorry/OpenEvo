# Supervised transfer v3 category-shard recovery

`supervised_transfer_v3_category_shard_recovery` is an explicit amendment for
recovering after a host failure at a category boundary. It is not a task,
round, cycle, artifact, database, or process resume.

## Accepted and discarded evidence

The amendment accepts only a category whose 50 tasks, 150 candidate
completions, 100 reflector calls, 300 Core jobs, 300 typed target artifacts,
and 300 context resolutions are all closed and whose SQLite and JSON/JSONL
state pass read-only integrity checks. For the 2026-07-30 crash, that shard is
`Name_Conversion` from parent run
`stv3-online-formal-20260730T031520Z`.

The parent `Property_Prediction` shard is discarded in full. Its completions,
reflector calls, jobs, artifacts, contexts, and checkpoints remain immutable
cost and crash-audit evidence, but are not recovery inputs and are excluded
from accuracy, learning-curve, target-growth, and final-result statistics.

## Execution boundary

The recovery runner creates a new run root and opens fresh Core bridges only
for frozen category indices 1 through 8. Its first execution is
`Property_Prediction`, task ordinal 0, round 0. Because the new category bridge
has no head, all three targets are generation zero. No parent artifact is
registered or copied, no parent SQLite database is attached or copied, and no
parent context is injected.

The normal v3 entry point remains unchanged. Category selection exists only on
the explicit `category-recover-online` command, whose only accepted start index
is 1. Within each selected category, the original three candidate sessions,
two evolution cycles, single structured reflector call per cycle, three Core
jobs, validators, promotions, and context resolutions are reused unchanged.

## Composition and accounting

After all eight new category shards close, a typed composition receipt binds
the parent `Name_Conversion` shard and the eight new shards. Every entry must
match the same config, split, model, executor, managed Codex digest, and
category-local protocol. The receipt labels the result as category-shard
recovery and never as one uninterrupted run.

Actual paid usage keeps three disjoint counters: included parent
`Name_Conversion` calls, discarded parent `Property_Prediction` calls, and new
recovery calls. Discarded calls never disappear from cost accounting and never
enter statistical results. The Train-shard composition receipt records 2,377
answer/reflector calls actually consumed at that boundary: 250 accepted parent
calls, 127 discarded parent calls, and 2,000 new remaining-Train calls. It also
records the separate 450-call composed Final Test plan, yielding a projected
minimum of 2,827 actual calls and 2,700 calls in the eventual final result.

Final Test remains a composed-shard stage: source category artifacts must stay
bound to their originating closed shard. The remaining-shard Train runner does
not import the accepted parent carry into its namespace.

`category-recovery-dry-run` performs the immutable parent audit, verifies the
frozen remaining-category order, renders the 400-task schedule, and reports
zero model calls without creating a run namespace.
