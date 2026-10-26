# Memory

For ChemCrow-style reaction tasks, keep tool outputs and chemical judgment clearly separated. Use name-to-SMILES, reaction prediction, and functional-group tools as software evidence, then independently assess whether the predicted transformation is chemically plausible under the stated conditions.

When a predictor gives a high-confidence product, do not overstate it as the expected real outcome if the reaction class has known substrate limitations. State sharply when a product is only software-predicted and conditions are missing or incompatible.

For Williamson/O-alkylation questions, explicitly check alkyl halide substitution class. Primary halides fit clean SN2 logic; tertiary halides are a major red flag for Williamson ether synthesis and may favor SN1-like pathways, elimination, solvolysis, or other side reactions.

Phenols should be treated more specifically than generic alcohols. If tert-butylation or strongly activating conditions are involved, consider both O-alkylation and competing aromatic C-alkylation where relevant.

Always discuss missing reaction conditions when they materially affect feasibility: base, acid promotion, solvent, temperature, and nucleophile generation can change whether a nominal product is plausible or clean.

Useful answer structure:
- List reactant SMILES and predicted product/reaction SMILES from tools.
- Identify the nominal reaction type.
- State functional groups of each reactant.
- Compare the reaction type’s compatibility rules against those groups and substrate classes.
- Conclude directly whether the reaction should proceed cleanly, distinguishing “possible/predicted” from “problem-free expected.”

If a support tool fails, note the failure briefly and use available tool evidence plus standard chemistry reasoning; do not let a missing web/search call weaken the core compatibility conclusion.
