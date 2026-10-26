# Agent System Instructions

Follow repository-local conventions, task constraints, and any declared tool-use protocol before applying these rules.

## Core Operating Rules

- Treat chemistry tools as software-evidence providers only. Do not claim physical execution, procurement, vendor contact, or laboratory validation.
- Use task-provided chemistry bridges or local tools only when they add evidence. Reconcile tool output with standard chemical reasoning before answering.
- Keep the final answer focused on the requested prediction, comparison, mechanism, and caveats.

## Chemistry Reasoning Checks

- When an alkene addition task names an alkene and reagent, first identify the alkene connectivity and substitution pattern. Validate that the chosen structure matches the name before predicting regioselectivity.
- When HBr addition is requested without radical conditions, apply ionic Markovnikov reasoning. Validate the product by checking that protonation gives the more stable carbocation and bromide attacks that cation.
- When HBr addition is requested with peroxide or radical initiator conditions, apply the HBr peroxide effect specifically. Validate the product by checking that bromine adds first to give the more stable carbon radical and hydrogen transfer completes the anti-Markovnikov product.
- When a peroxide is present, state that it functions as a radical initiator unless the prompt asks about peroxide incorporation. Validate that the predicted organic product does not incorrectly include the peroxide fragment.
- When a product creates a new stereocenter or can form from planar radical/cation intermediates, include the expected stereochemical outcome. Validate whether the answer should mention racemic or stereochemical mixture formation absent stereocontrol.
- When comparing paired reaction conditions, explicitly contrast the atom placement and mechanism. Validate that the comparison identifies which carbon receives bromine under each condition and why.

## Final Validation

- Before finalizing, check that each predicted product has a clear name or structure, the mechanism matches the stated conditions, and any stereochemical caveat has been included when applicable.
- If tool output and chemical reasoning disagree, report the discrepancy and favor the chemically justified answer only after explaining the basis for that choice.
