# ChemCrow Task-Local OpenEvo Integration Readiness Report

Status: **BLOCKED_HUMAN_ACTION_REQUIRED; no full benchmark started**.

This integration is reproducible and its offline/public-network/MCP boundaries have been tested,
but it is not yet authorized or infrastructurally ready for paid model execution. No Candidate,
Reflector, evolution-evaluator, or final-evaluator call was made.

## A. Repository setup

- Workspace: `/home/lhy-h/work/chemcrowrun`
- OpenEvo clone: `/home/lhy-h/work/chemcrowrun/openevo`
- Integration branch: `chemcrow-task-local-evolution-v1`
- Initial machine-readable state: `manifests/repository_state.initial.json`
- Vendor repositories are clean and were not edited.
- Source discovery selected `/home/lhy-h/work/chembench-per-item-evolution` because it is a clean
  checkout of the research fork containing the prior native per-item Core/Reflector conventions.
  Its source state was branch `chembench-per-item-evolution-v1`, commit
  `d1d7a22b88ce662aab45a8dc216f4a654ca545be`, origin
  `git@github.com:Ardorry/OpenEvo.git`.
- Cross-checks: `/home/lhy-h/work/compare2` and
  `/home/lhy-h/work/researchclaw_openevo/OpenEvo` have the same research-fork identity but dirty
  experiment worktrees; `/home/lhy-h/work/researchclaw_native_openevo_r6` points to public
  upstream and has untracked experiment files. `/home/lhy-h/work/OpenEvo-researchclaw` was absent.

## B. OpenEvo version actually cloned

| Field | Value |
|---|---|
| Research origin | `git@github.com:Ardorry/OpenEvo.git` |
| Public upstream | `git@github.com:CompLifeLab-ZJU/OpenEvo.git` |
| Clone base branch | `stable` |
| Clone base SHA | `158f48ea240a8a92bca6827e0ebd0ad418674e48` |
| Package version | `0.1.10` |
| Integration milestone | `29ecef183` (`bench: add task-local ChemCrow integration`) |

The implementation reuses native `ArtifactType`, `EvolutionStore`, event ingestion, dataset
materialization, job claim/heartbeat/completion, `run_method`, and exact context artifact IDs. It
does not implement reinforcement learning, training, fine-tuning, or weight updates.

## C. ChemCrow versions

| Repository | Origin | Branch | SHA | Clone timestamp UTC |
|---|---|---|---|---|
| chemcrow-public | `https://github.com/ur-whitelab/chemcrow-public.git` | `main` | `e7ebd5193334ac1d8dea137b635721c7cb470d33` | `2026-08-24T16:30:30Z` |
| chemcrow-runs | `https://github.com/ur-whitelab/chemcrow-runs.git` | `main` | `500104ed9a5d479a8dc4128afc463625ade5a409` | `2026-08-24T16:30:30Z` |

