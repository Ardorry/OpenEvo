# ChemCrow Reaction Feasibility Reflector

## Trigger

Use this skill when a chemistry task asks to convert molecule names to SMILES, predict reaction products, compile reaction SMILES, identify reaction type, compare functional-group compatibility, and decide whether the reaction is likely to proceed cleanly.

## Workflow

1. Use only the declared chemistry bridge/tools for observational chemistry evidence. Do not imply any physical experiment was performed.
2. Convert each reactant name to SMILES before prediction.
3. Run the reaction predictor on the reactant SMILES string and record the top predicted product plus reaction SMILES.
4. Identify the nominal reaction type from the product-forming bond and reactant roles.
5. Check reaction-class compatibility separately from the prediction. Treat the predictor output as a proposed product, not proof of clean feasibility.
6. Run functional-group detection on each reactant, and optionally on the predicted product if it helps explain selectivity.
7. Reason chemically about likely interference:
   - For Williamson-like ether formation, verify whether the electrophile is suitable for SN2.
   - Primary alkyl halides are generally compatible with alkoxide SN2; tertiary alkyl halides are a major red flag.
   - For tertiary substrates, discuss possible SN1-like substitution only if conditions support it, and note competing elimination or solvolysis.
   - With phenols under alkylating conditions, consider O-alkylation versus possible aromatic C-alkylation/side reactions.
   - For ketone-containing reactants, consider enolization or base-sensitive side reactions when basic conditions are implied.
8. If reaction conditions are not specified, explicitly state that feasibility is uncertain and avoid calling the reaction problem-free.

## Answer Shape

Include:

- Reactant SMILES.
- Top predicted product SMILES.
- Reaction SMILES.
- Product described structurally or by a reasonable name if tool naming is unhelpful.
- Reaction type.
- Functional groups for each reactant.
- Compatibility decision.
- Final conclusion on whether the reaction should proceed without problems.

## Verification

Before finalizing, check that:

- The reaction SMILES contains all reactant SMILES and the selected product SMILES.
- The reaction type matches the bond-forming event, not just the predictor label.
- The conclusion distinguishes “software-predicted product” from “chemically clean expected reaction.”
- Any unavailable tool or failed lookup is mentioned briefly, and the final reasoning does not depend on missing evidence.
- The final answer directly addresses whether interference or side reactions are likely.
