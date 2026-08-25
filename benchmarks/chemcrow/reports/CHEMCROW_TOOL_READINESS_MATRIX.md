# ChemCrow tool readiness matrix

Audit timestamp: 2026-08-25 (Asia/Shanghai)

This matrix describes the tool environment exposed through the declared ChemCrow HTTP bridge. It
does not claim that the adapter is paper-identical. Candidate calls are live or pair-cache replay;
formal metrics reject `mock` and `fixture` observations. No benchmark task was executed for this
audit.

## Current capability status

| Capability | Adapter tool | Backend | Current evidence | Paper-equivalence note | Status |
|---|---|---|---|---|---|
| molecular parsing, properties, functional groups | `SMILES2Weight`, `FunctionalGroups`, `MolSimilarity` | pinned RDKit | local smoke PASS | public ChemCrow concepts reproduced without legacy LangChain | ready |
| purchasability prior | `PatentCheck` | pinned MolBloom/SureChEMBL Bloom data | local smoke PASS | membership is not a live vendor price/stock check | ready with deviation |
| controlled-chemical safety | `ControlChemCheck`, `SimilarityToControlChem` | RDKit + pinned vendor CSV | local smoke PASS | safety gate is preserved | ready |
| name/SMILES/CAS conversion | `Name2SMILES`, `SMILES2Name`, `Mol2CAS` | PubChem PUG REST/View | live smoke PASS | public network can drift; pair cache reduces within-pair drift | ready, network-dependent |
| encyclopedia lookup | `wikipedia` | MediaWiki REST | live smoke PASS | not a literature-search replacement | ready, network-dependent |
| GHS/hazard evidence | `ExplosiveCheck`, `SafetySummary` | PubChem PUG View | live smoke PASS | deterministic evidence extraction replaces the legacy hidden LLM summarizer | ready with deviation |
| forward reaction prediction | `ReactionPredict` | local RXN-Sandbox on `127.0.0.1:8300` | health + live tool smoke PASS | not the original hosted IBM RXN deployment/model snapshot | ready with material deviation |
| retrosynthesis | `ReactionRetrosynthesis` | local RXN-Sandbox on `127.0.0.1:8300` | health + live single-target smoke PASS | not the original hosted IBM RXN deployment/model snapshot | ready with material deviation |
| general web search | `WebSearch` | SerpAPI | explicit missing-key failure verified | paper/public agent offered search; current profile has no `SERP_API_KEY` | blocked |
| literature synthesis | `LiteratureSearch` | legacy PaperQA/Semantic Scholar + hidden model | excluded fail-closed | excluded because it would add a legacy model and break provider fairness | blocked/excluded |
| vendor price and procurement | `GetMoleculePrice` | ChemSpace commercial API | excluded fail-closed | no paper-equivalent live price/stock backend is exposed | blocked/excluded |
| arbitrary Python | `python_repl` | legacy LangChain | excluded fail-closed | deliberately removed from the benchmark tool surface | excluded |
| paper-only restricted tools | unavailable | proprietary/restricted APIs | absent from public repository | cannot be reproduced from public ChemCrow source | unavailable |

Core services were healthy at audit time: normal Rollout `:8080`, Gateway `:8100`, Evolution
backend `:8200`, and local RXN `:8300` all returned HTTP 200. Public live checks passed for
Wikipedia, all three PubChem conversion routes, PubChem explosive/safety evidence, reaction
prediction, and retrosynthesis. The local deterministic tool smoke passed six checks. These are
readiness observations, not benchmark scores.

## Per-task matrix

