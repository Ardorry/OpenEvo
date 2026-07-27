# ChemBench4K taskwise online evolution benchmark

The primary comparison in this package is the paired taskwise online stream:

1. `repeated_session_control_v1` repeats each task for a fixed three-attempt
   budget without creating an evolution job or artifact.
2. `taskwise_online_evolution_v1` uses the same tasks, order, three-attempt
   budget, and no-early-stop rule, with two registered text-memory updates and
   memory carried through the ordered stream.
3. `chembench4k_frozen_generalization_v2` remains the third, offline
   generalization comparator.  Its frozen dev-only artifact and one-completion
   test semantics are unchanged.

All three use the immutable `AI4Chem/ChemBench4K` revision
`f8ad41a980170f4c5d0cc97e57722d06887c8f53`.

> **STANDALONE_MAINTAINER_BENCHMARK_EVIDENCE**
>
> **NOT_OPENEVO_PRODUCT_RELEASE_ATTESTATION**

The taskwise arms must be displayed with all four markers:

```text
ONLINE_TASKWISE_EVOLUTION
TEST_TIME_ADAPTATION
NON_STANDARD_CHEMBENCH4K_PROTOCOL
NOT_A_STANDARD_LEADERBOARD_SCORE
```

Their repeated-round and test-time-adaptation metrics are research outcomes,
not standard ChemBench4K accuracy and not leaderboard-comparable.

## Supervised frozen-transfer protocol

`chembench_supervised_transfer_v1` is a separate research-only protocol. Its
nine category-local reflectors may see full Train questions, options,
predictions, targets, correctness, and predecessor memory. Each category has
50 Train, 10 Probe, and 50 primary Test items. Each supervised update makes one
reflector call whose closed response is promoted through three independent Core
lifecycle targets: `text_memory`, `skill_bundle`, and `agent_system`. Probe is
evaluated at frozen checkpoints 0/10/20/30/40/50 without feedback or evolution;
final Test compares one no-context completion with one completion using the
complete frozen three-target category context.
Both task sessions and reflectors use the Codex package version declared by
OpenEvo Core's managed runtime contract. The repository pins
`@openai/codex@0.144.1`; the benchmark runtime preparation materializes that
exact package under its private state root, verifies its package metadata and
native executable digest, and gives each execution boundary a separately
materialized executable with that same pinned digest. Host `PATH` discovery is
not admitted for this protocol.
It must be reported as `RESEARCH_ONLY_SUPERVISED_EVOLUTION`,
`TRAIN_ANSWER_SUPERVISION`, `FROZEN_UNSEEN_TEST_TRANSFER`, and
`NOT_A_STANDARD_LEADERBOARD_SCORE`.

ExposureTaxonomyV2 distinguishes manifest listing from actual item exposure.
Holdouts exclude any UID with a task-model attempt, completion, private
evaluation, supervised-reflector input, or item-level human review. Mere
manifest listing, deterministic prompt rendering, and aggregate-only review do
not exclude a UID. The immutable V1 blocked receipt is retained as evidence of
the earlier over-conservative classification. The V2 manifests and isolation
receipt live in `manifests/supervised_transfer_v1/`; private manifests remain
mode `0600` and untracked.

Non-paid preparation and validation use:

```bash
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh prepare
benchmarks/chembench/scripts/prepare_supervised_transfer_v1_runtime.sh
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh verify
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh dry-run
```

Runtime preparation writes a deterministic managed-Codex receipt containing
the Core-declared package/version and package/native-binary digests. Every
preflight authority, formal run state, and final freeze binds that identity;
missing installation, host-only Codex, or binary drift fails before a model
call.

Paid execution is deliberately split into two non-resumable run identities:

```bash
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh \
  run-preflight --run-id <fresh-preflight-run-id>
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh \
  verify-preflight --run-id <terminal-preflight-run-id>
benchmarks/chembench/scripts/run_chembench_supervised_transfer_v1.sh \
  run-formal --run-id <fresh-formal-run-id> \
  --preflight-run-id <terminal-preflight-run-id> \
  --test-manifest test_primary
```

