---
name: ccu-organocatalyst-candidate
description: Use when proposing or evaluating an organocatalyst candidate for a specified carbon-dioxide conversion reaction. Produces a concrete, chemically plausible structure, software-only validation, calibrated novelty and performance claims, and clear next-step criteria.
---

# CCU Organocatalyst Candidate

## Scope

Use this skill when asked to design, compare, or assess an organocatalyst for carbon capture and utilization.

Treat every new structure as a **candidate**, not an improved catalyst, until comparative evidence supports activity, selectivity, stability, and compatibility claims.

Do not perform physical experiments, procure chemicals, contact vendors, or imply that laboratory work occurred. Chemistry software and database results are observational evidence only.

## Workflow

1. Define the target transformation before designing the catalyst:
   - substrates and desired product;
   - proposed catalytic role;
   - relevant operating constraints;
   - likely impurities in the intended captured-CO₂ stream.

2. State design requirements:
   - complementary activation modes;
   - accessible reactive geometry;
   - suitable ion mobility and polarity;
   - resistance to water, oxygen, acids, bases, and common capture-media components;
   - plausible recovery or reuse characteristics.

3. Propose one primary candidate:
   - provide a structure string when possible;
   - give an unambiguous structural description;
   - distinguish pendant groups, linkers, counterions, and catalytic sites;
   - avoid names that imply a substituent is directly attached when it is connected through a tether.

4. Explain the catalytic cycle step by step:
   - catalyst–substrate association;
   - bond activation or nucleophilic step;
   - CO₂ incorporation;
   - ring closure, elimination, or product-forming event;
   - catalyst regeneration.

5. Challenge the design:
   - ion pairing that suppresses nucleophilicity;
   - intramolecular binding that blocks active sites;
   - poor conformational alignment;
   - excessive rigidity or flexibility;
   - catalyst decomposition or product inhibition;
   - mass-transfer and solubility limitations;
   - interference from realistic capture-stream contaminants.

6. Compare against meaningful controls:
   - each catalytic component alone;
   - an untethered mixture of the same functional components;
   - a closely related established catalyst;
   - variants in linker length, counterion, or activating group.

## Software Checks

Run available cheminformatics helpers to:

- parse and canonicalize the structure;
- detect invalid valence, charge, aromaticity, and disconnected-fragment errors;
- calculate formula, formal charge, and molecular mass;
- confirm agreement among the structure string, written description, formula, and mass;
- identify reactive, unstable, or toxicity-relevant structural alerts;
- search exact structures, salts, stereoisomers, close analogues, and substructures in accessible literature, patent, and chemical databases.

If helper scripts are added to the bundle, they should accept a structure string and return parse status, canonical structure, formula, mass, charge, structural alerts, and similarity-search-ready fingerprints.

Database absence is not proof of novelty. Low similarity to a small reference set is weak evidence only. Report which search modes and sources were checked.

## Comparative Computation

When computational tools are available, prioritize quantitative comparisons of:

- substrate-binding geometry and energy;
- counterion separation and nucleophile accessibility;
- key transition-state or activation-energy proxies;
- conformational populations;
- solvent sensitivity;
- selectivity-relevant competing pathways.

Use identical methods across the candidate and controls. Do not treat reaction-product prediction as evidence of catalytic rate or superiority.

## Response Requirements

The final response should include:

- the exact target reaction;
- the candidate structure and precise structural description;
- design rationale and catalytic cycle;
- structure-consistency results;
- novelty-search scope and limitations;
- competing hypotheses and likely failure modes;
- safety and compatibility flags;
- comparative calculations or a software-first validation plan;
- explicit labels separating facts, computational observations, and hypotheses.

## Completion Criteria

The task is complete only when:

- the proposed structure is internally consistent and machine-parseable;
- the mechanism accounts for catalyst regeneration;
- claims of novelty and performance are calibrated to the evidence;
- ion pairing, geometry, controls, and capture-stream compatibility are addressed;
- quantitative comparisons are supplied when available, or clearly identified as required validation;
- no physical execution or procurement is claimed.
