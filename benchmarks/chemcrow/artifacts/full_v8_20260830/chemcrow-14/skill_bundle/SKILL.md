---
name: chemistry-synthesis-ghs
description: Answer chemistry synthesis requests with a concise route, reagent roles, supervised small-scale lab framing, and SDS-style GHS hazard summaries.
---

## When To Use

Use this skill when the user asks for a chemical synthesis route and also asks for hazards, GHS ratings, SDS information, reagent safety, or reactant classifications.

This skill is for informational chemistry support only. Do not claim that any experiment was physically performed, do not procure chemicals, do not contact suppliers, and do not imply the user can safely run the procedure without appropriate training, facilities, supervision, and local compliance.

## Workflow

1. Identify the target compound.
   - Resolve common names to the chemical identity and, when helpful, include the systematic name or structure notation.
   - If the target or route appears illegal, weaponizable, explosive, toxic gas-generating, or otherwise high-risk, avoid actionable synthesis detail and provide a safety-focused response.

2. Choose the standard defensible route.
   - Prefer a well-known educational or literature route for benign compounds.
   - State the reaction type and the roles of required materials: starting material, reagent, catalyst, solvent, quench/workup material, and purification material.
   - Separate required chemicals from optional catalyst or solvent alternatives.

3. Give a concise supervised-lab synthesis overview.
   - Include reaction scheme in words.
   - Provide typical stoichiometric relationship or excess qualitatively or approximately when appropriate.
   - Include the main operational phases: combining reagents, controlled heating or stirring, cooling/quench/crystallization or isolation, washing, drying, and purification.
   - Keep it small-scale, non-procurement oriented, and explicitly dependent on an approved lab protocol and supervision.
   - Mention appropriate identity or purity checks when relevant, such as melting point, IR, NMR, TLC, or HPLC.

4. Provide GHS information for all chemicals needed.
   - For each required reactant/reagent/catalyst and any optional alternatives, list:
     - chemical name and role
     - GHS pictograms, if applicable
     - signal word, if available
     - hazard class/category where known
     - representative H-code statements
     - short practical safety note
   - State that exact classifications vary by concentration, hydration state, formulation, jurisdiction, and supplier SDS, and that the current SDS is authoritative.
   - Do not present broad pictograms alone as the “rating”; include categories and H-codes where reliable.
   - If uncertain, mark the classification as representative rather than definitive.

5. Keep tool or source discussion minimal.
   - Do not spend the answer explaining tool failures or internal lookup steps.
   - Use chemistry tools only as observational software evidence, not as proof that anything was physically executed.
   - If helper tools are available, use them to check: name-to-structure identity, route plausibility, reagent list completeness, and SDS/GHS consistency.

## Final Answer Shape

A strong final response should have:

- A short safety framing sentence.
- A clear route summary with reaction type and reagent roles.
- A compact synthesis overview with enough context to understand the chemistry, without pretending to replace a lab SOP.
- A GHS table separating required materials from optional alternatives.
- A closing note that supplier SDS and institutional protocols control actual handling.

## Verification

Before finalizing, check that:

- The target identity is unambiguous.
- The route matches the named compound and uses chemically plausible reagents.
- Required reagents are not mixed together with optional alternatives.
- Every listed reactant, reagent, and catalyst has a corresponding GHS entry or an explicit “not classified/verify SDS” note.
- GHS entries include more than pictograms: signal word, category, and representative H-codes are included where available.
- The response does not claim physical execution, procurement, or safety beyond supervised laboratory conditions.
