# ChemCrow task-local OpenEvo readiness report

Generated: 2026-08-25 Asia/Shanghai

Status: **INTEGRATION IMPLEMENTED AND ZERO-MODEL GATES PASS; PAID PREFLIGHT HAS NO VALID RESULT; FULL 14-TASK RUN BLOCKED.**

Two bounded paid attempts stopped fail-closed on the first task. Their outputs are engineering evidence only and are excluded from metrics. No full benchmark was started, no phase was retried, and neither `chemcrow-03` nor `chemcrow-06` was submitted.

## A. Repository setup

- Workspace: `/home/lhy-h/work/chemcrowrun`
- OpenEvo clone: `/home/lhy-h/work/chemcrowrun/openevo`
- OpenEvo origin: `git@github.com:Ardorry/OpenEvo.git`
- Integration branch: `chemcrow-task-local-evolution-v1`
- Base branch/commit: `stable` / `158f48ea240a8a92bca6827e0ebd0ad418674e48`
- Current implementation commit before this report commit: `75f53891463e3884ef4fb742b05e2954d0f87f51`
- Initial machine-readable repository receipt: `manifests/repository_state.initial.json`
- Existing research checkouts were inspected but not modified. Vendor worktrees remain unmodified.

## B. OpenEvo version and mandatory execution route

OpenEvo package version is `0.1.10`. Candidate, Reflector, evolution evaluator, and final evaluator all use:

`TaskRequest -> OpenEvo Rollout :8080 -> Gateway :8100 -> managed Docker -> native Codex harness`

The exact managed image is
`sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`.
Host `codex exec`, caller MCP injection, custom shells/runtimes, mutable images, and non-subscription auth are rejected for all model roles.

OpenEvo Evolution Backend is enabled at `127.0.0.1:8200`. The verified framework wheel SHA-256 is
`1782a894984a7db833ebd031e802d5780cd635c82c12cb9a8004e5c73c586bc5`; its lock SHA-256 is
`fd0328568d05787e7ffcdbe6e844d0ab07bf6c3ffb5c326a8f7299f1ae194ab3`.
The native Reflector writes its one task-local artifact into the same Core Evolution Store used by Gateway context resolution.

This is runtime artifact evolution only: no reinforcement learning, fine-tuning, policy training, or model-weight update.

## C. ChemCrow and RXN versions

| Repository | Origin | Commit |
|---|---|---|
| chemcrow-public | `https://github.com/ur-whitelab/chemcrow-public.git` | `e7ebd5193334ac1d8dea137b635721c7cb470d33` |
| chemcrow-runs | `https://github.com/ur-whitelab/chemcrow-runs.git` | `500104ed9a5d479a8dc4128afc463625ade5a409` |
| rxn-sandbox | `https://github.com/rxn4chemistry/rxn-sandbox.git` | `d56aad22564a904a2fc737460adfbf0d4c4e9ac2` |
| rxn4chemistry | `https://github.com/rxn4chemistry/rxn4chemistry.git` | `d1ce65a180a55981be8321894f6a7f88c416236e` |

ChemCrow is `0.3.24`; its legacy GPT-4-0613/LangChain agent is not the Candidate. Local RXN uses pinned Compose image IDs and verified Pistachio2025Q2 model files. Hosted RXN is optional and not used in the current local profile.

## D. Adapter implementation

- Static-AST task extraction reads only original prompt literals.
- A LangChain-independent ChemCrow tool registry exposes chemistry tools through a pair-scoped REST/MCP service.
- Managed Docker reaches the service through `host.docker.internal`; the host receipt reader uses loopback.
- Every declared call, including unavailable/invalid calls, now has a structured observation receipt. Identical success or error calls may replay only within the same pair.
- Candidate transcript auditing rejects built-in web search and any mismatch between bridge commands and receipts.
- Baseline requests select an exact empty artifact inventory. Evolved requests require exactly one task-local artifact and an exact Core runtime-injection receipt.
- Feedback modes F0/F1/F2, separate evolution/final evaluators, blinded randomized A/B final judging, provisional LLM labels, and human-review packets are implemented.
- Phase claims, pair results, reset receipts, S0 hashes, artifact receipts, and fail-closed resume are durable.

