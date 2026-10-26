# Memory

- For chemistry prediction tasks, distinguish software observations from chemical interpretation. State clearly that no physical experiment was performed and that tool outputs are computational evidence only.
- Reaction prediction tools may require valid SMILES, not IUPAC/common names. Resolve named substrates to SMILES first, then rerun failed predictions with the resolved structure.
- Check whether the prompt’s literal reagent list contains all chemically necessary reagents. If a named reaction normally requires an omitted reagent such as a hydrogen source, explicitly separate the literal-input tool result from the chemically meaningful reaction condition.
- When a literal tool prediction is chemically questionable because the reagent set is incomplete, report it briefly as a model artifact and avoid over-explaining it mechanistically.
- Put the core chemical answer first when comparing catalysts: poisoned palladium under hydrogenation conditions stops at partial alkyne reduction to an alkene, while unpoisoned palladium can continue to the fully reduced alkane.
- Use conversion tools to translate top product SMILES into names when it helps comparison, but do not let tables of tool artifacts obscure the expected chemistry.
- Validation check: if an initial prediction errors with “invalid SMILES,” do not treat it as a failed chemistry result; repair the input format and rerun.
- Validation check: before finalizing, confirm that the final comparison explicitly names which catalyst gives partial reduction and which gives full reduction under the corrected reaction conditions.
