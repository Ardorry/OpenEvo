---
name: reaction-prediction-sanity-check
description: Use when converting named reactants to SMILES, predicting a reaction product, compiling reaction SMILES, and evaluating whether the proposed transformation is chemically plausible under unspecified or stated conditions.
---

# Reaction Prediction Sanity Check

## Workflow

1. Convert every named reactant into canonical or valid isomeric SMILES.
2. Confirm each structure by checking:
   - carbon skeleton and substitution pattern
   - aromaticity and ring connectivity
   - functional groups
   - charges and stereochemistry, when relevant
3. Obtain or formulate the software-predicted major product.
4. Independently identify:
   - likely reaction class
   - reacting atoms and bond changes
   - required mechanism and conditions
   - incompatible functional groups
   - major competing pathways
5. Separate two conclusions explicitly:
   - **Encoded transformation:** what the predicted product and reaction SMILES represent.
   - **Expected chemistry:** whether that transformation should dominate under ordinary or stated conditions.
6. Compile the reaction as:
   `reactant1.reactant2>>product`
   Include mapped atoms, reagents, or byproducts only if the task requires them or they are supported by evidence.
7. Describe proton transfers, leaving groups, and byproducts as formal net changes unless a mechanism and conditions justify stronger claims.

## Plausibility Checks

- Distinguish substitution, elimination, addition, condensation, oxidation, reduction, and rearrangement pathways.
- Check whether substrate substitution and nucleophile strength support the proposed mechanism.
- Flag cases where a hindered or tertiary electrophile makes a conventional bimolecular substitution implausible.
- Consider elimination, rearrangement, acid/base reactions, and decomposition as competitors.
- Do not infer successful synthesis merely because a structure predictor returns a product.
- If conditions are absent, make feasibility conclusions conditional and state which conditions would control the outcome.

## Response Structure

Provide:

1. Reactant names with SMILES.
2. Predicted product name or structural description with SMILES.
3. Reaction SMILES.
4. Functional-group and bond-change analysis.
5. Mechanistic feasibility and competing pathways.
6. A direct conclusion stating whether the reaction is expected to proceed cleanly, conditionally, or with substantial problems.

## Verification

Completion requires confirming that:

- each SMILES matches its stated molecule;
- the product contains exactly the intended connectivity change;
- atom and charge balance are reasonable for the chosen reaction representation;
- the reaction SMILES parses as reactants-to-product;
- the mechanistic assessment is independent of the software prediction;
- predicted transformation and likely experimental outcome are clearly distinguished;
- uncertainty from missing conditions is disclosed;
- no physical experiment, procurement, vendor contact, or claim of laboratory execution is included.
