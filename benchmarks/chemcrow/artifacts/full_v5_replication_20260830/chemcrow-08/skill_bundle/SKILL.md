# ChemCrow Thiourea Synthesis Planning

## Trigger

Use this skill when a user asks for a paper/software synthesis or retrosynthesis plan for a named organocatalyst or thiourea-like small molecule, especially when the target is specified by IUPAC name and ChemCrow-style chemistry tools are available. Keep the response strictly planning-only: do not imply physical execution, procurement, vendor contact, or experimental completion.

## Workflow

1. Resolve the target name to a stereochemically specified structure with `Name2SMILES`. If downstream structure tools reject the plain name, use the resolved SMILES for all structure-sensitive calls.
2. Collect basic target evidence with `SMILES2Weight`, `FunctionalGroups`, and any available safety/control checks. Treat incomplete functional-group output as tool-limited, not chemically definitive.
3. Run `ReactionRetrosynthesis` on the target SMILES. For thiourea organocatalysts, look for the standard disconnection to an isothiocyanate electrophile plus a chiral amine or amino alcohol nucleophile.
4. Convert proposed precursor SMILES to names with `SMILES2Name`, and collect identifiers or molecular weights with `Mol2CAS` and `SMILES2Weight` when available.
5. Cross-check the proposed forward step with `ReactionPredict` using the precursor SMILES joined as reactants.
6. Run `SafetySummary`, `ControlChemCheck`, and `ExplosiveCheck` for the target and key precursors. Highlight hazards that affect planning, especially isothiocyanate corrosivity, inhalation/skin exposure, sensitization, and the need for ventilation and dry/inert handling where appropriate.
7. Write the final answer with clear sections: tool evidence, retrosynthetic logic, forward synthesis plan, safety notes, and analytical confirmation. Include representative but non-executed conditions such as dry DCM or THF, room-temperature addition of isothiocyanate to the free amine under anhydrous/inert-compatible conditions, monitoring until precursor consumption, and equimolar or slight stoichiometric adjustment while avoiding unnecessary excess of the more hazardous reagent.

## Verification Guidance

Before finalizing, verify that:

- The target structure includes the requested stereochemistry, and the route preserves it by sourcing chirality from the chiral amine/amino alcohol precursor.
- Retrosynthesis and forward prediction agree on the same core bond-forming event.
- Precursor names, CAS identifiers, and molecular weights are presented only when supported by tool observations.
- Safety language is tied to observed hazards and does not overstate regulatory conclusions beyond tool output.
- The plan includes characterization expectations at a high level, such as NMR evidence for thiourea NH signals, MS/HRMS consistency with the product formula, and optical rotation or chiral HPLC where stereochemical integrity matters.
- The response explicitly states that the route is a software-supported planning outline, not an executed experiment.
