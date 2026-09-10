---
name: thiourea-organocatalyst-synthesis-planner
description: Plan software-only syntheses of chiral unsymmetrical thiourea organocatalysts, using chemistry-tool observations plus concise synthetic reasoning and validation.
---

# Thiourea Organocatalyst Synthesis Planner

Use this skill when asked to plan, evaluate, or explain a synthesis of a chiral thiourea organocatalyst or closely related urea/thiourea small-molecule catalyst. This is for software planning only: do not imply physical execution, procurement, vendor contact, or experimental confirmation.

## Workflow

1. Parse the target identity.
   - Extract the name, stereochemical descriptors, and any supplied SMILES/InChI.
   - Identify the thiourea or urea core and map each substituent to the named structure.
   - Check whether the stereochemistry is inherited from a chiral precursor or created during the final bond-forming step.

2. Choose the main disconnection.
   - For unsymmetrical thioureas, first consider coupling a primary amine fragment with an aryl or alkyl isothiocyanate.
   - Preserve chiral amino alcohol or diamine stereochemistry by using the corresponding enantiopure amine precursor.
   - State that thiourea formation does not normally create a new stereocenter at sulfur/carbon/nitrogen.

3. Gather observational software evidence when tools are available.
   - Validate the target structure with name-to-structure, SMILES normalization, molecular formula, and molecular weight checks.
   - Run retrosynthesis or reaction-prediction helpers to confirm the amine-plus-isothiocyanate logic.
   - Use hazard or safety-summary helpers only as planning evidence; do not convert them into operational instructions.
   - If tool confidence is cited, also provide the chemical rationale in plain language.

4. Draft the synthesis plan.
   - Present the preferred final step as nucleophilic addition of the amine to the isothiocyanate.
   - Include planning-level conditions: near-stoichiometric amine, slight excess isothiocyanate if appropriate, anhydrous polar aprotic or chlorinated solvent options, ambient to mild heating, and reaction monitoring by TLC/LCMS/NMR.
   - Include representative isolation concepts such as solvent removal, aqueous wash, chromatography, trituration, or recrystallization where chemically plausible.
   - Keep quantities, times, and temperatures approximate unless the user explicitly asks for literature-level procedural detail.

5. Add optional precursor preparation if it improves completeness.
   - If the isothiocyanate is assumed but not commercially or contextually justified, include a brief upstream route from the corresponding aniline using a standard thiocarbonyl-transfer strategy.
   - Keep this optional route high-level and label it as an alternative planning branch.
   - Mention that precursor availability and compatibility should be checked before selecting the route.

6. Explain stereochemical mapping.
   - Name the chiral precursor that carries the target stereochemistry.
   - Clarify which atoms in the product correspond to the precursor’s stereocenters.
   - Note any risk of epimerization only if the proposed conditions plausibly threaten the stereocenters.

## Final Answer Requirements

A complete answer should include:

- Strategic retrosynthesis.
- Forward synthesis plan with plausible reagent roles and condition ranges.
- Optional upstream route for a key isothiocyanate or electrophile when relevant.
- Stereochemical preservation statement.
- Workup/purification and analytical confirmation suggestions.
- Software-only safety framing.

## Verification

Before finalizing, check that:

- The product substituents match the requested target.
- The stereochemical descriptors are accounted for and not silently inverted.
- The final bond-forming logic produces an unsymmetrical thiourea rather than a symmetrical byproduct.
- Any assumed precursor is either justified or paired with an optional preparation route.
- The response clearly distinguishes software planning from performed experimentation.
