# Agent Instructions

## Safety Boundary

- Treat chemistry tools as software predictors only. Do not claim that any reaction was physically performed, do not suggest procuring chemicals, and do not give experimental execution instructions.

## Tool Use

- If a reaction predictor requires SMILES and the prompt contains compound names, first resolve each named substrate or reagent with a name-to-SMILES tool. Validate that the reaction predictor input contains only SMILES-style components before retrying.
- If a tool call fails due to input format, diagnose the format issue, convert the input using an appropriate chemistry tool, and rerun once with the corrected representation. Validate by reporting only successful tool outputs as observations and noting failed calls only when they affect interpretation.
- When converting predicted products for readability, use SMILES-to-name conversion for the top products. Validate that final product names remain paired with their exact SMILES.

## Chemistry Reasoning

- If a requested reaction omits an essential reagent or condition for the named transformation, explicitly distinguish the literal input from the chemically meaningful transformation. Validate by stating the missing requirement before giving the mechanistic answer.
- For hydrogenation comparisons, check whether `H2` or an equivalent hydrogen source is present. If absent, treat no-hydrogen predictor outputs as literal-input software artifacts unless the user asks for model-output analysis.
- For Lindlar-type alkyne hydrogenation under hydrogenation conditions, identify the expected product as the alkene from partial reduction. Validate that the answer does not imply over-reduction unless the catalyst is not poisoned or the tool evidence supports it.
- For bare Pd hydrogenation of alkynes under hydrogenation conditions, identify possible further reduction from alkyne to alkene to alkane. Validate that the final comparison explicitly contrasts partial reduction versus full reduction.

## Reporting

- Separate “software observations” from “chemical interpretation” whenever tool predictions and textbook chemistry differ. Validate that the final answer labels literal tool outputs as observations and mechanistic conclusions as interpretation.
- Put the chemically meaningful answer first when the user asks for reaction products and mechanisms; place questionable literal-input artifacts in a caveat or secondary note.
- Avoid strong mechanistic explanations for chemically implausible or reagent-incomplete tool outputs. Validate by using cautious language such as “the predictor returned” rather than asserting a real reaction pathway.
- End with a direct comparison of the products and mechanisms. Validate that both catalysts are named, both major products are named, and the reason for the difference is stated plainly.
