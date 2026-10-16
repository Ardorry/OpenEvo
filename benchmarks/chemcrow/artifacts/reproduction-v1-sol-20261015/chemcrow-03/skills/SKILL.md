---
name: hbr-alkene-regiochemistry
description: Predict and compare alkene hydrohalogenation with HBr under ordinary and peroxide-initiated conditions, including regioselectivity, mechanisms, stereochemistry, and structure validation.
---

# HBr Addition to Alkenes

Use this skill when asked to predict or compare products from:

- An alkene treated with HBr.
- The same reaction with an organic peroxide or radical initiator.
- Markovnikov versus peroxide-effect regioselectivity.
- Ionic and radical hydrohalogenation mechanisms.

## Workflow

1. Normalize the substrate.

   - Identify both alkene carbons and all substituents attached to them.
   - Determine whether either alkene carbon belongs to a ring.
   - Convert ambiguous names or drawings into a proposed structure and SMILES.
   - State any interpretation that materially affects the result.

2. Interpret the conditions.

   - HBr alone normally implies electrophilic, ionic addition.
   - HBr plus a peroxide normally implies radical-chain addition.
   - If the peroxide identity is ambiguous, say what class of initiator is assumed.
   - Treat peroxide as an initiator unless the prompt explicitly indicates otherwise; do not incorporate it into the expected hydrohalogenation product.
   - Do not generalize the peroxide effect automatically to HCl or HI.

3. Predict ordinary HBr addition.

   - Protonate the alkene in the direction that gives the more stable carbocation.
   - Compare carbocation substitution, resonance stabilization, and possible rearrangements.
   - Add bromide to the carbocation center.
   - Report the major constitutional product and qualify minor products when relevant.

4. Predict peroxide-initiated HBr addition.

   - Initiation: describe peroxide homolysis and formation of a bromine radical through hydrogen abstraction from HBr.
   - Propagation: place bromine on the alkene carbon that leaves the more stable carbon radical on the other carbon.
   - Show explicitly whether the radical-bearing atom is a ring atom or an exocyclic atom.
   - Complete the chain by hydrogen abstraction from HBr, regenerating the bromine radical.
   - Mention termination only briefly unless requested.

5. Compare the products.

   - Identify which alkene carbon receives bromine in each pathway.
   - State whether the products are constitutional regioisomers.
   - Check whether either pathway forms stereogenic centers.
   - If attack occurs through planar carbocation or radical intermediates without asymmetric control, describe the expected stereochemical mixture accurately.

6. Validate structures.

   - Check atom count, valence, ring connectivity, and bromine placement.
   - Translate each proposed product independently between a structural description, systematic name, and SMILES.
   - If software predictors are available, use them only as observational support.
   - Distinguish tool roles precisely: naming tools validate names for proposed structures, while reaction predictors suggest candidates and may conflict.
   - Resolve conflicting predictions through mechanistic reasoning rather than claiming confirmation.

## Response Pattern

Present:

1. The interpreted substrate.
2. The major product under ordinary HBr conditions.
3. The major product under peroxide-initiated conditions.
4. A compact ionic mechanism.
5. A compact radical-chain mechanism.
6. A direct comparison of regioselectivity and stereochemistry.
7. Validated names and SMILES when useful.

## Verification

Before completion, confirm that:

- Both alkene carbons were tracked consistently.
- The ordinary pathway uses the most plausible carbocation, including rearrangement analysis.
- The radical pathway places the intermediate radical at the more stabilized carbon.
- Bromine attachment is opposite between the two pathways when the substrate supports a regioselective peroxide effect.
- Ring atoms and exocyclic atoms are labeled unambiguously.
- The peroxide is identified as an initiator rather than a product component.
- Any stereochemical claim follows from the actual product structure.
- Software evidence is described as suggestive or structural validation, not as physical experimental proof.

## Safety Boundary

Keep the work computational and explanatory. Do not perform physical experiments, procure chemicals, contact vendors, or claim that a reaction was physically executed. Treat chemistry-tool output solely as observational software evidence.
