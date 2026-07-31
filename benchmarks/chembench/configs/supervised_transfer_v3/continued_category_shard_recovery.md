# Continued category-shard recovery

`continued-category-recover-online` is the chained recovery entry point for the
active two-round, one-evolution supervised-transfer v3 profile.

It accepts only an exact, fully closed category prefix from an immutable
fail-closed parent. The parent's current partial category is excluded in full
from recovery input and final statistics. A new run starts that category at
task 0, round 0 with generation-zero `text_memory`, `skill_bundle`, and
`agent_system` state.

The controller:

- opens parent SQLite databases read-only and immutable for audit;
- verifies closed task, completion, Core job, artifact, promotion, lineage, and
  context-resolution counts for every accepted category;
- never attaches or copies a parent database;
- never registers or imports a parent artifact;
- creates a new run namespace, Core stream, event ledger, and artifact registry;
- preserves the existing TaskRequest/CodexHarness candidate path and Core
  planned-job reflector path;
- records accepted-prefix, discarded-partial-shard, compatibility, execution
  plan, paid usage, and final composition receipts.

The ordinary v3 entry point and the first same-profile recovery at category
index 5 remain unchanged. `--start-category-index` identifies the first
discarded/re-executed category and is validated against the parent's exact
closed prefix.

## Artifact-validation failure recovery

The same category-atomic controller also accepts the audited
`TASKWISE_ARTIFACT_VALIDATION_FAILED` shape produced by
`stv3-temperature-recovery-20260731T071432Z`. It requires one uniquely bound,
unpromoted `text_memory` artifact containing exactly one NUL control byte,
twenty closed Temperature checkpoints, no task-20 Round-1 attempt, no lease or
staged output, and no partial skill, agent-system, or context lifecycle. The
twenty-one successful reflector calls and all discarded Core work remain in
actual paid-usage accounting, but the whole Temperature shard is excluded.

The supervised structured renderer now canonicalizes non-semantic C0/C1
control characters to spaces before producing any of the three target
payloads. The private raw Codex event stream and source-message digest remain
unchanged, while the renderer records
`supervised_multitarget_structured_render_v4`. Historical v3 receipts remain
readable, and accepted parent artifacts are scanned to prove that this change
does not alter any already accepted target. Recovery still starts
`Temperature_Prediction` at task 0, round 0 with generation-zero targets and
does not attach or copy the parent database.
