# Taskwise Online Evolution V1 Runbook

This runbook covers the paired non-standard protocols:

- `repeated_session_control_v1`
- `taskwise_online_evolution_v1`

The offline `chembench4k_frozen_generalization_v2` protocol remains a separate
third comparator. Its manifests, artifact lifecycle, result roots, and
statistics must not be mixed with this online experiment.

Every online report must display:

```text
ONLINE_TASKWISE_EVOLUTION
TEST_TIME_ADAPTATION
NON_STANDARD_CHEMBENCH4K_PROTOCOL
NOT_A_STANDARD_LEADERBOARD_SCORE
```

## Fixed per-task treatment

Every task receives exactly three completions from three new immutable Codex
sessions. There is no correctness-based stopping, extra attempt, best-round
selection, or score-based artifact selection.

```text
Round 0 with the stream head
  -> private evaluation and safe feedback
  -> Core update 1
  -> approved and Core-resolved M_i_1
Round 1 with M_i_1
  -> private evaluation and safe feedback
  -> Core update 2
  -> approved and Core-resolved M_i_2
Round 2 with M_i_2
  -> private evaluation
  -> fixed final task outcome
  -> carry M_i_2 to the next task in the same stream
```

The control arm also opens three new sessions per task, but creates no Core
dataset, job, artifact, context, or memory injection.

## Fail-closed evolution policy

An evolution update is atomic. Core job failure, reflector security violation,
artifact registration failure, validator rejection, promotion failure, context
resolution failure, digest mismatch, predecessor mismatch, or a stale/forked
artifact terminates the formal run as:

```text
TASKWISE_EVOLUTION_UPDATE_FAILED
```

The runner does not start the next round or task, retry an executed reflector,
reuse predecessor memory, skip the update, or substitute another artifact.
A failed run is not resumable.

Every session separately verifies the expected and actual Core artifact ID,
resolved-memory SHA-256, context-resolution digest, and injected-memory
SHA-256. Any mismatch terminates the run as:

```text
TASKWISE_CONTEXT_BINDING_VIOLATION
```

## Text-memory capacity contract

The capacity limit is part of the protocol and configuration hash:

```yaml
memory_limits:
  max_utf8_bytes: 16384
  max_estimated_tokens: 4096
  max_items_per_section: 24
  required_sections:
    - Do
    - Avoid
    - Validate
    - When Applicable
    - Retired Or Superseded
```

The validator applies these limits to the complete artifact payload before
promotion. Runtime injection never truncates an oversized artifact. Token
estimation is deterministic and bound into private lineage evidence. The
reflector must consolidate duplicate rules and move obsolete advice into
`Retired Or Superseded`; a violating artifact is rejected rather than trimmed.
Public reports contain only aggregate byte/token/item counts, never memory
text.

## Canary9

Canary9 is one nine-task stream with one task per category.

```text
CANARY_MECHANISM_AND_SECURITY_ONLY
NOT_A_PERFORMANCE_GATE
```

Its only gates are:

- all 27 task sessions completed;
- all 18 online evolution updates completed;
- zero security violations;
- zero context-binding violations;
- zero artifact-chain violations;
- zero cleanup residual roots.

Canary outcomes must not be used to tune memory, prompts, or thresholds.

Cost inventory:

```text
control task calls:    27
online task calls:     27
online reflector calls: 18
total calls:           72
```

## Pilot500 streams

Pilot500 comprises ten independent streams of fifty tasks. Each stream starts
from generation-zero memory and owns an independent result, Core state,
checkpoint, and artifact namespace. Memory is carried only inside a stream.
Task order is frozen and paired between control and online arms.

The suite orchestrator inspects each public stream state before acting:

- completed streams are skipped without reopening a session;
- safely resumable streams resume from their verified checkpoint;
- missing streams start from generation-zero memory;
- terminal security, evolution-update, artifact, or context-binding failures
  stop the suite and permanently mark it failed.

An incomplete comparison reads only public run-state evidence and returns
`INCOMPLETE` or `FAILED` with `NO_GO`; it does not read partial private
predictions. Only ten completed control/online stream pairs enter the private
paired statistics.

The primary statistical unit is the stream. Per-task statistics are emitted
only as:

```text
DESCRIPTIVE_ONLY_DEPENDENT_TASK_OBSERVATIONS
```

The preregistered GO gate is:

```text
completed_streams == 10
security_violations == 0
artifact_validation_failures == 0
context_binding_violations == 0
mean_stream_round2_delta >= 0.02
stream_level_ci_lower > 0
at_least_7_of_10_streams_positive == true
```

Cost inventory:

```text
control task calls:      1500
online task calls:       1500
online reflector calls:  1000
total calls:             4000
```

The online arm therefore uses additional paid reflector compute.

## Non-paid validation

Run from the OpenEvo repository root:

```bash
benchmarks/chembench/scripts/prepare_chembench4k_v2_runtime.sh
.venv/bin/python -m pytest benchmarks/chembench/tests -q -p no:cacheprovider
.venv/bin/python -m ruff check --no-cache benchmarks/chembench
benchmarks/chembench/scripts/dry_run_taskwise_online_v1.sh
benchmarks/chembench/scripts/dry_run_frozen_v2.sh
```

These commands must report zero GPT/Codex model calls.

## Paid sequence

Do not run these commands until source identity, clean-commit, dataset,
executor, and private-manifest gates all pass:

```bash
benchmarks/chembench/scripts/run_repeated_control_canary9_v1.sh
benchmarks/chembench/scripts/run_online_evolution_canary9_v1.sh
benchmarks/chembench/scripts/compare_online_canary9_v1.sh

benchmarks/chembench/scripts/run_repeated_control_pilot500_v1.sh
benchmarks/chembench/scripts/run_online_evolution_pilot500_v1.sh
benchmarks/chembench/scripts/compare_online_pilot500_v1.sh
```
