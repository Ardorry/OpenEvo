# ChemCrow × OpenEvo full-run readiness

Generated: 2026-08-26 (Asia/Shanghai)

## Final verdict

```text
BLOCKED_EXTERNAL
```

The repository is not allowed to print `READY_FOR_FULL_RUN`: Gate H is externally blocked and Gate
E cannot yet supply live three-artifact proof. The authoritative batch and production 42-call batch
remain unstarted.

## Evidence classes

### Historical evidence

- `full-v1/v2/v3`, old artifacts, claims, receipts, task-14 interruption,
  `STOPPED_BY_USER.json`, and task-15 unstarted state were preserved.
- `full-v3` has 12 sealed single-artifact pairs and is `LEGACY_PROVISIONAL` under the new protocol.
- Historical V2 HTTP 403 cause remains `UNPROVEN`.

### New engineering evidence

- Three independent Memory/Skill/Agent-System jobs, typed artifacts, lineage, sibling isolation,
  separation checks, exact-three injection, fairness, seal/reset, and leakage gates are implemented.
- Paper shim validates and claims the incoming strict-JSON request before omitting unsupported
  `response_format` from the first-party legacy GPT-4 transport payload.
- Upstream text still requires JSON parsing and strict `DualStudentAssessment` validation, with no
  retry for invalid output.
- `PAPER_COMPARISON_MATRIX.json` maps all 42 logical comparisons without inventing hashes for
  not-yet-produced full-v4 answers.

### New paid/debug smoke evidence

- Core v5-v8: four unique claims, four terminal HTTP 403 receipts, zero successful completions,
  zero usage receipts.
- Minimal direct v1: one unique claim, HTTP 403, router `attempt=0`, OpenAI and Azure both
  `selected=false`, zero successful completions, zero usage receipts.
- v4 infrastructure-only failure: zero provider attempts.
- New provider-request attempts: 5; all retained historical debug attempts: 7; automatic retries: 0.
- Debug spend is unknown because no usage receipt exists; it is not represented as zero.
- Production claims/results: empty and untouched.

### New live three-artifact preflight evidence

- Zero-paid preflight for tasks `02`, `03`, `06`: PASS.
- Real Candidate pairs: 0.
- Real Memory/Skill/Agent-System jobs: `0/0/0`.
- Real three-artifact injections, pair seals, and reset receipts: 0.
- Reason: the unrelated but terminal Paper route blocker was established first; paid subscription
  execution would not remove it. This live gate remains mandatory after OpenRouter repair.

### Remaining paper deviations

- The public evaluator prompt is a compatibility reconstruction, not verbatim historical text.
- The current GPT-4 service is not a frozen paper-era snapshot.
- Local RXN, deterministic safety extraction, absent web/literature/price tools, and modern RDKit
  differ from the paper environment. See `CHEMCROW_TOOL_READINESS_MATRIX.md`.
- GPT-4 grades are post-hoc 0-10 overall grades and must remain distinct from OpenEvo internal 0-4
  three-dimensional diagnostics and human three-dimensional 0-10 scores.

### Remaining human actions

1. Repair the OpenRouter account/workspace/key policy so a minimal first-party `openai/gpt-4` call
   is eligible. The ordinary inference key cannot inspect the exact policy assignment.
2. After the full-v4 run and 42-call evaluation, obtain four independent expert reviews for all 42
   blinded comparisons: 168 completed review forms.
3. Record the intended scientific label as reduced-profile unless the documented paper-tool
   deviations are separately resolved.

## External blocker evidence

The same 403 response and router metadata occur through both the complete Core route and a minimal
direct request. Router metadata reports `attempt=0`; neither available endpoint was selected. This
means no first-party OpenAI request was attempted. Current credential validity, credits, and model
visibility pass. The exact active policy rule is inaccessible to the ordinary key.

Required repair: inspect account/workspace/member/API-key privacy, ZDR, provider/model allow/ignore,
guardrail, and budget assignments in OpenRouter's management UI/context. Make first-party provider
`openai` eligible for `openai/gpt-4`. Do not enable fallback or switch models.

## Final local verification

```text
focused protocol/Paper tests: 50 passed
full ChemCrow pytest: 81 passed, 2 unchanged non-blocking dependency warnings
Ruff: PASS
git diff --check: PASS after the matrix EOF repair
```

The warnings are Starlette's `httpx` TestClient deprecation and a Pydantic Settings unresolved
forward-reference warning. Neither is introduced by this change and neither changes runtime gates.

## Commands after the blocker is repaired

Do not run these while this report says `BLOCKED_EXTERNAL`. The Core/RXN services must first pass
their health checks. Load model/auth settings from an ignored credential file or export the exact
non-secret model identities:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
export OPENEVO_CANDIDATE_MODEL=gpt-5.5
export OPENEVO_REFLECTOR_MODEL=gpt-5.5
export OPENEVO_EVOLUTION_EVALUATOR_MODEL=gpt-5.5
export OPENEVO_FINAL_EVALUATOR_MODEL=gpt-5.5
export OPENEVO_ROLLOUT_BASE_URL=http://127.0.0.1:8080
```

### 1. Authoritative fresh 14-task run

First rerun zero-model preflight, then complete the paid representative preflight `02,03,06` with a
fresh readiness run ID and audit it. Only after its real receipts pass:

```bash
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --project benchmarks/chemcrow openevo-chemcrow run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --allow-paid
```

Never resume an ambiguous claimed phase. The initial authoritative invocation is `run`, not
`resume`.

### 2 and 3. G1/G2 aggregate, then completed-run audit

The implementation audit requires `aggregate.json`, so the safe execution order is aggregate first,
then audit even though the logical reporting list names audit before aggregate:

```bash
uv run --project benchmarks/chemcrow openevo-chemcrow aggregate-run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --no-model-calls

uv run --project benchmarks/chemcrow openevo-chemcrow audit-run \
  --config benchmarks/chemcrow/configs/full.v4-three-pipeline.yaml \
  --core-completions /home/lhy-h/work/chemcrowrun/core-state/completions \
  --output /home/lhy-h/work/chemcrowrun/runs/full-v4-three-pipeline/completed_run.audit.json
```

### 4. Production 42-call GPT-4 evaluation

First create/freeze the plan with zero model calls:

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

After that report is `READY_FOR_EXPLICIT_PAID_AUTHORIZATION`, start the dedicated paper Rollout,
Gateway, and shim in separate terminals using
`configs/openevo_paper_evaluator_topology.yaml` and the frozen full-v4 plan/receipt root. Then:

```bash
export CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
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

## Explicit full-volume authorization values

```text
CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
CHEMCROW_PAPER_EVALUATOR_MAX_USD=11.83266
```

The debug-only recovery values are separate and do not authorize production:

```text
CHEMCROW_PAPER_SMOKE_AUTHORIZATION=I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V9_20260826
CHEMCROW_PAPER_SMOKE_MAX_USD=2.00
```

Exact branch, HEAD, working-tree status, pytest count, Ruff, and `git diff --check` are recorded in
the terminal handoff after the report commit.
