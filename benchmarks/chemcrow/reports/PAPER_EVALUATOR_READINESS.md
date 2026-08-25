# ChemCrow paper evaluator readiness

Generated: 2026-08-25 Asia/Shanghai

## Outcome

The paper-compatible evaluator integration is implemented and passed unit plus zero-paid MOCK
Core routing tests. No OpenRouter request and no real model call was made. Formal 14-task scoring
is currently **BLOCKED** because `full-v3` was stopped with only 12 of 14 task pairs fully sealed.

## Recovered official scoring semantics

Every scored task notebook calls `Evaluator(model="gpt-4", temp=0.1)` with the task, historical
ChemCrow answer, and historical no-tools GPT-4 answer. The stored outputs grade both students on a
0--10 scale and provide strengths, weaknesses, grade justification, and (where emitted) feedback.
The Nature paper describes the grading basis as whether the task was addressed and whether the
overall thought process was correct.

The public repository history and released ChemCrow packages do not contain the evaluator module
or its exact prompt. Therefore this implementation is explicitly labeled
`CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1`; it must not be described as a byte-identical prompt
reproduction. The paper also warns that EvaluatorGPT can prefer fluent text and is not a reliable
standalone factual-science judge, so every result remains `PROVISIONAL_LLM_JUDGED_RESULT` pending
blinded chemistry-expert review.

Primary paper reference: <https://www.nature.com/articles/s42256-024-00832-8>

## Frozen protocol

- Model: `openai/gpt-4`
- Provider: OpenAI endpoint only (`provider.only=["openai"]`)
- Temperature: `0.1`
- Output limit: 1200 tokens
- Score: one 0--10 grade per student
- Output fields: grade, strengths, weaknesses, justification, feedback
- Calls: 14 tasks x 3 comparisons = 42
- Comparisons: historical ChemCrow vs historical GPT-4; OpenEvo baseline vs the same historical
  GPT-4; OpenEvo evolved vs the same historical GPT-4
- No model fallback, no tools/plugins, `require_parameters=true`, `data_collection=deny`
- No Reflector or evolution-feedback access
- Historical answers are extracted only after a completed-run audit passes
- The original A/B orientation is retained (target system is Student A, historical GPT-4 is
  Student B) for notebook compatibility; the repeated Student-B means are reported as a position
  control.

The notebook historical evaluator means reconstructed from all 14 stored outputs are 7.357142857
for historical ChemCrow and 8.75 for historical GPT-4. The new control comparison reports current
judge-minus-notebook drift for both systems.

## Core route and isolation

The runtime never receives `OPENROUTER_API_KEY`. It receives only an OpenEvo Gateway session
credential. On Docker Desktop, the harness normalizes the Gateway address to
`http://host.docker.internal:8110/v1` because container loopback does not address the WSL host.
All completions still pass through the dedicated Gateway and the credential-isolating shim.

The shim:

- accepts only `openai/gpt-4`, temperature 0.1, non-streaming strict JSON;
- strips Core's local-vLLM-only `return_token_ids` field before forwarding;
- pins the OpenAI provider and disables fallbacks;
- writes one exclusive claim per call ID before any upstream request;
- forbids automatic retry after any claim or ambiguous transport outcome;
- stores only request/response hashes and provider/model/token/cost metadata, never prompt,
  response, or credential text.

## Cost gate

The current OpenRouter endpoint metadata reports an 8191-token context and list prices of $30/M
input tokens and $60/M output tokens. With 1200 output tokens reserved, the maximum input is 6991.
The exact frozen list-price ceiling is:

- $0.28173 per call
- $11.83266 for 42 calls

The longest prompt among the historical controls and the 12 currently sealed OpenEvo pairs is
estimated at 1860 cl100k tokens. Final prompt token counts are frozen only after all 14 answers are
sealed. Current provider metadata: <https://openrouter.ai/openai/gpt-4/providers>. Routing controls:
<https://openrouter.ai/docs/guides/routing/provider-selection>.

## Verification

- Ruff: PASS
- ChemCrow test suite: 44 passed
- Historical answer extraction: 14/14, with task 12's later safety-refusal overwrite correctly
  selected instead of its earlier output
