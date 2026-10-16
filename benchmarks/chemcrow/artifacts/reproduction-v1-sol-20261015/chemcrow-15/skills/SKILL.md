---
name: evidence-grounded-synthesis-planning
description: Plan literature-style organic syntheses with explicit evidence boundaries, decision gates, material-balance checks, analytical acceptance criteria, and safety review. Use for retrosynthesis, route assessment, or synthesis-plan critique in software-only chemistry tasks.
---

# Evidence-Grounded Synthesis Planning

## Safety boundary

Treat the task as a software planning exercise. Do not perform experiments, procure chemicals, contact vendors, or imply physical execution. Chemistry-tool results are observational or predictive evidence, not experimental validation.

## Workflow

1. Confirm the target before planning:
   - Resolve structure, formula, charge state, stereochemistry, and identifiers.
   - Check that computed mass and proposed analytical ions are internally consistent.
   - Flag unresolved structural or stereochemical ambiguity.

2. Build the retrosynthesis:
   - Identify the most reliable late-stage bond disconnection.
   - Propose accessible fragments and a forward sequence.
   - Track protecting groups, protonation states, chemoselectivity, and every stereocenter through all steps.
   - Prefer established transformations over speculative shortcuts.

3. Inspect evidence for each transformation:
   - Distinguish literature precedent, database identity, model prediction, and chemical inference.
   - Use reaction-model scores only as plausibility indicators; never present them as evidence of yield, selectivity, robustness, or validated conditions.
   - Label unsupported equivalents, solvents, temperatures, workups, and purification choices as provisional starting points.

4. Add decision gates:
   - Define an assay after each selectivity-sensitive or incomplete-conversion-prone step.
   - State what result permits continuation, what impurities must be rejected, and when rework or route reconsideration is needed.
   - Include a material-balance strategy for excess reagents, recovered starting material, overreaction products, salts, and closely related protected species.

5. Address route risks:
   - Consider mono- versus multiple functionalization, incomplete substitution, overalkylation, salt carryover, epimerization, incompatible protonation states, and difficult purification of basic or highly polar products.
   - Explain mitigations without overstating certainty.
   - Treat stereoretention as a mechanistic expectation unless authenticated comparative data establish it experimentally.

6. Define characterization:
   - Give formula- and structure-consistent mass checks.
   - List diagnostic NMR changes that distinguish starting material, intermediates, and product, including disappearance and appearance of key functional-group signals.
   - Add suitable orthogonal checks such as chromatography, IR, or mass spectrometry.
   - Require an authenticated reference or validated method before claiming identity from optical rotation or chiral chromatography.

7. Review safety:
   - Identify acute toxicity, corrosivity, sensitization, volatility, exotherm, gas evolution, and hazardous reagent or waste concerns.
   - Keep guidance conceptual and suitable for trained professionals using institutional procedures.

## Final response structure

Provide:

- verified target summary;
- preferred retrosynthetic logic;
- forward route with provisional conditions clearly labeled;
- evidence basis and uncertainty for each step;
- decision gates and material-balance plan;
- expected analytical observations;
- route-specific safety and failure modes;
- unresolved questions requiring literature or experimental confirmation.

## Completion criteria

A plan is complete only when:

- target identity and stereochemistry are internally consistent;
- each bond-forming step has a defensible precedent or clearly labeled inference;
- predictive outputs are not confused with experimental validation;
- unsupported operating details are marked provisional;
- critical intermediates have pass/fail assay criteria;
- excess reagents and major side products have a separation or recovery strategy;
- characterization can distinguish incomplete conversion and common overreaction products;
- stereochemical claims are appropriately qualified;
- no physical execution or procurement is claimed.
