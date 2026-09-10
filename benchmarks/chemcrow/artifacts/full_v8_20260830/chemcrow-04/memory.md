# Memory

- For synthesis-planning tasks asking for an insect repellent, choose a well-known target only after briefly justifying the selection against plausible alternatives.
- Always separate:
  - incorporated reactants that determine stoichiometric material needs
  - upstream reagents used to prepare intermediates
  - auxiliaries such as bases, solvents, drying agents, catalysts, or workup materials
- If the user asks for cost or purchasability, do not treat failed search/tool access as an answer. Explicitly mark each item as `verified purchasable`, `not verified`, or `not purchasable from available evidence`.
- Include a cost table whenever possible with compound, role, required theoretical amount for target mass, purchasability status, unit price/source basis, and estimated cost.
- When costs cannot be verified, say so directly and avoid implying the cost portion is complete.
- For 100 g synthesis accounting, show the mole basis from target molecular weight, then compute reagent masses from stoichiometry. Label values as theoretical and note that yield, purity, excess, and route choice change real purchasing quantities.
- If there are direct-buy and in-situ intermediate routes, provide a concise material-balance comparison so the user can see which reagents are counted under each scenario.
- Keep chemistry plans conceptual and safety-bounded: no physical execution claims, procurement actions, operational procedure detail, or vendor contact. Chemistry tools are observational evidence only.
