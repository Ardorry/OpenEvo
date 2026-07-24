# ChemBench4K frozen generalization benchmark

The formal protocol in this package is
`chembench4k_frozen_generalization_v2`, a standalone OpenEvo maintainer
benchmark over the immutable `AI4Chem/ChemBench4K` revision
`f8ad41a980170f4c5d0cc97e57722d06887c8f53`.

> **STANDALONE_MAINTAINER_BENCHMARK_EVIDENCE**
>
> **NOT_OPENEVO_PRODUCT_RELEASE_ATTESTATION**

The evaluated model is `gpt-5.5` with reasoning effort `medium`.  The
baseline and evolved test arms each make one completion per selected test
item.  The only treatment difference is a single, pre-approved, frozen Core
`text_memory` artifact.  No test label, score, completion, or error signal is
fed into evolution or a later test item.

## Formal v2 lifecycle

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