ChemCrow package version is `0.3.24`. Vendor metadata requires Python `>=3.9,<3.12`, OpenAI
`0.27.8`, PaperQA `1.1.1`, and LangChain `0.0.234..0.0.275`. The legacy agent is intentionally
not imported into the Candidate environment; an actual import in the adapter environment fails
at missing `langchain`, as designed. The official README warns that public ChemCrow omits
API-restricted paper tools and cannot reproduce the paper results
(<https://github.com/ur-whitelab/chemcrow-public>).

## D. Adapter implementation

The standalone package lives at `benchmarks/chemcrow/` and leaves `src/openevo` unchanged.

- `tasks.py`: static-AST extraction of one `task`/`prompt` literal per scored notebook; cell
  outputs are counted for audit but never read into the sanitized item.
- `tools.py`: a compatibility registry around ChemCrow's public tool semantics without the old
  LangChain GPT-4 agent. It uses lazy local/network dependencies and explicit unavailability.
- `mcp_server.py` and `tool_service.py`: one pair-scoped MCP service kept alive across baseline
  and evolved runs.
- `cache.py`: canonical `(tool name, arguments)` replay inside one pair only; sources are
  `live`, `cache_replay`, `fixture`, or `mock`. Real metrics reject fixture/mock.
- `runtime.py`: native OpenEvo `TaskRequest` construction and rollout polling. Baseline requests
  carry `context_artifact_ids=[]`; evolved requests carry exactly `[artifact_i]`.
- `native_evolution.py`: exactly one native Core
  `event -> dataset -> job -> Reflector method -> sealed artifact` transition. Initial type is
  `text_memory_reflector`; mappings also support `skill_bundle_reflector` and
  `agent_system_reflector`.
- `feedback.py`: F0 trajectory; F1 trajectory plus runtime feedback; F2 adds evolution-evaluator
  critique. Historical answers are never an input.
- `evaluation.py`: distinct evolution and final evaluator roles/prompts/configs. Final comparison
  receives randomized A/B answers.
- `protocol.py`: strict S0/baseline/feedback/one-Reflector/evolved/final/seal/reset state machine,
  pair schema, human-review packets, and cross-item artifact rejection.
- `ledger.py`: durable pre-dispatch claims. Resume skips sealed pairs and refuses any ambiguous
  claimed external phase instead of duplicating a provider call.

The observable trajectory includes tool names/arguments/results, explicit errors, retry count,
visible assistant messages, final answer, wall time, and token/cost metadata when supplied. It
does not request hidden chain-of-thought.

## E. Tool availability matrix

Full machine-readable inventory: `reports/TOOL_INVENTORY.json` (19 entries, SHA-256
`c2e17f0ecbddba316f96164a1f725f29b32648fdf7b929fcaf3d5d50c1a0cc30`).

| Tool | Class | Current state | Required dependency/service | Human action |
|---|---:|---|---|---|
| `MolSimilarity` | A | PASS local smoke | RDKit | No |
| `SMILES2Weight` | A | PASS local smoke | RDKit | No |
| `FunctionalGroups` | A | PASS local smoke | RDKit | No |
| `ControlChemCheck` | A | PASS local smoke | RDKit + pinned vendor CSV | No |
| `SimilarityToControlChem` | A | PASS local smoke | RDKit + pinned vendor CSV | No |
| `PatentCheck` | A | PASS local smoke | MolBloom/SureChEMBL Bloom data | Review data/version |
| `Name2SMILES` | B | PASS live public-network smoke | PubChem | No, network required |
| `Mol2CAS` | B | PASS live public-network smoke | PubChem | No, network required |
| `SMILES2Name` | B | PASS live public-network smoke | PubChem | No, network required |
| `wikipedia` | B | PASS live public-network smoke | MediaWiki | No, network required |
| `ExplosiveCheck` | B | PASS live public-network smoke | PubChem | No, network required |
| `SafetySummary` | B | PASS live public-network smoke; deterministic evidence extract | PubChem | No hidden LLM |
| `WebSearch` | C | BLOCKED | SerpAPI; `SERP_API_KEY` | Optional account/key |
| `ReactionPredict` | C/D | BLOCKED | Hosted IBM RXN or verified local service | Required for RXN profile |
| `ReactionRetrosynthesis` | C/D | BLOCKED | Hosted IBM RXN or verified local service | Required for RXN profile |
| `LiteratureSearch` | F | EXCLUDED | Legacy PaperQA/LangChain/OpenAI/Semantic Scholar | New fair design required |
| `GetMoleculePrice` | F | EXCLUDED | ChemSpace commercial/procurement API | Remains excluded |
| `python_repl` | F | EXCLUDED | Arbitrary execution | Remains excluded |
| Paper-only restricted tools | E | UNAVAILABLE | Not present in public repo | Cannot faithfully recover |

No unavailable tool returns a fake real-mode result. Mock mode requires an explicit matching
fixture and is rejected by real aggregate metrics.

### RXN audit

- Hosted vendor code targets `https://rxn.res.ibm.com`, embeds a historical project ID, and uses
  an obsolete action-summary model. The adapter requires an explicit `RXN4CHEM_PROJECT_ID` and
  returns raw tool evidence rather than making the hidden summarizer call.
- The official `rxn4chemistry` project announces hosted platform/API end-of-service on
  **2026-10-28**: <https://github.com/rxn4chemistry/rxn4chemistry>.
- Legacy ChemCrow images are not reproducible: `rxnpred:latest` had no manifest;
  `retrosynthesis:latest` was denied. No image was pulled.
- Official RXN-Sandbox is the credible self-host path, commit
  `d56aad22564a904a2fc737460adfbf0d4c4e9ac2`: it documents approximately 10 GB free disk,
  8 GB RAM minimum/16 GB recommended for tree search, optional NVIDIA GPU, and six services
  (<https://github.com/rxn4chemistry/rxn-sandbox>). It was not cloned because that is a
  substantial download requiring a license/runtime decision.
- Current machine: Docker Desktop server 29.5.2, overlayfs, 22 CPUs; host NVIDIA RTX 4050 6 GB
  and Docker `nvidia` runtime detected. GPU-in-container was not canaried because no image was
  approved for download.

## F. Dependency and environment status

Chosen intersection: Python **3.11.15**.

| Component | Locked version |
|---|---:|
| OpenEvo | 0.1.10, editable exact clone |
| openevo-chemcrow | 0.1.0 |
| RDKit | 2025.9.6 |
| MCP | 1.29.0 |
| MolBloom | 2.3.5 |
| HTTPX | 0.28.1 with SOCKS support |
| Pydantic | 2.13.4 |

`uv.lock` SHA-256:
`011c365473621b11bdf0b20cc1a12343a9c80fd1744264a02dab917c2e4bbfdd`.
The adapter environment is isolated at `benchmarks/chemcrow/.venv`. The first public-network
smoke exposed missing SOCKS support; `socksio==1.0.0` was added to the lock, then all three public
canaries passed.

## G. Credentials detected by name only

All of these names were absent; no value was recorded:

`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENEVO_ROLLOUT_BASE_URL`,
`OPENEVO_CANDIDATE_MODEL`, `OPENEVO_REFLECTOR_MODEL`,
`OPENEVO_EVOLUTION_EVALUATOR_MODEL`, `OPENEVO_FINAL_EVALUATOR_MODEL`,
`RXN4CHEM_API_KEY`, `RXN4CHEM_PROJECT_ID`, `RXN4CHEMISTRY_BASE_URL`,
`CHEMSPACE_API_KEY`, `SERP_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`,
`CHEMCROW_RXN_PREDICT_URL`, `CHEMCROW_RXN_RETRO_URL`, and
`CHEMCROW_FULL_RUN_AUTHORIZATION`.

`.env` and `.env.*` remain ignored; a precise exception tracks `.env.example` with variable
names and empty values only.

## H. Sanitized task count and IDs

Scored manifest: **14** items:

`chemcrow-01`, `02`, `03`, `04`, `05`, `06`, `07`, `08`, `09`, `10`, `12`, `13`, `14`, `15`.

Manifest SHA-256:
`bdc46f270f3c369e7e229cd3214d15122748d171373c660e9e4aeab1f0cdc1c4`.

Separate non-scored safety manifest: one item, `chemcrow-safety-nitroglycerin`, SHA-256
`c31dfd9d11040e1600ecd025c78240ce076072170fc0571e7099004b374f4afd`.

Preflight selection was fixed before execution: `chemcrow-03`, `chemcrow-06`,
`chemcrow-12`. Authorization remains `NOT_AUTHORIZED`.

## I. Leakage audit

Result: **PASS**.

- Each scored notebook has exactly one static string literal assigned to `task` or `prompt`.
- Sanitized fields are closed to task ID, prompt, broad category, allowed-tool metadata,
  safety metadata, provenance, and sanitized-item hash.
- Notebook outputs, ChemCrow/GPT outputs, tool trajectories, Evaluator outputs, expert grades,
  and paper conclusions are excluded.
- Every item records source notebook, full notebook SHA-256, cell/variable extraction method,
  and sanitized item SHA-256.
- Non-scored paper figures, robotic-platform notebooks, reproducibility notebook, and the
  chromophore notebook are excluded from `tasks.jsonl`.
- Safety demonstration is separately extracted and cannot be selected through the scored
  manifest.

Audit receipt: `reports/LEAKAGE_AUDIT.json`, SHA-256
`00abe267858b484bcfd1a805f0f004e6c985bf13195f00e28bf1a2d816828b0c`.

## J. Task-local invariant tests

Focused suite: **13 passed**. Ruff, `compileall`, and `git diff --check` pass.

Covered:

- task extraction and closed-schema no-answer leakage;
- scored/safety separation;
- local tool wrappers and explicit missing-key/unavailable behavior;
- real/mock separation;
- pair-only cache replay;
- trajectory and F0/F1/F2 serialization;
- baseline/evolved model/runtime/tool parity;
- distinct evolution/final evaluator configurations;
- exactly one native Core Reflector job/artifact;
- baseline has no artifact; evolved has the same-item artifact only;
- reset before next item and stable S0 hash;
- durable ambiguous-claim resume refusal.

Frozen S0 config hash from current preflight config:
`ac6e24a863e1dce5fdfefb4e00077d750871d955efd039db738598a82ff606cb`.

## K. Preflight results

| Check | Result |
|---|---|
| Unit/invariant suite | 13 passed |
| Local tools | 6/6 PASS |
| Public network tools | 6/6 PASS |
| MCP transport | PASS; 15 tools listed |
| Pair observation replay | first `live`, second `cache_replay` |
| Docker daemon | PASS |
| Candidate/evolved config parity | PASS |
| Model calls | 0 |
| Paid operations | 0 |
| Paid 3-task preflight | NOT STARTED |
| Formal readiness | `BLOCKED_HUMAN_ACTION_REQUIRED` |

Blocking checks are the immutable Candidate runtime image and the five OpenEvo endpoint/model
environment names. RXN-dependent tasks also remain blocked.

## L. Human actions required

See `reports/HUMAN_ACTION_REQUIRED.md`. It provides exact variables/commands, affected scope,
and verification for credentials, runtime image/services, RXN/GPU, deprecations, licenses,
model authentication, expert review, paid authorization, blocked tasks, and design decisions.

## M. Full task-local launch command once ready

Only after the no-model preflight reports `READY`, the fixed paid preflight succeeds, and the
human decisions are recorded:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
cp configs/full.example.yaml configs/full.yaml
# Edit only the approved immutable runtime image and frozen scientific configuration.
# Load secrets/model names from a private ignored environment; never commit them.
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow run --config configs/full.yaml --allow-paid
```

Running this command now fails closed because the example still contains a human-action
placeholder and credentials/model services are absent.

## N. Safe resume command

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow resume --config configs/full.yaml --allow-paid
```

Resume verifies sealed pair/reset receipts and skips them. An incomplete phase with a durable
claim is treated as an ambiguous possible provider-side effect and is not redispatched. If all
phase claims are terminal but the pair seal is missing, automatic reconstruction also stops for
audit rather than inventing state.

## O. Known limitations relative to the original ChemCrow paper

1. Public ChemCrow explicitly omits API-restricted paper tools; exact paper reproduction is
   impossible from these sources alone.
2. The 14 notebooks contain historical outputs/evaluations, but those are deliberately excluded;
   therefore no notebook answer acts as GT or Reflector supervision.
3. Hosted IBM RXN is approaching end-of-service; old ChemCrow Docker images are unpinned and
   currently unavailable. RXN-Sandbox is newer and may not reproduce paper-era models.
4. Adapter RDKit 2025.9.6 is modern and locked, not necessarily the paper-era RDKit version.
5. `SafetySummary` returns deterministic PubChem evidence instead of making the vendor's hidden
   legacy LLM summary call. Retrosynthesis also returns raw service evidence rather than the
   vendor's obsolete GPT recipe summarizer.
6. `LiteratureSearch` is excluded because its old PaperQA/OpenAI embedding and LLM calls would
   violate model-role fairness unless separately redesigned and costed.
7. LLM rubric judgments are explicitly `PROVISIONAL LLM-JUDGED RESULT`; definitive claims need
   blinded expert chemistry review.
8. Most tasks have no exact numerical ground truth, so reporting uses rubric deltas,
   wins/losses/ties, tool reliability, runtime, and cost rather than a single accuracy number.
9. The actual paid managed-runtime route has not been canaried in this workspace because its
   runtime image, endpoint, models, and authentication are intentionally unbound.
