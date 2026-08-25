# ChemCrow tool readiness matrix

Audit date: 2026-08-26 (Asia/Shanghai)

Authoritative profile: `public-source-reduced-local-rxn-no-serp-v1`. This is a reproducible
reduced-profile benchmark, not a paper-identical ChemCrow environment. Formal run observations may
be `live` or same-pair canonical `cache_replay`; `mock=0` and `fixture=0` are hard gates.

## Capability inventory

| Required capability | Available implementation | Backend | Mode/evidence | Paper-tool analogue | Known deviation | Reduced-profile status |
|---|---|---|---|---|---|---|
| molecular parsing / weight / functional groups / similarity | `SMILES2Weight`, `FunctionalGroups`, `MolSimilarity` | pinned RDKit 2025.9.6 | live local PASS | RDKit-style tools | modern package snapshot | ready |
| purchasability prior | `PatentCheck` | MolBloom/SureChEMBL Bloom data | live local PASS | purchasability/search | membership is not price or stock | ready with deviation |
| controlled-chemical checks | `ControlChemCheck`, `SimilarityToControlChem` | RDKit + pinned CSV | live local PASS | safety gate | public-data reconstruction | ready |
| Name2SMILES / Mol2CAS / SMILES2Name | matching public tools | PubChem PUG REST/View | live PASS | conversion tools | network data can drift | ready, pair-cache allowed |
| encyclopedia lookup | `wikipedia` | MediaWiki REST | live PASS | Wikipedia | not literature search | ready |
| safety / explosive evidence | `SafetySummary`, `ExplosiveCheck` | PubChem PUG View | live PASS | safety search/summarizer | deterministic extract replaces hidden LLM summarizer | ready with deviation |
| reaction prediction | `ReactionPredict` | local RXN-Sandbox `:8300` | live PASS | hosted RXN | different service/model snapshot | ready with material deviation |
| retrosynthesis / synthesis planning | `ReactionRetrosynthesis` | local RXN-Sandbox `:8300` | live PASS | hosted RXN | different service/model snapshot | ready with material deviation |
| web search | `WebSearch` exists but disabled | SerpAPI | not configured; explicit failure | paper search tool | no `SERP_API_KEY` in frozen profile | unavailable, declared |
| literature search | excluded fail-closed | legacy PaperQA/Semantic Scholar + hidden model | not called | paper literature tooling | would add an uncontrolled model/provider | unavailable, declared |
| price / procurement | excluded fail-closed | ChemSpace commercial API | not called | price/stock tool | no approved public equivalent | unavailable, declared |
| arbitrary Python | excluded fail-closed | legacy LangChain REPL | not called | paper tool surface | intentionally absent | unavailable, declared |
| proprietary paper-only tools | none | restricted APIs | absent | proprietary tools | public source has no faithful implementation | unavailable, declared |

Zero-model evidence root: `/home/lhy-h/work/chemcrowrun/runs/readiness-three-pipeline-v1/`. The local
smoke has six PASS checks and the external/local-RXN smoke has eight PASS checks.

Real paired evidence root:
`/home/lhy-h/work/chemcrowrun/runs/readiness-three-pipeline-live-v4/`. Tasks `02`, `03`, and `06`
completed 3 G1/G2 pairs with 77 tool calls: 71 live observations, 6 same-pair canonical cache
replays, `mock=0`, and `fixture=0`. The completed-run audit status is PASS. Cache replay is retained
only under the frozen pair-scoped reproducibility policy and is explicitly counted.

## Per-task accounting

| task_id | Required capability | Available implementation / backend | Expected formal mode | Paper analogue | Known deviation | Blocking/non-blocking |
|---|---|---|---|---|---|---|
| chemcrow-01 | retrosynthesis, identity, purchasability, price | local RXN; PubChem; MolBloom | live/cache | RXN + search/price | price/stock absent | non-blocking for frozen reduced profile; blocks paper-equivalent procurement claim |
| chemcrow-02 | novelty, literature, design, safety | RDKit; Wikipedia; PubChem safety; Candidate reasoning | live/cache | literature/web search | novelty/literature cannot be established faithfully | non-blocking reduced profile; blocks paper-equivalence |
| chemcrow-03 | forward reaction and mechanism | local RXN; PubChem; Candidate reasoning | live/cache | RXN | different RXN snapshot | non-blocking, material deviation |
| chemcrow-04 | target choice, retrosynthesis, stoichiometry, price | local RXN; RDKit; PubChem | live/cache | RXN + price/search | vendor price absent | non-blocking reduced profile; blocks procurement component |
| chemcrow-05 | retrosynthesis, hazards, source price | local RXN; PubChem safety | live/cache | RXN + safety + price | vendor price absent | non-blocking reduced profile; blocks commercial component |
| chemcrow-06 | catalyst-dependent product/mechanism | local RXN; RDKit/PubChem | live/cache | RXN | catalyst handling/model differs | non-blocking with capability risk |
| chemcrow-07 | conversion, groups, similarity, purchasability, fallback synthesis | PubChem; RDKit; MolBloom; local RXN | live/cache | converters + price/RXN | membership is not current stock/price | non-blocking with deviation |
| chemcrow-08 | exact structure and retrosynthesis | PubChem; local RXN | live/cache | RXN | different service/model | non-blocking, material deviation |
| chemcrow-09 | conversion, product, reaction class, compatibility | PubChem; local RXN; RDKit; Candidate reasoning | live/cache | converters + RXN | no dedicated compatibility tool | non-blocking with deviation |
| chemcrow-10 | product, CAS, boiling point | local RXN; PubChem CAS; Wikipedia | live/cache | RXN + property search | no dedicated authoritative property source | non-blocking reduced profile; blocks reliable paper-equivalent property claim |
| chemcrow-12 | similarity, safety, legality/MOA, purchasability | RDKit; controlled-chemical gate; PubChem safety; refusal policy | live/cache | similarity + literature/search/price | legality/activity/purchasing cannot be established | non-blocking reduced profile only with safety refusal; blocks paper-equivalence |
| chemcrow-13 | alternative synthesis and economics | local RXN; PubChem | live/cache | RXN + price/search | prices absent | non-blocking reduced profile; blocks economics component |
| chemcrow-14 | retrosynthesis, identity, GHS/hazards | local RXN; PubChem; safety tools | live/cache | RXN + safety search | deterministic safety extract and different RXN snapshot | non-blocking, documented deviation |
| chemcrow-15 | exact structure and retrosynthesis | PubChem; local RXN | live/cache | RXN | different service/model | non-blocking, material deviation |

Unavailable capabilities must return explicit unavailability; they may not be replaced by mocks,
fixtures, invented equivalents, hidden model calls, or post-hoc paper-evaluator information.
