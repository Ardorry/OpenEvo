---
name: compare-alkyne-hydrogenation-catalysts
description: Predict and compare products from alkyne hydrogenation with poisoned palladium versus ordinary palladium, including assumptions, selectivity, structure validation, and condition-dependent side reactions.
---

# Compare Alkyne Hydrogenation Catalysts

Use this skill when a chemistry question asks for products or mechanisms of an alkyne treated with:

- a poisoned palladium catalyst, such as Lindlar-type material;
- unpoisoned palladium;
- both catalyst systems for comparison.

Treat chemistry predictors and databases as observational software evidence only. Never imply that a physical reaction was performed.

## Workflow

1. Parse the complete reaction input.

   - Identify the alkyne, its substitution pattern, and other functional groups.
   - Determine whether hydrogen gas is explicit.
   - If only a hydrogenation catalyst is listed, state briefly that hydrogen is being assumed under the conventional catalytic-hydrogenation interpretation.
   - If that assumption is not justified by context, flag the reaction as under-specified instead of silently adding reagents.

2. Establish the expected selectivity.

   - Poisoned palladium generally reduces an alkyne to an alkene and suppresses further reduction.
   - For an internal alkyne, apply syn addition and report the corresponding alkene geometry when stereochemically meaningful.
   - A terminal alkyne produces a terminal alkene, so do not assign an inapplicable E/Z descriptor.
   - Unpoisoned palladium generally permits continued hydrogenation to the alkane.

3. Inspect competing reactivity.

   - Check whether reducible groups, protecting groups, or carbon–halogen bonds may react under the stated conditions.
   - Present the standard textbook product first when appropriate.
   - Qualify catalyst-, solvent-, pressure-, temperature-, and substrate-dependent hydrogenolysis or other side reactions rather than treating retention as unconditional.

4. Use software evidence carefully when available.

   - Resolve names to structures and confirm connectivity before prediction.
   - Run separate predictions for each catalyst condition.
   - Reject or clearly label outputs that omit the substrate, contain only catalyst fragments, violate atom balance, or otherwise fail basic chemical plausibility.
   - Do not let an anomalous prediction override a well-supported mechanistic conclusion without corroboration.
   - Keep retry details out of the final answer unless they materially affect confidence.

5. Report the result compactly.

   Prefer a small comparison table containing:

   - catalyst class;
   - predicted major product;
   - canonical or otherwise valid SMILES;
   - extent of reduction;
   - important selectivity or uncertainty.

   Follow with a short mechanistic explanation: poisoning lowers surface activity and promotes alkene desorption, whereas an active palladium surface can retain and further hydrogenate the intermediate.

## Verification

Before finalizing, confirm that:

- the carbon skeleton and substituent positions are preserved unless a specific side reaction is justified;
- the poisoned-catalyst product contains one fewer degree of unsaturation than the alkyne;
- the ordinary-palladium product reflects complete reduction of the targeted multiple bond;
- stereochemistry is included only when defined;
- every SMILES parses and matches the named structure;
- hydrogen assumptions and condition-dependent caveats are explicit but concise;
- software output is described as evidence, not as a physically executed experiment.
