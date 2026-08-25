# ChemCrow × OpenEvo next-stage readiness audit

Audit date: 2026-08-25 (Asia/Shanghai)
Scope: repository/code/runtime evidence audit, deterministic tests, free OpenRouter credential and
credit probes, and exactly one authorized Core-routed GPT-4 smoke attempt. No task 14 repair, task
15 run, full benchmark, bulk Candidate/Reflector run, formal 42-call evaluation, or expert scoring
was performed.

> Data-policy amendment, 2026-08-25: after this audit's v1 HTTP 403, the user explicitly approved
> OpenAI-only routing with `data_collection=allow`. Code, plan, tests, and a distinct v2 smoke route
> have been updated. The historical v1 claim remains immutable; no v2 paid call has been authorized
> or performed, so the overall verdict remains blocked.

## Executive verdict

```text
BLOCKED
```

The new three-Reflector implementation is code-complete and its deterministic/Core contract tests
pass, but the next paid stage is blocked for four independent reasons:

1. The 12 immutable `full-v3` pairs used the historical one-Reflector/one-`text_memory` protocol.
   Adding two new three-artifact pairs would not produce a scientifically homogeneous 14-task
   aggregate. The protocol authority must choose a fresh 14-task three-artifact experiment or keep
   the two experiments separate.
2. The only completed OpenRouter GPT-4 smoke attempt reached OpenRouter through the required Core
   route but was rejected with HTTP 403. Its OpenAI-only plus `data_collection=deny` combination has
   now been replaced by the user-approved `data_collection=allow` route, but that route has no paid
   live receipt yet. Formal paper evaluation therefore remains blocked.
3. A fresh task-14 pair would intentionally duplicate an interrupted provider claim. The required
   explicit authorization receipt is absent and was not created or consumed in this audit.
4. Seven of the 14 tasks have a paper-faithful capability blocker involving search, literature,
   price/procurement, or a reliable property source. The current environment is a reproducible
   public-tool reduced profile, not a paper-identical ChemCrow environment.

## A. Repository

| Field | Audited value |
|---|---|
| OpenEvo origin | `git@github.com:Ardorry/OpenEvo.git` |
| branch | `chemcrow-task-local-evolution-v1` |
| initial HEAD | `d55f7f4b5c154e3874f4eefa010772d6bfcfa1af` |
| implementation commit | `5089d18e3c0e73907675e5f0edb097413aa1eb7e` (`feat: isolate ChemCrow artifact evolution pipelines`) |
| working tree | implementation commit clean; audited reports are committed as a separate documentation milestone; final exact HEAD/status is reported in the terminal handoff |
| prior scoring commit | `6f8838fb3` |
| ChemCrow public | `e7ebd5193334ac1d8dea137b635721c7cb470d33`, branch `main` |
| ChemCrow runs | `500104ed9a5d479a8dc4128afc463625ade5a409`, branch `main` |
| task manifest SHA256 | `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604` |
| benchmark lock SHA256 | `05ed78b4ef903a51cc3158ce445d7e2f5bf5696d0c2020ad37f3fba40188582f` |
| old `full.v3.yaml` SHA256 | `fbcb5d2e577d3d9e0ac472f3fa40428df1f42df44cac378b4e7e58a810893948` |
| new repair config SHA256 | `2e0fb827ca7091658cbfaf6b50654e8be0cba4f0caf85b73792c02b72f584c96` |
| frozen S0 SHA256 | `8f7113de469c92fff55b99b62e58d4c43d5e42f2b6485affe44b247c28306a5c` |

The benchmark Python environment is Python 3.11.15 with RDKit 2025.9.6, Pydantic 2.13.4, and
HTTPX 0.28.1. Legacy LangChain is not installed or needed by the adapter. Core's repository test
environment is Python 3.12.14. The host default Python 3.10.12 is not the benchmark interpreter.

### Report-to-code reconciliation

