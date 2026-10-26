# Reproduce ChemCrowRun

This guide runs the 14-task ChemCrowRun protocol from a new OpenEvo checkout.

## Requirements

- Linux x86-64 or WSL2
- Python 3.11
- [uv](https://docs.astral.sh/uv/)
- Docker with Compose support
- a Codex subscription and local Codex authentication
- an OpenRouter API key for the optional calibrated GPT-4 evaluation

## Install

~~~bash
git clone https://github.com/rectangularcoordinatesystem/OpenEvo.git
cd OpenEvo
uv sync --frozen --project benchmarks/chemcrow --python 3.11 --extra dev
uv run --frozen --project benchmarks/chemcrow chemcrowrun --help
~~~

Fetch the public ChemCrow and RXN dependencies:

~~~bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun reproduction-fixtures \
  --lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --destination-root "$PWD/.reproduction-fixtures" --no-model-calls
~~~

## Runtime image

Download the Linux amd64 runtime archive from the
[ChemCrowRun runtime release](https://github.com/Ardorry/OpenEvo/releases/tag/chemcrowrun-runtime-0.1.1)
and load it into Docker:

~~~bash
docker load --input /path/to/openevo-science-runtime-0.1.1-linux-amd64.tar.gz
~~~

The release also provides the corresponding source and license package.

## Credentials

Authenticate Codex using the normal Codex CLI login flow. For the calibrated GPT-4 evaluation,
copy **reproduction/judge.env.example** to a private file outside the repository and add your own
OpenRouter key. Configure **HTTPS_PROXY** only when your network requires it.

**SERP_API_KEY** is optional and enables web search for tasks that use it.

## Single-task run

~~~bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id smoke-001 \
  --fixture-root "$PWD/.reproduction-fixtures" \
  --model gpt-5.6-sol \
  --substitution-reason "Historical gpt-5.5 is unavailable in the current catalog"

uv run --frozen --project benchmarks/chemcrow chemcrowrun up \
  --workspace outputs/chemcrowrun/smoke-001

uv run --frozen --project benchmarks/chemcrow chemcrowrun status \
  --workspace outputs/chemcrowrun/smoke-001

export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --frozen --project benchmarks/chemcrow chemcrowrun experiment \
  --workspace outputs/chemcrowrun/smoke-001 --smoke --allow-paid

uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/smoke-001 --smoke

uv run --frozen --project benchmarks/chemcrow chemcrowrun down \
  --workspace outputs/chemcrowrun/smoke-001
~~~

The smoke run executes one complete G1 → three Reflectors → G2 pair.

## Full 14-task run

Use a new run ID so the full experiment has independent state:

~~~bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id reproduction-001 \
  --fixture-root "$PWD/.reproduction-fixtures" \
  --model gpt-5.6-sol \
  --substitution-reason "Historical gpt-5.5 is unavailable in the current catalog"

uv run --frozen --project benchmarks/chemcrow chemcrowrun up \
  --workspace outputs/chemcrowrun/reproduction-001

uv run --frozen --project benchmarks/chemcrow chemcrowrun experiment \
  --workspace outputs/chemcrowrun/reproduction-001 --allow-paid

uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/reproduction-001
~~~

The experiment keeps each task independent and creates one text memory, one skill bundle, and
one agent-system artifact before G2.

## Calibrated GPT-4 evaluation

~~~bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun judge \
  --workspace outputs/chemcrowrun/reproduction-001 \
  --env-file /path/to/private-judge.env \
  --budget-usd 12 --allow-paid

uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/reproduction-001
~~~

The evaluator performs 42 comparisons: historical control, G1, and G2 for each of the 14 tasks.
The recorded reproduction spent $2.99460 for these 42 GPT-4 evaluations.

## Outputs

A completed full run writes:

- **outputs/chemcrowrun/<run_id>/chemcrowrun-report.json**
- **outputs/chemcrowrun/<run_id>/chemcrowrun-submission.json**
- **outputs/chemcrowrun/<run_id>/chemcrowrun-comparison.json**
- task-local G1/G2 results and evolution artifacts under the same workspace

Run the offline tests with:

~~~bash
export CHEMCROW_TEST_RUNS_ROOT="$PWD/.reproduction-fixtures/chemcrow-runs"
export CHEMCROW_TEST_PUBLIC_ROOT="$PWD/.reproduction-fixtures/chemcrow-public"
uv run --frozen --project benchmarks/chemcrow --extra dev \
  pytest benchmarks/chemcrow/tests -q
~~~
