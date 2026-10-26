---
name: chemistry-synthesis-cost-assessor
description: Use for paper-only assessment of a requested chemical synthesis, including literature-route verification, scale calculations, complete material costing, and a like-for-like make-versus-buy comparison.
---

# Chemistry Synthesis and Cost Assessment

## Safety boundary

Perform computational and documentary analysis only. Do not conduct experiments, procure chemicals, contact vendors, or imply physical validation. Screen the target, intermediates, reagents, and operations for hazards, controlled status, and dual-use concerns. Refuse or safely narrow requests that would materially enable harmful or unlawful activity.

## Workflow

1. Define the comparison basis:
   - Exact chemical identity, stereochemistry, salt or free form, hydration state, purity, and quantity.
   - Intended analytical or research-grade specification.
   - Whether the requested output is a route review, reproducible paper procedure, cost estimate, or all three.

2. Establish a documented route:
   - Prefer primary literature, patents, or authoritative process documentation.
   - Trace every transformation to supporting evidence.
   - Distinguish reported conditions and yields from inferred adaptations.
   - Treat reaction-prediction tools as plausibility checks, not experimental proof.
   - Resolve disagreements involving protecting groups, stereochemistry, salt formation, hydrolysis, or product identity before costing.

3. Build a complete process model:
   - List each step’s substrates, equivalents, catalysts, reagents, solvents, quench materials, extraction solvents, drying agents, purification media, and final isolation inputs.
   - Record temperatures, times, atmosphere, concentration, workup, purification, reported yield, and expected product form when supported.
   - Include deprotection, lactone opening or closing, salt conversion, pH control, stoichiometry, hydration state, and isolation details where relevant.
   - Flag any missing condition that prevents reproducibility.

4. Run scale calculations:
   - Work backward from the desired isolated quantity using molecular weights, stoichiometry, step yields, purity, and excess equivalents.
   - Keep full precision internally and round only displayed purchasing quantities.
   - Show formulas and a stepwise mass balance.
   - Provide sensitivity cases for uncertain yields rather than presenting one speculative number as certain.

5. Gather current price evidence:
   - Match vendor listings to exact identity, grade, concentration, and package size.
   - Record vendor, catalog identifier, package size, listed price, currency, access date, and exclusions such as tax or shipping.
   - Prefer directly verifiable listings; label quotations, unavailable prices, and substitutes explicitly.
   - Never initiate a purchase or vendor contact.

6. Calculate both relevant cost views:
   - Consumed-material cost: prorated amount used in the modeled run.
   - Cash outlay: minimum purchasable packages required.
   - Separately disclose solvents, purification media, analytical characterization, consumables, equipment access, labor, energy, shipping, taxes, hazardous-waste handling, and contingency.
   - Do not silently omit unavailable components; mark the total as a lower bound when necessary.

7. Compare like with like:
   - Compare the synthesized material only with a purchasable product having the same chemical form, purity, quantity, and intended use.
   - Do not equate formulated medicine with isolated research-grade active material.
   - If only non-equivalent products are available, present them in separate comparison columns and explain why no direct conclusion follows.
   - State whether the conclusion changes under consumed-cost, package-outlay, and full-cost assumptions.

## Helper checks

If helper scripts are added to the bundle, they should:

- Validate molecular formulas, molecular weights, units, equivalents, and limiting-reagent calculations.
- Propagate yields backward through the route and reconcile the mass balance.
- Detect omitted inventory categories and duplicate materials.
- Compare required quantities with package sizes and calculate package counts without fractional purchases.
- Flag stale, uncited, unavailable, or identity-mismatched price entries.
- Verify that source-supported facts, assumptions, and model predictions are labeled separately.

Manually spot-check at least one stoichiometric calculation, one yield propagation, and one package-cost calculation independently.

## Final response structure

Provide:

1. Scope, identity, scale, and safety limitations.
2. Route provenance and evidence quality.
3. Stepwise paper procedure with quantities, conditions, workups, yields, and uncertainty labels.
4. Complete bill of materials.
5. Price table with citations and access dates.
6. Consumed-material subtotal, minimum package outlay, excluded-cost estimate, and sensitivity range.
7. Like-for-like purchase alternatives.
8. A qualified make-versus-buy conclusion.
9. Unresolved evidence gaps and required experimental validation.

## Completion criteria

The assessment is complete only when:

- Every proposed step is supported or clearly labeled as inferred.
- Stereochemistry and final chemical form remain consistent throughout.
- The full inventory and arithmetic reproduce the stated totals.
- Current prices and minimum package sizes are documented.
- Omitted costs are quantified or explicitly identified.
- The purchased comparator is genuinely equivalent, or the lack of equivalence is stated.
- No prediction is represented as experimental validation.
- No procurement or physical execution occurred.
