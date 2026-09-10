# ChemCrow Synthesis Safety Answers

## Trigger

Use this skill when a chemistry task asks for a synthesis route, required reactants, CAS identifiers, or GHS/safety ratings, especially when ChemCrow-style tools are available.

## Workflow

1. Treat all chemistry tool outputs as software observations only. Do not imply physical execution, procurement, or real lab validation.
2. Keep synthesis guidance at a high educational level unless the user explicitly has a legitimate lab context and safety policy allows more detail. Avoid unnecessary temperatures, timings, quantities, or step-by-step operational optimization.
3. Resolve names to structures before using reaction tools:
   - Use `Name2SMILES` for target, reactants, catalysts, and likely byproducts.
   - Use SMILES strings, not common names, with reaction prediction or retrosynthesis tools.
4. Use retrosynthesis or reaction prediction to identify the minimum reactant set needed for the target.
   - Report only reactants/catalysts actually supported by the tool output or clearly label common chemistry knowledge separately.
   - Do not claim a catalyst or additive was tool-confirmed unless it appears in the returned route.
5. Use `Mol2CAS` to tie each reported substance to a CAS number when possible.
6. Use `SafetySummary` for each required reactant/catalyst.
   - If the safety tool returns general hazard evidence but not exact GHS pictograms or H-statements, say that exact GHS classification was not exposed by the tool.
   - If providing typical SDS-style GHS information from general knowledge, label it as provisional and require verification against the current supplier SDS.
7. Keep the final answer scoped to what was asked:
   - Include required reactants and catalysts.
   - Avoid optional alternatives, solvents, and workup materials unless the user asked for them or they are necessary to clarify scope.
   - Separate “tool-supported observations” from “general chemistry context” and “provisional safety guidance.”

## Verification

Before finalizing, check that:

- Every reaction-tool call used valid SMILES input.
- Any failed or unavailable tool call is disclosed only if it affects confidence.
- The route does not overstate what the tools confirmed.
- GHS entries are either directly supported by evidence or clearly marked provisional.
- The answer avoids procedural lab detail beyond the user’s need.
- The response includes a safety caveat that real work requires qualified supervision, appropriate controls, and current SDS review.
