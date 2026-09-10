# ChemCrow Synthesis Planning Skill

Use this skill when a task asks for a software-only synthesis plan for a named small molecule, especially organocatalysts, thioureas, chiral amines, or targets where ChemCrow-style tools are the declared evidence source.

## Safety Boundary

Treat all chemistry-tool output as observational software evidence. Do not claim that any reaction was physically performed. Do not procure chemicals, contact vendors, optimize for clandestine acquisition, or present a bench-ready protocol. Keep conditions at planning level unless the user explicitly asks for a permitted deeper literature discussion.

## Workflow

1. Establish the target identity.
   - If the user provides a name or IUPAC string, first resolve it with `Name2SMILES`.
   - Use the resolved SMILES for structure-dependent tools. Do not assume retrosynthesis tools accept raw names.
   - Check molecular weight and, when useful, registry identifiers with available structure tools.

2. Run retrosynthesis on the resolved structure.
   - Prefer the highest-confidence disconnection, but inspect whether it is chemically plausible.
   - For thiourea organocatalysts, look for a direct disconnection into a chiral amino-amine fragment and an aryl isothiocyanate fragment.
   - If a tool call fails because the input format is invalid, correct the input format and rerun rather than abandoning the route.

3. Verify the proposed coupling.
   - Run forward prediction on the proposed reactants.
   - Confirm that the target is the top or high-confidence product and that stereochemical labels are consistent with the requested enantiomer or diastereomer.
   - Explain chemoselectivity in words: for amino-tertiary-amine substrates, the free primary or secondary amine is the reactive nucleophile, while the tertiary amine mainly remains a basic/non-acylating site under the planned coupling.

4. Add practical planning context without over-specifying execution.
   - State the reaction class and bond formed.
   - Include planning-level conditions such as: near-stoichiometric amine/isothiocyanate pairing, inert or dry organic solvent class, mild temperature range, and standard monitoring/isolation concepts.
   - Mention expected purification at a high level, such as chromatography, trituration, crystallization, or salt/base workup when appropriate.
   - If a key precursor is specialized, state whether its preparation is outside scope or give only a high-level upstream route.

5. Check safety and control flags.
   - Use available safety, explosive, and controlled-chemical similarity tools for the target and important precursors.
   - Summarize hazards by category and signal word when available.
   - Report unavailable tools explicitly, including the missing capability and how that limits confidence.

## Response Shape

Return a concise synthesis-planning answer with these sections when applicable:

- Route
- Rationale and tool support
- Planning-level conditions
- Chemoselectivity/stereochemistry
- Identifiers
- Safety and regulatory observations
- Limitations

## Verification Checklist

Before finalizing, confirm that:

- The target name was resolved to a valid structure before structure-based prediction.
- Retrosynthesis and forward prediction support the same main route.
- The answer distinguishes tool evidence from literature precedent.
- No unavailable web or literature tool is implied as successfully used.
- Safety/control observations are included for both target and major precursors.
- The plan includes enough practical context to be useful, but does not claim physical execution or provide procurement guidance.
