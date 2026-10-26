---
name: evidence-grounded-synthesis-planner
description: Use for benchmark-only planning of small-molecule syntheses when structure, stereochemistry, literature support, provisional conditions, purification, safety, and analytical checks must be clearly separated and validated.
---

# Evidence-Grounded Synthesis Planner

## Trigger

Use this skill when asked to plan or assess a chemical synthesis from a name, structure, identifier, or target description.

Do not use it to perform experiments, procure materials, contact vendors, or imply that any procedure was physically executed. Treat chemistry tools as observational software evidence only.

## Workflow

1. Resolve the target identity.

   - Normalize the name and structure.
   - Check molecular formula, neutral exact mass, charge state, and identifiers when available.
   - Preserve explicit stereochemistry and identify every stereocenter.
   - Recalculate proposed ion masses from the neutral formula and stated adduct rather than copying approximate values.

2. Select the simplest defensible disconnection.

   - Identify the key bond formation and immediate precursors.
   - Prefer a direct, established reaction class when compatible with the target’s functionality.
   - Explain why the chosen disconnection is chemically plausible.
   - Check whether each precursor’s connectivity and stereochemistry produce the intended target.

3. Gather independent evidence.

   - Search primary literature, patents, and authoritative databases for the exact compound, close analogues, and the reaction class.
   - Distinguish exact precedent from analogue precedent and general chemical knowledge.
   - Record unavailable or failed evidence endpoints instead of treating absence as support.
   - Do not use prediction confidence, catalog availability, identity matches, or safety records as substitutes for synthetic precedent.

4. Draft a provisional procedure.

   - State scale, equivalents, solvent, concentration, addition order, atmosphere, temperature profile, duration, and monitoring method.
   - Label unverified details explicitly as screening conditions.
   - Address precursor salt forms, free-basing, water sensitivity, and functional-group compatibility.
   - Avoid unsupported precision in yield, reaction time, or selectivity.

5. Design workup and purification around likely impurities.

   - Consider unreacted starting materials, excess electrophile, hydrolysis products, salts, and closely related byproducts.
   - Specify a quench or scavenging contingency for hazardous or persistent residual reagents.
   - Prefer a staged workup before evaporation when volatile concentration could retain or concentrate reactive impurities.
   - Offer chromatography, trituration, or crystallization only when chemically reasonable; identify which option is evidence-based and which requires screening.

6. Check stereochemical claims.

   - Separate physical retention or epimerization risk from changes in CIP descriptors caused solely by altered substituent priority.
   - Identify conditions that could promote epimerization and explain why the proposed route avoids or controls them.
   - Do not infer stereochemical purity from the starting-material label alone.

7. Add analytical and safety checks.

   - Provide expected formula-based measurements such as neutral exact mass and clearly labeled ion/adduct masses.
   - Suggest suitable identity and purity checks without fabricating spectra or results.
   - Summarize material-specific hazards, incompatible operations, containment needs, and waste considerations at a planning level.
   - State that all conditions remain unexecuted unless supplied experimental evidence proves otherwise.

## Evidence Labels

Classify important statements as one of:

- **Verified:** directly supported by a cited primary or authoritative source.
- **Analogue-supported:** inferred from a closely related substrate or reaction.
- **Calculated:** derived from structure or formula.
- **Provisional:** a proposed screening choice requiring experimental validation.
- **Unavailable:** evidence was sought but could not be obtained.

## Final Response Structure

Present:

1. Target identity and stereochemical interpretation.
2. Retrosynthetic disconnection and precursor checks.
3. Evidence summary with provenance and limitations.
4. Provisional forward procedure.
5. Workup, impurity-removal contingencies, and purification options.
6. Analytical expectations and stereochemical controls.
7. Safety boundary and unresolved uncertainties.

## Completion Validation

Before finishing, verify that:

- The named target, structure, formula, and stereochemistry agree.
- Every exact-mass and ion-mass value has been independently recalculated.
- The proposed precursors form the correct bond and oxidation state.
- Literature-backed facts are distinguishable from analogue-based or provisional choices.
- Conditions include concentration, stoichiometry, temperature, time, monitoring, and addition order.
- Workup explicitly handles residual reactive starting material.
- No yield or isolation method is presented as established without evidence.
- Stereochemical retention and possible CIP-label changes are discussed separately.
- No claim suggests physical execution, procurement, or vendor interaction.
