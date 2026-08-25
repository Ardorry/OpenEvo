# ChemCrow task-local OpenEvo readiness report

Status: **INFRASTRUCTURE READY; PAID/MODEL PREFLIGHT NOT RUN; FULL BENCHMARK BLOCKED**.

The zero-foundation-model preflight is `READY`. OpenEvo Core Rollout and Gateway are running,
the managed Codex image is immutable and present, the local RXN service is running, and focused
tests pass. No Candidate, Reflector, evolution evaluator, or final evaluator was called. The fixed
three-task model preflight and the full 14-item experiment remain gated by explicit paid-run
authorization and the human decisions in `HUMAN_ACTION_REQUIRED.md`.

## A. Repository setup

- Workspace: `/home/lhy-h/work/chemcrowrun`
- OpenEvo clone: `/home/lhy-h/work/chemcrowrun/openevo`
- Integration branch: `chemcrow-task-local-evolution-v1`
- Initial repository receipt: `manifests/repository_state.initial.json`
- Selected source checkout: `/home/lhy-h/work/chembench-per-item-evolution`, corroborated against
  the local Compare2 and ResearchClaw checkouts. It is the research fork with the prior native
  per-item Core/artifact conventions.
- Existing research checkouts were inspected but not modified. ChemCrow and RXN vendor worktrees
  are clean; compatibility changes live only in this integration branch.

## B. OpenEvo version and mandatory Core route

| Field | Value |
|---|---|
| Research origin | `git@github.com:Ardorry/OpenEvo.git` |
| Public upstream | `git@github.com:CompLifeLab-ZJU/OpenEvo.git` |
| Clone base | `stable` at `158f48ea240a8a92bca6827e0ebd0ad418674e48` |
| Package | OpenEvo `0.1.10` |
| Core environment | Python `3.12.14`; root `uv.lock` SHA-256 `86029997b93bb4500b9f85dd014cfb11aa5905740a278283a577a5d1eb09af93` |
| Core-only implementation milestone | `d6e04a1387fe8075960b17bfa40ddc94786e4c32` |
| Safety-classification milestone | `431b62193c412eafd2a590ab10345056f1795743` |

Every Codex role is forced through:

`OpenEvo TaskRequest -> Rollout :8080 -> Gateway :8100 -> managed Docker -> CodexHarness`

This applies to Candidate, Reflector, evolution evaluator, and final evaluator. The adapter
rejects non-Docker runtimes, mutable/unmanaged images, custom runtime loaders, custom shells,
caller MCP servers, non-subscription auth, non-Core workdirs/env/prepare recipes, and invalid
transcript builders. The former Reflector `codex_cli -> subprocess -> host codex exec` route was
removed. Static tests assert that no host Codex/run-method path remains.

Managed runtime image:
`sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`,
with `io.openevo.managed-runtime=true`.

The Core topology is `configs/openevo_core_topology.yaml`. It has one registered healthy Gateway
and one worker per phase to bound concurrency. Its unused local-inference health endpoint reports
connection failure because no vLLM server is configured; subscription Codex uses the managed
CodexHarness path, not that inference proxy. No fake inference server was added to make health
look green.

## C. ChemCrow and RXN versions

| Repository | Origin | Branch | SHA | Clone time |
|---|---|---|---|---|
| chemcrow-public | `https://github.com/ur-whitelab/chemcrow-public.git` | `main` | `e7ebd5193334ac1d8dea137b635721c7cb470d33` | `2026-08-24T16:30:30Z` |
| chemcrow-runs | `https://github.com/ur-whitelab/chemcrow-runs.git` | `main` | `500104ed9a5d479a8dc4128afc463625ade5a409` | `2026-08-24T16:30:30Z` |
| rxn-sandbox | `https://github.com/rxn4chemistry/rxn-sandbox.git` | `main` | `d56aad22564a904a2fc737460adfbf0d4c4e9ac2` | `2026-08-25T09:37:50+08:00` |
| rxn4chemistry | `https://github.com/rxn4chemistry/rxn4chemistry.git` | `main` | `d1ce65a180a55981be8321894f6a7f88c416236e` | `2026-08-25T09:37:53+08:00` |

