# Safe Chemistry Procurement and Synthesis Responses

## Trigger

Use this skill when a user asks how to synthesize, manufacture, obtain reactants for, price, or procure chemicals for making a medicine, drug product, controlled precursor, hazardous reagent, explosive-adjacent material, or ingestible compound.

## Workflow

1. Treat chemistry tools as observational software evidence only. Do not imply any physical experiment, procurement, vendor contact, or lab execution occurred.

2. Establish identity before analysis:
   - Resolve the compound name to a structure identifier when available.
   - If a retrosynthesis tool requires a structure string, convert the name first instead of passing the plain name.
   - Optionally collect basic identifiers such as molecular weight or registry metadata when useful.

3. Check safety and regulatory context:
   - Run safety checks for the product and plausible reactants.
   - Run controlled-chemical checks for the product and precursors, but do not treat a negative exact match as proof that procurement is unrestricted.
   - Run explosive/reactivity checks for hazardous precursors, acylating agents, oxidizers, reducers, solvents, or energetic-adjacent materials.

4. For unsafe or regulated manufacture requests:
   - Refuse practical synthesis instructions, operational procedures, purification instructions, scale, stoichiometry, conditions, yield optimization, vendor lists, and shopping-cost totals.
   - Provide only non-operational educational chemistry: reaction class, broad transformation, and high-level precursor relationship if already supported by tool evidence.
   - Explain that no reliable cost estimate can be provided when pricing/vendor evidence is unavailable or when the procurement request itself is inappropriate.
   - Recommend regulated commercial sources for legitimate use of approved OTC or prescription medicines without naming specific vendors or prices.

5. Summarize hazards concretely:
   - Product risks, especially ingestion, overdose, impurity, dose-uniformity, and lack of pharmaceutical quality control.
   - Reactant hazards such as corrosivity, flammability, toxicity, environmental hazards, violent reactivity, vapor hazards, or decomposition products.
   - Appropriate non-procedural safety framing: supervised licensed lab, SDS review, fume hood, PPE, compatible storage, spill/fire planning, and hazardous-waste disposal.

6. Handle tool failures transparently:
   - State which evidence path failed and why.
   - Continue only with supported observations.
   - Do not fill missing live pricing, vendor, or regulatory facts from memory.

## Response Shape

Start with the safety boundary and refusal when procurement or practical manufacture is requested. Then give a concise supported educational summary, followed by safety issues and a safer alternative.

Avoid:
- Vendor names, links, prices, quantities, stepwise protocols, reaction conditions, purification details, or “shopping lists.”
- Claims that a chemical is legally unrestricted solely because a tool did not find an exact controlled match.
- Unsupported live-market or regulatory assertions.

## Verification

Before finalizing, confirm that the answer:
- Does not enable unlicensed manufacture or procurement.
- Contains no operational synthesis details.
- Contains no vendor list or total reactant cost.
- Clearly distinguishes tool observations from missing evidence.
- Includes relevant safety issues for both final compound and key precursors.
- Provides a benign alternative when the user’s real goal is obtaining a medicine for use.
