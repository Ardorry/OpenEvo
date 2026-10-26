# Reaction-Prediction Memory

- Convert every named reactant to SMILES independently before constructing the reaction SMILES; verify substitution patterns, functional groups, and atom connectivity.
- Clearly separate three layers:
  1. the software-predicted or intended transformation,
  2. mechanistic plausibility,
  3. the likely outcome under the stated or ordinary conditions.
- Treat reaction predictors as evidence of a proposed product, not proof of synthetic feasibility.
- Check electrophile class against the proposed mechanism. In particular, sterically hindered tertiary alkyl halides are generally unsuitable for standard bimolecular substitution and may instead undergo ionization or elimination.
- Inspect all potentially reactive groups in the partner molecule rather than assuming a single site: identify nucleophiles, carbonyls, acidic groups, aromatic substitution patterns, and incompatible functionalities.
- When conditions are unspecified, make conclusions explicitly conditional. State which pathway might furnish the proposed product and which competing pathways are likely under common conditions.
- Describe reaction SMILES as encoding the intended net transformation; do not imply that omitted reagents, conditions, or byproducts establish a detailed mechanism.
- Avoid presenting formal atom replacement or a nominal byproduct as the mechanistic sequence. Distinguish balanced/net bookkeeping from elementary steps.
- Validate the final response with a compact checklist:
  - reactant SMILES match the names;
  - product connectivity and attachment atom are correct;
  - reaction SMILES preserves unchanged fragments;
  - mechanistic constraints are addressed;
  - major competing reactions are named;
  - feasibility is not overstated.
- Keep chemistry-tool claims bounded to software observations; never suggest that a reaction was physically performed or experimentally confirmed.
