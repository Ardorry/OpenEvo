# Memory

- For chemistry planning tasks, keep the response explicitly framed as a software/literature-style plan. Do not imply physical execution, procurement, vendor contact, or experimental completion.
- When structure-sensitive tools reject a plain chemical name, resolve the name to a stereochemically specified SMILES first, then rerun retrosynthesis, prediction, safety, and property tools on the SMILES.
- Strong answers should triangulate synthesis logic with multiple tool observations: name-to-structure, retrosynthesis, forward reaction prediction, precursor naming, molecular weights, and safety/control checks.
- Preserve stereochemical reasoning explicitly. If chirality is already installed in a precursor and the proposed transformation occurs away from stereocenters under mild conditions, state that the configuration should be retained.
- For thiourea-forming plans, include a concise representative condition set: dry aprotic solvent such as DCM or THF, inert/dry handling where appropriate, room-temperature addition of isothiocyanate to free amine, and monitoring until starting materials are consumed.
- Include practical but non-operational optimization details when useful: approximately equimolar stoichiometry, possible slight excess of the less hazardous or easier-to-remove reagent, optional tertiary amine only if the amine is used as a salt, and purification by chromatography, trituration, or recrystallization depending on solubility.
- Safety summaries should connect hazards to handling controls. For reactive irritants/corrosives such as isothiocyanates, mention ventilation/fume hood, avoiding inhalation or skin contact, and minimizing unnecessary excess.
- Do not overtrust incomplete functional-group tool output. If the tool misses an obvious motif, rely on the resolved structure and reaction evidence while noting the chemistry correctly.
- Characterization expectations improve completeness: mention NMR evidence for key NH or product resonances, MS/HRMS matching formula/mass, and stereochemical confirmation by optical rotation or chiral HPLC when relevant.
- Final synthesis plans should be specific enough to show chemical feasibility but avoid detailed executable lab protocols beyond high-level conditions and analytical checkpoints.
