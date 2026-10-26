# ChemCrowRun

**ChemCrowRun v1.0** is an independent reproduction and evaluation implementation of the
ChemCrow benchmark on OpenEvo. It runs 14 tasks with isolated G1 → three native Reflectors →
G2, internal blind evaluation, and a separate calibrated GPT-4 Judge. It is not the original
ChemCrow agent, an official leaderboard, or an endorsed certification.

Start with [REPRODUCE.md](REPRODUCE.md). The public entry point is `chemcrowrun`; the
`openevo-chemcrow` command and Python package remain compatible. Run data live under
`outputs/chemcrowrun/<run_id>/`; reports and submissions use `chemcrowrun-report.json` and
`chemcrowrun-submission.json`.

```bash
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun --help
```

The reproduction guide covers public fixtures, the original locked runtime archive, isolated
services, a real single-task smoke run, a fresh 14-task run, budgeted evaluation, and audited
exports. Source availability and offline test success do **not** demonstrate successful live
reproduction. Anonymous runtime asset download and remote fresh-clone acceptance remain pending
until an approved release is actually published and tested.

Historical full-v8 remains the immutable reference experiment. Its identities and file hashes
are mapped in [legacy-aliases.json](reproduction/legacy-aliases.json); no historical payload or
hash is renamed or regenerated. [Historical published results](reports/full_v8_public_20260831/README.md)
report internal blind G1 9.9286/12 and G2 10.2857/12, calibrated GPT-4 G1 9.3571/10 and
G2 9.2143/10. They do not establish statistically significant G2 improvement and are not new-run
results. The [artifact archive](artifacts/README.md) holds 84 original text artifacts across two
experiments, with 42 belonging to full-v8.

See also [publication provenance](PUBLICATION.md), [release preparation](docs/RELEASE.md),
[service architecture](deployment/README.md), and [submission schema](reproduction/submission.schema.json).
The historical machine protocol identifier `chemcrow_full_v8_reproduction_v1` remains stable;
ChemCrowRun v1.0 is its public name, not a changed scoring protocol.
