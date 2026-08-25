# ChemCrow × OpenEvo next-stage readiness audit

Audit date: 2026-08-26 (Asia/Shanghai)

## Verdict

```text
READY_FOR_FULL_RUN
```

The safe next scientific operation is a fresh 14-task
`chemcrow-task-local-full-v4-three-pipeline` run. Historical full-v3 remains provisional and may not
be extended into the authoritative result.

## Frozen authority

- Branch: `chemcrow-task-local-evolution-v1`.
- Task IDs: `01-10,12-15` (14 tasks; repository IDs remain authoritative).
- Task manifest SHA256:
  `8c69883d4a2424ed66f4dea0b9d0a0496129556e998902f278ad36a72d132604`.
- Full-v4 config SHA256:
  `6aebd03667c0b88adb7fe26ec635891eabcbaf925e9d2c4fd62cdc6bf8bbf152`.
- Live-preflight S0 SHA256:
  `8d3935a4c968d1988102ffdbb1adef8a28de80ace3bca58cd8543f0c6a92154f`.
- Runtime image:
  `sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`.

The live-v4 readiness config differs from authoritative full-v4 only in experiment ID, task subset,
and run/cache/ledger roots.

## Three-pipeline proof

Every task uses three independent Core-managed GPT-5.5 jobs:

1. Memory -> `text_memory`
2. Skill -> `skill_bundle`
3. Agent System -> `agent_system`

Prompts and evidence packages freeze before sibling output exists. Each job has a unique job ID,
prompt hash, lineage, artifact ID, registration, and expected injection binding. Byte-identical,
normalized-identical, trigram-Jaccard `0.90`, sequence `0.95`, responsibility, lineage, and baseline
answer-copy guards fail closed.

The fresh live-v4 proof on tasks `02`, `03`, and `06` has:

| Evidence | Count |
|---|---:|
| completed pairs | 3 |
| independent Reflector jobs | 9 |
| Memory / Skill / AgentSystem jobs | 3 / 3 / 3 |
| unique artifacts | 9 |
| sibling-isolation records | 9 |
| exact-three Core injection receipts | 3 |
| reset receipts | 3 |
| mock / fixture observations | 0 / 0 |

All three pairs passed the authoritative completed-run audit. Failed prior preflights were preserved
and not reused.

## Paper Evaluator proof

Request-delta testing proved the old 403s were caused by direct HKG egress when the shim set
`trust_env=False`. The exact same neutral body succeeded through the configured environment proxy
with SJC egress. The shim now validates the proxy contract, refuses credential-bearing proxy URLs,
and records only sanitized descriptors.

Fresh Core v10 passed HTTP 200 through the dedicated Paper harness/Rollout/Gateway/shim path with
model `openai/gpt-4`, provider `OpenAI`, temperature 0.1, fallback disabled, valid JSON, valid
`DualStudentAssessment`, and a usage receipt. No production claim or result exists.

## Legacy full-v3 audit

| Evidence | Count |
|---|---:|
| sealed pairs | 12 |
| memory artifacts | 12 |
| skill artifacts | 0 |
| agent-system artifacts | 0 |
| typed three-Reflector jobs | 0 |
| tasks satisfying the new protocol | 0 |

Classification: `LEGACY_PROVISIONAL`. Task 14 interruption evidence and `STOPPED_BY_USER.json`
remain untouched; task 15 remains unstarted.

## Tool and scoring boundary

All 14 tasks are mapped in `CHEMCROW_TOOL_READINESS_MATRIX.md`. The reduced public profile has
working RDKit, MolBloom, PubChem, Wikipedia, conversion, safety, similarity/weight/groups, local RXN
prediction, and local RXN retrosynthesis routes. Missing web/literature/price/proprietary tools are
declared deviations; they cannot be silently mocked.

Internal 0-4 diagnostic scores, post-hoc GPT-4 overall 0-10 grades, and human three-dimensional
0-10 scores remain distinct. Paper GPT-4 output is excluded from Reflector evidence.

## 42-call audit

`PAPER_COMPARISON_MATRIX.json` proves 14 tasks × 3 comparisons = 42 rows. It records call ID, task,
comparison, student systems, source identities/hashes, prompt hash or deferred template hash, and
paper-control/project-added status. The blueprint is READY. The 28 baseline/evolved hashes will be
materialized only from the future sealed full-v4 output set; that is an expected prerequisite for
production execution, not a readiness defect. The production ledger is empty.

## Gate summary

| Gate | Status | Evidence |
|---|---|---|
| A Repository | PASS at final handoff | correct branch, committed fixes, clean tree |
| B Candidate architecture | PASS | GPT-5.5, native Codex, Core-managed pinned runtime |
| C Three-artifact evolution | PASS | three live typed pipelines plus duplicate/isolation gates |
| D Task-local protocol | PASS | bare G1, exact-three G2, seal/reset/cross-task guards |
| E Live representative preflight | PASS | tasks 02/03/06, 3 pairs, 9 jobs/artifacts |
| F Tools | PASS reduced profile | 14-task matrix; mock=0, fixture=0 |
| G Internal scoring | PASS | same G1/G2 evaluator; feedback boundary enforced |
| H Paper Evaluator | PASS | Core v10 HTTP 200, OpenAI, valid assessment/usage receipt |
| I 42-call blueprint | PASS | 42 mappings; deferred full-v4 hashes expected; ledger clean |
| J Tests | PASS | 89 ChemCrow + 300 Core integration; Ruff/diff PASS |

Exact launch commands and authorization literals are maintained in `FULL_RUN_READINESS.md`.
