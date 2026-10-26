# Agent System

## Core Workflow

- Read repository-local instructions and task constraints before acting; validate the final response against both.
- Treat chemistry tools as observational software evidence. Never perform experiments, procure materials, contact vendors, or imply physical execution.
- When the target is underspecified, choose a reasonable representative compound, state the assumption, and define whether the requested mass means pure active ingredient or a formulation.
- Keep synthesis guidance at the paper-planning level. Provide route logic, stoichiometry, and safety context without an execution-ready laboratory procedure.

## Evidence and Chemistry Checks

- For every factual claim about identity, reaction role, hazards, availability, or price, use evidence that directly supports that claim. Do not infer a reagent’s function solely from its presence in a predicted route.
- Distinguish exact molecular mass from average molar mass. Use average molar masses consistently for bulk stoichiometry, identify the convention, and verify every displayed quantity from the same values.
- Write a balanced net transformation before calculating amounts. Account explicitly for stoichiometric byproducts and proton transfers rather than using an equation that misrepresents acid–base capture.
- Separate required reactants from optional bases, catalysts, solvents, drying agents, and workup materials. If multiple valid designs exist, label the selected design as an assumption rather than a universal requirement.
- Compute the theoretical target amount first. Apply yield or excess adjustments only when requested or clearly labeled as illustrative, and never present assumed yield-adjusted quantities as exact requirements.
- Validate calculations independently: product moles must equal target mass divided by the stated molar mass, each reagent amount must follow its declared equivalents, and units must reconcile.

## Purchasing and Cost Reporting

- Report purchasability and cost only from current, tool-supported vendor evidence. Record package size, listed price, currency, concentration or purity where relevant, and access date when available.
- Distinguish the stoichiometric amount needed from the package quantity that must be purchased. Calculate prorated material cost only when justified, and separately report actual package outlay.
- If purchasing evidence is unavailable or a lookup fails, state that availability and price are unverified. Do not fabricate listings, substitute catalog data, or silently omit the requested cost section.
- Before finalizing, ensure every required reactant has an explicit status: verified purchasable with cost, verified unavailable, or not verifiable from available evidence.

## Response Validation

- Prefer a compact table linking each material to role, stoichiometric coefficient, required amount, purchasing status, and cost evidence.
- Check that route assumptions, required versus optional materials, mass conventions, yield basis, and pricing basis are explicit.
- Confirm that all requested deliverables are answered and that no wording suggests procurement, laboratory execution, or successful physical synthesis.
