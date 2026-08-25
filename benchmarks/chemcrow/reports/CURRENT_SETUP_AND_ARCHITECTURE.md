# ChemCrow / OpenEvo current setup and architecture

> Historical snapshot notice (2026-08-25): this report describes the sealed `full-v3` single-
> `text_memory` protocol at commit `d55f7f4b5`. It is superseded for current readiness by
> `NEXT_STAGE_READINESS_AUDIT.md`. OpenRouter credits are now sufficient, the one-call Core smoke
> failed closed with upstream HTTP 403, and the new three-Reflector protocol is a separate versioned
> path. The later request-delta audit proved the present-day 403s were caused by direct HKG egress
> when the debug client disabled environment-proxy inheritance. Fresh Core v10 now passes HTTP 200
> through SJC with first-party OpenAI. There is no active account/workspace guardrail blocker. The
> 12 sealed pairs below remain unchanged historical evidence.

Generated: 2026-08-25 Asia/Shanghai

## 1. Executive status and execution boundary

The integration is implemented on the dedicated branch and the fixed three-task `preflight-v3`
is fully sealed and audit-valid. The later `full-v3` was explicitly stopped by the user after 12
of 14 task-local pairs were sealed. It has no 14-task aggregate and must not be presented as a
complete ChemCrow result.

The original settings-check phase performed no Candidate, Reflector, evaluator, or OpenRouter model
call. A later authorized paper-evaluator audit proved sufficient credit, then issued one v1 and one
separately authorized v2 Core-routed provider attempt. Both failed closed at upstream HTTP 403 and
neither produced a usage receipt. The formal paper authorization remains absent. The duplicate
task-14 replacement authorization receipt is also absent; no benchmark task was resumed.

Current service state:

| Port | Component | State |
|---:|---|---|
| 8080 | regular OpenEvo Rollout | reachable |
| 8100 | regular OpenEvo Gateway | reachable |
| 8200 | OpenEvo Evolution Backend | reachable |
| 8300 | local RXN compatibility service | reachable |
| 8180 | dedicated paper-evaluator Rollout | closed |
| 8110 | dedicated paper-evaluator Gateway | closed |
| 8400 | OpenRouter authentication shim | closed |

## 2. Repository authority and reproducibility

| Component | Authority |
|---|---|
| Workspace | `/home/lhy-h/work/chemcrowrun` |
| OpenEvo clone | `/home/lhy-h/work/chemcrowrun/openevo` |
| OpenEvo origin | `git@github.com:Ardorry/OpenEvo.git` |
| Base | `origin/stable` at `158f48ea240a8a92bca6827e0ebd0ad418674e48` |
| Integration branch | `chemcrow-task-local-evolution-v1` |
| Scoring/readiness implementation commit | `6f8838fb323112ddc08f09e4da033239d09bdc8d` |
| ChemCrow public | `e7ebd5193334ac1d8dea137b635721c7cb470d33` |
| ChemCrow runs | `500104ed9a5d479a8dc4128afc463625ade5a409` |
| OpenEvo package | `0.1.10` |
| Adapter package | `openevo-chemcrow 0.1.0` |
| Python | `3.11.15` |
| RDKit | `2025.09.6` |
| HTTPX / Pydantic | `0.28.1` / `2.13.4` |
| MCP / MolBloom | `1.29.0` / `2.3.5` |
| Adapter lock SHA-256 | `05ed78b4ef903a51cc3158ce445d7e2f5bf5696d0c2020ad37f3fba40188582f` |
| Task manifest SHA-256 | `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604` |
| Leakage audit SHA-256 | `de4f5b87d9187e95d1d8c4b40d485b58381a9241e22761a2f0366d64bb50a585` |
| Resolved S0 SHA-256 | `8f7113de469c92fff55b99b62e58d4c43d5e42f2b6485affe44b247c28306a5c` |

All scientific integration changes live under `benchmarks/chemcrow/` plus the repository
`.gitignore`; no `src/openevo/` Core implementation file was modified. The benchmark package uses
the existing Core servers, task request schema, native Codex harness, Evolution Store, context
resolution, and runtime injection instead of reimplementing them.

## 3. Runtime architecture

### 3.1 Task-local Candidate/evolution path

