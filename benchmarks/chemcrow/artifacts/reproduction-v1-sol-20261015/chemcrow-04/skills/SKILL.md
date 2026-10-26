---
name: chemistry-synthesis-cost-planner
description: Use for benchmark requests to propose a paper synthesis plan, scale reactants to a requested product mass, and report evidence-backed purchasability and costs without performing experiments or procurement.
---

# Chemistry Synthesis and Cost Planning

## Trigger

Use this skill when a task asks for some combination of:

- selecting or identifying a chemical target;
- proposing a synthesis route;
- calculating reactant quantities for a target product mass;
- checking whether reactants are purchasable;
- estimating reactant costs.

Treat all chemistry operations as observational software analysis. Never perform experiments, place orders, contact vendors, or imply physical execution.

## Workflow

1. Define the deliverable.

   - State the selected target and why it fits the requested function.
   - Clarify whether the requested mass means pure isolated compound or a formulated product.
   - Record the requested scale, purity assumptions, currency, region, and pricing date when available.

2. Inspect identity evidence.

   - Confirm names, structures, formulas, and relevant identifiers using chemistry data sources.
   - Distinguish average molar mass, used for bulk stoichiometry, from exact or monoisotopic mass.
   - Use one documented set of average molar masses throughout all calculations.

3. Select and validate a route.

   - Prefer a short, chemically plausible literature- or database-supported transformation.
   - Write a balanced overall reaction or an explicit stoichiometric model.
   - Classify each material as stoichiometric reactant, optional base or scavenger, catalyst, solvent, workup material, or processing aid.
   - Do not infer a reagent’s role merely because a route finder lists it.
   - Mention viable alternatives when route choices materially affect quantities or cost.
   - Keep the route at paper-planning level; omit execution-oriented procedural detail unless the benchmark explicitly requires it and permits it.

4. Calculate the theoretical scale.

   Use:

   `product moles = requested product mass / product average molar mass`

   For each stoichiometric material:

   `required moles = product moles × stoichiometric equivalents`

   `required mass = required moles × reactant average molar mass`

   For liquids, convert mass to volume only when a sourced density and applicable purity are available.

   - Show equations, units, equivalents, and rounding.
   - Separate theoretical quantities from yield-adjusted planning quantities.
   - If applying an assumed yield:

     `planned input = theoretical input / fractional yield`

   - Label assumed yield clearly; do not present it as experimental evidence.
   - Keep optional reagents outside the minimum-required reactant total unless the chosen route explicitly requires them.

5. Check purchasability and pricing.

   For every listed material, inspect current vendor evidence and record:

   - purchasable, unavailable, or unverified;
   - supplier and package size;
   - listed price and currency;
   - purity or grade;
   - access date;
   - source link or tool evidence.

   Normalize package pricing carefully:

   `unit price = package price / package amount`

   `estimated consumed cost = required amount × unit price`

   Also report the practical purchase cost based on whole packages when relevant. Do not silently mix consumed cost with checkout cost.

   If purchasing evidence is missing or a tool fails, say that availability and price are unverified. Never invent a listing, package, or price.

6. Present the result.

   Include:

   - target and scope;
   - concise paper route;
   - balanced stoichiometric basis;
   - a reactant table with role, equivalents, molar mass, required amount, and yield-adjusted amount if used;
   - a purchasing table with evidence status, package information, unit price, consumed cost, and minimum package cost;
   - assumptions, exclusions, and uncertainty.

## Safety Boundary

Keep the response within software evaluation and paper analysis. Do not procure chemicals or claim that reactions were run. Flag major hazards and regulated or unusually sensitive materials at a high level. Avoid unnecessary operational instructions, especially detailed conditions, apparatus setup, workup, or purification steps.

## Verification

Before finalizing, confirm that:

- the target and requested product form are explicit;
- the reaction model is chemically and atomically coherent;
- every quantity traces to the same average-molar-mass convention;
- unit conversions and significant figures are consistent;
- theoretical and yield-adjusted quantities are clearly separated;
- optional bases, catalysts, solvents, and workup agents are not mislabeled as required reactants;
- each purchasability and price claim has current evidence;
- unavailable pricing is reported as unknown rather than estimated without support;
- consumed cost and whole-package purchase cost are not conflated;
- the response remains a paper plan and makes no claim of physical execution.
