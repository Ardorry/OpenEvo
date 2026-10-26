---
name: chem-synthesis-route-planner
description: Plan benchmark-only organic synthesis routes from target names, IUPAC identifiers, SMILES, or precursor hints using software evidence and chemically reasoned validation.
---

# Chem Synthesis Route Planner

Use this skill when asked to propose, critique, or refine an organic synthesis plan for a named target molecule, especially when the task expects a route-level answer rather than physical execution.

## Safety Boundary

This skill is for software evaluation and literature-style reasoning only. Do not perform experiments, procure chemicals, contact vendors, or imply that any operation was physically executed. Treat chemistry tools as observational software evidence.

## Workflow

1. Resolve the target identity.
   - Inspect the provided name, IUPAC name, SMILES, stereochemical descriptors, salts, and functional groups.
   - If tools are available, use them to cross-check structure, molecular formula, molecular weight, stereochemistry, and close synonyms.
   - Clearly separate confirmed structure facts from uncertain or failed tool observations.

2. Choose the key retrosynthetic disconnection.
   - Identify the most direct bond-forming step for the target.
   - Prefer commercially plausible or literature-common precursors when the target is a known organocatalyst, ligand, pharmaceutical intermediate, or reagent.
   - Explain why each functional group is compatible with the planned coupling and whether protection is needed.

3. Write the forward route.
   - Focus on chemically specific transformations, reagent classes, solvent choices, temperature ranges, stoichiometric logic, and purification strategy where appropriate.
   - For chiral targets, state how stereochemical purity is introduced or preserved, and flag racemization or epimerization risks.
   - Address practical side reactions such as overalkylation, chemoselectivity, moisture sensitivity, competing nucleophiles, or reagent decomposition when relevant.

4. Include precursor strategy only as far as needed.
   - If a precursor is commonly available, say so as a route option without relying on vendor procurement.
   - If preparing a precursor, include selectivity rationale, protecting-group logic, and stereochemical controls.
   - Avoid expanding into unnecessary multistep routes unless the prompt asks for full preparation from commodity materials.

5. Keep tool narration out of the final plan.
   - Do not describe every tool call or status message.
   - Summarize only evidence that affects the chemistry: identity checks, plausible precursors, safety constraints, patent/literature signals, and analytical identifiers.

## Verification

A complete answer should validate:

- Target identity: name, structure representation, formula or molecular weight when available.
- Route logic: the main disconnection and why it is chemically appropriate.
- Chemoselectivity: which functional group reacts and which remains intact.
- Stereochemistry: source and retention of configuration for chiral products.
- Conditions: plausible reagent/solvent/temperature/purification guidance without claiming execution.
- Characterization: expected confirmation methods such as NMR, MS, HPLC or chiral HPLC, optical rotation, or melting point when relevant.
- Caveats: safety, handling, legal/patent, or availability notes integrated into route choice rather than listed as unrelated disclaimers.
