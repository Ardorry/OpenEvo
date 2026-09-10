# Chemistry Synthesis Planning With ChemCrow

Use this skill when a user asks for a software-only chemistry synthesis plan, reagent quantities, purchasability, or cost estimates for a target chemical such as an insect repellent. Treat all chemistry tool outputs as observational evidence only. Do not imply physical execution, procurement, vendor contact, or lab readiness.

## Trigger

Apply this workflow when the request includes any of:

- Choosing or identifying a chemical product for a function.
- Planning a synthesis route from named or inferred precursors.
- Estimating reagent amounts for a target product mass.
- Reporting whether reactants are purchasable or what they cost.
- Combining retrosynthesis, safety checks, and stoichiometry.

## Workflow

1. State the evidence boundary up front: the answer is a software planning calculation, not a lab protocol or procurement action.

2. Identify the target compound with ChemCrow:
   - Use `Name2SMILES` for the target.
   - Use an evidence source such as `wikipedia` only to support functional identity, not as proof of purchasability.
   - If identity is uncertain, resolve synonyms before doing stoichiometry.

3. Generate and inspect retrosynthesis:
   - Use `ReactionRetrosynthesis` on the target SMILES.
   - Prefer the simplest chemically plausible disconnection that matches known functional group logic.
   - If the tool output includes solvents, bases, salts, or extra species, separate core reactants from likely auxiliaries and explain the simplification.

4. Validate named reactants:
   - Run `Name2SMILES` for each reactant or intermediate.
   - Run `SMILES2Weight` for the product and all quantified reactants.
   - Run `Mol2CAS` when reporting tabular reactant identities.

5. Run safety and control checks:
   - Use `ControlChemCheck` for the target and any concerning precursors when relevant.
   - Use `SafetySummary` and hazard-related tools where available.
   - Report safety findings at a high level. Do not turn the answer into a step-by-step experimental procedure.

6. Compute stoichiometry:
   - Convert target mass to moles using the ChemCrow molecular weight.
   - For each reactant, calculate mass as: target moles × equivalents × molecular weight.
   - Label these quantities as theoretical minimums under the stated yield and equivalent assumptions.
   - If using 1.00 equivalent assumptions for acids, amines, acid chlorides, bases, or chlorinating agents, say this is a simplifying model.
   - Add a practical caveat that real purchasing quantities depend on yield, purity, density/concentration, reagent excess, losses, and procedure choice.

7. Handle cost and purchasability:
   - Use only declared search/vendor tools permitted by the task.
   - If cost lookup succeeds, report vendor-backed prices with package size, supplier/source, date or access context if available, and prorated cost only when justified.
   - If search fails or is unavailable, do not invent prices or imply purchasability was verified. Put “not verified” or equivalent in the cost column and explicitly state that the cost portion cannot be completed under the available evidence.
   - A placeholder price table is acceptable only if clearly marked as unverified.

## Response Shape

A strong answer should include:

- Chosen target compound and why it fits the requested function.
- Target SMILES and CAS if available.
- A high-level route, not a procedural recipe.
- A stoichiometry table with reactant, CAS, role, equivalents assumption, theoretical amount, and cost/purchasability status.
- A concise note distinguishing theoretical reagent needs from practical purchasing quantities.
- A transparent explanation of any tool failure that prevents cost verification.

## Verification

Before finalizing, check that:

- Every chemical identity in the table has either a ChemCrow SMILES or CAS lookup behind it.
- Product moles are computed from the molecular weight used in the answer.
- Reactant masses are internally consistent with the stated equivalents.
- The answer labels calculated quantities as theoretical minimums when assuming ideal yield.
- Cost claims are backed by successful search/vendor evidence, or clearly marked unavailable.
- No physical experiment, procurement action, vendor contact, or lab execution is claimed.
- Safety/control results are reported without overclaiming that a chemical is safe.
