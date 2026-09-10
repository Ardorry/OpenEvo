# ChemCrow Desktop Synthesis and Cost Assessment

## Trigger
Use this skill when a user asks for a software-only chemistry assessment of how a named molecule could be synthesized and what the reactants would cost, especially when the task is constrained to ChemCrow or other declared chemistry tools.

## Workflow
1. Treat the task as a desktop analysis only. Do not imply any experiment, procurement, vendor contact, or physical execution.
2. Establish the target identity before planning:
   - Use name-to-structure lookup for the target.
   - Confirm molecular weight and identifiers from structure-based tools when available.
   - Clarify whether the requested target mass refers to free base, salt, hydrate, or another form before doing scale arithmetic.
3. Run safety and control checks early:
   - Use controlled-substance, explosive, patent, and safety-summary tools on the canonical structure where possible.
   - If a tool fails on a name, retry with the confirmed structure.
   - Report tool limitations as limitations, not as confirmed absence.
4. Build the route from tool evidence:
   - Use retrosynthesis for the main disconnection.
   - Confirm proposed precursors with name-to-structure lookup.
   - Use reaction prediction only for transformations it can actually evaluate.
   - If reaction prediction rejects a full reaction string, retry with the input format accepted by the tool, such as reactants-only input.
   - Do not claim a practical reagent or condition was validated unless the successful prediction included it or another tool directly supports it.
5. Keep the synthesis answer high level:
   - Provide a route sketch and rationale.
   - Avoid detailed operational conditions, purification instructions, and execution-ready lab procedure.
   - Flag stereochemistry, salt/free-base conversion, racemization risk, and yield uncertainty where relevant.
6. Handle cost separately from stoichiometry:
   - Use molecular weights to compute theoretical minimum amounts for the requested target scale.
   - Clearly label these as theoretical or yield-adjusted estimates, not a bill of materials.
   - If using assumed yields, state the assumptions and show their effect on quantities.
   - Use purchasability/price tools only if available and permitted.
   - If price search is unavailable or returns no usable evidence, state that the cost portion cannot be completed from available evidence rather than inventing prices.
7. Structure the final answer with distinct sections:
   - Target identity
   - Desktop route
   - Stoichiometric or yield-adjusted quantities
   - Cost availability or cost result
   - Safety/regulatory observations
   - Evidence limitations

## Verification
Before finalizing:
- Confirm the target structure, form, and molecular weight used in calculations.
- Check that every numerical quantity traces back to molecular weight, target mass, equivalents, and any yield assumption.
- Confirm price claims are backed by actual price evidence; otherwise mark cost as not computable.
- Ensure reaction-prediction claims do not overstate what the successful tool call showed.
- Separate “not found by this tool” from “does not exist” for patent, control, explosive, and availability findings.
- Verify the answer remains a desktop assessment and does not read like instructions to perform synthesis or buy chemicals.
