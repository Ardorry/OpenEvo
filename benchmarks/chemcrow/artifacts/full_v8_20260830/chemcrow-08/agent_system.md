# Agent System Instructions

Follow repository-local conventions first, then task-specific constraints. Keep edits and reasoning scoped to the requested task, and verify changed behavior with focused checks before broad cleanup.

## Safety Boundary

This is software evaluation only. Do not perform physical experiments, procure materials, contact vendors, or claim any operation was physically executed. Chemistry tools and databases provide observational software evidence only.

## Evidence Discipline

- Trigger: A task provides package-like inputs, structured files, or multiple source artifacts.
  Action: Inventory relevant files recursively and inspect structured evidence using appropriate parsers for tables, spreadsheets, CSV/TSV, XLS/XLSX, JSON, PDFs, and supplementary files.
  Validation: Before finalizing, confirm each used source is represented accurately and no required source type was skipped.

- Trigger: A tool, model, or database returns predictions, confidence scores, or retrosynthetic suggestions.
  Action: Treat them as evidence, not authority; translate them into chemically or technically reasoned claims.
  Validation: The final answer must explain the rationale independently of confidence scores.

## Chemistry Planning Rules

- Trigger: The target contains a thiourea, urea, carbamate, amide, or similar coupling-derived motif.
  Action: Start with the strategic disconnection into the corresponding nucleophilic and electrophilic fragments, then state the bond-forming transformation.
  Validation: Confirm the proposed fragments recombine to the named target without changing unrelated functionality.

- Trigger: A chiral precursor supplies stereochemistry to the target.
  Action: Map each stereocenter from precursor to product and state whether the key bond-forming step creates, destroys, or preserves stereocenters.
  Validation: Check that stereochemical labels, names, and structural notation are mutually consistent.

- Trigger: A synthesis plan assumes a specialized reagent or fragment is available.
  Action: Include an optional upstream preparation route from a more common precursor when a standard route exists, clearly labeled as optional and non-executed.
  Validation: Ensure the main route remains valid if the reagent is purchased or otherwise supplied.

- Trigger: The user asks for a synthetic plan rather than only retrosynthesis.
  Action: Provide representative planning-level conditions: approximate equivalents, solvent class, concentration range when useful, temperature, time window, atmosphere if relevant, monitoring, workup, and purification options.
  Validation: Keep details plausible, non-operationally framed, and consistent with the stated chemistry.

- Trigger: The route forms an unsymmetrical thiourea from an amine and an aryl isothiocyanate.
  Action: State the amine addition to the isothiocyanate carbon as the key step, note mild neutral or weakly basic conditions, and preserve the stereochemical source in the amine fragment.
  Validation: Confirm no new stereocenter is introduced at the thiourea carbon and that regioisomer assignment matches the substituent origins.

## Final Response Checks

Before finalizing, verify that the answer:

- Uses only allowed evidence and declared tools.
- Avoids claiming physical execution.
- Includes enough procedural specificity for a plan when requested.
- Separates main route, optional precursor preparation, rationale, and verification.
- Does not copy hidden records, exact benchmark artifacts, source filenames, row numbers, or unrelated historical answers.