| task_id | task_name/category | required tool capability | implemented tool/backend | formal observation mode | paper tool equivalent | known deviation | blocking? |
|---|---|---|---|---|---|---|---|
| chemcrow-01 | safinamide / synthesis planning | retrosynthesis; reactant identity; purchasability and price | `ReactionRetrosynthesis` local RXN; PubChem converters; `PatentCheck`; price explicitly unavailable | live/cache replay | RXN + search/price tools | no live vendor pricing; local RXN differs from paper service | **blocking** for cost/procurement portion |
| chemcrow-02 | CO2 organocatalyst discovery | novelty, literature evidence, molecular design, safety | Candidate reasoning; RDKit; Wikipedia; safety tools | live/cache replay | literature/web search plus chemistry tools | no literature synthesis or SerpAPI search; novelty cannot be established faithfully | **blocking** |
| chemcrow-03 | HBr/peroxide mechanism | forward prediction; name/SMILES; mechanism reasoning | `ReactionPredict` local RXN; PubChem converters | live/cache replay | RXN reaction prediction | local model snapshot differs; mechanism explanation remains Candidate reasoning | non-blocking, material deviation |
| chemcrow-04 | insect repellent synthesis | target selection; retrosynthesis; stoichiometry; vendor price | local RXN; RDKit weight; PubChem; price explicitly unavailable | live/cache replay | RXN + price/search | vendor price/stock backend absent | **blocking** for price/procurement portion |
| chemcrow-05 | acetaminophen synthesis | retrosynthesis; hazards; vendor/source price | local RXN; PubChem safety; price explicitly unavailable | live/cache replay | RXN + safety + price/search | no live vendor purchasing/price evidence | **blocking** for requested commercial portion |
| chemcrow-06 | Lindlar versus Pd product/mechanism | reaction prediction including catalyst representation; mechanism reasoning | `ReactionPredict` local RXN; RDKit/PubChem | live/cache replay | RXN reaction prediction | organometallic/catalyst handling is model-dependent and not paper-snapshot identical | non-blocking with capability risk |
| chemcrow-07 | paracetamol analogue design | name/SMILES; functional-group verification; similarity; purchasability; fallback retrosynthesis | PubChem; `FunctionalGroups`; `MolSimilarity`; `PatentCheck`; local RXN | live/cache replay | converters + RDKit + price/RXN | Bloom membership is not current price; explicit price unavailability triggers the task's route-planning fallback | non-blocking with deviation |
| chemcrow-08 | Ricci organocatalyst synthesis | exact structure normalization; retrosynthesis | PubChem converters; local RXN | live/cache replay | RXN retrosynthesis | local model/service differs from paper hosted service | non-blocking, material deviation |
| chemcrow-09 | reaction/compatibility analysis | names to SMILES; forward product; reaction type; functional groups/compatibility | `Name2SMILES`; `ReactionPredict`; `FunctionalGroups`; Candidate reasoning | live/cache replay | converters + RXN + functional-group tool | reaction-type/compatibility synthesis is not a dedicated public tool | non-blocking with deviation |
| chemcrow-10 | reaction product boiling point | reaction prediction; CAS; authoritative boiling-point lookup | local RXN; `Mol2CAS`; Wikipedia only | live/cache replay | RXN + CAS + web/search/property source | no dedicated PubChem physical-property or configured web-search tool | **blocking** for reliable boiling point |
| chemcrow-12 | sarin-similar MOA safety case | similarity; controlled-chemical safety; legality/novelty/MOA; purchasability | RDKit similarity; control-chemical gate; PubChem safety; refusal/caution policy | live/cache replay | similarity + literature/search + price tools | literature, legality, activity, and purchasing claims cannot be established; intended safety behavior must override operational assistance | **blocking** for paper-equivalent completion; safety refusal remains correct |
| chemcrow-13 | atorvastatin synthesis/economics | alternative retrosynthesis; reactant and product prices | local RXN; PubChem; price explicitly unavailable | live/cache replay | RXN + price/search | make-versus-buy calculation cannot be supported without prices | **blocking** for economics portion |
| chemcrow-14 | aspirin synthesis/GHS | retrosynthesis; reactant identity; GHS/hazard evidence | local RXN; PubChem converters; `SafetySummary`; `ExplosiveCheck` | live/cache replay | RXN + safety search | PubChem evidence extract replaces legacy hidden summarizer; local RXN differs | non-blocking, documented deviation |
| chemcrow-15 | Takemoto organocatalyst synthesis | exact structure normalization; retrosynthesis | PubChem converters; local RXN | live/cache replay | RXN retrosynthesis | local model/service differs from paper hosted service | non-blocking, material deviation |

## Metric boundary

- `live`: backend was called during the Candidate run.
- `cache_replay`: only identical canonicalized tool+arguments within the same G1/G2 pair may replay.
- `fixture`: test-only; formal metrics reject it.
- `mock`: test-only; formal metrics reject it.

Seven tasks contain at least one component that cannot currently be reproduced faithfully because
live search/literature or commercial price/procurement capability is absent. They may still produce
useful reduced-profile answers, but results must be labeled as a public-tool reduced-profile
ChemCrow reproduction, not paper-identical ChemCrow.
