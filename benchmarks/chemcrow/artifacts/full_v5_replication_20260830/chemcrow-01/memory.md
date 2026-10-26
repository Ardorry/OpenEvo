# Memory

- When a task restricts evidence sources, obey that boundary strictly and say when a requested subtask cannot be completed from the available evidence. Do not fill price, purchasability, or regulatory gaps with assumptions.
- Separate “route evidence” from “cost evidence.” If pricing tools fail, state that cost is not computable, then optionally provide only a clearly labeled stoichiometric mass worksheet.
- For chemical synthesis/cost prompts, first clarify the requested target form and scale, especially free base vs salt, before calculating quantities.
- Label reagent-amount tables as theoretical minimums unless yield assumptions, excesses, purity, solvent/reagent grade, and vendor package minimums are included.
- Do not overinterpret reaction-prediction tools. A reactants-only product prediction supports a disconnection, but it does not validate practical conditions, reductants, workup, selectivity, yield, or purification.
- If a chemistry tool rejects mapped reaction syntax, retry with the accepted input format and preserve the limitation in the final answer.
- Validate identity and arithmetic with independent tool calls when possible: name-to-structure, molecular weight, CAS/identifier, retrosynthesis, reaction prediction, safety, and control/explosive checks.
- Regulatory wording should be careful: “no exact match reported by this tool” is not the same as “not regulated.”
- Include practical chemistry caveats when relevant: stereochemistry retention/racemization risk, salt neutralization, purification, yield adjustment, and hazardous reagent handling.
- Safety summaries should highlight the most consequential hazards of key reagents and products without turning a desktop assessment into an executable lab protocol.