`run-preflight` alone performs update smoke, both canaries, Probe smoke, and
checkpoint-0 Probe. It emits an identity-closed authority containing only
aggregate checkpoint-0 statistics. `run-formal` refuses mismatched authority
and its first paid session is Control Train task 0 round 0; it never reruns a
preflight call or imports another run's Train rows. A private protocol-global,
append-only ledger claims the frozen Test manifest before the first Test call,
so a fresh run ID cannot consume Primary Test a second time. Recovery Test 01
requires a source-change invalidation backed by the failed Test run; score does
not authorize recovery. Both commands require the dedicated non-editable Core
runtime.

Control never creates or receives any of the three evolution targets. Online
Train carries the complete category-local context to the next immutable
session; Online Probe and Final Test inject the matching frozen three-target
checkpoint. A missing, mismatched, or partially resolved target fails closed,
and neither Probe nor Test can evolve any target. Formal Online Train therefore
uses 900 reflector calls but 2,700 verified Core jobs, approved artifacts, and
context resolutions.

### Supervised three-target transfer v2

`chembench_supervised_transfer_v2` is isolated from v1 and uses only one frozen
Train split and one permanently frozen Test split. Each Train item has four new
task sessions and three intervening evolution cycles. One structured reflector
call per cycle supplies distinct `text_memory`, `skill_bundle`, and
`agent_system` components; all three components then pass independent verified
Core plan-bound jobs, typed artifact validation, promotion, and context
resolution. Thus the paid plan is 1,800 Control task calls, 1,800 Online task
calls, 1,350 reflector calls, and 900 final-Test task calls (5,850 total), while
the three target lifecycles still produce 4,050 Core jobs and artifacts. That
5,850 figure counts answer and reflector calls only. Every
candidate session also runs one real Core-managed subscription readiness
`codex exec` before its answer call. The normal paid-call baseline is therefore
10,350 calls. Core permits one additional readiness attempt only when the first
probe is a validated refusal, so the reserved fail-closed maximum is 14,850;
the planned receipt reports the 5,850 answer/reflector calls, 4,500 required
readiness calls, and 4,500 retry capacity separately.

Candidate sessions are submitted through the OpenEvo Rollout/Gateway
`TaskRequest` path and the official `CodexHarness` in `managed_science` runtime.
The resulting controller trajectory binds the SHA-256 identity of the verified
OpenEvo Rollout JSONL transcript. The supervised packet projection remains a
private benchmark transformation, but every Core event and artifact lineage
binds that ordered source-execution provenance digest; transcript content is
not copied into the lineage.

The candidate boundary uses the Core subscription harness capability profile.
Its zero-tool rule is enforced as a transcript-audit fail-closed condition: any
observed tool event invalidates the task/run. It is not described as preventive
removal of every Core harness capability. The reflector boundary is separately
Bubblewrap-isolated and hard-disables tool, web-search, shell, approval, and MCP
features. Both boundaries independently materialize and verify the same pinned
Codex native executable digest and never fall back to the host `PATH`.
The canonical v2 skill renderer uses category-scoped chemistry names only; it
does not embed benchmark, manifest, dataset, run, or filesystem identifiers in
the promoted `SKILL.md` payload.

An explicitly unpaired online-only Pilot500 entry point is also available for
mechanism and descriptive analysis. It uses no control evidence, cannot issue
a paired GO/NO-GO conclusion, and writes all required
`ONLINE_ONLY_PILOT500` / `NO_CONTROL_ARM` / `NO_CAUSAL_CONTROL_COMPARISON`
classifications into its authority, suite state, stream markers, and final
report. Full 4009-item execution is now explicitly source-authorized by the
2026-07-26 experiment handoff, but remains gated by the unchanged paired Pilot500
GO receipt and its source-bound launch authorization.

