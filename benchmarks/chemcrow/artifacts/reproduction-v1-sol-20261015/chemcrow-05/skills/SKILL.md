---
name: chemistry-synthesis-planner
description: Use for requests combining a chemical synthesis route, reagent sourcing, cost estimation, and safety guidance. Produces an evidence-labeled, informational plan without claiming physical execution or procurement.
---

# Chemistry Synthesis Planner

## Trigger

Use when a user asks how to prepare a chemical and also requests one or more of:

- Reactant quantities or expected yield
- Suppliers, availability, or current pricing
- Total material cost
- Hazards, controls, waste handling, or regulatory considerations

Do not perform experiments, place orders, contact vendors, or imply that software predictions establish experimental outcomes.

## Workflow

1. **Assess scope and safety**
   - Identify the target, intended scale, jurisdiction, and stated purpose.
   - Check whether the target, precursors, or requested detail create controlled-substance, weaponization, poisoning, or other serious misuse concerns.
   - For ordinary educational chemistry, provide proportionate laboratory guidance. Restrict or redirect unsafe requests.

2. **Confirm chemical identity**
   - Resolve the target name, structure, formula, and molar mass using authoritative references.
   - Distinguish salts, solvates, hydrates, stereoisomers, grades, and similarly named compounds.
   - Record assumptions when the requested identity or scale is ambiguous.

3. **Evaluate the route**
   - Prefer a documented literature or authoritative preparative route.
   - Cross-check stoichiometry, limiting reagent, equivalents, theoretical yield, and mass balance.
   - Report an experimentally supported isolated-yield range when available; otherwise state that yield is unknown rather than inventing one.
   - Label retrosynthesis and reaction-prediction outputs as hypotheses. Do not present ranked alternative products as confirmed impurities without experimental evidence.

4. **Describe the process**
   - Give a coherent, scale-appropriate overview of charging, reaction, quench, isolation, purification, drying, and identity checks.
   - Include conditions only to the precision supported by the source.
   - Explain purification endpoints and likely material losses sufficiently to support cost and yield estimates.
   - State that laboratory-made material must not be consumed or used clinically.

5. **Build the procurement table**
   - List every consumed reagent and processing material needed for the stated scale.
   - For each item, include required quantity, practical package size, grade, supplier, price, currency, retrieval date, and source link.
   - Separate verified listings from plausible but unverified suppliers.
   - Do not fabricate prices, stock status, shipping eligibility, or vendor availability.
   - Note region restrictions, business-account requirements, hazardous-goods fees, taxes, and shipping separately when unknown or excluded.

6. **Calculate costs**
   - Report both:
     - **Upfront purchase cost:** complete packages that must be bought.
     - **Allocated material cost:** the fraction of each package consumed.
   - Show arithmetic and units.
   - Separate reactants from optional solvents, purification media, PPE, equipment, shipping, taxes, and waste fees.
   - If live prices cannot be verified, provide a fillable calculation and explicitly mark the total unavailable.

7. **Address hazards and compliance**
   - Retrieve current supplier SDS information for each material.
   - Tie hazards to specific controls: ventilation, splash protection, ignition control, incompatibilities, storage, spill response, and waste stream.
   - Avoid generic glove claims; direct the user to the SDS and institutional compatibility guidance for material and breakthrough time.
   - Distinguish “no database match found” from legal clearance. Advise checking current local precursor, transport, workplace, and waste rules.
   - Recommend trained supervision and an appropriately equipped laboratory.

8. **Communicate evidence quality**
   - Clearly label facts as literature-supported, vendor-verified, calculated, predicted, or assumed.
   - Mention conflicting sources and important uncertainty.
   - Prefer primary literature, official SDS documents, regulator guidance, and direct vendor listings.

## Helper Checks

When helper scripts are available, use them to:

- Recalculate molecular amounts, equivalents, limiting reagent, and theoretical mass.
- Validate unit conversions and currency arithmetic.
- Sum upfront and allocated costs independently.
- Flag missing supplier links, retrieval dates, package sizes, or SDS references.
- Detect unsupported certainty words attached to predicted reactions, impurities, yields, availability, or legality.

Helper output is advisory and must be checked against the cited evidence.

## Completion Checklist

Before responding, verify that:

- Target identity and scale are explicit.
- The route has evidence and predictions are labeled.
- Quantities, equivalents, theoretical yield, and expected isolated yield are included or explicitly unavailable.
- Every required material has sourcing status.
- Verified prices and a total are supplied, or the inability to verify them is plainly disclosed.
- Upfront cost is not confused with consumed-material cost.
- Safety controls are reagent-specific and linked to current SDS guidance.
- Regulatory language does not imply clearance.
- No purchase, experiment, or physical result is claimed.
- Citations support the route, hazards, availability, and prices at the level asserted.
