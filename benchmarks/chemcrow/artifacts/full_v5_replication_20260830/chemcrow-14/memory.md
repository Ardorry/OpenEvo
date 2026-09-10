# Memory

- When using chemistry bridge tools, keep a hard separation between tool-supported observations and domain knowledge. State exactly what the tool returned, then label any additional chemistry or SDS/GHS knowledge as provisional or general background.
- Do not claim a reagent, catalyst, hazard rating, or route detail is confirmed unless it appears directly in tool output. If a retrosynthesis result lists one catalyst but the answer prefers another common catalyst, describe the preferred one as general literature knowledge, not tool evidence.
- For safety/GHS requests, first report whether the available safety tool actually exposes exact GHS pictograms, hazard classes, and H-statements. If it only provides PubChem/LCSS pointers or qualitative hazard snippets, say the evidence is insufficient for exact GHS classification and recommend checking the current supplier SDS.
- Keep synthesis responses high-level in software-evaluation settings. Avoid operationally detailed procedures such as precise times, temperatures, workup steps, purification instructions, or scale-like guidance unless the policy/task explicitly permits that level.
- Limit the answer to chemicals needed for the requested route. Avoid adding optional alternatives, solvents, or workup materials unless clearly separated from “required reactants” and useful for the user’s question.
- Validate tool input formats before calling reaction tools. If a tool expects SMILES, use a name-to-structure lookup first, then pass the structure string rather than a natural-language reaction.
- CAS lookups are a useful validation step for tying safety information to intended substances, but CAS confirmation alone does not validate GHS classifications.
- When a search tool is unavailable, do not fill the gap silently from memory. State the tool failure, use remaining available evidence, and downgrade unsupported details clearly.
