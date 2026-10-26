---
name: chemistry-reaction-property-evidence
description: Use for chemistry questions that require predicting a reaction product, resolving its identity, and reporting a physical property with evidence such as a CAS-based lookup.
---

# Chemistry Reaction Property Evidence

Use this skill when the user asks for a reaction product and a property of that product, especially when the request mentions CAS numbers, boiling point, melting point, density, identifiers, or database-style lookup.

## Workflow

1. Parse the chemistry request into separate claims:
   - reactants and any stated conditions
   - requested product or class of product
   - requested property and units
   - required identifier path, such as CAS-based lookup

2. Inspect the reaction context before naming a single product:
   - distinguish main product from co-products
   - note whether the transformation requires catalysts, heat, equilibrium removal, solvent, or other conditions
   - if conditions are absent, state that the prediction is conditional rather than guaranteed by simple mixing

3. Use available chemistry tools as observational software evidence:
   - convert names to structures or identifiers when needed
   - run reaction prediction only if a reaction-prediction tool is actually available
   - resolve the predicted product to a stable identifier, preferably CAS when requested
   - look up the requested property through an identifier-based source when available

4. Keep unsupported chemistry knowledge separate:
   - do not present a manually reasoned product as tool-confirmed
   - do not claim a property was looked up if no lookup succeeded
   - if a value comes from general reference knowledge, label it as such and prefer a range or approximate value when appropriate

5. Produce a concise answer:
   - identify the likely product and any relevant co-product
   - give the CAS number or explain why it could not be verified
   - report the property with units and source status
   - include a short caveat about reaction conditions when relevant

## Helper Checks

Before finalizing, check:

- Did the answer distinguish predicted chemistry from retrieved evidence?
- Did every reported identifier and property have a stated evidence path?
- Did the response avoid implying a physical experiment was performed?
- Did it acknowledge reaction-condition dependence when the transformation is not spontaneous or complete under unspecified conditions?
- Are units included and converted if the user requested a specific unit system?

## Completion Criteria

The task is complete when the final response names the chemically plausible product, states any important co-product or condition caveat, provides the requested property in the requested units, and clearly marks whether the product identity, CAS number, and property value were tool-supported or inferred.

## Safety Boundary

This skill is for software-based chemical reasoning and lookup only. Do not perform or describe having performed physical experiments, procure chemicals, contact vendors, or provide operational synthesis instructions beyond the minimal reaction-context caveat needed to answer the user’s question.
