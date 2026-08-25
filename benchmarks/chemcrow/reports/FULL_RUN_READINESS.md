# ChemCrow × OpenEvo full-run readiness

Generated: 2026-08-26 (Asia/Shanghai)

## Final verdict

```text
READY_FOR_FULL_CHEMCROW_TASK_LOCAL_RUN
PAPER_EVALUATOR_ROUTE_READY
THREE_ARTIFACT_LIVE_PREFLIGHT_READY
READY_FOR_FULL_RUN
```

The readiness work is complete. The authoritative 14-task run, production 42-call GPT-4
evaluation, and human review were not started.

## OpenRouter request-delta finding

The v5-v8 and old minimal-direct HTTP 403s were route-specific, not account failures. The exact
causal delta was `httpx` environment-proxy inheritance:

- `trust_env=False`: identical neutral request body, direct HKG egress, HTTP 403, router
  `attempt=0`, no selected endpoint.
- validated environment proxy enabled: identical body SHA256, SJC egress, HTTP 200,
  `attempt=1`, provider `OpenAI` selected.

The shim now requires `--use-environment-proxy` for this deployment, rejects credential-bearing
proxy URLs, records only a sanitized proxy identity, and captures the actual outgoing request shape
immediately before transmission. Core's incoming
`response_format={"type":"json_object"}` contract is still validated and claim-bound; only the
unsupported upstream transport field is omitted. `user`, logprob/token-ID fields, tools, plugins,
and other forbidden fields are absent. See `OPENROUTER_REQUEST_DIFF_AUDIT.json`.

Historical V2 HTTP 403 cause remains **UNPROVEN**. The legacy GPT-4 `response_format` HTTP 400
incompatibility remains independently **PROVEN**.

## Paper Evaluator Core smoke

Fresh v10 passed the complete route:

```text
PaperEvaluatorHarness -> dedicated Rollout -> dedicated Gateway -> auth shim
-> OpenRouter -> openai/gpt-4 -> first-party OpenAI
```

| Property | Result |
|---|---|
| HTTP | 200 |
| model | `openai/gpt-4` |
| provider | `OpenAI` |
| temperature | 0.1 |
| fallback | false |
| JSON valid | yes |
| `DualStudentAssessment` valid | yes |
| usage receipt | present, 257 prompt + 136 completion tokens |
| reported cost | $0.01587 |
| production ledger | untouched |

Additional request-delta debugging used 4 attempts, 3 successful completions, one unreceipted 403,
and $0.03252 proven total cost. No automatic retry occurred. Debug claims and receipts are separate
from production. Evidence: `OPENROUTER_GPT4_CORE_PAID_SMOKE_V10.json`.

## Live three-artifact preflight

Fresh live-v4 completed representative tasks `02`, `03`, and `06` through real Core-managed GPT-5.5
Candidate, evaluator, and Reflector executions.

| Evidence | Count |
|---|---:|
| sealed pairs | 3 |
| Memory jobs | 3 |
| Skill jobs | 3 |
| AgentSystem jobs | 3 |
| unique artifacts | 9 |
| sibling-isolation records | 9 |
| exact-three injection receipts | 3 |
| bare-S0 reset receipts | 3 |
| live observations | 71 |
| allowed cache replay observations | 6 |
| MOCK | 0 |
| fixture | 0 |

All three pairs passed the completed-run audit. Failed/partial v1-v3 preflight identities were
preserved and not reused. The evidence root is
`/home/lhy-h/work/chemcrowrun/runs/readiness-three-pipeline-live-v4`; the value-free digest summary
is `THREE_ARTIFACT_LIVE_PREFLIGHT_V4.json`.

Live execution exposed and repaired baseline empty-inventory ordering and Core lifecycle races.
Runtime injection is now read back and published before Candidate execution and read back again
after execution to reject mutation. Periodic cleanup cannot race a dispatcher-owned session, late
DELETE cannot rewrite terminal cancel authority, and Codex canary failures retain only a closed
sanitized category.

## Protocol and legacy audit

- Candidate and all three Reflectors use GPT-5.5 through native Codex subscription transcript mode,
  OpenEvo Rollout/Gateway/Core, and the pinned managed runtime image.
- G1 requires empty artifact inventory. G2 requires exactly one task-local `text_memory`, one
  `skill_bundle`, and one `agent_system`; no fourth artifact is accepted.
- Byte, normalized-text, and frozen near-duplicate checks fail closed. Task/pair lineage,
  responsibility separation, sibling isolation, model/tool/config parity, feedback leakage, pair
  seal, destruction, and reset are enforced.
- Paper GPT-4 and human scores remain post-hoc and never enter evolution.
- Historical full-v3 contains 12 sealed single-memory pairs but zero typed Skill/AgentSystem jobs and
  zero three-artifact injections. It is `LEGACY_PROVISIONAL` and cannot be combined with new task
  14/15 pairs. The authoritative next run is a fresh 14-task `full-v4-three-pipeline`.

## Tools, scoring, and 42-call blueprint