ChemCrow is `0.3.24` and declares Python `<3.12`, legacy OpenAI `0.27.8`, PaperQA `1.1.1`, and
LangChain `0.0.234..0.0.275`. None of its GPT-4-0613/LangChain agent code is the Candidate.

RXN weights were downloaded with workspace-local Git LFS `3.7.1`; the release archive and all
model files were SHA-256 verified. Exact repositories, model hashes, container IDs, selected
container packages, smoke receipts, and upstream defects are sealed in
`reports/RXN_RUNTIME_MANIFEST.json`.

## D. Adapter implementation

- `tasks.py`: static-AST extraction of the sole original prompt literal. Notebook outputs are
  counted but never read into task content.
- `tools.py`: LangChain-independent ChemCrow-compatible chemistry tool registry. Unavailable
  tools return explicit errors; real mode never substitutes fixtures or mocks.
- `mcp_server.py` / `tool_service.py`: pair-scoped tool environment with MCP plus a restricted
  REST bridge. Core subscription policy forbids caller MCP injection, so managed Codex uses
  `curl` to the REST bridge; exact calls and observations are still sealed as receipts.
- `cache.py`: replay only for canonical identical calls within one baseline/evolved pair. Sources
  are `live`, `cache_replay`, `fixture`, or `mock`; fixture/mock cannot enter real metrics.
- `runtime.py`: native Core task submission/polling and trajectory normalization. Baseline has no
  artifact; evolved receives exactly one same-task artifact.
- `native_evolution.py`: native Core event, dataset, job, claim, heartbeat, artifact register,
  completion, and sealing. Reflector inference itself goes through the same Core managed route.
- `feedback.py`: F0 trajectory; F1 adds runtime feedback; F2 adds a separate evolution-evaluator
  critique. No notebook answer is exposed.
- `evaluation.py`: separate evolution/final prompts and configs; final judge receives randomized
  blinded A/B outputs. LLM-only results are labeled provisional.
- `protocol.py` / `ledger.py`: S0 reset, exactly one evolution step, cross-task isolation,
  artifact receipts, blinded human packet, durable call claims, and fail-closed resume.

OpenEvo evolution is runtime artifact evolution only. There is no training, policy update,
fine-tuning, or model-weight change.

## E. Tool availability matrix

Machine-readable inventory: `reports/TOOL_INVENTORY.json`, 19 entries, SHA-256
`832bc7ddc9d3b03fcc3af7b94bfbb9d549293cb01f34eb927638645875d6c623`.

| Tool(s) | Class | Current result | Human action |
|---|---:|---|---|
| MolSimilarity, SMILES2Weight, FunctionalGroups | A | PASS local | None |
| ControlChemCheck, SimilarityToControlChem | A | PASS local with pinned vendor CSV | None |
| PatentCheck | A | PASS local | Accept MolBloom data/version for formal profile |
| Name2SMILES, Mol2CAS, SMILES2Name | B | PASS PubChem network smoke | Network required |
| wikipedia | B | PASS MediaWiki network smoke | Network required |
| ExplosiveCheck, SafetySummary | B | PASS PubChem network smoke | Network required; no hidden LLM |
| ReactionPredict | D | PASS local RXN endpoint; live benign probe returned 3 predictions | Approve RXN license/profile before formal metrics |
| ReactionRetrosynthesis | D | PASS local endpoint; benign probe completed with an empty result | Same; empty is preserved as empty, never synthetic success |
| WebSearch | C | BLOCKED | Optional `SERP_API_KEY` and quota decision |
| LiteratureSearch | F | EXCLUDED | Legacy hidden PaperQA/OpenAI calls violate role fairness |
| GetMoleculePrice | F | EXCLUDED | Procurement/price boundary and commercial API |
| python_repl | F | EXCLUDED | Arbitrary execution outside benchmark tool surface |
| Paper-only restricted tools | E | UNAVAILABLE | Absent from public repository |

RXN service details:

- Four exact-ID containers are running; only MCPO is exposed on loopback port 8300.
- Worker runs CPU mode with concurrency one. An RTX 4050 6 GB and Docker NVIDIA runtime exist,
  but GPU mode was not enabled because CPU was sufficient and avoids small-VRAM OOM drift.
