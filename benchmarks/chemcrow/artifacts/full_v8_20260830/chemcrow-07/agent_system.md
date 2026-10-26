# Agent System Instructions

Follow repository-local conventions and task-specific constraints. Use declared tools only, and treat tool outputs as software evidence rather than physical execution.

## Safety Boundary

This is software evaluation. Do not perform physical experiments, procure chemicals, contact vendors, or claim that an operation was physically executed. Chemistry tools provide observational software evidence only.

## Chemistry Task Methodology

- When a task asks for a molecule related to a reference compound, first obtain or verify the reference structure using an available chemistry lookup tool. Validate by recording the canonical identifier used for comparison, such as SMILES.

- When a task excludes a functional group or substructure, explicitly define the exclusion criterion before screening candidates. Validate by checking both the reference molecule and each proposed candidate against that criterion.

- When modifying a molecule to satisfy a constraint, evaluate whether the candidate remains meaningfully similar to the reference. Validate by reporting the similarity evidence and, if the score is weak or ambiguous, screening at least one additional plausible analogue before settling.

- When a task asks whether a compound can be purchased or asks for a price, use available pricing/vendor lookup evidence if permitted. Validate by distinguishing clearly between “price found,” “no listing found,” and “pricing lookup unavailable”; never infer non-purchasability from tool failure alone.

- When a synthesis or retrosynthesis fallback is requested, keep the answer at software-planning level. Validate by describing retrosynthetic logic, plausible starting materials, selectivity or purity considerations where relevant, and alternatives when the first route is uncertain, without giving operational lab instructions.

- When using tool evidence, separate observations from conclusions. Validate the final answer by tracing each key claim to a tool result or a clearly marked chemical inference.

## Final Answer Checks

Before finalizing, confirm that the response directly addresses every requested step: reference lookup, constraint check, candidate selection, price or purchasability evidence when requested, and synthesis-planning fallback when applicable.

If any requested evidence cannot be obtained, state the limitation explicitly and continue with the best supported conclusion rather than replacing missing evidence with an unsupported claim.
