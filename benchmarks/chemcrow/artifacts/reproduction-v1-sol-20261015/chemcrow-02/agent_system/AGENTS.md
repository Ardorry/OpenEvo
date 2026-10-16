# Evolved Agent System

Follow repository-local conventions and task-specific constraints.

## Chemistry Proposal Method

- When proposing a catalyst, define the target reaction and substrate class before designing the structure; verify that the proposed activation modes address the reaction’s key elementary steps.
- Present every untested design as a candidate or hypothesis, not an enhanced catalyst; validate stronger performance language only with direct comparative evidence against relevant baselines.
- Provide an unambiguous structure using a machine-readable representation and a precise structural description; cross-check connectivity, counterions, molecular formula, charge balance, and molecular mass for mutual consistency.
- When the catalyst combines multiple functional roles, map each structural feature to its mechanistic role and analyze possible liabilities such as ion pairing, inaccessible geometry, competing interactions, or catalyst inhibition; verify that the proposed catalytic cycle remains chemically coherent under those constraints.
- When assessing novelty, search conceptually across exact structures, close analogues, scaffold combinations, and relevant patents or literature; treat database absence and low similarity as weak evidence, and report novelty as provisional unless broader evidence supports it.
- When claiming improved activity, selectivity, recyclability, operating conditions, or feed compatibility, require quantitative comparative calculations or experimental evidence; otherwise state the expected benefit, competing explanations, and the measurements needed to distinguish them.
- When captured or impure feed streams are relevant, identify likely interferents such as water, acids, bases, oxygen, or coordinating contaminants; validate compatibility claims only when their effects have been directly evaluated.
- Clearly separate structure checks, reaction predictions, literature analogies, and performance evidence; verify that no software output is described as proof of real catalytic efficacy.
- Include software-first validation criteria tied to the main hypotheses, such as conformational accessibility, ion-pair strength, substrate binding, transition-state barriers, selectivity, and comparison with established controls; ensure each claimed advantage has a corresponding test.

## Evidence and Editing Discipline

- Before editing, inspect repository-local instructions and relevant task artifacts; verify that the final work follows their formatting, scope, and validation requirements.
- When prior feedback identifies a recurring weakness, convert it into a pre-delivery check focused on the affected claim or artifact; confirm the check passes before broader cleanup.
- For package-like inputs, recursively inventory relevant files, including structured evidence formats and supplementary materials; validate conclusions against every source actually used.
- Prefer focused verification of changed behavior before broad validation; report unresolved uncertainty rather than filling evidentiary gaps with inference.

## Safety Boundary

- Treat all chemistry work as software-only evaluation. Do not perform or claim physical experiments, procure chemicals, contact vendors, or provide operational laboratory execution.
- Describe chemistry-tool results as observational computational evidence; verify that the final answer does not imply physical validation or real-world execution.
