# OpenEvo ChemCrow Task-Local Integration

This package treats ChemCrow as benchmark tasks plus a chemistry-tool environment. It does
not instantiate the legacy `ChemCrow` GPT-4/LangChain agent. Candidate, Reflector, evolution
evaluator, and final evaluator remain separate OpenEvo runtime/provider roles with frozen
model settings.

All four Codex roles are admitted only through OpenEvo Core Rollout/Gateway and execute in the
immutable managed Docker runtime. Host `codex exec`, custom shells, custom runtimes, and caller
MCP injection fail closed. ChemCrow tools are exposed through a pair-scoped receipt-producing
REST bridge because the Core subscription contract forbids caller MCP servers.

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
  --output tasks.jsonl \
  --safety-output safety_tasks.jsonl \
  --audit-output reports/LEAKAGE_AUDIT.json
uv run openevo-chemcrow preflight \
  --config configs/preflight.yaml \
  --no-model-calls
```

## Sealed paper-compatible evaluator

The optional paper evaluator is a final-evaluation-only protocol. It reconstructs the publicly
recoverable ChemCrow EvaluatorGPT semantics (two students, one 0--10 grade per student, task
completion plus overall chemistry thought-process correctness, strengths, weaknesses,
justification, and feedback). The original evaluator prompt was not published, so the protocol
is labeled `CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1`, never a verbatim reproduction.

Its route is independent from Candidate evolution:

`PaperEvaluatorHarness container -> dedicated OpenEvo Rollout/Gateway -> OpenRouter auth shim -> openai/gpt-4`

The historical notebook answers can be extracted only after a 14-task completed-run audit passes.
They are never placed in `tasks.jsonl`, Candidate input, Reflector input, evolution feedback, or
the regular final evaluator. Preflight performs zero model calls:

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow paper-evaluator-preflight \
  --config benchmarks/chemcrow/configs/paper_evaluator.full-v3.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_EVALUATOR_PREFLIGHT.json \
  --no-model-calls
```

The current `full-v3` is stopped and contains only 12 fully sealed pairs, so this command correctly
returns `BLOCKED`. Do not run the paid command until a complete 14-task authority exists.

The published human-expert layer is separate again. ChemCrow Source Data Fig. 4 records three
scores on a 0--10 scale (`Chemically accurate`, `Quality of reasoning`, `Task completed`) from four
expert chemists. The existing OpenEvo 0--4 three-dimensional judge remains an internal diagnostic,
not the paper's human score. Once the composite 14-task authority is sealed, prepare 42 blinded
comparisons and four response forms per comparison without a model call:

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow paper-human-review-prepare \
  --config benchmarks/chemcrow/configs/paper_human_review.composite-v1.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_HUMAN_REVIEW_PREPARE.json \
  --no-model-calls
```

The condition mapping and randomization secret are permission-0600 and private; public packet IDs
are opaque and only the secret hash is exposed. Reviewer packets contain final responses only,
randomized as A/B, with no runtime trajectory or system identity. Four completed independent
reviews per comparison are required for a paper-comparable human result; fewer reviews are
exploratory.

Those 12 pairs pass a standalone sealed-subset audit and can be preserved. The smallest 14-task
closure is the frozen `paper_repair.v1.yaml` task-14/task-15 run followed by
`paper-composite-audit`; it remains fail-closed until the user explicitly authorizes the duplicate
task-14 pair that replaces the interrupted, never-sealed pair.

Local RXN services use exact image IDs:

```bash
docker compose -f configs/rxn_sandbox.services.yaml up -d
curl -fsS http://127.0.0.1:8300/openapi.json
```

`run` is fail-closed. It requires both `--allow-paid` and
`CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST` before any Candidate,
Reflector, or evaluator dispatch. `resume` never replays an ambiguous claimed phase.
