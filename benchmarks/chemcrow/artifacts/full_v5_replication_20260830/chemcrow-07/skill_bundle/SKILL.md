# ChemCrow Analog Design

Use this skill when a chemistry task asks to design or identify a molecule similar to a reference compound while excluding a specified functional group or substructure, then check purchasability/price and optionally plan a synthetic route.

## Workflow

1. Resolve the reference compound with `Name2SMILES`.
2. Propose candidate analogs by making small, chemically interpretable substitutions that preserve the requested scaffold or pharmacophore.
3. For each candidate:
   - Convert name to SMILES with `Name2SMILES`.
   - Check broad functional groups with `FunctionalGroups`.
   - Check similarity to the reference with `MolSimilarity`; format input as exactly two SMILES separated by a period.
   - Verify the forbidden group explicitly as a substructure, not only by broad functional-group labels.
4. Define the forbidden motif before deciding. For methylamide/acetamide-like exclusions, state the structural interpretation, such as an amide carbonyl directly bonded to methyl, and check candidates consistently against that definition.
5. Prefer the most similar candidate that satisfies the substructure constraint. If choosing a less similar candidate, explain the chemical reason clearly.
6. Use `SMILES2Name` and `Mol2CAS` to report identifiers for the selected molecule.
7. Use available purchase/search tools only as observational software evidence. If price or availability cannot be established, say so directly.
8. If the molecule is above the requested price threshold or purchasability is unconfirmed, call `ReactionRetrosynthesis` for the selected product and validate the proposed reactants with `ReactionPredict`.

## Verification

- Report the reference SMILES, selected analog name, selected analog SMILES, functional groups, similarity score, and registry identifiers when available.
- Include the explicit forbidden-substructure check and whether the candidate passes it.
- If route planning is needed, report the retrosynthesis and forward-prediction evidence with confidence values when available.
- Note major selectivity risks in the planned route. For aminophenol acylations, mention possible N-acylation versus O-acylation competition.
- Keep all synthesis and purchasing statements at software-planning level. Do not claim physical execution, chemical procurement, or vendor contact.