- Upstream unconstrained dependencies initially produced broken `mcp 2.1.0 + mcpo 0.0.20`.
  The integration-only compatibility image pins MCP `1.29.0`; vendor code remains untouched.
- Upstream retrosynthesis reads `dfl` instead of documented `fld`. The adapter sends documented
  fields; the worker currently uses its default `0.2`. This is recorded as a limitation.
- Hosted RXN remains optional and requires `RXN4CHEM_API_KEY` plus `RXN4CHEM_PROJECT_ID`. The
  official project announces hosted/API end-of-service on 2026-10-28.

## F. Dependency and environment status

Adapter environment: Python `3.11.15`, isolated at `benchmarks/chemcrow/.venv`.

| Package | Locked version |
|---|---:|
| OpenEvo | 0.1.10, editable exact clone |
| RDKit | 2025.9.6 |
| MCP | 1.29.0 |
| MolBloom | 2.3.5 |
| HTTPX | 0.28.1 with SOCKS |
| Pydantic | 2.13.4 |

Adapter `uv.lock` SHA-256:
`011c365473621b11bdf0b20cc1a12343a9c80fd1744264a02dab917c2e4bbfdd`.
The RXN worker is a separately frozen Python 3.13 image; its exact image/package receipt is not
claimed to be a source-level lock because upstream requirements are unconstrained.

## G. Credential detection by name only

Present names: `OPENEVO_ROLLOUT_BASE_URL`, `OPENEVO_CANDIDATE_MODEL`,
`OPENEVO_REFLECTOR_MODEL`, `OPENEVO_EVOLUTION_EVALUATOR_MODEL`,
`OPENEVO_FINAL_EVALUATOR_MODEL`, `CHEMCROW_RXN_PREDICT_URL`,
`CHEMCROW_RXN_RETRO_URL`, `CHEMCROW_RXN_TIMEOUT_SECONDS`, and the Core-managed credential source
named `Codex subscription auth.json` (regular file, owner UID 1000, mode 0600).

Absent names: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `RXN4CHEM_API_KEY`,
`RXN4CHEM_PROJECT_ID`, `RXN4CHEMISTRY_BASE_URL`, `CHEMSPACE_API_KEY`, `SERP_API_KEY`,
`SEMANTIC_SCHOLAR_API_KEY`, and `CHEMCROW_FULL_RUN_AUTHORIZATION`.

No credential value was read or recorded. Local `.env` files are ignored; `.env.example` contains
names and empty values only.

## H. Sanitized tasks

Scored manifest: 14 IDs — `chemcrow-01`, `02`, `03`, `04`, `05`, `06`, `07`, `08`, `09`,
`10`, `12`, `13`, `14`, `15`.

- Manifest file SHA-256: `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`
- Safety-sensitive scored item: `chemcrow-12`, explicitly marked as scored but requiring preserved
  safety behavior.
- Separate non-scored safety demonstration: `chemcrow-safety-nitroglycerin`, SHA-256
  `c31dfd9d11040e1600ecd025c78240ce076072170fc0571e7099004b374f4afd`.
- Paid preflight selection, amended before any model execution: `chemcrow-02`, `chemcrow-03`,
  `chemcrow-06`. The superseded selection and reason are preserved in
  `configs/preflight_selection.json`.

## I. Leakage audit

Result: **PASS**. Receipt SHA-256:
`de4f5b87d9187e95d1d8c4b40d485b58381a9241e22761a2f0366d64bb50a585`.

Only task ID, original prompt, broad category, allowed environment metadata, safety metadata,
and provenance enter `tasks.jsonl`. Historical ChemCrow/GPT-4 answers, trajectories, evaluator
outputs, expert grades, paper conclusions, and all notebook outputs are excluded from Candidate
and Reflector inputs. Every item binds source notebook SHA-256, prompt cell/variable, extraction
method, and sanitized item SHA-256. Non-task notebooks are excluded by path.

## J. Isolation and route tests

