This draft requests review of **OpenEvo Agent9 + GPT-6.1 Sol (high)** on TB2.1, using the two complete, preserved source jobs. It references **623 actual trials: all 89 tasks with seven attempts each**. Local recomputation gives **541/623 = 86.84%** before any official judge/disqualification review. Metrics in the submission JSON are left to the official CI.

Related protocol/maintainer-evaluation request: #255.

### Important review conditions

- We understand that community submissions are currently closed and only maintainer-run submissions are added. This is a draft review request, not a claim that this entry has been accepted.
- Nine fixed tasks receive same-task prior-run AGENTS.md generated from complete earlier trajectories and correctness/grading feedback, including failed test names. These are task-specific prior-run post-mortems; eligibility under the judge instructions is unresolved. No memory/skill artifact or old solver session is loaded; Doom has no evolved artifact.
- The earlier published **88.54% (394/445)** is a separate, post-completion selected-five research cohort. It is **not** the score claimed for the complete source jobs in this PR. No matching source trial is discarded here.
- Historical configs use a custom import_path, name=null and bare model_name=gpt-6.1-sol; actual trial identity is openevo-agent9-codex / openai/gpt-6.1-sol. The current official matching_agent check is expected to reject that config/source_filter mismatch. We have preserved the historical configs and request a maintainer-approved compatibility/evaluation path rather than editing them to pass.

### Execution and provenance

GPT-6.1 Sol high, Codex 0.166.0, Harbor 0.21.0, concurrency 4. Original task tests/default budgets/resources were used, with no automatic replacement of admitted trials. A clean, pinned official Git mirror supplied task transport; All 89 content digests match that Git snapshot's dataset.toml. A subsequent query of the official pinned Registry dataset matches 88 of 89 tasks; the sanitize-git-repo discrepancy is detailed below.

The source jobs preserve original trial UUIDs, results, errors, config and lock bytes. Native upload copies add only a job-level analysis.md explaining the method and provenance. Errored/ungraded trials remain in the denominator. No official judge has run for this submission.

[Frozen source, exact AGENTS artifacts, hashes and reproduction instructions](https://github.com/Ardorry/OpenEvo/tree/bae1b900e043685d5ebc3a93dc6edd2ee5c9ddfd/benchmarks/terminal_bench/releases/tb21-sol-high-agent9-20261109).

### Local validation

- 15 adapter tests passed; six execution-module SHA256 checks passed.
- All 89 Git-manifest content digests and clean pinned source checkout verified; live Registry agrees on 88/89 tasks (see below).
- All 623 original trials parsed by native Harbor upload loading; all 541 successful ATIF trajectories validated.
- 63 injection receipts matched the nine-task fixed artifact policy.
- All 8,118 original files in upload staging matched original SHA256 values.

Both complete source jobs were uploaded with native Harbor, verified UUID-for-UUID and reward-for-reward, and made public:

- https://hub.harborframework.com/jobs/862a2581-7850-46f9-b955-58aa3e8cc58f
- https://hub.harborframework.com/jobs/1f474dd2-013a-48a4-ab8e-1f90289ff85c

All 541 successful Hub trials have trajectory_path records. Source jobs preserve every trial. The official filter produced reasoning_effort=null because it could not match the custom import-path agent; the submission display/filter effort is explicitly set to the actual high setting from the unchanged original configs. The raw filter output is retained locally for audit.

 Local full-source metrics: recorded inference cost $128.157656, $0.2057105 per trial, 461,013,808 total input+output tokens. Three usage receipts are missing; costs exclude earlier evolution and sandbox costs.

### Actual official static-check results and task equivalence evidence

The unmodified official per-trial static checker was executed locally against both public Hub jobs: **8 checks passed, 2 failed**. No official judge or maintainer CI result is claimed.

1. `Valid source_filter`: the historical custom import-path agent/config naming mismatch described above. Please advise a supported normalization path for custom agents; the original configs remain unchanged.
2. `Valid task digests`: all seven `sanitize-git-repo` trial configs reference the official Git snapshot digest `sha256:73c94a21ebe370bae843adbeeaaa9e991374867b18483aaf56c7cd470dcddea7`, while Registry dataset `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a` expects `sha256:6e86297715fae62cd499fbdd27013e11a38d05d7e05b7f661cb50b4ecead128f`.

We downloaded that exact Registry task and compared every file against the clean Git snapshot. The only differences are a task-root `.gitignore` present only in Git, and five fake credential constants in `tests/test_outputs.py`: Git uses literal string concatenation, Registry uses equivalent single string literals. After folding only string-literal additions, the full Python ASTs are identical; the five constants have identical runtime values. All other task files are byte-identical. This is equivalence evidence for maintainers to assess, not a claim that the current digest check passes. Historical task refs, results, grading and logs have not been edited or replaced.

Given these compatibility issues and the unresolved eligibility of same-task prior-run AGENTS, this PR remains a draft for maintainer review; it is not an accepted leaderboard entry.

### First actual GitHub CI run

[Static Analysis run 43887991663](https://github.com/harbor-framework/terminal-bench-2-1/actions/runs/43887991663) completed with failure before producing its report: `harbor hub job show 862a2581-7850-46f9-b955-58aa3e8cc58f` exited 1, followed by the comment action failing on an empty report. The CI used Harbor 0.20.0. We downloaded the exact 0.20.0 wheel into an isolated audit directory and successfully ran the same CLI command with the owner's login (exit 0, valid JSON). An anonymous direct read of the public job header also succeeded. This narrows the issue to the CI access/environment path; we cannot determine its exact credential/network cause from the available log. Please verify the CI Harbor credential/access. Our full local official static-check report still has the two independently disclosed compatibility failures.
