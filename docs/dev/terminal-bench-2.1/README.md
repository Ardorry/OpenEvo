# OpenEvo on Terminal-Bench 2.1

OpenEvo with GPT-6.1 Sol (high) passed **394 of 445 trials (88.54%)** across 89 tasks, at a recorded inference cost of **$89.79**. See the [results report](RESULTS.md) for results by round, token usage, the artifact comparison, and the leaderboard reference values.

| Material | Contents |
| --- | --- |
| [Corrected five-round results](corrected-five/) | 445 trial records in original rounds 3–7, with one canonical replacement per round |
| [Original seven-round results](original-seven/) | All 623 original trial records |
| [Round summary](round-summary.csv) | Pass counts, token usage, and cost |
| [Canonical replacement records](canonical-replacement/PROVENANCE.md) | Seven graded attempts and their mapping to the five reported replacements |
| [Frozen instructions](artifacts/) | Nine task-specific `AGENTS.md` files and their checksums |
| [Submission and runtime metadata](submission/) | Pinned dataset refs, runtime versions, and the descriptor for the five-round Hub job |

The nine frozen instructions were generated from earlier attempts on the same tasks, with complete trajectories and grading feedback. The other 80 tasks received no evolved instructions. The archived runs used Codex 0.166.0 and Harbor 0.21.0. [Pinned harness](https://github.com/Ardorry/OpenEvo/commit/bae1b900e043685d5ebc3a93dc6edd2ee5c9ddfd) · [Canonical admission package](https://github.com/Ardorry/OpenEvo/commit/015569bda1478d385fd08a70d2c33d928e1c12a3)

Raw trial archives for the published cohorts are in Harbor Hub. Match them using the trial IDs in the tables. [Original rounds 1–5](https://hub.harborframework.com/jobs/862a2581-7850-46f9-b955-58aa3e8cc58f) · [Original rounds 6–7](https://hub.harborframework.com/jobs/1f474dd2-013a-48a4-ab8e-1f90289ff85c) · [Corrected rounds A–E](https://hub.harborframework.com/jobs/e4ab0103-3b2c-5b16-b093-5740765c5255)