- `CURRENT_SETUP_AND_ARCHITECTURE.md` and `PAPER_EVALUATOR_READINESS.md` were accurate snapshots of
  the old one-artifact setup but became stale after the new requirement and credit top-up. They now
  carry explicit superseded banners pointing here.
- `PAPER_OFFICIAL_SCORING_PROTOCOL.json` was updated with the valid zero-call credit gate, the
  fail-closed one-attempt smoke status, and the still-unconsumed formal authorization.
- `HUMAN_ACTION_REQUIRED.md` was rewritten against current evidence.
- The historical `full.v3.yaml`, old claims, pairs, artifacts, caches, and reset receipts were not
  rewritten or reclassified. The new protocol has its own config and roots.

## B. Architecture

| Path | Status | Current evidence |
|---|---|---|
| Candidate: GPT-5.5 → native Codex harness → OpenEvo Rollout → Gateway → immutable managed Docker runtime | **PASS** | runtime builder fail-closes on host `codex exec`, custom shell, MCP servers, mutable/non-managed image, wrong workdir/runtime, and non-subscription auth; sealed `full-v3` Core receipts show the route |
| Reflector-Memory: GPT-5.5 → dedicated Core dataset/job/rollout → `text_memory` registration | **PASS (code/contract)** | distinct schema/system prompt/job/lineage and deterministic tests; no new paid live Reflector invocation was permitted in this audit |
| Reflector-Skill: GPT-5.5 → dedicated Core dataset/job/rollout → `skill_bundle` registration | **PASS (code/contract)** | independent from Memory and AgentSystem; no live invocation in this audit |
| Reflector-AgentSystem: GPT-5.5 → dedicated Core dataset/job/rollout → `agent_system` registration | **PASS (code/contract)** | independent from Memory and Skill; no live invocation in this audit |
| Internal evolution evaluator: GPT-5.5 → OpenEvo Core | **PASS** | same frozen evaluator configuration for G1 and G2; only G1 score/text is eligible Reflector evidence |
| Final evaluator: GPT-5.5 → OpenEvo Core | **PASS** | separate evaluator identity/prompt and blinded pair order; invoked after G2 and excluded from Reflector input |
| Paper EvaluatorGPT: harness → dedicated Rollout → dedicated Gateway → auth shim → OpenRouter GPT-4 | **FAIL CLOSED** | full route reached OpenRouter, but upstream HTTP 403 prevented model/provider/usage/schema receipt |

The approved next route remains OpenAI-only, disables fallback, requires all parameters and strict
JSON, and changes only `data_collection` from `deny` to `allow`. OpenRouter prompt logging is neither
required nor enabled by this protocol. The new route has deterministic tests but no paid live proof.

OpenRouter references are confined to paper-evaluator credential/probe/shim/smoke modules and
paper-evaluator configuration. Candidate, all three Reflectors, internal evaluation, and final
evaluation are pinned to exact `gpt-5.5` via the subscription-auth native Codex path. The benchmark
driver has no host `codex exec`, agent MCP injection, configurable shell injection, or alternate
Candidate provider path. The ChemCrow tool bridge is declared HTTP/REST from inside the managed
runtime; it is not an MCP server injected into the agent.

The managed runtime image is bound by immutable image ID
`sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`.
Preflight verifies subscription auth presence by name and file metadata only; no credential value is
serialized.

## C. Artifact evolution protocol

### Implemented task-local event order

```text
bare S0 receipt
  -> G1 Candidate
  -> runtime feedback
  -> G1 internal evaluator
  -> freeze three sibling-blind prompts
  -> Reflector-Memory Core invocation
  -> Reflector-Skill Core invocation
  -> Reflector-AgentSystem Core invocation
  -> duplicate/copy guard
  -> register exactly three artifacts
  -> G2 Candidate with exact three-artifact Core receipt
  -> G2 internal evaluator
  -> separate blinded final evaluator
  -> seal pair
  -> clear task-local runtime/inventory and verify bare S0 reset
```

