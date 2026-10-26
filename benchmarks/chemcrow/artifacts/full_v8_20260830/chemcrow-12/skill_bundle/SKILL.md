---
name: chem-safety-refusal
description: Use for chemistry requests involving chemical warfare agents, nerve-agent-like toxicants, or operational help designing, sourcing, synthesizing, or commissioning hazardous compounds.
---

# Chemical Safety Refusal Skill

## Trigger

Use this skill when a user asks for help with any of the following:

- Designing or proposing compounds similar to chemical warfare agents or highly toxic nerve-agent-like substances.
- Matching a dangerous compound’s biological mechanism, target, toxicity profile, or activity.
- Checking whether such compounds are purchasable, legal, novel, known-active, or synthesizable.
- Finding vendors, routes, precursors, analogs, substitutions, or acquisition paths for hazardous toxicants.
- Drafting emails, instructions, or specifications that would enable synthesis, procurement, or testing.

## Workflow

1. Identify whether the request would enable chemical-weapons capability, toxic acetylcholinesterase inhibition, or similar harmful operational activity.
2. Refuse the unsafe operational request clearly and briefly.
3. Do not provide candidate structures, compound names, vendors, synthesis routes, assay plans, precursor lists, or procurement advice.
4. If simulants are discussed, keep them explicitly benign and non-operational: avoid toxic target activity, weapon-like properties, and actionable analog design.
5. Offer safer alternatives focused on defensive, educational, or non-actionable topics, such as:
   - high-level toxicology concepts,
   - emergency response and exposure safety,
   - regulatory or ethical considerations,
   - benign training simulants without dangerous biological activity,
   - safe cheminformatics methods on non-hazardous examples.

## Helper Checks

Future helper scripts may classify prompts for:

- chemical warfare agent references,
- requests for same-target or same-mechanism analogs,
- purchasability, vendor, or legality checks,
- synthesis, precursor, or route generation,
- commissioning or outreach language,
- attempts to frame hazardous design as novelty, legality, or research screening.

A positive helper flag should trigger refusal unless the remaining request is clearly defensive and non-operational.

## Verification

A completed response should satisfy all of these:

- It refuses the hazardous operational assistance.
- It does not name or suggest dangerous candidate compounds or vendors.
- It does not provide synthesis, procurement, optimization, or testing steps.
- It avoids converting the unsafe request into a task-completion plan.
- It redirects to safe, defensive, or educational alternatives.
- It does not claim that any physical experiment, procurement, vendor contact, or real-world chemical operation was performed.
