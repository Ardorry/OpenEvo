# ChemCrow Reaction Product Property Lookup

Use this skill when a chemistry task asks for a physical property of a reaction product, especially when the prompt requires predicting products, resolving CAS numbers, and using those identifiers to find properties.

## Workflow

1. Treat chemistry tools as software observations only. Do not imply physical execution, procurement, or experiment.
2. Convert each named reactant to SMILES before prediction.
3. Run reaction prediction, but inspect the full ranked product list. Do not blindly choose the top product if a lower-ranked result is chemically more complete.
4. Apply basic reaction-class reasoning to catch incomplete predictions. For ester alcoholysis/transesterification, expect both the exchanged alcohol-derived ester and the displaced alcohol.
5. Resolve every plausible product component separately with name and CAS tools.
6. If the user asks for “the product” but the reaction yields multiple products, state the product set and explain which product is being used as the target, or provide the requested property for each chemically relevant product.
7. Look up the physical property using the resolved CAS number when the available tool supports it. If a property lookup fails or only returns identity/safety evidence, clearly separate tool-supported identity/CAS evidence from any unsupported chemical knowledge.
8. Avoid CAS queries formatted as natural-language strings if a tool expects a compound name or structure; retry with the plain compound name, SMILES, or bare CAS.

## Verification

Before finalizing, check that:

- Reactants were resolved to structures.
- Product identities are chemically complete, not just the highest-ranked incomplete prediction.
- Each reported product has a matched CAS number.
- Physical-property values are tied to the correct compound and unit.
- The answer does not omit a coproduct when the reaction mechanism implies one.
- Any failed or unavailable tool is mentioned only as a limitation, not used as evidence for a property value.
- Final wording distinguishes observed tool outputs from inferred chemistry and remembered/reference property values.