```text
sanitized task manifest
  -> ChemCrow benchmark driver
      -> pair-scoped chemistry tool service
      -> OpenEvo Rollout :8080
          -> OpenEvo Gateway :8100
              -> Core-managed immutable Docker runtime
                  -> native Codex harness (subscription transcript capture)
                  -> Candidate / evolution evaluator / Reflector / final evaluator
      -> OpenEvo Evolution Backend :8200
          -> register one task-local text_memory artifact
          -> exact artifact context resolution/materialization
          -> Gateway runtime-injection receipt
      -> seal trajectories, feedback, artifact, pair result, reset receipt
```

Codex is never invoked by a benchmark-local host `codex exec`. Every model role is submitted as an
OpenEvo `TaskRequest`, routed through Rollout and Gateway, and executed in the same pinned managed
Docker image. Caller-supplied MCP servers, custom shells, mutable images, non-subscription auth,
and host execution are rejected.

### 3.2 Per-item state machine

For each task `i`, the implemented protocol is:

```text
assert exact bare S0
  -> baseline(task_i, artifact_ids=[])
  -> observable trajectory + runtime feedback
  -> independent evolution evaluator (F0/F1/F2 switch)
  -> exactly one Reflector step
  -> register artifact_i in OpenEvo Core
  -> evolved(task_i, artifact_ids=[artifact_i])
  -> exact Core injection receipt
  -> separate blinded final evaluator
  -> seal pair evidence
  -> discard artifact_i and prove empty next-task state
```

The runner asserts identical resolved Candidate configuration across the pair, no baseline
artifact, exactly one same-task evolved artifact, unique artifact IDs, one Reflector event, exact
lineage, reset to empty memory/skill/agent-system lists, identical S0 between tasks, and no MOCK or
fixture observation in real metrics.

### 3.3 Tool path and paired fairness

```text
managed Candidate container
  -> host.docker.internal pair tool bridge
      -> canonicalize tool + arguments
      -> pair-local observation cache
          -> live local RDKit/MolBloom
          -> public PubChem/Wikipedia, when enabled
          -> local RXN service :8300
      -> structured observation receipt
```

Every tool attempt records call ID, tool, canonical argument hash, result or explicit error,
source (`live` or `cache_replay` in real mode), and elapsed time. Identical calls can replay only
inside the same baseline/evolved pair. Novel evolved calls execute live when available. Missing
keys, unavailable services, invalid inputs, empty results, HTTP errors, and timeouts are explicit;
there is no real-mode fake output.

## 4. Data and leakage architecture

The sanitized scored manifest contains exactly 14 tasks:
`01`, `02`, `03`, `04`, `05`, `06`, `07`, `08`, `09`, `10`, `12`, `13`, `14`, `15`.
Task 12 is safety-sensitive. The nitroglycerin safety demonstration is stored separately and is not
part of the scored 14.

Task extraction uses static AST/literal parsing and records source notebook, extraction method,
source SHA-256, and sanitized-item SHA-256. Candidate, Reflector, evolution evaluator, and regular
final evaluator do not receive historical ChemCrow answers, historical GPT-4 answers, notebook
trajectories, old EvaluatorGPT output, expert grades, or paper conclusions.

Historical answers are accessible only from the sealed-output paper evaluation modules after a
complete 14-task authority passes its audit. This dependency direction is one-way and cannot feed
back into evolution.

## 5. Feedback and evaluation architecture

### 5.1 OpenEvo internal diagnostics

The runtime/evolution path retains the existing three-dimensional 0--4 rubric:
chemical correctness, reasoning quality, and task completion. This is an OpenEvo diagnostic, not
the paper's official human score. F0/F1/F2 select trajectory only, trajectory plus runtime
feedback, or trajectory plus runtime and evaluator critique. Only baseline-derived feedback can
reach the Reflector.

The regular final evaluator is separately configured, blinded to baseline/evolved identity, and
never supplies Reflector feedback. Its results are labeled `PROVISIONAL_LLM_JUDGED_RESULT`.

### 5.2 Paper-compatible EvaluatorGPT layer

Only after all baseline/evolved pairs are sealed, the paper layer creates 42 fixed comparisons:

1. historical ChemCrow vs historical no-tools GPT-4;
2. current OpenEvo baseline vs the same historical GPT-4;
3. current OpenEvo evolved vs the same historical GPT-4.

