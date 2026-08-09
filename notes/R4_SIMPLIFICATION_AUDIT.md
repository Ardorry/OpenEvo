# R4 Simplification Audit

## Bound evidence

- Positive reference: `rcb_oe_v0_candidate_specific_v2_semantic_life005_20260808T202753Z`,
  Life_005 `45.9 -> 47.4` (`+1.5`).
- Negative references: R3.14 Astronomy_004 `11.75 -> 2.75` (`-9.0`) and
  Chemistry_004 `1.1 -> 0.0` (`-1.1`).
- All comparisons below use Candidate/public artifacts, sanitized feedback, native artifact
  texts, Candidate transcripts, and durable receipts. Hidden GT, target images, and Judge
  reasoning are not evolution inputs.

## Mechanism differential

### What the positive V2 run did

V2 gave each native Reflector one compact task-local view. Its useful payload named the
baseline scientific idea (RNA periodic symmetry/orthogonality, probability trends, and
heatmaps), asked for an independent numerical check, and left the Reflector room to express
the result naturally. The evolved Candidate received about 1.8 kB of memory before the
public task. Its skill described one coherent RNA analysis workflow rather than a table of
output-by-output obligations.

The positive result did not prove that V2 always preserves capability, but it demonstrates a
useful information pattern: one substantive baseline idea plus one additive improvement was
small enough to remain salient and executable.

### What R3 added

R3.14 expanded a single baseline pipeline into output-shaped required achievements:

- Astronomy: 12 achievements generated from one script and its figures/numeric outputs.
- Chemistry: 7 achievements generated from one script and its figures/numeric outputs.

The private reflector capsule grew to roughly 54 kB for Astronomy and 29 kB for Chemistry.
The model-visible composition then compacted each method to the first six signature tokens,
each action to 48 characters, and each verification to 28 characters. Consequently:

- Astronomy's full ledger knew about `train_logistic`, `predict`, and `confusion`, but the
  visible method became `read catalog percentile summarize entropy3` for all 12 rows. The
  baseline selection parameters `p_QSO >= 0.90` and `p_WISE_QSO >= 0.97` never reached the
  native artifact instructions.
- Chemistry's full ledger knew about `pdf_literal_to_text` and
  `extract_pdf_metadata_and_text`, but the visible method became
  `clamp triangular normalize scores write cs`. The evolved code replaced real PDF text
  extraction with a static "public structured synthesis".

The evolved Candidate did consume the registered memory, skill, and agent-system. The loss
therefore occurred before Candidate execution: semantic information was compressed while
turning a concrete script/trajectory into repeated structural obligations.

### Artifact and instruction burden

- V2 evolved memory prefix: about 1.8 kB; one compact domain workflow.
- R3 Astronomy evolved memory prefix: about 6.2 kB; 12 nearly identical preservation bullets.
- R3 Chemistry evolved memory prefix: about 4.7 kB; 7 nearly identical preservation bullets.
- R3 skills repeated the same method row once per output role and required four formal phases,
  while agent-system repeated preservation constraints already present in memory and skill.

This shifted attention from understanding the baseline science to satisfying achievement IDs,
file inventories, phases, and gate vocabulary.

### Why the gates passed incorrectly

The R3 artifact gate rewarded structural coverage: an achievement ID, output role, file path,
and short method stem could satisfy a row. Repetition of one compressed method across many
rows looked like full coverage.

The baseline-equivalence gate then reused global script/report/output evidence for every
achievement. In both negative runs, every finding had the same all-true evidence pattern.
Thus one replacement script plus a sufficiently broad report satisfied 12/12 or 7/7 even when
the baseline scientific capability had changed. This is a confirmed false positive, not a
borderline score interpretation.

### R4.2 live finding: method names were not enough

R4.2 improved Astronomy (`5.15 -> 6.00`) but Chemistry fell from `0.8 -> 0.0`.
The Chemistry memory and skill were task-specific: they retained real PDF extraction, named
functions, the descriptor CSV, all three baseline figures, and an additive validation request.
The failure was subtler. Its live `MinimalBaselineTrace` had `parameters=[]` for the scientific
paths, so the fresh Candidate had to reinvent the implementation behind those names. It changed
the Candidate table values and scoring equations, shortened the report from 87 to 51 lines, and
silently flipped the baseline recommendation from BNE to DNE. The legacy equivalence diagnostic
still passed 5/5 because it recognized the same function/file/output shape.

Earth then exposed an independent admission bug. One hidden literal collided with a phrase that
the sealed Candidate had independently written in its transcript, code, and report. The capsule
gate treated GT scan sources and Candidate provenance as the same set and rejected this
Candidate-owned source. No GT leakage was demonstrated, no Reflector ran, and the run was reset
at the undispatched evolution boundary.

R4.3 therefore changes no schema and adds no new preservation layer. It only makes the existing
trace retain bounded numeric constructors, scientific formula assignments, main-pipeline
parameters, and report-linked Candidate reasoning; and it applies Candidate-source reuse to the
capsule literal scan without removing provenance or GT-only rejection.

## Production classification for R4

| Mechanism | R4 classification | Reason |
| --- | --- | --- |
| Sanitized evaluation boundary | KEEP | Required isolation boundary; feedback semantics stay frozen. |
| Core provenance scan/source separation | KEEP | Correct safety fix; candidate reuse does not erase provenance. |
| Native registry and fresh evolved Candidate | KEEP | Part of the experiment definition. |
| BaselineSuccessTrace v1 | DIAGNOSTIC_ONLY | Too output/file shaped and drops concrete parameters. |
| BaselineAchievementLedger v2 | DIAGNOSTIC_ONLY | Multiplies one scientific route into repetitive obligations. |
| BalancedEvolutionContext v1 | REMOVE FROM PRODUCTION INPUT | Its fixed truncation discarded late method names and parameters. |
| R3 per-achievement percentages/reference counts | DIAGNOSTIC_ONLY | Describe structure, not scientific capability. |
| Generic-advice ratio | DIAGNOSTIC_ONLY | Useful observation only; zero did not imply semantic fidelity. |
| Baseline-equivalence v1 | DIAGNOSTIC_ONLY | Proven false positive; cannot gate Judge dispatch. |
| One-off cancelled-Earth invalidation helper | REMOVE AFTER RECONCILIATION | Historical Earth is already durably reset; no R4 production need. |

## Minimal R4 authority map

R4 adds only one Candidate-grounded projection, `MinimalBaselineTrace`, with 3--8 dense
successful paths. Each path carries only summary, rationale, public inputs, concrete steps,
Candidate-chosen parameters, produced outputs, and report role. It is built deterministically
from the sealed baseline transcript/code/report/outputs and makes zero provider calls.

The native Reflector sees two visibly separate sections:

1. `WHAT ALREADY WORKED`: the MinimalBaselineTrace, including method names and exact
   Candidate-chosen parameters.
2. `WHAT NEEDS IMPROVEMENT`: every existing admitted sanitized diagnosis, unchanged.

The production artifact check asks only whether the major baseline scientific paths and their
bounded parameter anchors remain recognizable in memory/skill, at least one sanitized weakness
has a concrete improvement, and leakage/provenance findings are empty. Legacy R3 percentages and
equivalence remain archived diagnostics and do not decide dispatch.

## Conclusion

The dominant R3 failure was not insufficient preservation structure. It was lossy semantic
compression followed by gates that measured the resulting structure. R4 should remove that
compression, retain concrete Candidate actions and parameters, and reduce instruction burden
before adding any further validator.
