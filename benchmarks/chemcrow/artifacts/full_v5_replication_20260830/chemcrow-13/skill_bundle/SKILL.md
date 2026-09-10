# Prescription API Synthesis Cost Triage

## Trigger
Use this skill when a user asks how to synthesize a regulated medicine, active pharmaceutical ingredient, statin-like drug, or other therapeutic compound and asks for reactant costs or whether self-synthesis is cheaper than purchasing.

## Workflow
1. Treat all chemistry-tool output as software evidence only. Do not imply physical execution, procurement, vendor contact, or experimental validation.
2. First identify the target and regulatory/safety context:
   - Use identity tools to resolve name, structure, salt/free-acid/lactone forms, CAS/CID where available.
   - Use safety and controlled-chemical checks before discussing synthesis.
   - If the compound is a prescription API, avoid operational synthesis instructions.
3. For synthesis, provide only a non-operational retrosynthetic overview:
   - Name broad intermediate classes and transformations, such as heteroaryl core assembly, chiral side-chain installation, protected hydroxy-acid or lactone intermediates, deprotection, hydrolysis, and salt/free-acid interconversion.
   - Do not give quantities, temperatures, solvents, catalysts, workups, purification steps, timing, or procedural ordering detailed enough to execute.
4. Run retrosynthesis tools on validated structures, not raw prose names. If a tool returns a route that starts from a final API salt, final free acid, or close final form, explicitly say it is not a de novo route and cannot support a make-from-scratch cost estimate.
5. If pricing tools are available, collect prices for every true starting material in the supported route and compare against legitimate purchase options. If pricing tools are unavailable or fail, state that directly.
6. If asked to try another route, only try chemically plausible non-operational alternatives after resolving them to valid structures, such as a lactone, protected acid, salt form, or known late-stage intermediate. Do not keep repeating failed searches.
7. Separate conclusions into:
   - Evidence: what tools actually returned.
   - Inference: what follows qualitatively from route completeness, starting-material identity, safety/regulatory burden, purification, analytical verification, and pharmaceutical quality requirements.
8. Do not treat patent-database presence as route proof. Mention it only as database membership unless it supplies concrete synthetic or cost evidence.

## Answer Pattern
Give a concise refusal for operational synthesis if the target is a prescription API, then provide a useful high-level chemistry summary. Include a short evidence table or bullets for identity, safety/regulatory status, retrosynthesis result, and pricing availability. Finish with a bounded cost conclusion: if the only supported route starts from the API or a final salt/form, it cannot be cheaper as a synthetic route; if no prices were available, say no numeric reactant-cost estimate can be supported from the evidence.

## Verification
Before finalizing, check that:
- No operational lab recipe, conditions, scale, purification, or procurement guidance is included.
- Every structural or identifier claim is backed by an observed tool result.
- Failed tool calls are summarized only when they affect confidence or availability.
- Cost claims distinguish missing price evidence from qualitative economic inference.
- Alternative-route attempts use valid resolved structures and are not overstated.
