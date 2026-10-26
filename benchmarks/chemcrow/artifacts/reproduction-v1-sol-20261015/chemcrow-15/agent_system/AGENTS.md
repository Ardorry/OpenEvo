# Agent System

## Scope and Safety

- Treat chemistry tasks as software-based planning only. Do not perform physical experiments, procure materials, contact vendors, or imply that any procedure was physically executed.
- Use only sources and tools permitted by the task. Do not browse or introduce hidden, historical, sibling, future, or ground-truth information.
- Follow repository-local instructions and task-specific constraints before applying these rules.

## Evidence Discipline

- When using reaction predictors or chemistry software, describe outputs as predicted plausibility rather than experimental validation. Verify that no score is presented as evidence of yield, selectivity, robustness, or validated conditions.
- When proposing equivalents, solvents, temperatures, workups, or purification methods without direct source support, label them as provisional starting points. Verify that sourced facts, model predictions, and chemical judgment are clearly distinguishable.
- When claiming identity, stereochemistry, purity, or literature agreement, require an authenticated reference or validated analytical method. Otherwise, state the limitation explicitly.

## Synthesis Planning

- When planning a route, confirm the target structure, formula, charge state, stereochemistry, and expected diagnostic mass before retrosynthetic analysis. Verify that these identifiers are mutually consistent.
- When selecting a disconnection, explain why the bond formation and functional-group sequence are chemically plausible. Verify each intermediate’s connectivity, chemoselectivity, and compatibility with downstream steps.
- When a step can produce under-reaction, over-reaction, regioisomers, or multiple protection states, add an explicit decision gate: define the assay, acceptance criterion, rejection criterion, and corrective branch before proceeding.
- When excess reagent or closely related byproducts complicate isolation, include a material-balance strategy covering separation, recovery or disposal, and rejection of undesired products. Verify that the proposed workup plausibly distinguishes all major mixture components.
- When a route contains stereocenters that are not deliberately modified, identify conditions that might cause epimerization or racemization. Verify stereochemical retention using a justified method, and do not claim confirmation from optical rotation or chiral chromatography without a suitable reference.
- When handling salts, free bases, or protonation-sensitive coupling partners, specify the required form at each step. Verify that the planned workup produces the form needed for the next transformation.

## Analytical Validation

- For every key intermediate and final product, define orthogonal acceptance criteria using appropriate combinations of mass, chromatography, and spectroscopy.
- When a transformation changes substitution or functional-group class, list expected diagnostic spectral changes rather than only generic “consistent NMR” language. Verify disappearance of starting-material signals and appearance of product-specific features.
- When purification is likely to be affected by basic, acidic, strongly adsorbing, or salt-forming functionality, flag the risk and provide a provisional alternative. Verify compatibility between purification conditions and the intended isolated form.

## Quality Control

- Before finalizing, trace every detailed recommendation to either supplied evidence or an explicit provisional assumption.
- Check that major route risks have corresponding analytical gates and contingency actions.
- Prefer focused verification of identities, decision points, and changed behavior before broad optimization or cleanup.
- Present unresolved uncertainty directly; do not convert plausible chemistry into claimed experimental fact.
