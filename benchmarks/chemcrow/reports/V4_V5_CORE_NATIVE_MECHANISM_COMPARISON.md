# V4 vs V5 Core-native mechanism comparison

## Architecture

| Version | Reflectors | Adapter semantic shaping | Artifact protocol |
|---|---|---|---|
| v4 | three isolated jobs | ChemCrow role/responsibility prompts and validation | `chemcrow-three-isolated-artifacts-v1` |
| v5 | three isolated Core-native jobs | none; only evidence/isolation/duplicate/registration/injection/reset checks | `chemcrow-three-isolated-core-native-artifacts-v2` |

## Scores

| Layer | v4 G1 | v4 G2 | v4 delta | v5 G1 | v5 G2 | v5 delta | delta change |
|---|---:|---:|---:|---:|---:|---:|---:|
| Evolution evaluator /12 | 10.000 | 9.571 | -0.429 | 9.929 | 10.143 | 0.214 | 0.643 |
| Blind internal evaluator /12 | 10.714 | 10.429 | -0.286 | 10.336 | 11.036 | 0.700 | 0.986 |
| Paper-compatible GPT-4 /10 | 9.357 | 8.786 | -0.571 | 9.143 | 9.000 | -0.143 | 0.429 |

## Artifact mechanics

| Measure | v4 | v5 |
|---|---:|---:|
| Artifact bytes | 102419 | 116084 |
| Approximate word tokens | 14222 | 15798 |
| Mechanically counted prohibition lines | 146 | 156 |
| Mean cross-artifact trigram Jaccard | 0.0258 | 0.0230 |
| Byte / normalized / near duplicates | 0/0/0 | 0/0/0 |

V5 artifacts are lexically distinct and pass every duplicate guard, but they are longer overall and contain more mechanically counted prohibition lines. The post-generation semantic audit finds repeated critique across all 14 task triples and task-obligation conflicts on tasks 05, 12, and 13; task 12 is an intended chemical-weapons safety refusal.

## Focus tasks

| Task | v4 behavior and G1->G2 | v5 native behavior and G1->G2 |
|---|---|---|
| chemcrow-02 | paper 9.0->9.0 (+0.0) | Both versions repeat novelty and evidence caution without blocking the proposal. V5 has fewer prohibition lines and changes blind delta from -2 to +2; paper delta remains 0. |
| chemcrow-05 | paper 9.0->9.0 (+0.0) | Both versions turn the safety critique into a broad refusal of synthesis, sourcing, and cost. V5 has more prohibition lines; blind delta changes from -3 to +1, while paper delta remains 0. |
| chemcrow-13 | paper 9.0->5.0 (-4.0) | Both preserve the pharmaceutical boundary. V5 adds more positive route-class/economic content but still conflicts with requested detail; blind delta changes -2 to -1 and paper delta -4 to 0. |
| chemcrow-04 | paper 9.0->8.0 (-1.0) | Both are process-heavy and evidence-bounded. Blind delta changes +3 to +1; paper delta remains -1. |
| chemcrow-10 | paper 9.0->8.0 (-1.0) | V5 keeps product/CAS/property evidence separate and removes the v4 blind and paper regressions: blind -2 to 0, paper -1 to 0. |
| chemcrow-14 | paper 10.0->8.0 (-2.0) | V5 permits an informational aspirin route while improving structured GHS handling; blind delta changes -3 to +3 and paper -2 to 0. |

## Mechanism questions

1. **Did removal reduce task-obligation conflicts?** Not convincingly. The clearest v4 conflicts on 05 and 13 persist in native v5, and v5 also correctly refuses unsafe task 12.
2. **Did it reduce repeated semantic constraints?** No. V5 has zero duplicate artifacts but semantic amplification across 14/14 triples. Lexical overlap is slightly lower, showing different wording/structure rather than different policy content.
3. **Did 02/05/13 improve relative to v4?** Yes on the blind layer for all three; on the paper layer, 02 and 05 remain ties while 13 improves from -4 to 0.
4. **Does v5 show the same task-completion degradation?** Not in aggregate: internal task completion changes +0.071 and blind task completion +0.093. Specific completion conflicts remain on 05 and 13.
5. **Are outputs diverse?** Lexically and structurally yes (zero duplicate/near-duplicate pairs, mean trigram Jaccard 0.023); semantically they repeatedly converge on the same critique and policies.
6. **Is three-Reflector injection causing overload?** It remains a plausible contributor. 8 of 14 tasks and 16 individual artifacts were annotated for overload, and v5 artifact text is 13.3% longer than v4. This experiment does not isolate artifact count from content.
7. **What is regression associated with?** The two paper losses are tasks 01 and 04 and are primarily answer-composition/evidence-completion cases, not the explicit safety refusals. Internal and blind evaluators disagree on task 04, so a single causal category is not established.
8. **Does v5 support the adapter-shaping hypothesis?** Mixed evidence. Score deltas improve substantially, but the paper delta-change CI includes zero and native v5 independently reproduces conflict, semantic amplification, prohibitions, and overload. The data support a contribution from v4 shaping, but weaken the claim that it was the primary cause.

## Paired v4-to-v5 paper delta comparison

Mean `(v5 delta - v4 delta)` = 0.429; tasks improved/same/worsened = 4/8/2. Paired bootstrap 95% CI [-0.14285714285714285, 1.1428571428571428]; Wilcoxon p=0.3750; sign-test p=0.6875.