The frozen configuration is `openai/gpt-4`, temperature `0.1`, one 0--10 overall grade per
student, plus strengths, weaknesses, justification, and actionable feedback. The paper's public
semantics are recovered, but the original Evaluator prompt is absent from public source, so the
protocol is named `CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1`, not a verbatim reproduction.

The dedicated route is:

```text
sealed paper plan
  -> dedicated Rollout :8180
      -> dedicated Gateway :8110
          -> PaperEvaluatorHarness in managed Docker
              -> Gateway session credential only
          -> auth shim :8400
              -> inject OPENROUTER_API_KEY
              -> OpenRouter openai/gpt-4, OpenAI provider only
```

The shim forces model/temperature/strict JSON/output cap, disables provider and model fallback,
requires supported parameters, requests data-collection denial, binds every request to the frozen
plan and prompt hash, and writes an exclusive claim before upstream dispatch. It stores only
hashes and provider/model/token/cost receipts, not credentials, prompts, or responses. An ambiguous
claimed call is never automatically retried.

### 5.3 Paper-compatible human expert layer

Official Source Data Fig. 4 shows four expert chemists scoring both responses from 0 to 10 on:

- `Chemically accurate`;
- `Quality of reasoning`;
- `Task completed`.

The new offline packet generator creates the same 42 comparison families and four independent
review forms per comparison, for 168 completed forms. It randomizes A/B order with a private
permission-0600 secret, exposes only the secret hash, uses opaque packet IDs, stores condition
mapping separately at mode 0600, excludes trajectories/system identity, and validates completed
scores against the 0--10 schema. Fewer than four reviewers is exploratory, not paper-comparable.

## 6. Tool inventory

The tracked inventory has 19 entries:

| Class | Count | Tools/state |
|---|---:|---|
| A | 6 | PatentCheck, MolSimilarity, SMILES2Weight, FunctionalGroups, ControlChemCheck, SimilarityToControlChem |
| B | 6 | Wikipedia, Name2SMILES, Mol2CAS, SMILES2Name, ExplosiveCheck, SafetySummary; public network drift applies |
| C | 1 | WebSearch; unavailable without optional `SERP_API_KEY` |
| C/D | 2 | ReactionPredict and ReactionRetrosynthesis; local RXN is currently reachable |
| E | 1 | paper-only restricted tools; absent from public repository |
| F | 3 | python_repl, LiteratureSearch, GetMoleculePrice; excluded for safety/fairness/reproducibility |

The current reduced profile is intentionally not an exact reproduction of all paper-era services.
The local RXN model/service differs from the historical hosted platform, and retrosynthesis quality
is not treated as successful when the service returns empty output.

## 7. Code and configuration changes

### 7.1 Benchmark package modules

| Module | Change/purpose |
|---|---|
| `tasks.py` | static sanitized extraction and safety-task separation |
| `models.py` | strict task, trajectory, feedback, artifact, pair, and observation schemas |
| `tools.py` | LangChain-independent chemistry tool registry and explicit degradation |
| `tool_service.py`, `mcp_server.py` | pair-scoped REST/MCP bridge and receipts |
| `cache.py` | canonical pair-only observation cache |
| `runtime.py` | Core-only TaskRequest construction, managed runtime checks, transcript audit |
| `protocol.py` | exact task-local baseline/evolve/evolved/seal/reset state machine |
| `feedback.py`, `evaluation.py` | F0/F1/F2 runtime feedback and separated evaluators |
| `native_evolution.py` | one Reflector call, Core artifact registration and injection binding |
| `ledger.py` | exclusive phase claims and ambiguous-effect fail-closed resume |
| `audit.py`, `aggregate.py` | completed-run/invariant verification and rubric aggregates |
| `composite.py` | preserved-12 plus fresh-2 composite authority and duplicate-call gate |
| `paper_evaluator.py` | historical extraction after sealing and 42-call frozen plan |
| `paper_harness.py`, `paper_core.py` | dedicated Core paper-evaluator execution path and safe resume |
| `openrouter_shim.py` | credential isolation, provider pinning, plan allowlist, cost receipts |
| `paper_human_review.py` | official 0--10 three-dimensional four-reviewer blinded packets |
| `credentials.py` | value-free environment report plus `/key` and `/credits` boolean probe |
| `cli.py` | zero-model preflights/audits and gated run/resume commands |
| `hashing.py` | canonical content receipts |

