---
name: reaction-product-property-verifier
description: Use when asked to predict products of a chemical reaction and retrieve a physical property through CAS-number-based software evidence.
---

# Reaction Product Property Verifier

Use this skill for requests that combine reaction prediction, product identification, CAS resolution, and physical-property lookup.

## Workflow

1. Parse the reactants and requested conditions. Normalize names or structures before prediction.
2. Predict the reaction, but do not accept model ranking alone. Write the chemically balanced transformation and check atom conservation, expected coproducts, reaction class, and reversibility.
3. Prefer the complete, balanced product set over a higher-ranked incomplete prediction. Distinguish principal product, coproducts, catalysts, solvents, and unchanged reactants.
4. Resolve every relevant product independently:

   - canonical chemical name
   - structure or canonical SMILES
   - CAS Registry Number

   Reject an identifier when the returned name or structure does not match the predicted compound.
5. Query the requested property using the validated CAS number. Record the returned value, units, pressure or other measurement conditions, and source.
6. If CAS-based property retrieval fails, say so explicitly. A name-only page, search snippet, or identity lookup does not prove that the property was retrieved by CAS.
7. When multiple products are chemically relevant, report each one or clearly explain which product the question is interpreted to mean.

## Helper checks

If helper scripts are available, use them to:

- compare reactant and product elemental formulas for atom balance;
- canonicalize structures and detect duplicate product identities;
- cross-check name, structure, and CAS mappings;
- normalize property units without discarding measurement conditions;
- flag predictions that omit a necessary coproduct.

Treat all chemistry-tool output as observational software evidence. Never imply that a reaction or measurement was physically performed.

## Final validation

Completion requires all of the following:

- the proposed transformation is chemically plausible and atom-balanced;
- all material products are acknowledged;
- each reported CAS number matches the associated structure;
- each property value is supported by a successful property-bearing lookup tied to that identifier;
- units and relevant conditions are stated;
- uncertainty, equilibrium behavior, or missing evidence is disclosed without inventing a value.
