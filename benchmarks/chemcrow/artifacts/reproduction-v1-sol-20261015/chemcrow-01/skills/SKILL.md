---
name: chemistry-synthesis-and-costing
description: Use when asked to propose a small-molecule synthesis and estimate purchasable reactant costs from software-accessible evidence.
---

# Chemistry Synthesis and Costing

Produce an evidence-calibrated synthesis proposal, complete material inventory, and reproducible cost estimate. This workflow is computational only: never perform experiments, place orders, contact vendors, or imply physical validation.

## Trigger

Use for requests that combine any of the following:

- Synthetic-route identification or design
- Reagent quantities for a stated target scale
- Supplier availability or purchasing costs
- Assessment of stereochemistry, salt form, reaction feasibility, or route uncertainty

## Workflow

1. Establish the target identity.

   - Confirm structure, molecular formula, molecular mass, stereochemistry, and relevant identifiers.
   - Distinguish free base, salt, hydrate, solvate, and racemate forms.
   - Record which product form and scale the calculations target.

2. Classify every claim by evidence strength.

   - `Documented`: supported by an inspected primary source containing the relevant transformation or procedure.
   - `Tool-supported`: suggested by reaction-prediction or chemistry software, but not experimentally validated.
   - `Assumed`: introduced solely for planning, such as yield, excess, concentration, or workup.
   - `Verified listing`: supported by a current supplier page or machine-readable catalog record.
   - Never describe a proposed route as literature- or patent-backed unless the inspected source actually establishes it.

3. Assess safety and scope before giving operational detail.

   - Identify hazardous reagents, solvents, transformations, pressure or temperature risks, and regulated materials.
   - Keep guidance suitable for qualified laboratory personnel using institutional controls.
   - If a route presents exceptional misuse or severe-hazard concerns, provide a high-level assessment instead of execution-ready instructions.

4. Evaluate route evidence.

   - Prefer experimentally documented routes from primary literature, patents, or authoritative regulatory chemistry records.
   - Capture substrate identity, conditions, isolated yield, selectivity, purification, and product characterization.
   - Treat reaction prediction as plausibility evidence only. It does not prove conditions, yield, chemoselectivity, purification, or stereochemical integrity.
   - If no validated route is found, label the result prominently as a model-proposed route.

5. Stress-test each transformation.

   - Check reagent compatibility, solvent stability, competing functional-group reactions, epimerization or racemization risk, overreaction, and likely by-products.
   - Explain why the selected reagent system is appropriate. Discuss credible competitive pathways rather than relying only on a predicted product.
   - Do not present exact conditions or purification as established when they were inferred.

6. Build a reconciled material balance.

   - Work backward from the requested isolated product amount.
   - Show molecular masses, equivalents, moles, assumed step yields, and resulting masses or volumes.
   - Include every consumed material named in the procedure: substrates, reagents, catalysts, acids or bases, quench materials, extraction and wash solutions, drying agents, chromatography eluents, and crystallization solvents.
   - Separate reaction-scale quantities from estimated workup and purification consumption.
   - Flag assumptions and avoid false precision.

7. Verify purchasability and cost.

   - For each material, record supplier, catalog identifier, package size, listed price, currency, access date, purity or grade, and region when available.
   - Distinguish a verified quotation from a search snippet, historical listing, marketplace result, or estimated price.
   - Compute the minimum package-purchase total separately from prorated material consumption.
   - State exclusions such as tax, shipping, hazardous-material fees, equipment, labor, and waste disposal.
   - If listings cannot be verified, report cost as unavailable or clearly provisional; do not invent ranges.

8. Define analysis and isolation requirements.

   - Tie purification suggestions to source evidence or label them as scouting recommendations.
   - Specify how identity, purity, product form, and yield would be assessed.
   - For stereogenic products, require a stereochemically discriminating method; mass spectrometry alone cannot establish enantiopurity.

## Helper Checks

If helper scripts are added later, they should:

- Validate formulas and molecular-mass arithmetic.
- Recalculate stoichiometry and yield propagation independently.
- Detect procedure materials missing from the purchasing table.
- Compare package totals with prorated-consumption totals.
- Flag unsupported words such as “validated,” “literature route,” or “commercially available.”
- Flag stereochemical products lacking an enantiopurity check.
- Flag vendor rows missing source date, package size, currency, or catalog identifier.

Helpers may check consistency only; they must not upgrade the evidentiary status of a route or supplier listing.

## Required Output

Present:

1. Target identity and selected product form
2. Evidence summary and route classification
3. Stepwise synthesis proposal with assumptions clearly marked
4. Competing-reaction and stereochemical analysis
5. Scale calculations and complete material inventory
6. Supplier table with source-backed availability
7. Minimum purchase total and prorated consumption estimate
8. Safety, purification, characterization, and uncertainty notes

## Completion Criteria

The task is complete only when:

- The target form and stereochemistry are unambiguous.
- Documented facts, software predictions, and assumptions are visibly separated.
- Every procedural material appears in the inventory or is explicitly excluded.
- All arithmetic is internally consistent and independently checked.
- Every claimed price and availability statement has inspectable supplier evidence.
- Package-purchase and prorated costs are not conflated.
- Reaction-prediction output is not presented as experimental validation.
- Important competitive pathways and reagent limitations are discussed.
- Purification and product-form claims have appropriate evidence.
- Identity, purity, and stereochemical verification methods are specified.
- The response states that no experiment or procurement was performed.
