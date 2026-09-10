---
name: regulated-pharmaceutical-synthesis-cost
description: Handle requests for synthesizing, sourcing, or cost-comparing regulated pharmaceuticals by refusing operational lab/procurement guidance while still giving useful high-level chemistry, safety, and cost-context answers.
---

# Regulated Pharmaceutical Synthesis And Cost Requests

Use this skill when a user asks how to synthesize, buy reactants for, optimize, scale, purify, or cost-compare making an active pharmaceutical ingredient, prescription drug, controlled medicine, or similarly regulated therapeutic compound.

This skill is especially relevant when the user asks whether making the compound personally is cheaper than buying a regulated commercial product.

## Core Policy

Do not provide a lab-ready synthesis, route recipe, shopping list, vendor workflow, stoichiometry, reaction conditions, purification protocol, or analytical release procedure that would enable unlicensed preparation of a regulated drug.

Do provide a useful non-operational answer: explain the molecule’s broad synthetic architecture, why the work is specialized, what cost categories dominate, what checks were performed, and why regulated generic supply is usually safer and cheaper than personal synthesis.

Do not perform or claim to perform physical experiments, chemical procurement, vendor contact, or real-world execution. Chemistry tools and helper scripts provide observational software evidence only.

## Workflow

1. Classify the request.
   - Identify whether the target appears to be a prescription drug, active pharmaceutical ingredient, controlled substance, investigational drug, or regulated therapeutic.
   - Inspect whether the user is requesting operational details such as exact route steps, quantities, conditions, reactant list, vendors, purification, yield optimization, or personal manufacture.

2. Resolve identity before using checks.
   - If chemistry helper tools are available, first resolve the compound name to stable identifiers such as canonical name, SMILES, formula, and salt form.
   - If a lookup fails, say which check failed and avoid overstating the result.
   - If a later check succeeds using a resolved identifier, make that chain clear.

3. Run observational helper checks when available.
   - Compound identity lookup: confirm the target and distinguish free acid, salt, hydrate, stereoisomer, or formulation.
   - Drug or regulatory-status check: determine whether the compound is a therapeutic or prescription product.
   - Patent or known-molecule check: use the resolved identifier when possible; report uncertainty if the first query fails.
   - Price context check: prefer prices for regulated finished product or broad, non-actionable market context. Avoid compiling a purchasable reactant cart.
   - Safety/context checks: note that pharmaceutical synthesis requires licensed facilities, quality systems, impurity control, and analytical release.

4. Structure the answer.
   - Start with a brief refusal of operational synthesis/procurement guidance if the target is regulated.
   - Give a high-level, non-lab-ready synthetic overview using broad route categories only, such as assembling a heterocyclic core, installing stereochemically defined side chains, salt formation, purification, and quality testing.
   - Provide a cost framework rather than an actionable shopping list. Include categories such as specialty chiral intermediates, catalysts or coupling reagents, solvents, chromatography or crystallization, waste handling, analytical testing, failed batches, equipment, documentation, and regulatory compliance.
   - Give an approximate qualitative or bounded cost comparison when supportable: personal small-scale synthesis is almost certainly more expensive, less safe, and less reliable than buying regulated generic medicine through lawful channels.
   - Recommend lawful, medically appropriate routes such as discussing prescriptions, generics, insurance, pharmacy discount programs, or clinician/pharmacist alternatives.

## What To Avoid

- Do not list exact reactants in a way that forms a procurement recipe.
- Do not provide temperatures, times, equivalents, solvents, catalysts, workups, yields, purification gradients, or scale calculations.
- Do not recommend vendors for precursors or intermediates.
- Do not imply that non-prescribed use, home synthesis, or bypassing regulated supply is acceptable.
- Do not hide behind refusal only; answer the cost-comparison part with safe, high-level reasoning.

## Verification

Before finalizing, confirm that the response:

- Identifies the regulated/pharmaceutical nature of the target when applicable.
- Refuses operational synthesis and procurement details.
- Still provides useful non-operational route context.
- Includes a cost framework with major cost drivers, not merely a vague statement.
- Directly answers whether personal synthesis is likely cheaper than lawful regulated supply.
- Transparently states tool failures, uncertainty, and identifier assumptions.
- Makes clear that any chemistry-tool output is observational software evidence only, not physical execution.
