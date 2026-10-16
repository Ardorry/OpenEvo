# Chemistry Prediction Memory

- Identify implicit reagents and assumptions before predicting products. For catalytic hydrogenations, state briefly when molecular hydrogen is assumed; distinguish the conventional reaction interpretation from a literally catalyst-only input.
- Compare catalysts by selectivity:
  - Poisoned palladium commonly stops alkyne reduction at the alkene through surface syn addition and rapid alkene desorption.
  - Unpoisoned palladium commonly continues reduction to the alkane.
- Check whether the alkene product actually supports stereochemical labeling. A terminal alkene generally has no E/Z designation.
- Preserve unrelated substituents in the expected textbook product unless conditions suggest otherwise. Qualify this when unpoisoned palladium could cause condition-dependent side reactions such as dehalogenation.
- Validate each proposed structure by checking carbon count, substituent positions, saturation level, charge, and SMILES connectivity.
- Present the result compactly: catalyst, principal product, structure representation, then a short mechanistic comparison.
- Treat chemistry-tool outputs as software observations, not physical experiments. If a predictor returns catalyst fragments, omits implicit reagents, or otherwise behaves anomalously, rely on established chemistry while noting the limitation briefly.
- Avoid lengthy retry logs or tool-process narration when the user asks primarily for the chemical outcome.
