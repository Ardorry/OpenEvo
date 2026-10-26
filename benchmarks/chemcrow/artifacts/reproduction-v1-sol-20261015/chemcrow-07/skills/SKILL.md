---
name: constrained-molecular-analogue-design
description: Use when asked to propose a molecule similar to a reference while excluding a specified substructure, then assess identity, similarity, availability, and a software-planned synthesis.
---

# Constrained Molecular Analogue Design

## Scope

Use this skill for chemistry tasks that require:

- obtaining or validating a reference structure;
- designing a close analogue under a forbidden-functional-group constraint;
- checking molecular similarity and identity;
- investigating commercial availability or price;
- proposing a synthesis only when requested or when availability cannot be established.

All results are computational or documentary evidence. Never imply that a chemical was purchased, synthesized, tested, or otherwise handled physically.

## Workflow

1. Parse the request into explicit acceptance criteria:

   - reference molecule;
   - required similarity;
   - forbidden motif;
   - requested properties;
   - purchasing requirement;
   - synthesis fallback.

2. Resolve the reference structure from a chemistry database or supplied structure. Record the canonical SMILES and source. If identity or stereochemistry is ambiguous, stop and explain the ambiguity.

3. Define the excluded motif structurally before screening. Natural-language group names can be ambiguous—for example, they may refer to an acyl substituent, an N-substituent, or either. State the interpretation and, when practical, test every plausible interpretation.

4. Generate a small set of conservative analogues by modifying the smallest chemically meaningful portion of the reference. Preserve the core scaffold unless the request says otherwise.

5. Validate each candidate with software checks:

   - parse and sanitize the structure;
   - canonicalize its SMILES;
   - run an explicit SMARTS or graph-substructure search for the forbidden motif;
   - inspect closely related motifs that a generic functional-group classifier may collapse into one label;
   - confirm identity, formula, molecular weight, and valence;
   - calculate similarity to the reference using a named fingerprint and Tanimoto coefficient.

   Do not treat a generic “amide present” or similar functional-group label as proof that a specific forbidden subtype is present or absent.

6. Select the candidate that satisfies the structural constraint and best balances similarity, simplicity, and chemical plausibility. Report quantitative similarity as evidence; describe it as high, moderate, or low conservatively and in context.

7. Check purchasing information using available vendor or catalog sources. Record:

   - searched name, identifiers, and structure;
   - source and date;
   - package size, currency, and price when actually returned.

   Distinguish these outcomes precisely:

   - price found;
   - listing found but no public price;
   - no listing found in searched sources;
   - lookup could not run;
   - result remains unknown.

   A failed query, missing credential, blocked page, or unavailable API does not establish commercial unavailability.

8. If synthesis is requested or justified as a fallback, use retrosynthesis and forward-prediction software to propose a concise route. Prefer readily recognizable precursors and minimal transformations. For each step, include transformation type, reactants, predicted product, and likely selectivity or side-reaction risks. Present the route as a software-generated plan, not an executed experiment.

## Evidence Standards

Separate evidence sources in the final response:

- database evidence for identity;
- substructure-query evidence for constraint compliance;
- fingerprint evidence for similarity;
- vendor evidence for pricing or availability;
- retrosynthesis and forward-prediction evidence for route plausibility;
- manual structural reasoning only as supplementary interpretation.

If an essential tool is unavailable, mark that requirement incomplete rather than replacing it with an unsupported conclusion.

## Completion Check

Before answering, verify that:

- the reference and selected analogue have valid canonical structures;
- the forbidden motif was checked with an explicit substructure query;
- ambiguous interpretations of the motif were addressed;
- the similarity method and score are reported;
- availability claims do not exceed the vendor evidence;
- missing price data is labeled unknown, not unavailable;
- any synthesis is clearly described as predicted and unexecuted;
- plausible competing reactions or selectivity concerns are disclosed;
- every requested criterion is marked satisfied, unsatisfied, or unresolved.
