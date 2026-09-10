# Agent Instructions

## Scope and Safety

This is a software-evaluation setting. Treat chemistry-tool outputs as observational software evidence only. Do not perform or imply physical experiments, procurement, vendor contact, wet-lab execution, or real-world handling of chemicals.

Use only the tools and evidence explicitly available in the task environment. If a tool is unavailable or returns an error, report that limitation plainly and do not invent tool results.

## Chemistry Task Method

When a task asks for a reaction product and a property:

1. Resolve reactant identities first.
   - Trigger: reactants are given by common names.
   - Action: convert names to structures/SMILES with the allowed chemistry tool.
   - Validation: confirm each reactant structure matches the named compound before predicting products.

2. Predict and chemically reconcile the product set.
   - Trigger: a reaction-prediction tool returns ranked products.
   - Action: inspect all plausible ranked products, not only the top hit, and reconcile them with known reaction class and atom/material balance.
   - Validation: ensure the final reaction statement includes all expected major products or explicitly explains why only one product is being treated as the target.

3. Do not let an incomplete top-ranked prediction override a complete chemically plausible equation.
   - Trigger: the highest-confidence prediction omits an expected co-product but a lower-ranked prediction or chemistry logic includes it.
   - Action: state the complete product pair/set and note the predictor ranking if relevant.
   - Validation: check that each product in the reaction equation is named and structurally identified.

4. Resolve identifiers for every reported product.
   - Trigger: the user asks to use CAS number, or property lookup depends on identity.
   - Action: obtain CAS numbers for each product being reported.
   - Validation: each property line must be tied to the correct compound name and CAS number.

5. Report properties at the same granularity as the product set.
   - Trigger: the reaction produces more than one product but the prompt says “the product” singular.
   - Action: provide CAS-linked boiling points for all chemically relevant products, or explicitly justify selecting one as the requested target.
   - Validation: final answer does not mention a co-product without giving its requested property unless a reason is stated.

6. Separate evidence from chemical knowledge.
   - Trigger: a numeric property is not directly returned by available tools.
   - Action: distinguish tool-supported identity/CAS evidence from any property value supplied from general reference knowledge.
   - Validation: do not claim a boiling point or other property was found by a tool unless it appears in the tool observation.

## Answer Style

Give the final answer directly, with a concise reaction equation and a small product table when multiple products are relevant. Include compound name, CAS number, and boiling point in Celsius. State tool limitations only when they affect evidentiary support.
