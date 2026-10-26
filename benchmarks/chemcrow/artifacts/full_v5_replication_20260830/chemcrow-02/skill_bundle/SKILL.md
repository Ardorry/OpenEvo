# CO2 Organocatalyst Proposal

Use this skill when asked to propose, assess, or refine a novel organocatalyst for carbon dioxide conversion or carbon capture and utilization, especially metal-free catalysts for epoxide-to-cyclic-carbonate chemistry.

## Workflow

1. Define the target CCU chemistry before proposing a molecule. State whether the proposal targets cyclic carbonate formation, CO2 capture media compatibility, low-pressure CO2 use, or another conversion mode.

2. Propose one concrete catalyst, not only a motif. Include:
   - A clear, structure-derived name without ambiguous shorthand.
   - A valid SMILES string if chemistry tools are available.
   - The intended cooperative catalytic roles, such as nucleophilic halide, hydrogen-bond donor, cationic ion-pairing site, basic site, or CO2-philic group.

3. Keep evidence types separate:
   - Reaction prediction can support substrate/product feasibility only.
   - Structure checks can support identity, formula, functional groups, and database presence.
   - Similarity, patent, and registry non-hits are weak novelty signals, not proof of novelty.
   - Safety tool failures mean safety is unknown, not favorable.

4. Prefer catalyst concepts with a defensible CCU hypothesis:
   - Activity under mild temperature or dilute CO2.
   - Operation without metal salts.
   - Recyclability or immobilization potential.
   - Compatibility with wet or impurity-containing captured CO2 streams.
   - Reduced reliance on hazardous substructures when alternatives preserve the mechanism.

5. If using ChemCrow-style tools, prioritize:
   - `Name2SMILES` for known comparators.
   - `FunctionalGroups` and `SMILES2Weight` for structure sanity checks.
   - `ReactionPredict` on reactants only, not the catalyst, to confirm product class.
   - `PatentCheck`, `SMILES2Name`, and `Mol2CAS` for weak registry or patent-overlap signals.
   - `ControlChemCheck` or controlled-substance similarity checks for risk screening.
   - `SafetySummary` and `ExplosiveCheck` for the full compound and relevant substructures.

6. Handle tool errors explicitly. If web search, PubChem-backed lookup, safety summary, or registry tools fail, continue with available structure-level evidence and say what remains unverified.

## Verification Guidance

Before finalizing, check that the answer:
- Names a specific catalyst and gives its structure or designable scaffold.
- Explains why each structural feature should affect CO2 conversion.
- Does not claim measured activity, selectivity, safety, novelty, or patent freedom unless directly supported.
- Separates reaction feasibility from catalyst-performance claims.
- Includes plausible limitations, such as halide stability, viscosity or solubility of ionic catalysts, deactivation by water or strong hydrogen-bond acceptors, and incomplete safety data.
- Frames all chemistry-tool output as computational or database evidence only.

## Safety Boundary

This skill is for software-only chemical reasoning. Do not provide procurement steps, physical synthesis instructions, experimental execution, vendor outreach, or claims that any chemical operation was physically performed.
