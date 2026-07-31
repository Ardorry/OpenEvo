# Composed category-shard Final Test

`composed-final-test` is the explicit Final Test stage for the two-round,
one-evolution v3 category-shard recovery. It consumes the audited nine-category
composition rooted at `stv3-temperature-controlfix-recovery-20260731T105523Z`.

Before any Test call, the controller reopens every source category SQLite file
with `mode=ro&immutable=1`, validates the complete 50-checkpoint chain and all
150 successful jobs, promoted typed artifacts, lineage bindings, and
materialized contexts, and checks the composition receipt against the closed
source evidence. It then issues the exact final task-49 three-target context for
each category and freezes a 27-target receipt in a fresh Final Test namespace.

The controller does not copy or attach a source database, copy or register a
source artifact, reconstruct an evolution update, run a reflector, or update a
target. Source artifacts remain bound to their originating category shard. The
candidate path is unchanged:

```text
Test task -> TaskRequest -> Rollout -> Gateway -> CodexHarness -> managed Codex
```

Test is single-pass through the append-only v3 consumption ledger. It has 450
planned candidate calls, zero reflector calls, zero Core jobs, no feedback, and
no Test-time evolution. The resulting experiment must be reported as category-
shard recovery, not as one uninterrupted Train/Test process.
