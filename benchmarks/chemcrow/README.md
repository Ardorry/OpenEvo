# ChemCrowRun

ChemCrowRun is an independent reproduction of the 14-task ChemCrow benchmark on OpenEvo.
It evaluates a baseline generation (G1), creates three task-local evolution artifacts, and
evaluates a second generation (G2) with those artifacts. The three artifact types are text
memory, skill bundle, and agent system.

This directory contains:

- the ChemCrow adapter and **chemcrowrun** command;
- the 14-task protocol and model configuration;
- RXN/MCP container configuration;
- reproducible installation and execution instructions;
- the published G1/G2 result table and machine-readable scores;
- the 42 evolution artifacts produced by the reproduction run;
- the result submission schema.

ChemCrowRun is an OpenEvo benchmark contribution. It is separate from the original ChemCrow
agent and does not represent official ChemCrow leaderboard certification.

## Quick start

~~~bash
uv sync --frozen --project benchmarks/chemcrow --python 3.11 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun --help
~~~

Follow [REPRODUCE.md](REPRODUCE.md) for the complete workflow.

## Results

| Run | Internal G1 /12 | Internal G2 /12 | GPT-4 G1 /10 | GPT-4 G2 /10 |
|---|---:|---:|---:|---:|
| OpenEvo G1/G2 | 9.929 | 10.286 | 9.357 | 9.214 |
| ChemCrowRun Reproduction | 10.629 | 11.014 | 9.607 | 9.571 |

The reproduction used **gpt-5.6-sol** because the historical **gpt-5.5** model was not
available in the current subscription catalog. The model difference is retained in the
result metadata.

See [the full comparison](reports/CHEMCROW_RESULTS_COMPARISON.md),
[the reproduction results](reports/CHEMCROWRUN_RESULTS.json), and
[the evolution artifacts](artifacts/reproduction-v1-sol-20261015/README.md).