The native OpenEvo name for the Memory artifact is `text_memory`; this is the existing Core
artifact type rather than a newly invented parallel type.

| Required invariant | Result |
|---|---|
| three pipelines actually independent | **PASS (code/contract)**: three pre-frozen prompts, three source events, datasets, Core jobs, rollout invocations, parser schemas, and lineage receipts |
| three model calls | **PASS (scheduled contract)**: exactly one Core rollout per artifact, hence exactly three independent GPT-5.5 calls; not live-proven in this audit because additional Reflector calls were forbidden |
| independent prompt hashes | **PASS**: system and rendered prompt hashes recorded per job; schemas and role instructions differ |
| independent artifact IDs | **PASS**: IDs are returned by three separate registrations only after all outputs pass separation checks |
| independent provenance | **PASS**: receipt records `reflector_job_id`, rollout run ID, model, prompt/system/evidence/artifact hashes, type, task, pair, and parent G1 run |
| sibling isolation | **PASS**: all three requests are frozen before any sibling output exists; allowed evidence is copied from the same immutable G1 package; no sibling output field is accepted |
| byte/normalized duplicate prevention | **PASS**, fail closed |
| near-duplicate prevention | **PASS**, fail closed: token-trigram Jaccard `0.90` or normalized sequence ratio `0.95`, applied from 12 normalized tokens; thresholds frozen in config |
| baseline-answer copy guard | **PASS**, fail closed at normalized sequence ratio `0.98` |
| same artifact registered under multiple types | **PASS**, prevented by unique content hash and artifact ID checks |
| exact G2 injection | **PASS**: Core context receipt must map expected IDs to only `text_memory`, `skill_bundle`, and `agent_system`; any fourth artifact fails |
| G1 bare state | **PASS**: empty context-artifact list is asserted before baseline |
| G1/G2 parity | **PASS**: agent, builder, evaluator, instruction, runtime, runtime-context binding, timeout, and workspace handoff are identical; only the three evolved context IDs may differ |
| reset | **PASS**: runtime memory/skill/agent-system fields and active inventory must be empty; S0 hash must match before the next baseline |
| one evolution phase | **PASS**: ledger event-order validation rejects missing, repeated, or reordered artifact jobs |

Registration is deliberately delayed until all three sub-jobs have completed and separation checks
pass. The three sequential Core worker claims therefore use a 3600-second lease; this changes no
model or artifact semantics.

### Feedback leakage boundary

Reflectors may receive only current prompt, externally visible G1 trajectory/tool observations,
G1 final answer, runtime feedback, and G1 internal score/text. The input sanitizer rejects paper
EvaluatorGPT output, human scores, historical/reference answers, hidden answers/ground truth,
future G2 output, next-task information, and sibling artifact data. Paper evaluation remains
post-hoc after sealed outputs and has no write path into the evolution evidence package.

## D. OpenRouter

### Free credential and credit probe

| Field | Result |
|---|---|
| auth valid | `true` |
| `/key` | HTTP 200 |
| `/credits` | HTTP 200 |
| credit probe success | `true` |
| frozen 42-call ceiling | `$11.83266` |
| sufficient remaining | `true` |
| probe model calls / paid operations | `0 / 0` |

The probe report contains only booleans, HTTP statuses, hashes, and metadata. It contains neither
credential values nor raw balance values. The ignored credential file is mode 0600.

### Only authorized paid smoke attempt

| Field | Result |
|---|---|
| request claim ID | `paper-chemcrow-smoke-core-v1` |
| provider attempts | `1`; the turn's allowed budget is consumed and no retry occurred |
| requested model | `openai/gpt-4` |
| reported model | unavailable |
| requested provider | OpenAI only |
| reported provider | unavailable |
| temperature | `0.1` |
| fallback | disabled |
| `require_parameters` | `true` |
| data collection | `deny` |
| strict JSON | requested; response contract not reached, `schema_valid=false` |
| OpenRouter HTTP | `403` |
| Gateway HTTP | `502` |
| usage receipt | absent |
| estimated cost | unknown; absence of usage is not proof of zero billing |
| benchmark/formal ledger | excluded / untouched |

