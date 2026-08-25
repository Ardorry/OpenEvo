# ChemCrow × OpenEvo next-stage readiness audit

Audit date: 2026-08-26 (Asia/Shanghai)

## Verdict

```text
BLOCKED_EXTERNAL
```

The local implementation, deterministic protocol checks, reduced-profile tool checks, and
Paper-Evaluator transport compatibility fix are complete. The remaining hard blocker is current
OpenRouter routing state: both the Core-managed route and a minimal direct diagnostic return HTTP
403 before any provider is selected. No authoritative 14-task run or production 42-call paper
evaluation was started.

## Authoritative experiment authority

- Branch: `chemcrow-task-local-evolution-v1`.
- Frozen task IDs: `01-10, 12-15` (14 tasks; repository IDs remain authoritative).
- Task manifest SHA256:
  `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`.
- Frozen next experiment: `chemcrow-task-local-full-v4-three-pipeline`.
- Full-v4 config SHA256:
  `6aebd03667c0b88adb7fe26ec635891eabcbaf925e9d2c4fd62cdc6bf8bbf152`.
- Frozen S0 SHA256:
  `065df958ce3c1015d30534f50be85240deb4f45bbb5648bc05f05457812a14f4`.
- Runtime image ID:
  `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`.

The authoritative next run must be a fresh 14-task `full-v4-three-pipeline`. The historical
`full-v3` experiment cannot be continued or combined with new three-artifact tasks.

## Three-pipeline implementation

The task-local protocol now admits exactly three independent Core-managed GPT-5.5 Reflector jobs:

1. Memory -> `text_memory`
2. Skill -> `skill_bundle`
3. Agent System -> `agent_system`

All three prompts and allowed-evidence packages are frozen before any sibling output exists. Each
job has its own dataset, job ID, rollout, prompt hash, lineage, registration, and expected injection
binding. Registration occurs only after all outputs pass byte-identical, normalized-identical,
near-duplicate, role-responsibility, task/pair lineage, and baseline-answer-copy checks. The frozen
near-duplicate thresholds are trigram Jaccard `0.90` or normalized sequence ratio `0.95`, applied at
12 or more normalized tokens. Any violation fails closed.

G1 requires an empty artifact inventory. G2 requires exactly the three task-local IDs and no fourth
artifact. Model, settings, native Codex harness, Core route, runtime, image, tools, network policy,
timeouts, S0, and workspace handoff are parity-bound. A sealed pair requires a Core injection
summary and a bare-S0 reset receipt. Paper scores, human grades, historical answers, hidden answers,
future output, prior-task state, and sibling output are rejected from Reflector evidence.

## Zero-paid readiness evidence

`/home/lhy-h/work/chemcrowrun/runs/readiness-three-pipeline-v1/preflight.json` is `READY` with zero
model calls. It binds representative tasks `02`, `03`, and `06`, all GPT-5.5 roles, native Codex,
OpenEvo Core, the immutable runtime, exact three-artifact G2 inventory, empty G1 inventory, and the
14-task manifest.

Tool evidence in the same root records:

- 19 declared tools;
- six deterministic local live checks: PASS;
- eight public-network/local-RXN live checks: PASS;
- mock observations: 0;
- fixture observations: 0;
- model calls: 0.

The paid three-task Candidate/Reflector/G2 preflight was deliberately not started after the paper
route became externally blocked. Spending those subscription calls cannot clear or diagnose the
OpenRouter gate, and a live preflight would still leave the overall full-run readiness result
blocked. It remains required after the external route is repaired.

## Legacy full-v3 audit

The immutable historical root contains 12 sealed pairs for tasks `01-10,12,13`, 12 reset receipts,
and 12 single `text_memory` artifact receipts. It contains:

| Evidence | Count |
|---|---:|
| sealed pairs | 12 |
| memory artifacts | 12 |
| skill_bundle artifacts | 0 |
| agent_system artifacts | 0 |
| typed Memory Reflector jobs | 0 |
| typed Skill Reflector jobs | 0 |
| typed AgentSystem Reflector jobs | 0 |
| tasks with three independent jobs | 0 |
| tasks with sibling-isolation evidence | 0 |
| G2 receipts binding all three artifacts | 0 |
| tasks satisfying the new protocol | 0 |

Task 14 retains interrupted evidence and `STOPPED_BY_USER.json`; task 15 was not started. Nothing was
modified. Classification: `LEGACY_PROVISIONAL`.

## Paper Evaluator compatibility and live evidence

The shim still validates incoming `response_format={"type":"json_object"}` and includes the
validated request in claim/hash semantics. It then removes `response_format` only from the upstream
legacy GPT-4 transport payload. The returned text is accepted only after `json.loads` and strict
`DualStudentAssessment` Pydantic validation. Invalid JSON, invalid schema, and grades outside
`[0,10]` fail closed without retry. Model, provider, temperature, token limit, and prompt-hash
semantics are unchanged.

