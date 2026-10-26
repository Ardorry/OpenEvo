---
name: ccus-organocatalyst-proposer
description: Use when asked to propose or assess a novel organocatalyst for CO2 conversion, carbon capture and utilization, epoxide/CO2 coupling, cyclic carbonate synthesis, or related CCU chemistry.
---

# CCUS Organocatalyst Proposer

## Trigger

Use this skill when the task asks for a chemically plausible organocatalyst design for carbon dioxide conversion or carbon capture and utilization. Typical requests include proposing a new catalyst, explaining a catalyst rationale, comparing against known bifunctional organocatalysts, or giving a design-level mechanism for CO2-to-value-added-product chemistry.

Do not use this skill to plan physical experiments, procure chemicals, provide operational synthesis instructions, or claim laboratory execution.

## Workflow

1. **Define the target transformation**
   - Identify the CO2 conversion reaction, such as epoxide/CO2 coupling to cyclic carbonates, CO2 fixation into carbamates, or another CCU pathway.
   - State the expected catalytic roles needed for that transformation, such as nucleophilic activation, hydrogen-bond activation, Lewis/base activation, ionic-liquid behavior, or cooperative substrate binding.

2. **Inspect known catalyst motifs**
   - Compare the proposed design against common organocatalyst families: ammonium/phosphonium halides, imidazolium salts, ureas, thioureas, squaramides, amidinium/guanidinium systems, ionic liquids, betaines, and bifunctional H-bond donor/halide catalysts.
   - Treat database or literature absence as weak evidence only. It may support a cautious novelty statement, but it does not prove novelty.

3. **Design conservatively**
   - Propose one concrete molecular design with a clear name or descriptive identifier.
   - Include a structure representation when appropriate, preferably SMILES or another unambiguous text format.
   - Check that the design has chemically reasonable charge balance, valence, counterions, and functional-group compatibility.
   - Avoid overcomplicated scaffolds unless each added feature has a mechanistic role.

4. **Explain the catalytic rationale**
   - Map each structural element to its role in the catalytic cycle.
   - For epoxide/CO2 coupling, explicitly cover substrate activation, ring opening, CO2 insertion, ring closure, and catalyst regeneration at a design level.
   - Discuss why the organocatalyst could improve activity or selectivity relative to simpler analogues.

5. **State limitations**
   - Include plausible drawbacks: overly tight ion pairing, viscosity or mass-transfer limits, reduced nucleophile availability, catalyst deactivation, moisture sensitivity, competing substrate binding, steric inhibition, or recyclability concerns.
   - Avoid claiming performance, yield, selectivity, or novelty as established unless supported by actual validated evidence.

## Helper Checks

If chemistry software or database helpers are available, use them only as observational software evidence:

- Validate structure parseability and molecular formula where possible.
- Check molecular weight, formal charge, counterion balance, and obvious valence problems.
- Search by name, structure, or similarity in available chemical databases.
- Compare substructures against known catalyst motifs.
- Record failed lookups as “no match found in the checked source,” not as proof of novelty.
- If functional-group detection is shallow or incomplete, say so and do not overinterpret it.

## Final Response Requirements

A complete answer should include:

- Proposed catalyst name or identifier.
- Unambiguous structure representation when feasible.
- Target CO2 conversion reaction.
- Mechanistic rationale tied to specific structural features.
- Novelty claim phrased modestly and bounded by the checks actually performed.
- Comparison to known organocatalyst motifs.
- Limitations and next validation steps.
- Clear statement that the proposal is a computational/design hypothesis, not experimentally validated.

## Verification

The task is complete when the response gives a concrete, chemically coherent organocatalyst proposal and the reasoning does not overclaim. Final validation should confirm:

- The structure and name are internally consistent enough for a design proposal.
- Charge balance and counterions are plausible.
- The catalytic cycle is explicitly described.
- Evidence from tools or databases is framed as limited observational evidence.
- Safety boundaries are respected: no physical execution, procurement, or guaranteed experimental claims.
