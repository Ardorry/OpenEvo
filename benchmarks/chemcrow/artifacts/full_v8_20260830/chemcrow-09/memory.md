# Memory

- For chemistry prediction tasks, separate the stated reaction conditions from any assumed conditions. If only reactants are given, explicitly say when no clean or reliable reaction should be predicted without catalyst, acid/base, solvent, heat, or activation context.
- When proposing an electrophilic aromatic substitution or Friedel-Crafts-type product, verify that the conditions actually support carbocation generation and aromatic substitution. Do not present an assumed acid/Lewis-acid pathway as the default outcome for bare reactants.
- Always provide reactant name-to-SMILES conversions first, then distinguish tool-supported observations from chemical inference.
- Check functional group compatibility in a structured way: identify nucleophilic/basic sites, acid-sensitive groups, possible competing reactions, deactivation/activation of the arene, and regioselectivity limits.
- If a product is condition-dependent, phrase it as “under assumed suitable conditions” and also state the likely outcome for the reactants as written.
- Reaction SMILES should match the stated or assumed chemistry. Include catalysts/reagents only when part of the assumption, and avoid adding byproducts that imply a mechanism without explaining the activation context.
- Avoid overclaiming a single selective product when the substrate could give mixtures, poor conversion, side reactions, or no practical reaction under unspecified conditions.
- Validation habit: before finalizing, ask whether the answer would still be valid if no hidden reagents or conditions are allowed. If not, qualify the prediction clearly.
