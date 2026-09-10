---
name: chem-analogue-screening
description: Use for software-only chemistry tasks that ask for a molecule similar to a reference compound while excluding a functional group, checking purchasability or price evidence, and proposing a retrosynthetic fallback.
---

# Chem Analogue Screening

## Trigger

Use this skill when the task asks to:

- Find or design a molecule similar to a named reference molecule.
- Obtain or validate SMILES/InChI or other structural identifiers.
- Exclude a specified functional group or substructure.
- Compare molecular similarity against the reference.
- Check whether the candidate can be purchased or priced.
- Provide a retrosynthesis route if purchase or price evidence is unavailable.

This skill is for computational chemistry planning only. Do not perform, instruct, or imply physical synthesis, procurement, vendor contact, or experimental execution.

## Workflow

1. Resolve the reference molecule.
   - Use chemistry lookup tools to obtain a canonical structure identifier.
   - Record the identifier source and normalize the structure before comparison.
   - If the reference itself contains the excluded group, explicitly state that it fails the constraint.

2. Define the exclusion criterion before screening.
   - Translate the named group into an explicit substructure rule.
   - If the term is ambiguous, state the interpretation used and, when useful, check the plausible alternatives separately.
   - Do not silently equate broad family membership with a more specific group unless the task wording supports it.

3. Generate or select candidate analogues.
   - Prefer close scaffold-preserving modifications before moving to more distant structures.
   - Check more than one candidate when the first valid candidate has weak similarity.
   - Track rejected candidates with the reason for rejection.

4. Run helper checks.
   - Structure lookup: confirm candidate SMILES or equivalent identifier.
   - Functional group/substructure check: prove the excluded group is absent.
   - Similarity check: compute or retrieve a similarity score against the reference.
   - Basic chemical reasonableness check: verify valence, recognizable scaffold, and intended functional groups.

5. Evaluate similarity.
   - Report the similarity score and method if available.
   - If the score is modest, discuss whether it plausibly satisfies “similar” and compare against at least one closer or alternative candidate when feasible.
   - Prefer the highest-similarity candidate that passes the exclusion rule and remains chemically sensible.

6. Check purchase or price evidence.
   - Use only software lookup, catalog, or web evidence.
   - Report an actual price, catalog evidence, or clearly state that price evidence could not be found.
   - Do not treat tool failure or missing search results as proof that the molecule cannot be purchased.

7. Retrosynthesis fallback.
   - If the task asks for synthesis when purchase is unavailable, provide a software-level retrosynthetic plan only.
   - Keep routes at planning granularity: starting-material classes, transformations, selectivity concerns, purification considerations, and alternatives.
   - Avoid operational quantities, temperatures, times, step-by-step lab instructions, or procurement directions.

## Final Response Requirements

A complete answer should include:

- Reference structure identifier and candidate structure identifier.
- Explicit excluded-substructure definition.
- Evidence that the candidate lacks the excluded group.
- Similarity score or qualitative comparison with method/source.
- Price or purchasability evidence, or a clear limitation if unavailable.
- Retrosynthetic fallback only when justified by the task and clearly marked as software planning.
- A short statement that no physical experiment or procurement was performed.

## Verification

Before finalizing, check that:

- The chosen candidate actually satisfies the exclusion rule.
- Similarity is not accepted uncritically if it is low or borderline.
- Pricing status is evidence-based and not inferred from tool unavailability.
- Any synthesis discussion remains non-executable and computational.
- The answer separates observed tool evidence from chemical inference.
