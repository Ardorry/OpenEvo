# ChemBench configurations

Formal v2 configurations are the six files matching:

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

All other YAML files in this directory belong to legacy
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
