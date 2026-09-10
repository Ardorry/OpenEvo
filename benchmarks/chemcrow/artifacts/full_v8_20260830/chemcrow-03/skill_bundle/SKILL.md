---
name: alkene-hbr-regiochemistry
description: Use for organic chemistry prediction questions involving HBr addition to alkenes, especially comparisons with and without peroxide radical initiators.
---

# Alkene HBr Regiochemistry

## Trigger

Use this skill when a task asks for the product, mechanism, or comparison of hydrobromination of an alkene under:

- ordinary HBr conditions
- HBr plus peroxide or radical initiator
- paired conditions asking how peroxide changes regiochemistry

This skill is for reasoning and software-assisted structure checks only. Do not imply any physical experiment was performed.

## Workflow

1. Identify the alkene precisely.
   - Parse the name, SMILES, or drawing into the actual connectivity.
   - Check whether the double bond is terminal, internal, exocyclic, or substituted asymmetrically.
   - If using chemistry software, treat it as observational evidence and reconcile it with mechanism-based reasoning.

2. Predict ordinary HBr addition.
   - Use the ionic Markovnikov pathway.
   - Protonation occurs to form the more stable carbocation.
   - Bromide attacks the carbocation.
   - Prefer carbocation stability in this order when applicable: resonance-stabilized, tertiary, secondary, primary.
   - Consider rearrangements if a more stable carbocation can form under ionic conditions.

3. Predict HBr addition with peroxide.
   - Treat peroxide as a radical initiator only; it is not incorporated into the final product.
   - Apply the peroxide effect specifically to HBr, giving anti-Markovnikov addition.
   - Bromine adds first to form the more stable carbon radical.
   - The radical abstracts hydrogen from HBr to give the final alkyl bromide.
   - Do not extend this peroxide rule uncritically to HCl or HI.

4. Compare products.
   - State which carbon receives bromine in each pathway.
   - Explain the stability driver: carbocation stability for ordinary HBr, radical stability for peroxide conditions.
   - Note when the same product would result because the alkene is symmetrical.

5. Inspect stereochemistry.
   - Check whether addition creates one or more stereocenters.
   - For planar carbocations or radicals without stereocontrol, report mixtures such as racemic or diastereomeric mixtures when appropriate.
   - Avoid overclaiming a single stereoisomer unless the substrate or conditions enforce it.

## Helper Checks

When available, use cheminformatics helpers to:

- resolve names to structures or SMILES
- verify atom connectivity and alkene substitution
- generate candidate product connectivity
- inspect whether the product has newly formed stereocenters

Do not let software output replace mechanistic validation. The final answer should explain why the regiochemistry follows from the relevant intermediate.

## Final Validation

A complete answer should include:

- the ordinary HBr product connectivity
- the peroxide-condition product connectivity
- a concise mechanism for both pathways
- a direct comparison of bromine placement
- a note that peroxide acts as a radical initiator and is not part of the product
- stereochemical mixture guidance when a new stereocenter is formed
- no claim of real-world synthesis, procurement, or physical execution
