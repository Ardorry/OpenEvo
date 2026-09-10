---
name: cautious-chemistry-reaction-prediction
description: Use for chemistry tasks that ask Codex to convert molecule names to SMILES, predict reaction products, or compile reaction SMILES when conditions may be underspecified.
---

# Cautious Chemistry Reaction Prediction

## Trigger

Use this skill when a task asks for any combination of:

- Converting chemical names to SMILES
- Predicting organic reaction products
- Producing reaction SMILES
- Assessing reagent or functional-group compatibility
- Explaining whether a proposed reaction is likely under stated conditions

This skill is especially relevant when the prompt gives reactants but omits catalyst, solvent, acid/base, heat, stoichiometry, or other conditions needed to justify a clean transformation.

## Workflow

1. Identify the stated inputs exactly.
   - Separate molecule identity from reaction conditions.
   - Do not silently add catalysts, acids, bases, light, heat, or workup steps.

2. Convert each molecule name to SMILES.
   - Use chemistry software evidence when available.
   - If multiple tautomers, ionization states, or regioisomers are plausible, state the chosen representation and why.

3. Inspect functional groups and likely roles.
   - Identify electrophiles, nucleophiles, acidic/basic sites, directing groups, leaving groups, oxidizable/reducible groups, and steric constraints.
   - Note functional groups that may interfere with the proposed reaction class.

4. Determine whether the reaction is supported as written.
   - First answer the literal prompt conditions.
   - If no necessary activation or catalyst is supplied, say that a clean product should not be confidently predicted.
   - Only then discuss a conditional product under an explicit assumption such as acid-promoted, base-promoted, Lewis-acid-mediated, photochemical, thermal, or catalytic conditions.

5. Predict products conditionally and cautiously.
   - State the assumed reaction class.
   - Give major product candidates only when regioselectivity and compatibility are defensible.
   - Mention plausible side reactions, low conversion, mixtures, or no reliable reaction when warranted.

6. Compile reaction SMILES.
   - Include only species justified by the stated or explicitly assumed conditions.
   - If adding a byproduct depends on a mechanism or acid/base balance, explain that assumption.
   - Do not present an underspecified reaction SMILES as definitive.

## Helper Checks

When chemistry helper tools are available, use them for software evidence only:

- Name-to-SMILES lookup or molecule parsing
- Functional group detection
- Reaction classification or reaction prediction
- SMILES validation/canonicalization
- Product plausibility checks

Treat tool output as observational evidence, not physical execution. Do not claim that a reaction was run experimentally.

## Final Answer Requirements

A complete answer should include:

- Reactant names and SMILES
- A clear distinction between:
  - likely outcome for the reactants exactly as stated
  - predicted product under explicitly assumed conditions
- Reaction class, if any, with the required conditions named
- Product SMILES only when the assumption supports it
- Reaction SMILES labeled as stated-condition or assumed-condition
- A short compatibility note tied to functional groups or tool observations
- Any uncertainty about selectivity, conversion, or competing pathways

## Verification

Before finalizing, check that:

- No unstated reagent or catalyst has been smuggled into the main conclusion.
- Conditional predictions are labeled as conditional.
- The reaction SMILES matches the text explanation.
- Byproducts are chemically justified.
- The answer does not imply physical experimentation, procurement, or real-world execution.
