# ChemCrowRun

ChemCrowRun is an independent reproduction and evaluation implementation of the
ChemCrow benchmark on [OpenEvo](OPENEVO.md). **ChemCrowRun v1.0** runs 14 tasks through
G1 → three independent Reflectors → G2, internal blind evaluation, and a separate
calibrated GPT-4 Judge. It does not claim official ChemCrow endorsement or leaderboard certification.

[Reproduce the experiment](REPRODUCE.md) ·
[Delivery inventory](benchmarks/chemcrow/DELIVERY.md) ·
[Results](benchmarks/chemcrow/reports/chemcrowrun_acceptance_20261015/chemcrowrun-report.json) ·
[42 evolution artifacts](benchmarks/chemcrow/artifacts/reproduction-v1-sol-20261015/README.md)

## Start here

Use Linux x86-64 or WSL2 with Docker. The benchmark uses the original managed science runtime;
Core runs Codex inside its session containers. The host runs the orchestration services and provides
an authorized credential source. The OpenEvo Desktop application is not required for this benchmark.

```bash
git clone --branch chemcrow https://github.com/Ardorry/OpenEvo.git ChemCrowRun
cd ChemCrowRun
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun --help
```

Follow [REPRODUCE.md](benchmarks/chemcrow/REPRODUCE.md) to fetch locked public fixtures, obtain
and verify the runtime, configure your own subscription/API credentials, start services, run the
single-task acceptance test and full experiment, evaluate, and export a submission. Use a fresh
run ID for every independent experiment. Results live in `outputs/chemcrowrun/<run_id>/`.

## Candidate status

Verified on 2026-10-15: the independent local clone completed a real single-task smoke,
a fresh 14-task experiment, and all 42 calibrated GPT-4 evaluations. Internal scores are
G1 **10.6286/12**, G2 **11.0143/12**; GPT-4 scores are G1 **9.6071/10**, G2 **9.5714/10**.
Neither evaluation establishes a significant G2 improvement. Audit and submission validation pass.
Two pre-model infrastructure failures were explicitly recovered under the existing no-effect rules;
all original failures and recovery evidence remain recorded. The final candidate passes 328 tests
with two optional tests skipped. See the acceptance record for source revisions and postprocessing fixes.

WebSearch was unavailable because SERP_API_KEY was absent; 71 of 686 tool observations failed
for this reason. Model substitution, tool availability and service versions limit comparability.

The original runtime and its required source/license companion are distributed separately from
Git; see [runtime downloads](benchmarks/chemcrow/reproduction/RUNTIME_DOWNLOADS.md).
The source/license and applicable relink materials have been prepared. Public remote fresh-clone
paid end-to-end acceptance remains pending. The optional bundled zsh requires GLIBC_2.38,
while the original image has GLIBC_2.36; this limitation is preserved and disclosed.

This fork already includes Core session-directory isolation. If using a separate upstream
checkout, apply the [Core compatibility patch](benchmarks/chemcrow/patches/README.md) to its
documented base. Keep the benchmark under `benchmarks/chemcrow` so its editable Core dependency
resolves correctly. RXN/MCP build adaptations are in `benchmarks/chemcrow/deployment`.

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