### 7.2 Frozen configs and environments

- Separate `.env.example`; real `.env` files are ignored.
- Pinned Python 3.11 adapter environment and `uv.lock`.
- Core topology for regular runs and a separate paper-evaluator topology.
- Pinned local RXN Compose services and image/model receipts.
- Immutable preflight-v3 and full-v3 selections/configs.
- Frozen task-14/task-15 repair config with the same model roles and S0 as full-v3.
- Composite audit config and separate full/composite paper-evaluator configs.
- Separate paper human-review config.
- Explicit paid flags plus literal authorization variables on run paths.

### 7.3 Test coverage added

Tests cover task extraction, leakage, tool wrapping, errors, serialization, cache scope, mock/real
separation, missing keys/services, Core-only routing, transcript/receipt parity, artifact
registration/injection, reset/isolation, evaluator separation, claims/resume, paper plan, shim
credential isolation, 42-call plan, credit capacity, and official human packet blinding/scale.

Current adapter result: **66 passed**, Ruff PASS, `git diff --check` PASS. The two warnings are
existing Starlette/httpx and Pydantic-settings deprecation/forward-reference warnings. The frozen
repair execution-parity gate also passes: nine scientific execution modules remain byte-identical
to full-v3 reference commit `6e1337834203086e2dca74e7fb61fc4964fce11d`.

## 8. Run evidence

### 8.1 Fixed three-task preflight-v3

`preflight-v3` is complete and audit-valid for tasks `02`, `03`, and `06`:

- 3/3 sealed pairs;
- 15 terminal phases;
- 3 unique artifacts and 3 exact Core injection receipts;
- 61 tool observations: 49 live and 12 cache replay;
- 0 MOCK/fixture observations;
- identical resolved S0 `8f7113...` for all pairs;
- completed-run audit: PASS;
- aggregate label: `PROVISIONAL_LLM_JUDGED_RESULT`.

Aggregate-only internal diagnostic: baseline means `3.80 / 3.37 / 3.83`, evolved means
`3.87 / 3.93 / 3.93`, deltas `+0.07 / +0.57 / +0.10`, and pairwise `2 wins / 0 losses / 1 tie`.
These are 0--4 LLM diagnostic scores for a fixed three-task preflight, not official ChemCrow paper
scores.

### 8.2 Stopped full-v3

The sealed subset audit passes for 12 task-local pairs (`01`--`10`, `12`, `13`):

- 12 unique artifacts and 12 Core injection receipts;
- 60 terminal phases;
- 302 tool observations: 250 live and 52 cache replay;
- 60 explicit tool errors;
- zero MOCK/fixture observations;
- exact same S0 as preflight-v3.

Task 14 has terminal baseline, evolution-evaluator, and Reflector phases, but its evolved phase is
only `claimed`; it has no eligible pair result. Task 15 has no claims and was never started. The
run has `STOPPED_BY_USER.json`, no aggregate, and must remain stopped.

The prepared minimum closure preserves these 12 pairs and creates a new isolated task-14/task-15
repair only after a new user-bound duplicate-task receipt. The composite audit then validates all
14 pair hashes, S0, unique artifacts, exact reset/injection evidence, and the interrupted claim
binding before paper evaluation can see historical answers.

## 9. Credential and budget state

Only names/status are reported; no secret, balance, limit, usage, account label, or identifier is
stored.

- `/home/lhy-h/work/chemcrowrun/.env.paper-evaluator`: mode 0600.
- `OPENROUTER_API_KEY`: present and authentication-valid.
- `/api/v1/key`: HTTP 200.
- `/api/v1/credits`: HTTP 200 with required fields.
- Frozen evaluator list-price ceiling: `$11.83266` for 42 calls.
- Capacity result: `sufficient_remaining_for_frozen_ceiling=true`.
- Latest probe status: `VALID`.
- `CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION`: absent.
- Model calls during the zero-paid probe: 0.
- Separate provider request attempts: 2 (v1 and v2); successful completions: 0; billing is unknown
  because neither 403 response included a usage receipt.

## 10. Historical action list — superseded

