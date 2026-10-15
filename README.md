# ChemCrowRun

ChemCrowRun is an independent reproduction and evaluation implementation of the
ChemCrow benchmark on [OpenEvo](OPENEVO.md). **ChemCrowRun v1.0** runs 14 tasks through
G1 → three independent Reflectors → G2, internal blind evaluation, and a separate
calibrated GPT-4 Judge. It does not claim official ChemCrow endorsement or leaderboard certification.

[Reproduce the experiment](REPRODUCE.md) ·
[Engineering acceptance](benchmarks/chemcrow/reports/chemcrowrun_acceptance_20261015/ACCEPTANCE.md) ·
[Release preparation](benchmarks/chemcrow/docs/RELEASE.md)

## Start here

Use Linux x86-64 or WSL2 with Docker. The benchmark uses the original managed science runtime;
Core runs Codex inside its session containers. The host runs the orchestration services and provides
an authorized credential source. The OpenEvo Desktop application is not required for this benchmark.

```bash
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun --help
```

Follow [REPRODUCE.md](benchmarks/chemcrow/REPRODUCE.md) to fetch locked public fixtures, obtain
and verify the runtime, configure your own subscription/API credentials, start services, run the
single-task acceptance test and full experiment, evaluate, and export a submission. Use a fresh
run ID for every independent experiment. Results live in `outputs/chemcrowrun/<run_id>/`.

## Candidate status

Snapshot: 2026-10-15 04:22 UTC; the next experiment check is scheduled for 08:30 UTC.

The local candidate has passed independent-clone installation, runtime verification, service and
chemical-tool checks, and one real end-to-end task. A fresh 14-task experiment is in progress;
consult the dated acceptance record for its completion and calibration status. Offline tests and
historical scores do not stand in for new experimental results.

The runtime archive has been verified locally. Public asset publication, anonymous download and
fresh-clone acceptance from the actual GitHub release remain pending. Native dependency attribution
review also remains open; the archive is not yet cleared for public distribution.

The current subscription no longer offers the historical `gpt-5.5` model. This candidate's new
experiments explicitly declare `gpt-5.6-sol` across all six roles as a model-substitution condition.
The task set, three-Reflector procedure, evaluation rules and historical records remain unchanged.
Do not describe substituted-model results as a completely identical model reproduction.

## Historical reference and provenance

The immutable **legacy full-v8** experiment is the reference for ChemCrowRun v1.0:

| Evaluation | G1 | G2 |
| --- | ---: | ---: |
| Internal final blind score /12 | 9.9286 | 10.2857 |
| Calibrated GPT-4 score /10 | 9.3571 | 9.2143 |

The historical experiment did not establish a statistically significant G2 improvement.
These are historical, provisional LLM-judged results, not the scores of the new run.

- [Historical public report](benchmarks/chemcrow/reports/full_v8_public_20260831/README.md)
- [Legacy identities and immutable hashes](benchmarks/chemcrow/reproduction/legacy-aliases.json)
- [84 original artifacts across two experiments](benchmarks/chemcrow/artifacts/README.md), 42 from full-v8
- [Experiment and code provenance](benchmarks/chemcrow/PUBLICATION.md)
- [Submission schema](benchmarks/chemcrow/reproduction/submission.schema.json)
- [Service architecture and deployment](benchmarks/chemcrow/deployment/README.md)

The Python package and legacy `openevo-chemcrow` command remain compatible. The public
entry point is `chemcrowrun`; reports and submissions use `chemcrowrun-report.json` and
`chemcrowrun-submission.json`. Historical payloads, paths, run identities and hashes are preserved.

For the separate OpenEvo framework and Desktop/Daemon product, see [OPENEVO.md](OPENEVO.md).