The operational checklist, fail-closed states, capacity contract, and cost
inventory are documented in
[`TASKWISE_ONLINE_RUNBOOK_V1.md`](TASKWISE_ONLINE_RUNBOOK_V1.md).

## Primary taskwise online lifecycle

Both arms use a deterministic `canary9`.  The primary pilot uses ten
independent, stratified `pilot500_stream_00` through `pilot500_stream_09`
streams with 50 tasks each and identical paired ordering.  Each stream starts
from generation-zero memory and owns a separate runner, result root, Core
database, checkpoint, and artifact namespace.  Each task always receives
attempts 0, 1, and 2;
`stop_when_correct=false`, and the reported final-round metric is round 2.
The control has zero evolution updates and no cross-task memory carry.  The
online arm has two real Core evolution updates per task and carries approved
text memory into the next task.  Control must never create a placeholder
update, job, or artifact.

The public stream manifests contain no target.  Their paired private manifests
are evaluator-only, mode `0600`, ignored by Git, and excluded from source
identity.  The online stream has a separate protocol family, seeds, manifest
paths, result roots, and statistics from frozen-generalization v2.

The former single 500-task memory chain is preserved byte-for-byte under
`manifests/taskwise_online_v1/legacy_single_stream/` and marked
`LEGACY_SINGLE_STREAM_PILOT_MANIFEST` and
`NOT_PRIMARY_STATISTICAL_PROTOCOL`.  It is not used by the primary pilot
entrypoints or stream-clustered inference.

The tracked taskwise configs use the non-self-referential source marker
`BIND_CLEAN_HEAD_AT_LAUNCH`.  Before constructing either the executor or Core
port, the CLI requires a clean benchmark package and binds the actual current
commit into run state.  This avoids a config/commit hash cycle while preserving
strict source-commit verification on resume.

## Offline frozen-generalization comparator

The evaluated model is `gpt-5.5` with reasoning effort `medium`.  The
baseline and evolved test arms each make one completion per selected test
item.  The only treatment difference is a single, pre-approved, frozen Core
`text_memory` artifact.  No test label, score, completion, or error signal is
fed into evolution or a later test item.

### Frozen v2 lifecycle

```text
45 category-local dev leave-one-out trajectories
  -> OpenEvo Core event/dataset
  -> immutable plan-bound job
  -> verified registered text_memory_expel_reflector method
  -> typed Core artifact registration
  -> benchmark artifact validation and Core promotion
  -> Core context resolution
  -> immutable resolved text memory
  -> paired frozen test arms
```

The Core integration uses `EvolutionStore`, the verified method registry,
`worker.run_once`, typed artifact registration, promotion, and
`resolve_materialized_context`.  The v2 path does not import or call the legacy
adapter-local `SafeEvolutionReflector`, `CandidateArtifact`,
`ApprovedArtifact`, or task-local online runner.

## Dataset and prompts

The frozen snapshot lives outside this package at:

```text
../data/chembench4k/AI4Chem_ChemBench4K/
  f8ad41a980170f4c5d0cc97e57722d06887c8f53/
```

It contains 45 dev and 4009 test items across the nine official categories.
Every test prompt uses category-local dev indices `0..4` in fixed order.
Development trajectories use four-shot leave-one-out prompts, so an item's own
target never appears in its prompt.

The official metric uses OpenCompass-compatible first-uppercase parsing:
the first character for which `str.isupper()` is true is the prediction,
including a non-choice letter such as `T`.  Such a prediction is parsed but
scores as wrong unless it equals the private target; the parser never skips
forward to a later A/B/C/D.
`strict_single_letter_parser` additionally reports whether the complete
trimmed output is exactly one of `A/B/C/D`.

## Security boundary

`PrivateChemBench4KTask` holds the target.  `PublicChemBench4KTask`, rendered
test prompts, executor requests, transcripts, public results, and public
manifests do not.

