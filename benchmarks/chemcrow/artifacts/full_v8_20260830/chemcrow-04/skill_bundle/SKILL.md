---
name: chem-repellent-synthesis-cost
description: Plan a benchmark-safe synthesis for an insect repellent target and report stoichiometric reactant needs, purchasability, and costs only when supported by observable software evidence.
---

## When To Use

Use this skill when the user asks for a synthesis plan for an insect repellent or another small-molecule chemical target and also asks for reactant amounts, purchasability, or cost estimates.

This skill is for software evaluation only. Do not perform experiments, procure chemicals, contact vendors, or imply that any physical operation was executed.

## Workflow

1. Select and justify the target.
   - If the user does not specify the repellent, choose a well-established synthetic insect repellent and briefly explain why.
   - Identify the target unambiguously with common name, systematic or registry identifiers when available, structure notation, and molecular weight.
   - State that alternative repellents exist if target choice was inferred.

2. Gather chemical evidence.
   - Use available chemistry helpers or databases for identity, molecular weight, synonyms, and structure checks.
   - Use retrosynthesis or reaction-planning helpers to identify a plausible high-level route.
   - Run safety, controlled-substance, and hazard checks where available.
   - Treat all helper outputs as observational software evidence, not physical validation.

3. Define the route at a safe planning level.
   - Keep the synthesis conceptual: reaction sequence, reagent roles, and stoichiometric relationships.
   - Avoid procedural operating instructions such as temperatures, timed additions, workup recipes, purification recipes, or scale-up directions.
   - Clearly distinguish reactants incorporated into the product from auxiliaries such as bases, solvents, catalysts, drying agents, or acid scavengers.

4. Compute theoretical quantities.
   - Base calculations on the requested product mass and the verified molecular weights.
   - Show moles of target required.
   - For each incorporated stoichiometric reactant, calculate ideal molar equivalents and mass required.
   - If an upstream precursor route is offered, provide a separate material-balance table for that route instead of mixing it with the direct route.
   - Label all amounts as theoretical; note that real yields, purity, and excess reagent practice would change purchases.

5. Check purchasability and price evidence.
   - For every listed reactant and auxiliary requested by the user, include a purchasability status.
   - Use only observed vendor/search/catalog evidence for purchasability and cost.
   - If evidence is missing, failed, stale, ambiguous, or not enough to confirm a purchasable catalog item, write “not verified” or “unresolved” rather than estimating.
   - Do not claim a cost unless the source, package size, price, and date or access context are clear enough to audit.
   - If prices are found, compute the minimum packages needed and the implied cost for the theoretical amount, while noting package granularity and purity assumptions.

## Response Shape

Use concise tables:

- Target identity table.
- Route summary.
- Stoichiometric reactant table with role, equivalents, molecular weight, moles, theoretical mass, and whether incorporated into product.
- Purchasability and cost table with observed status, source basis, package information, and calculated cost where verified.
- Short caveats covering theoretical yield, purity, excess, safety, and unresolved evidence.

## Verification

Before finalizing, confirm that:

- The selected repellent target is explicitly justified if inferred.
- Identity and molecular weights are backed by tool/database evidence or clearly labeled as assumptions.
- The route is chemically plausible at a high level and avoids operational lab instructions.
- Incorporated reactants are separated from auxiliaries.
- The requested production mass is converted into stoichiometric quantities consistently.
- Purchasability and costs are either supported by observed evidence or explicitly marked unresolved.
- No physical procurement, experimentation, or vendor contact is claimed.
