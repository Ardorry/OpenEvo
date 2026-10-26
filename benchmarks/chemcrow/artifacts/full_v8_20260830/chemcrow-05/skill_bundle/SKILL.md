---
name: chemistry-synthesis-cost-safety
description: Use when answering chemistry synthesis requests that ask for route feasibility, reagent sourcing, cost estimates, and safety issues using observational software evidence only.
---

## Trigger

Use this skill when a user asks how to make, source reactants for, estimate costs for, or assess hazards for a chemical target. Apply extra care when the request includes pharmaceutical compounds, reactive acylating/oxidizing/reducing agents, controlled or regulated precursors, or language that implies real-world execution.

## Safety Boundary

This skill is for software evaluation and educational chemistry discussion only. Do not perform experiments, procure chemicals, contact vendors, or imply any physical operation was executed. Chemistry tools and vendor tools provide observational evidence only.

Keep synthesis guidance non-operational when reagents, products, purity risks, or user context make hands-on execution unsafe. Avoid detailed execution parameters such as exact step timing, heating profiles, workup recipes, purification instructions, or optimization advice unless the surrounding policy and task context clearly permit them.

## Workflow

1. Identify the target molecule, requested deliverables, and any requested batch size or purity context. If no batch size is given, use a clearly labeled illustrative calculation basis rather than implying a recommended preparation scale.

2. Gather or inspect available chemistry evidence:
   - target identity, synonyms, SMILES/InChI if available
   - molecular weight and formula
   - standard high-level retrosynthetic route
   - key starting materials and stoichiometric relationships
   - major impurity or product-quality concerns
   - reagent and reaction hazards

3. For synthesis, provide a concise educational route overview:
   - name the general transformation and principal reactants
   - explain the stoichiometric relationship in words or a simple equation
   - describe the workflow at a high level without operational conditions
   - distinguish tool-supported facts from chemical inference

4. For sourcing and cost:
   - use verified vendor/cost observations when available
   - if pricing cannot be verified, say so directly
   - still provide a reusable cost framework using molecular weights, stoichiometry, expected yield assumptions, package sizes, and placeholder unit prices
   - calculate reagent masses or moles only as an illustrative planning calculation, not as an instruction to run the reaction
   - do not recommend circumventing restrictions, buying regulated chemicals improperly, or using consumer products as reagent sources

5. For safety, separate issues into clear categories:
   - reagent hazards
   - reaction and handling hazards
   - waste and disposal
   - pharmaceutical-quality or ingestion risks
   - regulatory or procurement constraints when relevant

6. Preserve uncertainty. If vendor availability, current pricing, purity, legality, or safety data were not verified, state that limitation rather than filling gaps with invented specifics.

## Helper Checks

If helper scripts or tools are available, use them only for observational checks:
- molecular identity lookup: confirm formula, molecular weight, identifiers, and synonyms
- retrosynthesis lookup: identify plausible high-level route and reactant classes
- stoichiometry calculator: compute illustrative moles, masses, limiting reagent, and yield-adjusted requirements
- vendor summarizer: report observed supplier/package/price data with date or source caveats
- hazard summarizer: organize SDS-style hazards without overstating unsupported claims

Helper outputs must be treated as evidence to summarize, not as proof that materials can be bought or used.

## Final Validation

A complete answer should:
- address synthesis, sourcing/cost, and safety explicitly
- include a high-level route and stoichiometric cost method
- avoid operational lab instructions for unsafe contexts
- clearly mark unavailable vendor or pricing evidence
- separate verified observations from assumptions
- include enough calculation detail for cost reasoning without enabling execution
- state that no chemicals were procured and no experiment was performed
