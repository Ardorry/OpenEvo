# Agent System Instructions

## Safety Boundary

This is software evaluation only. Do not perform or imply physical experiments, chemical procurement, vendor contact, or real-world execution. Treat chemistry tool outputs as observational software evidence, not experimental results.

## Chemistry Tool Methodology

- When a chemistry task gives named molecules or reagents, first normalize each relevant species with `Name2SMILES`; validate that the final answer includes either the normalized SMILES, the name, or both for every key product.

- When using a reaction predictor, submit valid molecular inputs in the format accepted by the tool. If a plain-language reaction or arrow-form reaction causes an invalid-SMILES error, convert all species to SMILES and retry with dot-separated reactants; validate by confirming the tool returns a successful product list before relying on it.

- When a predictor returns multiple candidates, identify the chemically relevant candidates by mechanism and atom placement rather than confidence score alone; validate by checking that the selected product matches the expected regioselectivity and reagent conditions.

- When known reaction conditions imply a named mechanism, explain the mechanism independently of the predictor output. If the predictor output does not directly list the mechanistically expected product, state that the final prediction is based on established chemical reasoning with tool output used only as supporting evidence.

- For alkene addition reactions, explicitly compare regiochemistry when conditions change. Validate by stating which alkene carbon receives each new atom or group in each condition.

- For HBr addition to alkenes, distinguish peroxide-free ionic Markovnikov addition from peroxide-enabled radical anti-Markovnikov addition. Validate by identifying the carbocation or radical stability argument that controls regioselectivity.

- When a reaction forms a new stereocenter or permits attack from either face, include a brief stereochemical note such as racemic mixture or stereoisomeric mixture unless the task specifies chiral control. Validate by checking whether the named/SMILES product has a newly stereogenic atom.

- Before finalizing, convert selected product SMILES back to names with `SMILES2Name` when names are requested or helpful. Validate that names and SMILES describe the same connectivity and that regioisomer labels are not swapped.
