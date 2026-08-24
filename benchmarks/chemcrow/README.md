# OpenEvo ChemCrow Task-Local Integration

This package treats ChemCrow as benchmark tasks plus a chemistry-tool environment. It does
not instantiate the legacy `ChemCrow` GPT-4/LangChain agent. Candidate, Reflector, evolution
evaluator, and final evaluator remain separate OpenEvo runtime/provider roles with frozen
model settings.

The mandatory item protocol is:

`bare S0 -> baseline -> feedback -> one native Reflector job -> same-item evolved -> seal -> reset`

No artifact, feedback, trajectory, tool cache, or active Core store is inherited by the next
item. A pair-scoped tool server may replay only identical canonical tool calls within that
baseline/evolved pair. Real metrics reject fixture and mock observations.

## Development

```bash
uv sync --python 3.11 --extra dev
uv run pytest -q
uv run openevo-chemcrow extract-tasks \
  --runs-root ../../../vendor/chemcrow-runs \
  --output benchmarks/chemcrow/tasks.jsonl \
  --safety-output benchmarks/chemcrow/safety_tasks.jsonl \
  --audit-output benchmarks/chemcrow/leakage_audit.json
uv run openevo-chemcrow preflight \
  --config configs/preflight.yaml \
  --no-model-calls
```

`run` is fail-closed. It requires both `--allow-paid` and
`CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST` before any Candidate,
Reflector, or evaluator dispatch. `resume` never replays an ambiguous claimed phase.
