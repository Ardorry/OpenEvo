# EvoLab on Terminal-Bench 2.1

This branch archives the November 2026 evaluation of EvoLab with GPT-6.1 Sol (high), Codex 0.166.0, and Harbor 0.21.0. The corrected five-round cohort passed **394 of 445 trials (88.54%)** across all 89 tasks at a recorded inference cost of **$89.79**. The seven original rounds passed **541 of 623 trials (86.84%)**.

| Material | Contents |
| --- | --- |
| [Audit index](docs/dev/terminal-bench-2.1/README.md) | Evidence sets, trial ordering, Hub archives, and review status |
| [Results report](docs/dev/terminal-bench-2.1/RESULTS.md) | Per-round scores, usage, inference cost, and artifact comparison |
| [Original seven rounds](docs/dev/terminal-bench-2.1/original-seven/) | 623 trial records and audit summaries |
| [Corrected five rounds](docs/dev/terminal-bench-2.1/corrected-five/) | 445 trial records, including five canonical task replacements |
| [Replacement provenance](docs/dev/terminal-bench-2.1/canonical-replacement/PROVENANCE.md) | Grading recovery and how the replacements were assigned |
| [Frozen agent instructions](docs/dev/terminal-bench-2.1/artifacts/manifest.json) | Nine task-specific instruction files and their SHA-256 checksums |
| [Submission records](docs/dev/terminal-bench-2.1/submission/) | Dataset refs, runtime versions, static-check summary, and submission receipt |

EvoLab is the display name used in this archive. Harbor lookups use the recorded agent ID `openevo-agent9-codex`. Reproduction uses the [pinned harness](https://github.com/Ardorry/OpenEvo/commit/bae1b900e043685d5ebc3a93dc6edd2ee5c9ddfd) and [canonical admission package](https://github.com/Ardorry/OpenEvo/commit/015569bda1478d385fd08a70d2c33d928e1c12a3). The branch's inherited product sources and documentation come from the [historical base snapshot](https://github.com/rectangularcoordinatesystem/OpenEvo/tree/b7baa85b6699ed797c61a3e2166fa0b5ad2a0284).
