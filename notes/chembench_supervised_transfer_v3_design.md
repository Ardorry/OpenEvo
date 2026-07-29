# ChemBench supervised transfer v3 design

`chembench_supervised_transfer_v3_three_answer_online_only` is an exploratory,
evolved-only transfer protocol. It reuses the frozen v2 Train/Test membership and
the verified benchmark-local OpenEvo execution components, but owns separate v3
run, state, result, receipt, ledger, and report namespaces.

The Train state machine is fixed:

```text
Round 0 -> Cycle 1 -> Round 1 -> Cycle 2 -> Round 2 Final
```

Each cycle makes one structured reflector call and commits three independent Core
jobs/artifacts (`text_memory`, `skill_bundle`, `agent_system`). A successor is
carried only after all three validators, promotions, and context resolutions
succeed. Core's existing three-target ABI remains the trusted implementation; its
stream-position primitive is parameterized with `updates_per_task=2` for v3 while
retaining the v2 default of 3.

After 50 tasks per category, the final Cycle-2 context is frozen. Test performs one
Evolved-only completion for each of the unchanged 450 Test UIDs. It has no
reflector, feedback, target update, or cross-question state. An append-only v3
ledger prevents a second valid completion.

There is deliberately no Control, Probe, paired statistic, McNemar test, or causal
improvement claim. Reports describe Train round behavior and the frozen evolved
system's absolute unseen-Test performance.

Candidate sessions still enter OpenEvo as `TaskRequest -> Rollout -> Gateway ->
CodexHarness`; reflector execution remains `Core planned job -> worker -> codex_cli
provider -> typed artifacts`. The already verified managed runtime service is
shared infrastructure, rebuilt and source-bound after the v3 commit; experiment
state and artifact chains are not shared with v2.