This section records the 2026-08-25 snapshot and is not current run authority. Use
`FULL_RUN_READINESS.md`; do not perform Dashboard repair or a two-task full-v3 continuation.

1. The former OpenRouter guardrail hypothesis is resolved as a request-egress difference; no
   Dashboard change is required.
2. Do not authorize a fresh isolated full-v3 task-14 repair for the authoritative result.
   If approved, the exact literal is
   `I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP`; it will create a user-bound receipt tied to
   prior claim SHA-256
   `bfa5d6f880bb0f8b1f0149ceef364c51e2a0fe2e290734e0ca3827078fcae40b`.
3. Keep paid paper authorization absent until the 14-task composite audit and paper plan preflight
   pass. Later authorization is `I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS`.
4. Review/accept the RXN-Sandbox OpenMDW 1.1 and MolBloom/SureChEMBL terms for formal claims.
5. Decide whether optional SerpAPI is part of the frozen formal profile; it is currently excluded.
6. Approve task-12 safety scoring semantics and never treat intended refusal as execution success.
7. Recruit four independent expert chemists for the paper-comparable 168-form blind review; a
   smaller panel is exploratory.
8. Accept the `PROMPT_COMPATIBLE`, `PROVISIONAL_LLM_JUDGED_RESULT`, and modern-model/provider drift
   qualifications.
9. Give a fresh explicit instruction before any repair, full run, or 42-call paper evaluation is
   started. The current instruction is to test and report only.

## 11. Commands that are safe now

Credential/capacity check only, with zero model calls:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source /home/lhy-h/work/chemcrowrun/.env.paper-evaluator
set +a
uv run --project benchmarks/chemcrow openevo-chemcrow paper-credential-probe \
  --output /home/lhy-h/work/chemcrowrun/reports/OPENROUTER_CREDENTIAL_PROBE.json \
  --no-model-calls
```

Repair preflight only, with zero model calls:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source benchmarks/chemcrow/.env
set +a
uv run --project benchmarks/chemcrow openevo-chemcrow preflight \
  --config benchmarks/chemcrow/configs/paper_repair.v1.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_REPAIR_PREFLIGHT.json \
  --no-model-calls
```

The second command remains blocked on the absent duplicate-task authorization receipt. No paid
run/resume/paper-evaluator command is authorized or recommended at the current state.

## 12. Known differences from the original paper

- The full original EvaluatorGPT prompt is not public; the reconstruction is semantic-compatible.
- Current OpenRouter `openai/gpt-4` is not guaranteed to be the 2023 model snapshot.
- The public ChemCrow repository omits restricted paper tools.
- Local RXN and modern RDKit differ from paper-era versions/services.
- LiteratureSearch, procurement pricing, and arbitrary Python are excluded.
- Public network observations can drift; only identical within-pair calls are replayed.
- Human review uses final responses without ReAct trajectories; this masks the most obvious runtime
  style but is not a verbatim copy of the paper's private evaluation sheet/summarization process.
- The current full run is 12/14 sealed, not a complete benchmark.
- LLM judge outputs remain provisional and cannot replace expert chemistry review.

## 13. Branch milestone history

The integration branch contains these logical milestones after `origin/stable`:

1. `29ecef183` task-local ChemCrow integration
2. `53e819ce3` readiness gates
3. `d6e04a138` Core-only runtime and local RXN
4. `431b62193` scored safety classification
5. `a8cf59e9e` infrastructure evidence
6. `af738da30` fixed preflight authorization
7. `e4c3ba724` Core injection/tool bridge fail-closed checks
8. `75f538914` failed tool observation preservation
9. `7691780f8` invalid preflight sealing
10. `5271764cf` isolated preflight-v3 authorization
11. `e8b4eb6a8` S0 reconciliation and completed-run audit
12. `7546cebbd` full reduced-profile freeze
13. `ba93c06af` loopback control isolation
14. `6e1337834` batched bridge receipt support
15. `a7f7f4b08` sealed paper evaluator Core route
16. `f98b92e49` paper evaluator readiness evidence
17. `3559a45b0` fail-closed 12+2 repair
18. `0826c2ac1` preserved-12 plus repair documentation
19. `951aaf2ed` repair execution-parity gate
20. `d63884101` execution-parity evidence
21. `6f8838fb3` official scoring and credit gates
