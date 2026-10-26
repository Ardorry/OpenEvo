# ChemCrow Hydrohalogenation Support

Use this skill when answering organic chemistry tasks that ask for alkene hydrohalogenation products, especially comparisons of HBr addition with and without peroxide or radical initiators, and ChemCrow-style tools are available.

## Workflow

- Treat chemistry-tool output as software evidence only. Do not imply any wet-lab execution, procurement, vendor contact, or experimental confirmation.
- Normalize named substrates and reagents with `Name2SMILES` before reaction prediction.
- Give `ReactionPredict` valid reactant SMILES separated by dots. Avoid natural-language reaction strings and dangling reaction arrows, which can produce input errors.
- Use `ReactionPredict` candidates as hypotheses, not as the sole authority. Filter candidates by core organic chemistry rules, atom economy, and the stated reaction conditions.
- For HBr addition to an unsymmetrical alkene without peroxide, apply the ionic carbocation pathway: protonation gives the more stable carbocation, then bromide attacks. The expected regiochemistry is Markovnikov.
- For HBr with peroxide or a radical initiator, apply the radical-chain peroxide effect: bromine radical addition forms the more stable carbon radical, followed by H abstraction from HBr. The expected regiochemistry is anti-Markovnikov. Clarify when this conclusion comes from known mechanism rather than directly from top-ranked predictor output.
- Convert selected product SMILES back to names with `SMILES2Name` when helpful, and report both names and SMILES if the user needs unambiguous comparison.
- Compare products by locating which alkene carbon received bromine and whether the products are constitutional regioisomers.
- Check stereochemistry. If a new stereocenter is generated under achiral radical or ionic conditions, note that attack from either face can give a racemic or stereochemical mixture unless the prompt specifies stereocontrol.

## Verification

Before finalizing:

- Confirm substrate SMILES corresponds to the named alkene and the alkene connectivity used in the mechanism.
- Confirm the selected products add H and Br across the original double bond without unjustified peroxide incorporation or double bromination.
- Confirm Markovnikov versus anti-Markovnikov placement is consistent with carbocation or radical stability.
- Resolve conflicts between predictor rankings and textbook mechanism explicitly; do not overstate tool support.
- Include a concise mechanism explanation and a direct comparison of the two products.