Each Codex invocation gets a new private root with isolated `HOME`,
`CODEX_HOME`, XDG directories, SQLite, temp directory, private event storage,
and an empty work directory.  Shell, file, MCP, network/web, browser, plugin,
app, subagent, external-tool, and unknown tool-like events fail closed as
`SECURITY_TOOL_USE_VIOLATION`.  A violation forbids retry, replacement
completion, and resume.

The Core reflector has an additional `ReflectorExecutionBoundaryV2`.
The registered worker resolves the npm launcher to the actual static Codex
binary and reaches it only through a single-use `PATH` wrapper.  Bubblewrap
mounts that binary, required certificate/name-resolution files, one read-only
45-record dev LOO input, isolated auth, empty work/output directories, and no
repository, test dataset, results, notes, prior transcripts, or ordinary user
home.  Raw JSON events are retained privately with mode `0600`; malformed or
tool-like events terminate the Core job without retry.  Model transport uses
the shared network namespace, while web/plugins/apps/MCP/subagents/shell are
disabled and audited fail closed.

## Two-commit freeze order

The source/artifact hash dependency is intentionally acyclic:

1. The first commit contains benchmark source, tests, scripts, placeholder
   config templates, deterministic public manifests, this documentation, and
   `chembench_source_manifest_v2.json`.  It excludes the dataset snapshot,
   results, private manifests, runtime/Core state, auth, event logs, the real
   artifact, and the execution receipt.
2. After paid dev trajectory collection and artifact generation, a second
   commit is reserved for the six frozen config bindings, the regenerated
   source manifest, and any necessary public artifact metadata.  The real
   artifact payload and Core state remain untracked.
3. `benchmark_execution_receipt_v2.json` is generated only after the second
   commit.  It is runtime evidence excluded from source identity, so receipt
   generation and reverification do not dirty or recursively change the source
   tree.  Its recorded source commit must be the second commit.

## Non-paid validation

From the OpenEvo repository root:

```bash
benchmarks/chembench/scripts/prepare_chembench4k_v2_runtime.sh
benchmarks/chembench/scripts/dry_run_frozen_v2.sh
.venv/bin/python -m pytest benchmarks/chembench/tests -q -p no:cacheprovider
.venv/bin/python -m ruff check --no-cache benchmarks/chembench
```

The dedicated runtime installs the locally built, pinned OpenEvo wheel
non-editably.  This is required by the verified Core method registry.

## Paid execution order

Do not run these until the benchmark source is committed (or a written
source-manifest-only acceptance exists), the artifact has been built and
validated, and `freeze_benchmark_receipt_v2.sh` returns a positive receipt.

```text
build_dev_loo_dataset_v2.sh
run_text_memory_evolution_v2.sh
validate_frozen_text_memory_v2.sh
freeze_benchmark_receipt_v2.sh
run_baseline_canary18_frozen_v2.sh
run_evolved_canary18_frozen_v2.sh
compare_canary18_frozen_v2.sh
run_baseline_pilot500_frozen_v2.sh
run_evolved_pilot500_frozen_v2.sh
compare_pilot500_frozen_v2.sh
run_baseline_full_frozen_v2.sh
run_evolved_full_frozen_v2.sh
compare_full_frozen_v2.sh
```

Full-run launch is blocked unless the unchanged paired pilot report satisfies
the preregistered GO gate.

## Legacy protocol

The older `jablonkagroup/ChemBench` adapter, configurations, and partial
results are retained as immutable legacy evidence:

```text
LEGACY_JABLONKAGROUP_CHEMBENCH
NON_STANDARD_TASK_LOCAL_ONLINE_RECOVERY
NOT_A_CHEMBENCH4K_RESULT
NOT_A_CORE_REGISTERED_OPENEVO_RESULT
```

That path is `task_local_online_recovery_v1`; it is disabled by default and
must not be mixed with v2 manifests, artifacts, runners, or statistics.