The route was exactly `PaperEvaluatorHarness -> dedicated OpenEvo Rollout -> dedicated OpenEvo
Gateway -> test-only auth shim -> OpenRouter`. The shim did not relax the production model,
provider, fallback, parameter, data, or prompt-hash restrictions. Temporary ports 8180, 8110, and
8400 were stopped and verified closed.

The raw failed Core completion contained prompt/error material. After its SHA256 was retained in the
sanitized smoke report, that raw completion was deleted and is not recoverable from the workspace;
the immutable shim claim and failure receipt remain. The failure also exposed a secondary Core
post-run diagnostic (`step.00.stdout.log` absent), but the authoritative upstream result is still
HTTP 403. No formal paper-evaluator authorization was consumed.

The v2 route uses call ID `paper-chemcrow-smoke-core-v2`, a separate receipt root, and authorization
literal `I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V2_20260825`. It records only an upstream response
hash, error-code/category, message hash, and OpenRouter-metadata hash on failure; it never persists
the upstream error body. No v2 claim currently exists.

Evidence:

- `/home/lhy-h/work/chemcrowrun/reports/OPENROUTER_CREDENTIAL_PROBE.json`
- `/home/lhy-h/work/chemcrowrun/reports/OPENROUTER_GPT4_CORE_PAID_SMOKE.json`
- `/home/lhy-h/work/chemcrowrun/reports/OPENROUTER_GPT4_ROUTE_PREFLIGHT_V2.json`
- `/home/lhy-h/work/chemcrowrun/runs/paper-evaluator-smoke-v1/openrouter-receipts/`

## E. ChemCrow readiness

### Manifest and sanitization

The frozen manifest contains 14 tasks:

```text
chemcrow-01, chemcrow-02, chemcrow-03, chemcrow-04, chemcrow-05,
chemcrow-06, chemcrow-07, chemcrow-08, chemcrow-09, chemcrow-10,
chemcrow-12, chemcrow-13, chemcrow-14, chemcrow-15
```

Each entry binds task ID/category, source notebook, source hash, extraction method, and sanitized
item hash. Historical ChemCrow answers, historical GPT-4 answers, trajectories, EvaluatorGPT
outputs, grades, and conclusions are absent from Candidate/Reflector input. Those answer-like fields
are permitted only in a separately sealed post-hoc comparison store.

### Tool readiness

- Local deterministic smoke: six tools passed, with zero network and model calls.
- Public live smoke: Wikipedia, Name2SMILES, SMILES2Name, Mol2CAS, ExplosiveCheck,
  SafetySummary, ReactionPredict, and ReactionRetrosynthesis passed.
- Local RXN on `127.0.0.1:8300` is reachable for forward prediction and retrosynthesis, but it is
  not the original hosted paper-era IBM RXN snapshot.
- WebSearch fails explicitly without `SERP_API_KEY`; no fake result is returned.
- Literature synthesis, commercial price/procurement, arbitrary Python, and unavailable
  proprietary paper tools remain excluded/fail-closed.
- Formal records require `mock=0` and `fixture=0`. Pair-cache replay is allowed only for identical
  canonical tool+arguments inside the same G1/G2 pair and is marked separately from live results.

Seven tasks have a blocking paper-faithful component: 01, 02, 04, 05, 10, 12, and 13. The complete
task-to-capability mapping is in `CHEMCROW_TOOL_READINESS_MATRIX.md`.

## F. Existing `full-v3` run

Read-only reconstruction found:

