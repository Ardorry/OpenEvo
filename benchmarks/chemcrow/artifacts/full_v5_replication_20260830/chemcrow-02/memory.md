# Memory

- For chemistry proposal tasks, keep claims tightly separated: reaction-product feasibility evidence is not evidence of catalyst rate enhancement, selectivity, or recyclability.
- Database non-hits can support novelty only weakly. State which structural combination is intended to be new, then caveat that PubChem/SureChEMBL absence is not a full literature or patent search.
- Use clear structure-derived names. Avoid abbreviations that have established meanings unless the proposed structure actually contains that motif.
- When proposing CCU catalysts, include hypothesized practical performance criteria: low temperature, dilute or low-pressure CO2 activity, tolerance of captured-stream impurities, recyclability, and ease of separation. Keep these framed as hypotheses unless directly supported.
- Always include plausible limitations: halide nucleophilicity versus stability, salt viscosity or solubility, ion-pair effects, deactivation by strong hydrogen-bonding impurities, and incomplete safety data for novel compounds.
- If web search or literature tools are unavailable, explicitly pivot to available software evidence and avoid presenting the result as literature-backed.
- For ChemCrow-style tools, validate input format before calling: most molecular tools need valid SMILES, and similarity tools may require exactly two SMILES separated by a period rather than commas or names.
- Use `Name2SMILES` before SMILES-only tools when starting from common names. Failed PubChem lookups for new structures should be treated as tool/database limits, not proof of nonexistence.
- Safety checks often fail on unregistered full structures. Check close analogs or substructures where possible, but clearly say this does not establish full-compound safety.
- Maintain the software-only boundary: no synthesis, procurement, vendor contact, or claims of physical execution.
