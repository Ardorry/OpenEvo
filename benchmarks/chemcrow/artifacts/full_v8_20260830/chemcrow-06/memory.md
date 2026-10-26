# Memory

- For ChemCrow-style prediction tasks, lead with the exact software prediction for the exact reactant strings supplied by the prompt. Treat tool output as benchmark evidence, not as a physical experiment or a claim of laboratory execution.
- Keep conventional chemistry reasoning separate from literal predictor results. If the prompt omits a reagent such as hydrogen, do not make the chemically expected hydrogenation product the main answer unless the tool input/result supports it.
- When catalyst-only and chemically complete conditions differ, structure the answer as:
  1. exact input interpretation,
  2. exact predicted products for each requested condition,
  3. comparison of those predictions,
  4. caveat about conventional reaction conditions and expected selectivity.
- For alkyne hydrogenation questions, remember the common chemistry distinction: poisoned Pd catalysts favor partial reduction, while unpoisoned Pd under hydrogenation conditions can continue to full reduction. State this only as mechanistic context when the benchmark input actually includes or implies hydrogen.
- Validate substrate identity before prediction. Convert names to SMILES carefully and verify substituent positions before comparing products.
- Avoid burying literal software outputs in notes or caveats. The benchmark usually rewards the requested prediction most directly.
- Do not overstate mechanistic conclusions from catalyst strings alone. If a necessary reagent is absent, explicitly say the exact software-input prediction may differ from textbook reaction expectations.