Historical V2 HTTP 403 exact cause: **UNPROVEN**. It must not be attributed to `response_format`.
The first-party legacy GPT-4 `response_format` HTTP 400 incompatibility is independently
**PROVEN** by the retained strict A/B evidence.

Current debug evidence is stronger and separate:

- v4 failed inside Core before the shim because the dedicated Gateway used the wrong project
  environment; it was fixed and sealed with zero provider attempts.
- Core v5-v8 each reached the shim and returned HTTP 403; each has a fresh claim and terminal
  sanitized receipt and was never retried.
- v6-v8 router metadata records `attempt=0`, two candidate endpoints (`OpenAI`, `Azure`), and both
  `selected=false`.
- The minimal direct v1 diagnostic omitted Core, Gateway, shim, `response_format`, and `user`, but
  returned the identical HTTP 403 and routing metadata.
- `/key`, `/credits`, and model visibility still pass with the ordinary inference key.
- New provider-request attempts in this readiness goal: 5. Including historical v1/v2 retained
  attempts: 7. Successful completions and usage receipts: 0. Billing/spend is not proven and is not
  reported as zero.
- All debug claims and results are outside the production 42-call ledger.

This rules out the current Core/Gateway/shim path, request body, prompt, and the now-removed optional
fields as causes of the active 403. The remaining operational cause is an external OpenRouter
account/workspace/key routing policy that excludes every endpoint before selection. Its exact rule
cannot be read with the ordinary inference key. OpenRouter's router metadata and provider-selection
documentation describe these pre-provider routing fields:
<https://openrouter.ai/docs/guides/features/router-metadata> and
<https://openrouter.ai/docs/guides/routing/provider-selection>.

## 42-call audit

`PAPER_COMPARISON_MATRIX.json` contains 14 tasks x 3 comparisons = 42 rows:

- `historical_control`: historical ChemCrow vs historical GPT-4; closest public paper-style control,
  but only prompt-compatible, not a verbatim historical-prompt/model-snapshot reproduction.
- `baseline`: full-v4 G1 vs historical GPT-4; project-added metric.
- `evolved`: full-v4 G2 vs historical GPT-4; project-added metric.

The 14 historical-control prompt and source hashes are frozen. The 28 G1/G2 prompt and source hashes
are intentionally null until the authoritative full-v4 outputs exist; template hashes are provided
but do not masquerade as final prompt hashes. Therefore the blueprint is complete, but the formal
plan is not yet ready and the production ledger remains clean.

## Gate summary

| Gate | Status | Reason |
|---|---|---|
| A Repository | PASS at handoff | fixes/reports committed; exact HEAD and clean status in terminal handoff |
| B Candidate architecture | PASS (code/preflight) | GPT-5.5, native Codex, Core-managed immutable runtime |
| C Three-artifact evolution | PASS (code/tests) | three jobs/types/prompts/lineages plus isolation and duplicate gates |
| D Task-local protocol | PASS (code/tests) | bare G1, exact-three G2, seal/reset/cross-task gates |
| E Live representative preflight | BLOCKED/NOT RUN | real Candidate/three-Reflector/G2 proof still required after external repair |
| F Tools | PASS for declared reduced profile | all 14 tasks mapped; mock/fixture forbidden; paper deviations explicit |
| G Internal scoring | PASS (code/tests) | same G1/G2 evaluator; paper/human layers isolated |
| H Paper Evaluator | BLOCKED_EXTERNAL | current OpenRouter HTTP 403, router attempt 0, no selected provider |
| I 42-call plan | BLOCKED | 28 hashes require sealed full-v4 outputs; production ledger clean |
| J Tests | PASS at handoff | exact results recorded in `FULL_RUN_READINESS.md` and terminal handoff |

## Exact unblock action

Using OpenRouter Dashboard or a management-authorized context, inspect all current account,
workspace, member, and API-key routing/privacy/guardrail assignments for this inference key. Ensure
`openai/gpt-4` and first-party provider `openai` are eligible, no stricter ZDR/data policy excludes
OpenAI, no ignored/allowlisted model or provider rule excludes it, and no applicable budget is
exhausted. Do not change the frozen model, provider, fallback, temperature, or strict validation.

After repair, use the prepared fresh v9 call only; never retry v1-v8 or the direct diagnostic:

```text
CHEMCROW_PAPER_SMOKE_AUTHORIZATION=I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V9_20260826
CHEMCROW_PAPER_SMOKE_MAX_USD=2.00
```

The v9 smoke must return HTTP 200, model `openai/gpt-4`, provider `OpenAI`, valid JSON, valid
Pydantic assessment, and a usage receipt before the representative live preflight may complete the
remaining readiness gates.
