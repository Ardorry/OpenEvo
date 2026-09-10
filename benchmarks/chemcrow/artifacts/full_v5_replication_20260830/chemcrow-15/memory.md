# Memory

- For ChemCrow synthesis-planning tasks, resolve names to SMILES before using tools that require structural input. If a retrosynthesis call fails on a name, rerun with the resolved SMILES instead of treating the route as unsupported.
- Cross-check a proposed disconnection with both retrosynthesis and forward reaction prediction when available. Report confidence values only as software evidence, not as experimental proof.
- Keep tool provenance clear: distinguish ChemCrow/PubChem-style observations from literature precedent, and explicitly state when web search or another evidence source is unavailable.
- A good synthesis plan should include more than the disconnection: add concise planning-level conditions such as solvent class, approximate stoichiometry, mild temperature/time expectations, monitoring, and isolation or purification strategy when chemically appropriate.
- Discuss chemoselectivity when multiple functional groups could seem reactive. For amine additions to heterocumulenes, note why the intended nucleophile reacts preferentially and whether other amines are tertiary, hindered, or otherwise nonparticipating.
- Include identity checks for target and precursors when tools support them: canonical names, SMILES, formula/weight, CAS/CID where available. This helps catch stereochemical or synonym ambiguity.
- Include safety and regulatory checks as a separate section. Cover irritation/corrosion/toxicity flags, explosive classification if checked, and controlled-chemical similarity/match status where relevant.
- Do not imply physical execution, procurement, vendor availability, or confirmed lab yields in benchmark chemistry tasks. Frame routes as software-supported planning proposals.
- If a key precursor is treated as commercially available or assumed in hand, either state that assumption or briefly mention that upstream synthesis of the precursor is outside scope.
