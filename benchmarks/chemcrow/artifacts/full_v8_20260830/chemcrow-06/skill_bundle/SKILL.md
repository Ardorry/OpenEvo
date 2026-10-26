---
name: chemcrow-reaction-prediction-reflector
description: Use for ChemCrow-style reaction prediction tasks that compare products from catalyst or reagent variants and require separating exact software predictions from conventional chemistry expectations.
---

## Trigger

Use this skill when a task asks Codex to predict or compare organic reaction products using ChemCrow or a similar software bridge, especially when the requested inputs are exact reactant or catalyst strings and the chemistry context may imply missing reagents, conditions, or conventional assumptions.

## Workflow

1. Treat the task as software evaluation only. Do not describe any physical experiment, procurement, synthesis, or real-world execution.
2. Identify the exact benchmark input strings the user supplied:
   - substrate or reactant representation
   - catalyst or reagent mixture variants
   - any explicitly included or omitted reagents, such as hydrogen sources
3. Run the available reaction-prediction helper or ChemCrow bridge on the exact supplied inputs first.
4. Preserve a distinction between:
   - direct software predictions for the literal input strings
   - chemistry caveats or conventional expectations under additional conditions
5. Lead the answer with the exact literal-input prediction results when the benchmark asks for prediction.
6. Add a clearly labeled caveat only after the direct results if conventional chemistry would require missing conditions or would plausibly give different products.
7. For comparison questions, explicitly state whether the predicted products are identical or different, then explain the difference in terms of catalyst selectivity, reagent presence, or model behavior.
8. Keep mechanistic discussion concise and tied to the predicted comparison. Avoid letting textbook expectations replace the exact software evidence.

## Helper Checks

Before finalizing, check that:

- The substrate and reagent/catalyst strings used by the predictor match the user’s requested strings.
- No implied reagent was silently added to the benchmark prediction.
- Any conventional chemistry interpretation is separated from the literal software result.
- The final comparison directly answers whether the two predicted products differ.
- The response does not claim physical execution or experimental validation.

## Verification

Completion is proven when the final answer contains:

- The direct prediction for each requested input variant.
- A direct comparison of those predictions.
- A short explanation of why the products are the same or different.
- A caveat section if conventional chemistry depends on conditions absent from the provided input.
- No unsupported claim that any wet-lab operation was performed.
