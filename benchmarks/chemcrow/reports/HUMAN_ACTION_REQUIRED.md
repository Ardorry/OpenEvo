# ChemCrow human actions after readiness

Updated: 2026-08-26 (Asia/Shanghai)

No OpenRouter Dashboard change is required. The first-party GPT-4 Core route and three-artifact live
preflight both pass.

## Before starting a full-volume phase

The operator must deliberately set the authorization for the phase being started. Readiness work did
not set or consume either production authorization:

```text
CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
CHEMCROW_PAPER_EVALUATOR_MAX_USD=11.83266
```

Use the exact commands in `FULL_RUN_READINESS.md`. The authoritative benchmark must be a fresh
14-task `full-v4-three-pipeline`; do not combine historical full-v3 with task 14/15 repairs.

## Scientific labeling decisions

- Report the benchmark as `public-source-reduced-local-rxn-no-serp-v1`, not paper-identical.
- Preserve declared gaps for price/procurement, literature, configured web search, and proprietary
  paper-only tools.
- Record institutional approval for RXN-Sandbox and MolBloom/SureChEMBL licensing if formal metrics
  require it.
- Preserve the safety/refusal interpretation for task 12; never relabel a refusal as physical task
  execution.

## Human expert layer

Four independent chemistry experts are required for formal paper-style comparison. Each expert must
review all 42 blinded comparisons and score both answers from 0 to 10 on:

- chemical correctness;
- reasoning quality;
- task completion.

That is 168 completed forms. Reviewers must not receive the private A/B mapping. A smaller panel is
exploratory and must be labeled accordingly.

## Credential handling

The ignored `/home/lhy-h/work/chemcrowrun/.env.paper-evaluator` file remains the only Paper Evaluator
credential source and must remain mode 0600. Never copy its values into reports, logs, commits, or
chat. Candidate/Reflector subscription credentials likewise remain outside the repository.
