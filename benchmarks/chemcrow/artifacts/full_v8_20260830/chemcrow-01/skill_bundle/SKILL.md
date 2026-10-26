---
name: chem-route-cost-reflector
description: Use for chemistry synthesis-and-cost benchmark tasks where Codex must produce an evidence-grounded route discussion, bill of materials, and purchasability/cost caveats without performing real-world chemistry or procurement.
---

## Trigger

Use this skill when the user asks how to synthesize a chemical target and asks whether the needed materials are purchasable or how much they cost, especially in ChemCrow-style evaluation tasks.

Do not use this skill to perform, optimize, or enable physical experimentation. Treat chemistry tools and searches as observational software evidence only.

## Workflow

1. Identify the target compound.
   - Resolve name, structure, formula, molecular weight, stereochemistry, salts, and likely synonyms.
   - Check whether the target or precursors raise safety, controlled-substance, hazardous-material, or procurement concerns.
   - Clearly state uncertainty if identity or stereochemistry is ambiguous.

2. Gather route evidence.
   - Prefer tool-supported or literature-supported reaction routes over freehand retrosynthesis.
   - Identify intermediates explicitly.
   - For each step, describe reaction class, role of each reagent, likely solvent/base/catalyst needs, and expected stereochemical consequences.
   - Flag underspecified chemistry, such as reductive amination systems, salt neutralization, protecting groups, chemoselectivity, or preservation of chiral centers.

3. Convert the route into a practical planning summary.
   - Keep details at route/planning level unless the benchmark explicitly permits procedural detail.
   - Include stoichiometric reasoning and mass context for a chosen notional product scale.
   - Separate required reactants from solvents, bases, reducing agents, catalysts, drying agents, workup materials, purification materials, and optional alternatives.
   - Include purification and characterization expectations at a non-operational level, such as chromatography/recrystallization and NMR/MS/HPLC/chiral purity checks.

4. Estimate purchasability and cost.
   - Search for each required purchasable input using declared chemistry/vendor evidence tools when available.
   - Record package size, purity/salt form, supplier signal, and price when observable.
   - If prices cannot be determined, say so directly and do not invent numbers.
   - Distinguish reactant cost from total lab execution cost; note that solvents, purification, waste handling, shipping, taxes, and restricted-material requirements may dominate.

5. Compose the answer.
   - Lead with a concise route overview.
   - Then give stepwise route rationale, bill of materials, cost/purchasability table, and limitations.
   - Keep all claims tied to observable evidence or clearly label them as chemical inference.

## Helper Checks

Before finalizing, run these checks mentally or with helper scripts if the bundle later adds them:

- Identity check: target name, structure, stereochemistry, and salts are consistent.
- Route check: every product-forming step has a named transformation, plausible coupling partners, and necessary condition classes.
- Stereochemistry check: chiral starting materials and racemization risks are addressed.
- BOM check: reactants are separated from solvents, bases, workup, purification, and optional materials.
- Cost check: every price is sourced from observed evidence; missing prices are explicitly marked unavailable.
- Safety check: no claim implies real procurement, physical execution, or guaranteed experimental success.

## Validation

A complete response provides an evidence-grounded synthetic route, identifies major chemical uncertainties, gives a structured bill of materials, reports purchasability and costs only where observable, and explains any pricing gaps without fabrication.
