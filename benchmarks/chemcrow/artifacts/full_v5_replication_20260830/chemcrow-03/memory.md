# Memory

- For ChemCrow chemistry tasks, treat tool results as software evidence only. Do not imply lab execution or experimental confirmation.
- Normalize named molecules with `Name2SMILES` before using reaction tools.
- `ReactionPredict` may reject plain-English reaction strings and reaction-SMILES with an empty product side. A successful pattern was dot-separated reactant SMILES without `>>`.
- Do not blindly take the top `ReactionPredict` candidate. Filter candidates through known organic chemistry mechanisms, especially when additives change mechanism.
- For HBr addition to unsymmetrical alkenes:
  - Without peroxide: use ionic carbocation reasoning and Markovnikov regiochemistry.
  - With peroxide: use the HBr radical chain effect and anti-Markovnikov regiochemistry.
  - Clarify that the peroxide conclusion is based on the known radical mechanism, with software prediction as supporting or imperfect evidence.
- When comparing products, explicitly state where the halogen ends up and identify whether products are regioisomers.
- Include stereochemical caveats when a new stereocenter is formed. Radical or planar-intermediate pathways can give mixtures, often racemic absent chiral influence.
- Use `SMILES2Name` to label selected product SMILES so final answers are unambiguous, but verify names against mechanism before presenting them.