The 14 repository tasks are `01-10,12-15`; the manifest hash is
`8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`.
`CHEMCROW_TOOL_READINESS_MATRIX.md` accounts for every task and public reduced-profile capability.
Unavailable paper-only web/literature/price/proprietary tools remain declared deviations rather than
mocked equivalents.

`PAPER_COMPARISON_MATRIX.json` maps 14 tasks × 3 comparisons = 42 calls:

- `historical_control`: compatible paper-control reconstruction, not a verbatim prompt/model
  snapshot reproduction;
- `baseline`: full-v4 G1 vs historical GPT-4, project-added;
- `evolved`: full-v4 G2 vs historical GPT-4, project-added.

The blueprint is READY and the production ledger is empty. Fourteen historical-control hashes are
frozen. The 28 G1/G2 hashes are expected deferred materialization from future authoritative sealed
outputs and are not a readiness failure.

Scoring layers remain separate: OpenEvo internal three-dimensional 0-4 diagnostics, post-hoc GPT-4
overall 0-10 grades, and four-expert human three-dimensional 0-10 review.

## Verification

```text
Core Gateway/runtime integration tests: 300 passed
full ChemCrow pytest: 89 passed, 2 unchanged dependency warnings
ChemCrow Ruff: PASS
changed Core Ruff: PASS
git diff --check: PASS
```

## Remaining non-blocking deviations and human work

- This is a public-source reduced-profile benchmark, not a paper-identical tool environment.
- Local RXN and modern RDKit differ from paper-era services; price, literature, and configured web
  search are unavailable.
- The evaluator prompt label remains `CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1`.
- Four independent expert chemists must complete 42 blinded comparisons each (168 forms) for the
  formal human layer. A smaller panel must be labeled exploratory.

## Exact full-volume commands — do not execute without the corresponding authorization

Set the frozen non-secret identities in every Candidate terminal:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
export OPENEVO_CANDIDATE_MODEL=gpt-5.5
export OPENEVO_REFLECTOR_MODEL=gpt-5.5
export OPENEVO_EVOLUTION_EVALUATOR_MODEL=gpt-5.5
export OPENEVO_FINAL_EVALUATOR_MODEL=gpt-5.5
export OPENEVO_ROLLOUT_BASE_URL=http://127.0.0.1:8080
```

### 1. Authoritative fresh 14-task run

```bash
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --project benchmarks/chemcrow openevo-chemcrow run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --allow-paid
```

Use `run`, not `resume`, initially. Never redispatch an ambiguous claimed phase.

### 2. G1/G2 aggregate generation

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow aggregate-run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --no-model-calls
```

### 3. Completed-run audit

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow audit-run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --core-completions /home/lhy-h/work/chemcrowrun/core-state/completions \
  --output /home/lhy-h/work/chemcrowrun/runs/full-v4-three-pipeline/completed_run.audit.json
```

### 4. Production 42-call GPT-4 paper evaluation

First materialize and freeze the zero-model plan:

```bash
set -a
source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator
set +a
export CHEMCROW_PAPER_EVALUATOR_MODEL=openai/gpt-4
export CHEMCROW_PAPER_EVALUATOR_MAX_USD=11.83266
uv run --project benchmarks/chemcrow openevo-chemcrow paper-evaluator-preflight \
  --config benchmarks/chemcrow/configs/paper_evaluator.full-v4-three-pipeline.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_EVALUATOR_FULL_V4_PREFLIGHT.json \
  --no-model-calls
```

After that report is ready, start the production Rollout, shim, and Gateway in separate terminals:

```bash
uv run --project benchmarks/chemcrow python -m openevo.rollout.server \
  --config benchmarks/chemcrow/configs/openevo_paper_evaluator_topology.yaml --log-level info
```

```bash
set -a; source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator; set +a
export CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
uv run --project benchmarks/chemcrow python -m openevo_chemcrow.openrouter_shim \
  --host 127.0.0.1 --port 8400 --use-environment-proxy \
  --plan /home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v4-three-pipeline/private/plan.json \
  --receipt-root /home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v4-three-pipeline/openrouter-receipts
```

```bash
uv run --project benchmarks/chemcrow python -m openevo.gateway.server \
  --config benchmarks/chemcrow/configs/openevo_paper_evaluator_topology.yaml \
  --node-id chemcrow-paper-gateway-01 --log-level info
```

Then execute the frozen calls:

```bash
set -a; source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator; set +a
export CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
export CHEMCROW_PAPER_EVALUATOR_MAX_USD=11.83266
uv run --project benchmarks/chemcrow openevo-chemcrow paper-evaluator-run \
  --config benchmarks/chemcrow/configs/paper_evaluator.full-v4-three-pipeline.yaml \
  --allow-paid
```

### 5. Human blind-review packet

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow paper-human-review-prepare \
  --config benchmarks/chemcrow/configs/paper_human_review.full-v4-three-pipeline.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_HUMAN_REVIEW_FULL_V4_PREPARE.json \
  --no-model-calls
```

## Explicit authorization variables

```text
CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
CHEMCROW_PAPER_EVALUATOR_MAX_USD=11.83266
```

The v10 smoke/debug authorization strings do not authorize either full-volume operation.
