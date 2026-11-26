# EvoLab on Terminal-Bench 2.1

This archive records the November 2026 GPT-6.1 Sol (high) evaluation with EvoLab, Codex 0.166.0, and Harbor 0.21.0. It preserves three evidence sets with distinct denominators.

| Evidence set | Scope | Passes | Recorded inference cost |
| --- | --- | ---: | ---: |
| [Original seven rounds](original-seven/) | All original attempts on 89 tasks, including rounds 1 and 2 | 541/623 (86.84%) | $128.16 |
| [Canonical replacement attempts](canonical-replacement/attempts.json) | Seven solver executions of canonical `sanitize-git-repo` and their recovered grades | 7/7 | No additional model calls for regrading |
| [Corrected five-round submission](corrected-five/) | Original rounds 3–7 (A–E) with one canonical replacement in each round | 394/445 (88.54%) | $89.79 |

The seven original round pass counts are **76, 71, 81, 79, 80, 79, 75** (each out of 89). Attempts are ordered per task by execution start within the original five-round job and the additional two-round job. [Round summary](round-summary.csv) records pass counts, ungraded outcomes, and usage. Blank rewards indicate ungraded trials, which stay in the accuracy denominator. Input-token counts include cached input tokens. The recorded cost totals sum the available trial receipts.

The historical `sanitize-git-repo` records used a noncanonical task ref. Seven new solver executions used the canonical ref. Their admission order was fixed by the original lock-file timestamps before grading recovery. Attempts 3–7 replace the same task in A–E, and the other 440 rows preserve their original outcomes and usage. The replacement corrected task identity and evidence while the five-round pass count stayed at 394. Attempts 1 and 2 remain in the replacement ledger. [Recovery methods and provenance](canonical-replacement/PROVENANCE.md)

## Artifacts and evidence

- [Frozen agent instructions](artifacts/) contains the nine `AGENTS.md` files used in the run and their SHA-256 manifest. Each came from an earlier complete trajectory for the same task, task information, and grading feedback, including failed-test names. The other 80 tasks received no evolved artifact.
- The original and corrected trial tables include task refs, rewards, usage, timestamps, trial identifiers and result hashes. The corrected table identifies the historical trial and actual grading source separately. The original table also includes ATIF hashes where a file was present.
- Harbor Hub hosts the published logs and trajectories. The trial IDs in the tables identify the corresponding records. [Rounds 1–5 on Hub](https://hub.harborframework.com/jobs/862a2581-7850-46f9-b955-58aa3e8cc58f) · [Rounds 6–7 on Hub](https://hub.harborframework.com/jobs/1f474dd2-013a-48a4-ab8e-1f90289ff85c) · [Corrected A–E on Hub](https://hub.harborframework.com/jobs/e4ab0103-3b2c-5b16-b093-5740765c5255)
- The seven-attempt ledger includes verifier receipts, original and graded trial IDs, and trajectory hashes. Full raw files for the two canonical attempts outside A–E remain in the private run archive. The Git branch carries results, provenance, and the frozen instructions. Hub carries the published trial archives.
- The executable harness and the documentation branch have separate commit pins. [Executed harness and frozen inputs](https://github.com/Ardorry/OpenEvo/commit/bae1b900e043685d5ebc3a93dc6edd2ee5c9ddfd) · [Canonical admission package](https://github.com/Ardorry/OpenEvo/commit/015569bda1478d385fd08a70d2c33d928e1c12a3)

## Submission materials

The [submission JSON](submission/submission.json) identifies the corrected five-round Hub job. It uses EvoLab as the display name and the original Harbor agent ID for trial matching. Its metrics field is left empty for official recomputation. The [local static report](submission/local-static-report.md) records 10/10 with the unmodified checker at commit `12b7565679ce5006230b820666353673a133db93` and Harbor 0.20.0. The November 18 official rerun stopped at the initial Hub read. Protocol eligibility and any leaderboard result are pending maintainer review. [Evaluation request #255](https://github.com/harbor-framework/terminal-bench-2-1/issues/255) · [Submission PR #256](https://github.com/harbor-framework/terminal-bench-2-1/pull/256) · [Official rerun log](https://github.com/harbor-framework/terminal-bench-2-1/actions/runs/44088415544/job/134485566766)
