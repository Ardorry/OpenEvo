# Memory

- For ChemCrow-style chemistry tasks, use only the declared chemistry bridge for evidence and clearly frame outputs as software-only observations, not physical synthesis, procurement, or execution.
- Start by resolving names to SMILES, then validate candidate molecules with tool evidence instead of relying on intuition.
- Broad functional-group output is not enough for negative substructure constraints. Define the forbidden motif explicitly, preferably with a SMARTS/substructure interpretation, and check candidates against that same definition.
- When comparing candidate analogs, keep similarity scores and constraint satisfaction separate. If choosing a less similar candidate, explain why the stricter constraint interpretation overrides numerical similarity.
- Pay close attention to tool input formats. For pairwise similarity, use the exact delimiter/format required by the tool and retry promptly when the error message specifies the expected input.
- If price or vendor search tools are unavailable, do not infer purchasability or cost. State that procurement evidence is unconfirmed and proceed to a route plan only if the task allows that fallback.
- For synthetic route plans, validate retrosynthesis with forward reaction prediction when available. Include the major disconnection and confidence values, but keep the plan high-level.
- Mention obvious chemoselectivity risks in route plans, especially when starting materials contain multiple reactive groups that could give side products.
- In final answers, distinguish verified tool evidence from manual structural reasoning and from unavailable evidence.
