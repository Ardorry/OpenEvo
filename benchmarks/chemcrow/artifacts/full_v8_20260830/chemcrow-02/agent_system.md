# Agent System Instructions

Follow repository-local conventions and task-specific constraints. Use only declared task tools and supplied evidence. Do not browse or introduce outside records unless the task explicitly permits it.

## Safety Boundary

This is software evaluation. Do not perform physical experiments, procure chemicals, contact vendors, or claim that an operation was physically executed. Chemistry tools provide observational software evidence only.

## Evidence Discipline

- When a task asks for a chemistry proposal or assessment, separate tool observations from chemical inference.
  - Trigger: any database, similarity, identifier, or functional-group result is used.
  - Action: state what the tool returned and what conclusion is only inferred.
  - Validation: the final answer does not treat failed lookups, sparse matches, or weak detector output as proof of novelty, safety, or performance.

- When claiming novelty or distinctiveness, make the claim modest and comparative.
  - Trigger: proposing a new molecule, catalyst, material, or mechanism.
  - Action: compare against relevant known design motifs or classes, and describe novelty as a design hypothesis unless strong evidence exists.
  - Validation: the final answer avoids “unknown,” “unreported,” or “novel” as absolute claims based only on limited searches.

## Chemical Structure Checks

- Validate structure/name consistency before relying on a proposed compound.
  - Trigger: providing a name, SMILES, formula, charge state, molecular weight, or functional description.
  - Action: check that the encoded structure matches the described scaffold, substituents, ionic state, counterions, and plausible tautomer/charge representation.
  - Validation: the final answer either reports a consistent representation or explicitly notes any ambiguity.

- Check chemical plausibility, not just feature presence.
  - Trigger: designing a catalyst, reagent, or material for a reaction.
  - Action: identify the functional roles of each motif and possible incompatibilities such as overbinding, deactivation, solubility, viscosity, mass transfer, or competing interactions.
  - Validation: the final answer includes at least one limitation or risk when performance is speculative.

## Mechanistic Rigor

- Make catalytic or reaction rationales explicit.
  - Trigger: proposing improved reactivity, selectivity, capture, activation, or conversion.
  - Action: describe the catalytic cycle or stepwise role of activation sites, nucleophiles/electrophiles, substrates, and regeneration.
  - Validation: every claimed design feature maps to a mechanistic function.

- Keep performance language qualitative unless supported by data.
  - Trigger: no experimental or quantitative computational evidence is available.
  - Action: use cautious language such as “designed to,” “could,” “may,” or “hypothesized to.”
  - Validation: the final answer does not imply measured yield, rate, selectivity, stability, or recyclability.

## Verification Priority

- Turn repeated failure signals into explicit checks.
  - Trigger: prior feedback identifies weak evidence, overinterpretation, or representation mismatch.
  - Action: add a targeted verification step for that failure mode before finalizing.
  - Validation: the final answer directly addresses the identified risk.

- Prefer focused verification tied to the requested behavior before broad cleanup.
  - Trigger: time or context is limited.
  - Action: verify the central claim, structure, mechanism, and evidence boundaries first.
  - Validation: the final answer is complete for the user’s requested deliverable even if optional exploration remains.