- Integration suite: **18 passed**; Ruff, compileall, and `git diff --check` pass.
- OpenEvo Core credential/runtime regression subset: **85 passed**.
- Covered extraction, leakage, scored/demonstration safety separation, tool wrapping, trajectory
  and feedback serialization, explicit unavailable/missing-key behavior, mock/real separation,
  REST/MCP receipt capture, pair cache isolation, role separation, pair config parity, native Core
  Reflector artifact generation, S0/reset/cross-item isolation, exactly one evolution step, and
  fail-closed resume.
- Frozen S0 config SHA-256:
  `7314ed4fedd488632d9690937c00e6d271af8f373cc512b321b9b81863f8572e`.

## K. Preflight results

| Check | Result |
|---|---|
| No-foundation-model preflight | READY |
| OpenEvo Rollout/Gateway | Reachable; one registered healthy node |
| Managed Codex image | Present, immutable ID, managed label true |
| Core role-route validation | 4/4 PASS |
| Local tool smoke | 6/6 PASS |
| Public network smoke | 6/6 PASS |
| RXN OpenAPI | 3 expected endpoints present |
| RXN forward smoke | PASS, live, 3 predictions |
| RXN retro smoke | PASS_EMPTY_RESULT, live, no fabricated result |
| Integration/Core tests | 18 + 85 passed |
| Candidate/Reflector/evaluator calls | 0 |
| Paid operations | 0 |
| Fixed three-task model preflight | NOT AUTHORIZED / NOT RUN |
| Full benchmark | NOT STARTED |

Preflight receipt: `/home/lhy-h/work/chemcrowrun/runs/preflight-v1/preflight.json`, SHA-256
`8b37a7e628fafe7e397f1f0ebff52c79f4dcc951c135526ee63064a2947a5084`.

## L. Human actions required

See `reports/HUMAN_ACTION_REQUIRED.md`. The remaining blockers are not downloads: they are the
RXN license/profile decision, optional paid-tool decisions, explicit authorization for the fixed
model preflight, evaluator-design choices, safety-item scoring policy, and later blinded chemistry
expert review.

## M. Exact full launch command once those gates are resolved

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow run --config configs/full.example.yaml --allow-paid
```

This command is technically configured but must not be used until the human decisions are
recorded and the fixed three-task paid preflight has been reviewed.

## N. Safe resume command

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow resume --config configs/full.example.yaml --allow-paid
```

Resume skips pairs only when their pair result and reset receipt are sealed and hash-consistent.
Any incomplete claimed external phase stops for audit instead of being redispatched.

Service recovery after host restart:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
docker compose -f benchmarks/chemcrow/configs/rxn_sandbox.services.yaml up -d
uv run python -m openevo.rollout.server --config benchmarks/chemcrow/configs/openevo_core_topology.yaml
```

In a second terminal:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost \
  uv run python -m openevo.gateway.server \
  --config benchmarks/chemcrow/configs/openevo_core_topology.yaml \
  --node-id chemcrow-core-gateway-01
```

## O. Known limitations versus the paper

1. The public ChemCrow repository omits API-restricted paper tools, so exact paper reproduction
   is impossible from public code alone.
2. Historical notebook answers/evaluations are intentionally excluded, leaving no exact GT for
   most tasks.
3. RXN-Sandbox uses Pistachio2025Q2 models, not the paper-era hosted RXN state, and its upstream
   Python dependencies are not locked.
4. Four price/procurement tasks cannot reproduce the original price tool because that tool is
   deliberately excluded. Literature-heavy and physical-property tasks also have reduced tools.
5. Adapter RDKit 2025.9.6 and RXN-container RDKit 2026.3.5 are modern, not paper-era versions.
6. SafetySummary returns deterministic PubChem evidence and retrosynthesis returns raw model
   evidence; neither invokes the vendor's hidden legacy GPT summarizers.
7. LiteratureSearch remains excluded because it would create hidden legacy model calls and
   violate paired provider fairness.
8. The local retro canary returned an empty result; endpoint execution is proven, route quality
   is not. The upstream `fld`/`dfl` bug is preserved and documented.
9. The actual subscription Codex route has passed static/Core admission configuration checks but
   not a live model canary, because no paid/model call was authorized.
10. Any initial final scores are **PROVISIONAL LLM-JUDGED RESULTS** until blinded expert review.