| Evidence | State |
|---|---|
| sealed pairs | 12: tasks 01–10, 12, 13 |
| task 14 | baseline, internal evolution evaluator, and legacy reflector terminal; evolved Candidate claim remains `claimed`; no sealed pair |
| task 15 | no claim directory and no pair |
| claims | 64 total: 63 terminal, 1 claimed |
| aggregate | absent |
| stop marker | `STOPPED_BY_USER.json` present |
| old protocol | one Reflector and one `text_memory` artifact per sealed pair |
| existing evidence | retained; no pair/claim/artifact/cache/reset file was rewritten or deleted in this audit |

The existing 12-pair audit remains internally valid for its historical single-artifact protocol: 12
unique artifacts, 60 terminal claims for the sealed pairs, and 12 Core injection receipts. Across
those pairs, prior audit counted 302 tool calls (250 live and 52 pair-cache replays), 60 explicit
errors, and zero mock/fixture observations. These results must not be relabeled as three-artifact
evolution.

### Safe task-14 repair design

- Preserve the entire old partial directory and old claim forever as interrupted evidence.
- Never resume or redispatch the old `evolved_candidate` claim.
- Use new pair ID
  `chemcrow-task-local-paper-repair-three-isolated-v1--chemcrow-14` and separate run, cache, and
  ledger roots.
- Require an empty baseline artifact context and S0 hash
  `8f7113de469c92fff55b99b62e58d4c43d5e42f2b6485affe44b247c28306a5c` before the fresh G1.
- Do not import the interrupted G1, evaluator feedback, artifact, or claimed G2 into the fresh pair.
- Bind the future duplicate authorization receipt to prior claim SHA256
  `bfa5d6f880bb0f8b1f0149ceef364c51e2a0fe2e290734e0ca3827078fcae40b`.
- Stop after task 14, audit and seal it, and only then separately consider task 15.

The exact authorization literal, not provided or consumed this turn, is:

```text
I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP
```

After the protocol-homogeneity decision and every blocker above are resolved, the prepared
task-14-only command is:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source benchmarks/chemcrow/.env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --project benchmarks/chemcrow openevo-chemcrow run \
  --config benchmarks/chemcrow/configs/paper_repair.three-isolated-v1.yaml \
  --stop-after-task-id chemcrow-14 \
  --allow-paid
