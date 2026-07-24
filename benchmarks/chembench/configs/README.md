# ChemBench configurations

The primary taskwise online comparison uses:

```text
{control,online}_canary9_taskwise_online_v1.yaml
{control,online}_pilot500_stream_{00..09}_taskwise_online_v1.yaml
{control,online}_pilot500_stream_suite_taskwise_online_v1.yaml
```

The control protocol is `repeated_session_control_v1`; the treatment protocol
is `taskwise_online_evolution_v1`.  Both fix three attempts, two inter-round
slots, `stop_when_correct=false`, final score round 2, and concurrency one.
Control has zero evolution updates, no evolution method, and no carried
memory.  Online has two registered `text_memory_expel_reflector` updates and
carries approved memory only within the deterministic stream.  The pilot suite
fixes ten independent streams, 50 tasks per stream, and
`reset_memory_between_streams=true`.

Every taskwise config carries:

```text
ONLINE_TASKWISE_EVOLUTION
TEST_TIME_ADAPTATION
NON_STANDARD_CHEMBENCH4K_PROTOCOL
NOT_A_STANDARD_LEADERBOARD_SCORE
```

These results must not be reported as standard ChemBench4K or leaderboard
scores.  The control and online configs are paired against one shared public
manifest and one evaluator-only private manifest for each scope.

The old `{control,online}_pilot500_taskwise_online_v1.yaml` pair binds only
the byte-preserved `legacy_single_stream` manifest.  Public pilot entrypoints
do not use it; its provenance is marked `LEGACY_SINGLE_STREAM_PILOT_MANIFEST`
and `NOT_PRIMARY_STATISTICAL_PROTOCOL`.

`source_commit: BIND_CLEAN_HEAD_AT_LAUNCH` is a tracked, non-self-referential
template marker.  The paid CLI accepts it only when `benchmarks/chembench` is
clean, then records the actual current commit in the immutable run binding and
resume state.  An explicit 40-character commit may be used instead and must
equal current `HEAD`.

The third, offline generalization comparator is frozen v2.  Its six
configurations match:

```text
{baseline,evolved}_{canary18,pilot500,full}_frozen_v2.yaml
```

They bind:

- `chembench4k_frozen_generalization_v2`;
- `AI4Chem/ChemBench4K` at
  `f8ad41a980170f4c5d0cc97e57722d06887c8f53`;
- `gpt-5.5`, reasoning effort `medium`, Codex CLI `0.144.6`;
- the official category-local five-shot renderer and first-capital evaluator;
- concurrency one, no model retry, and the zero-tool executor policy.

The paired parity gate permits only these arm differences:

```text
arm
run_name
output_directory
evolution.enabled
frozen artifact identity
```

Before artifact construction, `pilot_protocol_hash` and evolved artifact
fields contain `REQUIRED_AT_FREEZE`; paid execution therefore fails closed.
The registered Core evolution step freezes all six configs to one artifact and
one protocol binding.

`text_memory_evolution_v2.yaml` documents the paid, explicit dev-LOO/Core
artifact job.  Merely loading or validating that file never starts the job.

All YAML files other than the taskwise configs above, the two preserved
single-chain taskwise configs, the six frozen-v2 configs, and
`text_memory_evolution_v2.yaml` belong to legacy
`jablonkagroup/ChemBench` task-local online recovery.  They are retained
unchanged as historical evidence and are classified:

```text
LEGACY_JABLONKAGROUP_CHEMBENCH
NON_STANDARD_TASK_LOCAL_ONLINE_RECOVERY
NOT_A_CHEMBENCH4K_RESULT
NOT_A_CORE_REGISTERED_OPENEVO_RESULT
```

Legacy configs, manifests, and results must not be imported by the formal v2
runner or included in v2 statistics.
