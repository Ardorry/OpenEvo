# Supervised transfer v3: two-round, one-evolution amendment

This profile is an explicit protocol amendment for a fresh Online Train run. It
does not change or reinterpret any completed or interrupted three-answer v3
run.

## Train lifecycle

Each Train task executes exactly:

```text
Round 0 candidate
-> one supervised reflector call
-> three typed Core artifact jobs
-> validation, promotion, and context materialization
-> Round 1 final candidate
```

The profile therefore fixes two candidate sessions and one evolution cycle per
Train item. "One overfit" means one supervised update between the initial and
final answer; it does not mean an in-session update or access to a future task.
The three targets remain `text_memory`, `skill_bundle`, and `agent_system`.

The profile is selected with
`chembench_supervised_transfer_v3_two_round_one_evolution.yaml` and has protocol
ID `chembench_supervised_transfer_v3_two_round_one_evolution_online_only`.
The legacy `chembench_supervised_transfer_v3.yaml` profile remains three
candidate sessions and two evolution cycles so historical runs remain
auditable.

## Isolation and compatibility

- A run using this amendment must use a fresh run ID and start every category
  at task 0 with generation-zero targets.
- No closed or partial shard from a three-answer/two-cycle run is compatible
  with this profile.
- The category-shard recovery controller rejects this profile fail closed.
- Candidate execution, reflector execution, three-target lifecycle, evaluator,
  parser, dataset, split, model, reasoning effort, timeout, managed Codex
  binding, and final single-pass Test semantics are unchanged.
- The default legacy v3 config remains unchanged unless the amended config is
  passed explicitly.

## Planned counts

For 450 Train and 450 final Test items:

```text
Online Train candidate calls: 900
Online Train reflector calls: 450
Core jobs / typed artifacts / context resolutions: 1350 each
Final evolved Test candidate calls: 450
Answer plus reflector calls: 1800
Required candidate readiness calls: 1350
Minimum total model calls including readiness: 3150
```

The final report reads the train-round and evolution-cycle counts from the
immutable run state. It emits one correctness transition (`0_to_1`) for this
profile while retaining two transitions for the legacy profile.

## Validation

Before paid execution, run the v3 focused tests, v2 regression tests, package
tests, Ruff, shell syntax checks, and the explicit amended-config dry run. A
paid smoke with the same source commit, config digest, runtime-services
identity, model, and managed Codex identities is required before formal Online
Train.
