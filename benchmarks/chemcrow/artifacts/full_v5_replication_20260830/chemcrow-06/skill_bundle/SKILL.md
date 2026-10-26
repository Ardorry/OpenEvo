# ChemCrow Lindlar/Pd Hydrogenation Comparison

## Trigger
Use this skill when a task asks for ChemCrow-assisted prediction and mechanistic comparison of an alkyne reaction under Lindlar or poisoned-palladium conditions versus unpoisoned palladium, especially when the prompt provides catalyst SMILES and asks for both products and mechanisms.

## Workflow
1. Treat all ChemCrow outputs as software observations only. Do not imply any physical experiment was run.
2. If any reactant is given as a name, resolve it with `Name2SMILES` before using `ReactionPredict`; `ReactionPredict` expects SMILES-style inputs.
3. Run `ReactionPredict` on the literal mixture requested by the user.
4. Check whether a hydrogen source is present. Lindlar and Pd hydrogenations require molecular hydrogen; if the prompt omits it, clearly separate:
   - the literal no-hydrogen ChemCrow prediction, as a software artifact of the exact input
   - the chemically meaningful hydrogenation expectation, using an added hydrogen source only as an explicit diagnostic assumption
5. Convert important product SMILES to names with `SMILES2Name` so the final comparison is readable.
6. In the answer, lead with the chemically meaningful comparison when the user asks for reaction products and mechanisms:
   - poisoned/Lindlar-type Pd partially hydrogenates alkynes and stops at the alkene
   - unpoisoned Pd can continue hydrogenation from alkyne to alkene to alkane
7. If literal no-hydrogen predictions look like coupling, rearrangement, or otherwise nonstandard products, report them briefly but do not give them a strong mechanistic interpretation unless the user specifically asks to analyze model artifacts.

## Response Shape
- State that only ChemCrow software observations were used.
- Provide a compact table of literal tool predictions, including product SMILES/name and confidence when available.
- Add a caveat if hydrogen is missing from the user’s input.
- Provide the hydrogenation-condition comparison separately and explicitly.
- Keep the mechanism focused on catalyst activity/selectivity: poisoned Pd suppresses over-reduction; bare Pd is more active and can fully reduce the alkyne.

## Verification
Before finalizing, check that:
- named substrates were converted to SMILES before prediction
- tool errors from invalid non-SMILES inputs are not treated as results
- missing hydrogen is called out with exact role and consequence
- literal-input model outputs are not overinterpreted as normal chemistry
- the final comparison clearly distinguishes partial reduction by Lindlar-type catalyst from full reduction by unpoisoned Pd