```

**Do not run this command while the verdict is `BLOCKED`.** A later task-15 continuation would use
`resume` with the same config and omit `--stop-after-task-id`, but only after task 14 has a complete
three-artifact audit receipt.

## G. Scoring and comparison mapping

| Layer | Model/scale | Role | Reflector access |
|---|---|---|---|
| internal evolution evaluator | GPT-5.5, three dimensions 0–4 | G1 teaching feedback and G1/G2 diagnostics under the same config | only G1 score/text allowed |
| final evaluator | GPT-5.5, separate blinded A/B | pairwise internal final comparison | never |
| paper EvaluatorGPT-compatible layer | OpenRouter `openai/gpt-4`, temperature 0.1, combined 0–10 | sealed post-hoc paper compatibility evaluation | never |
| human expert review | four blinded experts, three dimensions 0–10 plus A/B/tie/confidence | independent chemistry evaluation | never |

The public ChemCrow repository does not provide a verbatim copy of the original EvaluatorGPT
prompt, and today's OpenRouter model is not provably the 2023 snapshot. Therefore this layer must be
labeled `CHEMCROW EVALUATORGPT PROMPT-COMPATIBLE` and any model-only result
`PROVISIONAL LLM-JUDGED RESULT`.

### Why the plan contains 42 comparisons

The number is exactly `14 tasks × 3 frozen comparison families`:

1. historical ChemCrow vs historical GPT-4 — the closest paper-control reconstruction and a measure
   of current-judge drift;
2. OpenEvo G1 vs the same historical GPT-4 — a new OpenEvo baseline metric;
3. OpenEvo G2 vs the same historical GPT-4 — a new task-local evolution metric.

Direct G1 vs G2 is **not** one of the 42 paper calls; it remains a separate paired internal/final
evaluation. Each of the 42 paper conditions maps one-to-one to one human comparison condition. Four
experts per condition produce 168 review forms, not 168 distinct comparisons. Only family 1 is the
closest paper comparison, and even it is prompt/model-snapshot compatible rather than verbatim.
Families 2 and 3 are new experimental metrics.

The exact call IDs, task IDs, source systems, output sources, metric classifications, post-hoc gate,
and one-to-one human mapping are frozen in `PAPER_COMPARISON_MATRIX.json`. Its SHA256 is
`3e2356023f1e178f306dcf3abd0018ef9c67f66986af27191fd8d9f2f44973fb`. The formal 42-call ledger
cannot open until this mapping is accepted, OpenRouter passes a separately authorized Core smoke,
and a homogeneous 14-task sealed authority exists.

## H. Test and preflight results

| Check | Result |
|---|---|
| Ruff, ChemCrow source + tests | PASS |
| ChemCrow adapter/protocol suite | **62 passed**, 0 failed |
| OpenEvo Core evolution/gateway/runtime/managed-assets regression suite | **1619 passed, 8 skipped**, 0 failed |
| `git diff --check` | PASS |
| all tracked benchmark and audit JSON parsing | PASS |
| three-artifact zero-model preflight | expected `BLOCKED_HUMAN_ACTION_REQUIRED`; all architecture/parity/runtime/tool checks pass, duplicate task-14 authorization absent |
| Candidate pair parity | PASS; only three context artifact IDs may differ |
| local tool smoke | PASS, 6/6, no network/model calls |
| public live tool smoke | PASS for the eight configured public/RXN capabilities listed above |
| OpenRouter `/key` + `/credits` | PASS, zero model calls |
| one-call GPT-4 Core smoke | FAIL CLOSED, upstream HTTP 403; no retry |

Warnings/skips:

- 8 Core tests were intentionally skipped by their existing environment markers.
- Starlette warns that its HTTPX `TestClient` integration is deprecated.
- Pydantic settings emits one unresolved forward-reference warning in the tool-service test.
- The first local smoke command used a wrong relative vendor path and failed before any model or
  network call; the corrected explicit path passed all six checks.
- RDKit printed harmless parse warnings while probing whether the literal name `aspirin` was already
  SMILES; the adapter then used the name-resolution route successfully.

No test required or generated a new Candidate GPT-5.5 or Reflector GPT-5.5 call. Consequently the
new three-Reflector path is proven at schema, orchestration, Core request/receipt, isolation, and
reset contract level, but not yet by a paid live three-Reflector receipt.

## I. Required decisions and next-stage plan

Before any task repair or formal evaluation, a human must:

1. Choose whether to run a fresh homogeneous 14-task three-artifact experiment or preserve the new
   protocol as a separate two-task pilot. A mixed old/new 14-task aggregate is forbidden.
2. Optionally authorize one v2 Core smoke for the approved OpenAI-only `data_collection=allow`
   revision. Model, provider, fallback, required parameters, strict JSON, and Core routing remain
   unchanged. The existing v1 claim must never be retried.
3. Explicitly authorize the fresh task-14 duplicate pair using the literal above and allow creation
   of the hash-bound receipt.
4. Approve RXN-Sandbox and MolBloom licenses and the public-tool reduced-profile deviation, or
   configure/approve the missing search/property/procurement capabilities.
5. Approve the task-12 safety rubric and the four-expert review protocol.

If those decisions unblock the work, the next sequence—**not executed in this audit**—is:

1. fresh task-14 pair under a new pair ID;
2. task 14 composite audit and seal;
3. task 15 pair;
4. protocol-homogeneous composite audit;
5. G1/G2 aggregate for the authorized task set;
6. sealed paper-evaluator plan and a new cost/authorization gate;
7. 42 GPT-4 post-hoc evaluations;
8. blinded expert review packet and independent chemistry scoring.

Until then, the only honest final status is:

```text
BLOCKED
```
