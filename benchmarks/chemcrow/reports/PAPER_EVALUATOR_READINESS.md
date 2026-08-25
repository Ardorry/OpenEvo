# ChemCrow paper evaluator readiness

Generated: 2026-08-26 (Asia/Shanghai)

## Outcome

```text
PAPER_EVALUATOR_ROUTE_READY
```

The production 42-call evaluation remains intentionally unstarted. Its blueprint is ready; its 28
full-v4-dependent prompt/source hashes will be frozen after the authoritative run seals.

## Frozen compatible protocol

- Label: `CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1`
- Model: `openai/gpt-4`
- Provider: first-party OpenAI only (`provider.only=["openai"]`)
- Temperature: 0.1
- Output limit: 1200 tokens
- `allow_fallbacks=false`, `require_parameters=true`, `data_collection=allow`
- One overall 0-10 grade per student plus strengths, weaknesses, justification, and feedback
- 14 tasks × 3 comparisons = 42 calls
- No retry after a claim, ambiguous request, invalid JSON, invalid schema, or invalid grade
- Paper scores are post-hoc and never enter evolution

The exact historical evaluator prompt was not recovered, so this is not described as a verbatim
reproduction. Human expert scoring remains a separate layer.

## Transport compatibility

Core continues to require `response_format={"type":"json_object"}`. The shim validates and hashes
that incoming request, then omits only `response_format` from the upstream legacy GPT-4 payload.
The returned text must pass `json.loads` and strict `DualStudentAssessment` Pydantic validation.
Invalid JSON, invalid schema, or grades outside `[0,10]` fail closed without retry.

The old Core v5-v8 and minimal-direct HTTP 403s were caused by the shim/direct debug client setting
`trust_env=False`, which selected a direct HKG egress. An identical request body through the
configured environment proxy used SJC and returned HTTP 200. The repair is explicit:

- start the shim with `--use-environment-proxy`;
- validate a single supported proxy URL and reject embedded credentials;
- persist only proxy scheme/host/port/hash, never proxy credentials;
- capture a sanitized actual outgoing request descriptor before sending.

No OpenRouter Dashboard change is required. Historical V2's exact 403 cause remains unproven.

## Fresh Core v10 evidence

The real paid smoke executed:

```text
PaperEvaluatorHarness -> dedicated OpenEvo Rollout -> dedicated OpenEvo Gateway
-> auth shim -> OpenRouter -> OpenAI
```

Result:

| Field | Value |
|---|---|
| HTTP | 200 |
| requested/reported model | `openai/gpt-4` |
| reported provider | `OpenAI` |
| temperature | 0.1 |
| fallback | false |
| response format upstream | omitted after internal validation |
| JSON / Pydantic / schema | valid / valid / valid |
| usage | 257 prompt, 136 completion, 393 total |
| reported cost | $0.01587 |
| claim/receipt | sealed |
| prompt/response in report | absent |
| production ledger | untouched |

Evidence is in `OPENROUTER_GPT4_CORE_PAID_SMOKE_V10.json` and
`OPENROUTER_REQUEST_DIFF_AUDIT.json`.

## Cost and production boundary

The frozen list-price ceiling remains $0.28173 per call and $11.83266 for 42 calls. Production
requires a complete sealed full-v4 source set, zero-model preflight, the explicit 42-call
authorization, and the dedicated production receipt root. Debug authorization cannot satisfy this
gate.

The production root `/home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v4-three-pipeline` does
not exist, so there are zero production claims/results. Exact launch commands are in
`FULL_RUN_READINESS.md` and include the mandatory `--use-environment-proxy` switch.

## Scoring boundary and remaining human requirement

- OpenEvo internal evaluator: frozen three-dimensional 0-4 diagnostics for evolution feedback.
- ChemCrow-compatible GPT-4: post-hoc overall 0-10 grade.
- Human layer: four independent chemists, each scoring both blinded answers on chemical accuracy,
  reasoning quality, and task completion from 0 to 10.

The final human layer requires 168 completed review forms. GPT-4 results remain provisional until
that review is complete.