- Historical teacher grades: 14/14
- 42-call plan construction test: PASS
- no-answer-leakage boundary: historical data absent from sanitized `tasks.jsonl`; extractor is
  sealed-paper-evaluator-only
- OpenRouter shim routing/credential/duplicate-claim tests: PASS
- zero-paid Core route MOCK smoke: PASS; see `PAPER_EVALUATOR_MOCK_CORE_SMOKE.json`
- current real preflight: BLOCKED with zero model calls; see workspace report
  `/home/lhy-h/work/chemcrowrun/reports/PAPER_EVALUATOR_PREFLIGHT.json`

## Current blockers

1. `OPENROUTER_API_KEY` is absent. Add it only to the ignored permission-0600 file
   `/home/lhy-h/work/chemcrowrun/.env.paper-evaluator`.
2. The paid authorization literal remains empty intentionally.
3. `full-v3` has 12 sealed/reset pairs. Task 14 is interrupted and task 15 never started. A
   14-task paper-compatible mean cannot be produced from this run as-is.
4. A chemistry expert is still required for blinded final review.

The first 12 pairs have now passed a separate sealed-subset audit: 12 unique artifacts, 12 Core
runtime-injection receipts, 60 terminal phases, 302 real/cache-replay tool observations, and zero
MOCK/fixture observations. They do not need to be rerun. A frozen two-task repair config for tasks
14 and 15 has the identical S0 hash and identical Candidate/Reflector/evaluator role configs. Its
zero-model preflight passes Core, Evolution Backend, Docker image, local RXN, and role parity; it is
blocked only because no duplicate-task authorization receipt has been created.

The prepared composite path is:

`12 sealed full-v3 pairs + fresh task-14/task-15 repair -> composite audit -> 14-task paper plan`

It will accept task 14 only when an authorization receipt is bound to the exact interrupted claim
hash. The pending template is
`configs/CHEMCROW_14_DUPLICATE_AUTHORIZATION.example.json`; it is not an authorization.

No paid command should be issued until blocker 3 is resolved and a new preflight says
`READY_FOR_EXPLICIT_PAID_AUTHORIZATION`.

## Commands after all blockers are cleared

First source the ignored 0600 file in every terminal that needs its values. Start the shim only
after the immutable plan exists and the explicit authorization literal has been set:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator
set +a
uv run --project benchmarks/chemcrow python -m openevo_chemcrow.openrouter_shim \
  --host 127.0.0.1 --port 8400 \
  --plan /home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v3/private/plan.json \
  --receipt-root /home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v3/openrouter-receipts
```

Start the dedicated Rollout and Gateway in separate terminals:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
uv run --project benchmarks/chemcrow python -m openevo.rollout.server \
  --config benchmarks/chemcrow/configs/openevo_paper_evaluator_topology.yaml \
  --log-level info
```

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
uv run --project benchmarks/chemcrow python -m openevo.gateway.server \
  --config benchmarks/chemcrow/configs/openevo_paper_evaluator_topology.yaml \
  --node-id chemcrow-paper-gateway-01 --log-level info
```

The exact formal launch command is:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator
set +a
uv run --project benchmarks/chemcrow openevo-chemcrow paper-evaluator-run \
  --config benchmarks/chemcrow/configs/paper_evaluator.full-v3.yaml \
  --allow-paid
```

The exact safe-resume command is the same command. It validates and skips sealed results, but if a
shim claim exists without a sealed result it stops instead of redispatching that call. Never delete
a claim to force a retry; audit the provider-side effect first.

After the API key is written, validate it without a model call or charge:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator
set +a
uv run --project benchmarks/chemcrow openevo-chemcrow paper-credential-probe \
  --output /home/lhy-h/work/chemcrowrun/reports/OPENROUTER_CREDENTIAL_PROBE.json \
  --no-model-calls
```

The probe uses OpenRouter's documented `GET /api/v1/key` endpoint. It records only validity and
boolean capacity checks; key labels, identifiers, limits, usage values, and the key itself are not
written. Documentation: <https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key>.
