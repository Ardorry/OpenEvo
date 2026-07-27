# ChemBench supervised transfer protocol conformance audit

Protocol: `chembench_supervised_transfer_v1`
Audit decision: `MECHANISM_MISMATCH_FOUND`

## Multi-target mechanism amendment

The first audit below evaluated the original memory-only treatment. A later
mechanism review confirmed that baseline commit
`cd319dc5f5528ff1ad60f381e44ac9c0fa095aa7` did not evolve `skill_bundle` or
`agent_system`. The benchmark-local implementation now uses one supervised
reflector call and three verified Core target lifecycles per update, with
complete same-category predecessor chains and frozen three-target Probe/Test
contexts. See `notes/supervised_transfer_multitarget_mechanism_audit.md`.

The overall decision remains `MECHANISM_MISMATCH_FOUND` only because a fresh
paid preflight authority has not yet been produced for the new source. The old
single-target preflight authorities cannot be reused.

## Conforming mechanisms

### Frozen split

PASS. The V2 split is deterministic and byte-stable. Train, Probe, Primary
Test, Recovery Test 01, and Reserve are pairwise disjoint. Holdouts exclude all
historically actually exposed UIDs. Private manifests are `0600`. Current
failed/interrupted runs did not mutate a manifest or split receipt.

### Control Train semantics

PASS in the task loop. `_run_control_train` uses the same balanced Train order
as Online, creates exactly three sessions for each item, always passes
`memory=None`, performs no evaluator-to-agent feedback injection, and creates
no Core update. There is no early stop, correctness-dependent round count, or
best-round selection.

### Online Train semantics

PASS in the task loop. Each category owns a separate formal Core bridge and
head. Round 0 is followed by update 1, round 1 by update 2, and round 2 uses the
approved second memory. The second memory becomes only that category's next
task predecessor. Each update uses real Core event ingestion, dataset creation,
plan-bound job execution through the verified registry, typed artifact
registration, validation, promotion, and context resolution.

### Supervised reflector input

PASS. `SupervisedEvolutionPacketV1` contains the current Train question,
A/B/C/D options, raw and parsed prediction, target letter, correct option text,
correctness taxonomy, category, round, predecessor memory identity/body, and
trajectory identity. The packet is projected only from `_run_one_online_task`.
Probe and Test call `_execute_session` directly and never construct a packet.
Observed reflector UIDs are a Train-only subset.

### Reflector filesystem boundary

PASS. The supervised Core bridge selects the supervised
`ReflectorExecutionBoundaryV2`. Bubblewrap mounts one bounded packet dataset,
isolated auth, transport CA material, empty work/output roots, and the static
Codex binary. Repository, data, results, state, notes, ordinary home, shell,
MCP, web, apps, plugins, subagents, and other tools are unavailable or fail
closed.

### Category memory and validator

PASS. Nine formal bridge/state/head namespaces are independent. The artifact
itself is checked for category heading, exact sections, rule status/evidence,
capacity, token-aligned question/option overlap, answer maps, UID/path markers,
lineage, predecessor identity, and context binding. Runtime injection does not
truncate an over-limit artifact.

### Probe isolation

PASS. Probe checkpoints are scheduled at 0/10/20/30/40/50. Both arms make one
session per Probe item. Probe calls do not create packets or jobs, and a Core
job-count invariant is checked before/after each checkpoint. Training heads are
not changed by Probe results.

### Final-memory freeze

PASS within one uninterrupted run. All nine checkpoint-50 heads must exist.
The frozen receipt binds artifact, payload, context, source, split, and primary
Test manifest identities before the first Test call. Test sessions do not
create updates, and a Core job-count invariant is checked.

### Stage order inside one complete run

PASS for ordering only. The coded order is:

1. update smoke;
2. online canary;
3. control canary;
4. Probe smoke;
5. formal Core initialization;
6. checkpoint-0 Probe;
7. Control Train;
8. Online Train with checkpoints 10/20/30/40/50;
9. final freeze;
10. Final Test;
11. reporting.

Thus the current code does not skip Control Train or run formal Online Train
first.

## Nonconforming mechanisms

### M1: no reusable preflight authority

FAIL. Every invocation of the only paid `run` entry point unconditionally
repeats smoke, both canaries, Probe smoke, and checkpoint-0 Probe. It does not
look for an identity-closed receipt and cannot start a fresh formal run directly
at Control task 0. This is why run-05 re-entered online canary after the run-04
TLS failure. It was not an intentional service-recovery gate decision.

### M2: stage receipts are incomplete for reuse

FAIL. Public events, private executor success receipts, and the checkpoint-0
memory receipt provide evidence, but no single closed receipt binds all of:
source commit, config digest, split receipt, model/reasoning effort, Codex CLI,
executor policy, verified Core registry, stage counts, cleanup, zero findings,
and Probe no-evolution. Therefore the old stages cannot be safely reused by a
new formal run without new code.

### M3: external pause is not terminally persisted

FAIL. TERM leaves run-05 with `status=INITIALIZED` even though its process is
gone. The run is preserved and will not be resumed, but the state machine lacks
an immutable `PAUSED_BY_USER`/fail-closed receipt binding the last public/private
event digests and process-stop evidence.

### M4: global Test single-use is not enforced across runs

FAIL. `test_primary_use_receipt.json` is created inside one run directory only.
A separate fresh run can create another receipt for the same primary Test. No
atomic protocol-global ledger rejects a second use, records source-bug
invalidation, or switches only to the pre-frozen recovery manifest. Test has
not yet been accessed, so this defect is repairable without split mutation.

### M5: stage transitions are not a closed state machine

FAIL. `_set_stage` accepts arbitrary strings and the CLI has only a monolithic
paid entry point. Tests cover split, packet, memory, statistics, and dry-run but
do not prove allowed stage transitions, preflight reuse, formal task-0 start,
cross-run isolation, or global Test single-use.

## Overall decision

`MECHANISM_MISMATCH_FOUND`

The scientific treatment mechanics are largely conforming, and no data leak or
Test access occurred. The mismatch is orchestration/receipt authority, not the
frozen split or supervised learning protocol. Real calls remain paused until
M1-M5 are fixed, tested, committed, and re-audited.

## Remediation status before the second audit

The benchmark-local correction now implements:

- separate terminal `run-preflight` and authority-consuming `run-formal`
  entry points;
- a source/config/split/model/Codex/executor/registry-bound preflight authority
  with aggregate-only checkpoint-0 rows;
- a closed run-mode-aware stage transition table and exact stage counters;
- explicit session admission by stage, arm, and Train/Probe/Test partition;
- Control task 0 round 0 as the first possible formal paid session;
- no checkpoint-0 model call or cross-run Train-row import in the formal run;
- an append-only private Test-manifest ledger with one Primary claim, evidence-
  gated completion, and source-change-only Recovery authorization;
- retry of a Test item only for a sanitized executor failure with
  `completion_observed=false` and `retry_allowed=true`;
- an exclusive administrative pause receipt that never rewrites run state.

The dedicated supervised/bridge/executor/reflector/context test list passed
295 tests. A later focused supervised/Core list passed 161 tests after the
final reporting and Test-ledger checks. The full benchmark collection produced
827 passes and 58 passing subtests; its 28 failures are pre-existing legacy
workspace-layout fixtures that still resolve parent-level `OpenEvo/...`,
external provenance notes, or unmaterialized legacy private manifests. No
failure imports or exercises a changed supervised-transfer module. Those
failures are recorded as `FAILED_PREEXISTING_LAYOUT_FIXTURES` and are not
repaired in this isolated protocol change.
