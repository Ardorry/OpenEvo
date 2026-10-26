# Memory

## Chemistry Synthesis Planning

- For target molecule synthesis prompts, start with a clear strategic disconnection that maps the target functional group to plausible, named precursor classes.
- Explicitly preserve stereochemistry: identify which precursor carries the stereocenters, whether any new stereocenter is formed, and how the product stereochemical assignment follows from the starting material.
- Do not stop at a high-level route. Include a concise representative, non-executed procedure with typical equivalents, solvent class, concentration range, temperature, reaction time range, atmosphere if relevant, monitoring, workup, and purification options.
- If a key coupling partner is assumed commercially available, add an optional upstream preparation route from a common precursor when straightforward.
- Add brief mechanistic rationale in plain language instead of relying only on tool confidence or prediction scores.
- State that all chemistry is planning/software-only and not physically executed.

## Validation Habits

- Cross-check the proposed disconnection against the target’s IUPAC name, stereochemical descriptors, and expected connectivity.
- Verify that the reaction does not accidentally racemize or invert stereocenters under the proposed conditions.
- Confirm molecular role assignments: nucleophile/electrophile, leaving group or transfer reagent, and final functional group formation.
- Include safety and hazard framing at a planning level without giving procurement or execution encouragement.
- When using tool evidence, translate it into chemically interpretable reasoning and note uncertainty where the plan depends on assumed availability or compatibility.