## E. Tool availability matrix

| Tool(s) | Class | Current state | Formal limitation/action |
|---|---:|---|---|
| MolSimilarity, SMILES2Weight, FunctionalGroups | A | local PASS | none |
| ControlChemCheck, SimilarityToControlChem | A | local PASS | pinned vendor CSV |
| PatentCheck | A | local PASS | approve MolBloom/SureChEMBL terms |
| Name2SMILES, Mol2CAS, SMILES2Name | B | PubChem smoke PASS | public-network drift |
| wikipedia | B | network smoke PASS | public-network drift |
| ExplosiveCheck, SafetySummary | B | PubChem smoke PASS | deterministic evidence, no hidden LLM |
| ReactionPredict | D | local RXN PASS | approve RXN license/profile |
| ReactionRetrosynthesis | D | endpoint PASS, canary empty | no fabricated success; quality unproven |
| WebSearch | C | explicit unavailable observation | optional `SERP_API_KEY` |
| LiteratureSearch | F | excluded | hidden legacy model calls violate fairness |
| GetMoleculePrice | F | excluded | procurement/commercial boundary |
| python_repl | F | excluded | arbitrary execution outside tool surface |
| Paper-only restricted tools | E | unavailable | absent from public repository |

Unavailable tools never return fake/stub output in real mode. MOCK exists only for unit tests and cannot enter real metrics.

## F. Dependency/environment status

- Adapter: Python `3.11.15`, RDKit `2025.9.6`, MCP `1.29.0`, MolBloom `2.3.5`, HTTPX `0.28.1`, Pydantic `2.13.4`.
- Adapter lock SHA-256: `011c365473621b11bdf0b20cc1a12343a9c80fd1744264a02dab917c2e4bbfdd`.
- Core: Python `3.12.14`; root lock SHA-256: `86029997b93bb4500b9f85dd014cfb11aa5905740a278283a577a5d1eb09af93`.
- Benchmark manifest SHA-256: `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`.
- S0 config SHA-256: `7314ed4fedd488632d9690937c00e6d271af8f373cc512b321b9b81863f8572e`.

## G. Credential names detected

Present by name only: `OPENEVO_ROLLOUT_BASE_URL`, `OPENEVO_CANDIDATE_MODEL`,
`OPENEVO_REFLECTOR_MODEL`, `OPENEVO_EVOLUTION_EVALUATOR_MODEL`,
`OPENEVO_FINAL_EVALUATOR_MODEL`, `CHEMCROW_RXN_PREDICT_URL`,
`CHEMCROW_RXN_RETRO_URL`, `CHEMCROW_RXN_TIMEOUT_SECONDS`, and the Core-managed
`Codex subscription auth.json` source.

Optional/absent by name: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `RXN4CHEM_API_KEY`,
`RXN4CHEM_PROJECT_ID`, `RXN4CHEMISTRY_BASE_URL`, `CHEMSPACE_API_KEY`,
`SERP_API_KEY`, and `SEMANTIC_SCHOLAR_API_KEY`.

No credential value is recorded. `.env` is ignored and `.env.example` contains names only.

## H. Sanitized task set

The scored manifest contains 14 IDs:
`chemcrow-01`, `02`, `03`, `04`, `05`, `06`, `07`, `08`, `09`, `10`, `12`, `13`, `14`, `15`.

`chemcrow-12` is a safety-sensitive scored item. The separate non-scored safety demonstration is
`chemcrow-safety-nitroglycerin`. The fixed preflight selection was `chemcrow-02`, `03`, `06`; it was not changed after outcomes.

## I. Leakage audit

Status: **PASS**. Candidate and Reflector receive no historical ChemCrow/GPT-4 answer, trajectory, evaluator feedback, expert grade, paper conclusion, or notebook output. Each sanitized item records source notebook, extraction method, source SHA-256, and sanitized-item SHA-256. The leakage-audit receipt SHA-256 is
`de4f5b87d9187e95d1d8c4b40d485b58381a9241e22761a2f0366d64bb50a585`.

