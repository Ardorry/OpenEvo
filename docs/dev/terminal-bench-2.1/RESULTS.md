# EvoLab results on Terminal-Bench 2.1

**EvoLab with GPT-6.1 Sol (high) passed 394 of 445 trials (88.54%) at a recorded inference cost of $89.79.** The evaluation covered all 89 Terminal-Bench 2.1 tasks with five trials per task. Sixty-eight tasks passed in all five rounds, and 84 tasks passed at least once.

## Five-round evaluation

The reported cohort consists of original rounds 3–7. In each round, we replaced the historical `sanitize-git-repo` record with the corresponding graded record for the canonical task version, in the original admission order. The other 440 trial outcomes and usage records are unchanged. [Trial tables](corrected-five/) · [Replacement ledger](canonical-replacement/attempts.json)

| Original round | Submission batch | Passed / trials | Pass rate | Inference cost |
| --- | --- | ---: | ---: | ---: |
| 3 | A | 81 / 89 | 91.01% | $18.15 |
| 4 | B | 79 / 89 | 88.76% | $18.68 |
| 5 | C | 80 / 89 | 89.89% | $16.98 |
| 6 | D | 79 / 89 | 88.76% | $18.43 |
| 7 | E | 75 / 89 | 84.27% | $17.56 |
| **All five rounds** | **A–E** | **394 / 445** | **88.54%** | **$89.79** |

The five rounds recorded 321,455,892 tokens: 13,026,825 uncached input, 305,106,176 cached input, and 3,322,891 output. Average recorded inference cost was about $0.20 per trial across all 445 trials. The total uses unrounded trial costs. [Metrics and usage](corrected-five/metrics.json) · [Public Harbor Hub job](https://hub.harborframework.com/jobs/e4ab0103-3b2c-5b16-b093-5740765c5255)

## Learning from failed attempts

EvoLab's Reflector uses a complete prior execution trajectory, the task description, and grading feedback to produce revised instructions for a subsequent attempt. In this evaluation, nine tasks received a frozen task-specific `AGENTS.md` derived from prior attempts on those same tasks. The other 80 tasks ran without an evolved artifact. The underlying model remained fixed. [Frozen instructions and provenance](artifacts/manifest.json)

In a separate comparison on ten previously unsolved tasks, we ran each task three times under each setting. The frozen `AGENTS.md` setting achieved the highest pass rate at **21/30 (70.0%)**, compared with **14/30 (46.7%)** without an artifact. That is a **23.3 percentage point** gain on this set. Skill-only and memory-only settings passed 18/30 and 17/30, respectively. Both `configure-git-webserver` and `mteb-retrieve` passed 3/3 with `AGENTS.md` and 0/3 without an artifact. This comparison shows that feedback from earlier attempts can translate into repeatable corrections on specific tasks while keeping the base model fixed.

## Terminal-Bench 2.1 leaderboard comparison

The reference rows are the first three entries on the Terminal-Bench 2.1 leaderboard as viewed on November 21, 2026. Their costs are shown as displayed on the leaderboard. [Terminal-Bench 2.1 leaderboard](https://www.tbench.ai/?version=2.1)

| Model (reasoning effort) | Agent | Resolution rate | Reported cost (USD) |
| --- | --- | ---: | ---: |
| GPT-6.1 Sol (high) | EvoLab | 88.54% | $89.79 |
| GPT-6 Astra (high) | Codex | 87.4% | $0.8k |
| Fable 5 (xhigh) | Claude Code | 83.8% | $0.6k |
| GPT-5.5 (xhigh) | Codex | 83.2% | $2.1k |

## Why seven rounds were recorded

The first campaign ran five rounds. We added two full repetitions under the same settings to check the results after the early rounds had more incomplete verifications. The archive retains all seven original rounds. The reported five-round result uses rounds 3–7 with the canonical replacements described above. [Original seven-round archive](original-seven/)