## J. Isolation and invariant evidence

Post-fix adapter suite: **21 passed**. Post-fix targeted Core Evolution/runtime tests: **34 passed** (21 materialization/framework/store plus 13 exact-context/injection tests). The earlier Core regression subset also passed 85 tests. Ruff and `git diff --check` pass.

A zero-model Core diagnostic proved exact-ID resolution and managed-runtime readback:
`context_injected=true`, exact artifact ID, receipt schema 3, runtime file hashes, and shell return code 0. Exact empty selection returned no artifacts. The diagnostic outer status was `ERROR` only because a shell harness has no assistant transcript; it is not a Candidate result.

Mandatory assertions cover identical S0, empty baseline inventory, one same-task artifact for evolved, exact injection receipt, one evolution step, model/config parity, cross-task reset, evaluator separation, pair cache scope, and mock/real separation.

## K. Paid preflight outcome

There is **no valid scientific preflight result** and therefore no baseline/evolved score, delta, win/loss/tie, or aggregate metric.

1. `preflight-v1` stopped on `chemcrow-02`. Five phases may have provider effects: baseline, evolution evaluator, Reflector, evolved, and final evaluator. It was invalidated because Evolution was disabled, the tool bridge advertised container-local loopback, and built-in web search appeared after bridge failure. Receipt:
   `runs/preflight-v1/INVALIDATED.json`, SHA-256 `441b3af90a96541af1ca5e722814bb448d422b4300a46f06dfd32b8070d21e7b`.
2. `preflight-v2` stopped after only the `chemcrow-02` baseline. It made 21 declared bridge calls; all reached the service, but seven explicit HTTP 422 errors were omitted from the old receipt list, so the 21-versus-14 audit failed. No evaluator, Reflector, evolved run, or later task was submitted. Receipt:
   `runs/preflight-v2/INVALIDATED.json`, SHA-256 `9f217b3fe3fe039441baefb7503cd136f25c272ffea05e8d529f3d1b5a71483e`.

The error-receipt defect is fixed and tested, but `chemcrow-02` is not retried automatically. All claims, completions, caches, and logs remain sealed.

## L. Human actions required

See `HUMAN_ACTION_REQUIRED.md`. The immediate blocking decision is whether a new replacement preflight may deliberately repeat `chemcrow-02`, or whether the experiment must remain closed/incomplete. Full execution also requires tool-profile/license/evaluator/safety decisions and later expert review.

## M. Full launch command after all gates are resolved

Do not run this yet:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow run --config configs/full.example.yaml --allow-paid
```

## N. Safe resume command

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow resume --config configs/full.example.yaml --allow-paid
```

Resume skips only hash-verified sealed pairs. An incomplete claimed phase stops for audit and is never silently redispatched.

Required service startup after reboot is documented in `HUMAN_ACTION_REQUIRED.md`, including RXN, verified Evolution Backend, Rollout, and Gateway.

## O. Known limitations versus the ChemCrow paper

1. Restricted paper tools are absent from the public source.
2. Historical notebook answers/evaluations are intentionally excluded.
3. RXN-Sandbox Pistachio2025Q2 is not the paper-era hosted RXN state.
4. Price/procurement tasks lack the excluded price tool.
5. LiteratureSearch is excluded because it adds hidden legacy model calls.
6. Modern RDKit/RXN versions differ from paper-era versions.
7. SafetySummary is deterministic PubChem evidence, not a hidden GPT summarizer.
8. The local retro canary returned an empty result and upstream uses `dfl` where documentation says `fld`.
9. Network tools may drift; paired identical calls are cached only within a pair.
10. Any future LLM final-judge result is **PROVISIONAL LLM-JUDGED RESULT** pending blinded chemistry-expert review.
11. The two current paid attempts are engineering-invalid and must never be reported as ChemCrow benchmark results.
