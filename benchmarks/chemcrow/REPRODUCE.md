# Reproduce ChemCrowRun v1.0

This guide describes the candidate workflow. Consult the acceptance report for which commands
have actually passed. The original archive is locally verified; public anonymous asset download
and post-publication remote-clone acceptance require an approved release and remain PENDING.
The [2026-10-15 acceptance record](reports/chemcrowrun_acceptance_20261015/ACCEPTANCE.md)
separates real execution evidence from offline checks and pending steps.

## Install from an empty directory

Use Linux x86-64 or WSL2 with Docker Desktop host networking enabled. The recorded host toolchain
is Python 3.11.15, uv 0.12.5, Docker CLI/Engine 29.5.2. The runtime itself contains Python 3.12.13
and Codex CLI 0.144.1; do not confuse host and container versions. Allow substantial disk space for
RXN/PyTorch image builds. Core services run as local Python processes; chemistry services use Compose.

```bash
git clone https://github.com/Ardorry/OpenEvo.git ChemCrowRun
cd ChemCrowRun
git checkout <APPROVED_FULL_40_CHARACTER_RELEASE_COMMIT>
uv sync --frozen --project benchmarks/chemcrow --python 3.11.15 --extra dev
# Install Git LFS using your platform package manager before the next command.
git lfs version
uv run --frozen --project benchmarks/chemcrow chemcrowrun reproduction-fixtures \
  --lock benchmarks/chemcrow/reproduction/fixtures.lock.json \
  --destination-root "$PWD/.reproduction-fixtures" --no-model-calls
```

Fixtures are fetched anonymously at exact public commits and checked against Git trees and file
hashes. RXN LFS objects are downloaded and verified. Existing dirty/wrong-version checkouts are
rejected, never overwritten. Use `--check-only` to revalidate. Do not copy historical experiment
databases, generated answers, caches, or trajectories into the new workspace. The upstream notebook
fixture is used by tests and the **post-run** calibration evaluator, not mounted into Candidate or
Reflector containers. `chemcrow-runs` has no discovered redistribution license: fetch it from the
upstream; do not bundle it in this release.

## Obtain the exact runtime

```bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun runtime \
  --url <APPROVED_PUBLIC_HTTPS_RELEASE_ASSET_URL> \
  --output .reproduction-cache/runtime-verification.json
```

The URL is deliberately not invented before publication. For local candidate acceptance only:

```bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun runtime \
  --archive /absolute/path/openevo-science-runtime-0.1.1-linux-amd64.tar.gz \
  --output .reproduction-cache/runtime-verification.json
```

The installer verifies size 352236726, SHA-256
`ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149`, OCI index
`sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b`, config ID,
platform and managed label, then imports and starts the exact image with networking disabled.
It records local archive, cache hit, or newly verified anonymous download separately. Failed downloads
are staged privately; they never overwrite a valid cache. A corrupt cached file is rejected: move
it aside for diagnosis, then download again. No approximate rebuild is labeled as the original.

## Credentials and network

Configure your own legitimate Codex subscription authentication in `~/.codex/auth.json` using the
provider's login mechanism. Core stages only the required credential into each managed container;
it does not mount your working directory, chat history or benchmark fixtures. The historical configuration requests `gpt-5.5` for all six roles with transcript capture.
On 2026-10-15 the current subscription catalog did not offer that model. A bounded real Core
probe passed with `gpt-5.6-sol`. For an explicit substitution, add both flags to each `init` command:
`--model gpt-5.6-sol --substitution-reason "Historical gpt-5.5 absent from current subscription catalog"`.
This records `chemcrowrun_v1_model_substitution_v1` in configuration, reports and submission;
all six roles change together. The original protocol remains available and unchanged.
Substituted models are compared descriptively but cannot be pooled as identical model conditions. The host's proxy and Docker Desktop's network may differ: verify
both. The GPT-4 shim requires `--use-environment-proxy`; setting only `HTTPS_PROXY` on a process
that disables environment proxy support does not route that process through it. Both lowercase
`https_proxy` and uppercase `HTTPS_PROXY` are recognized.

RXN runs locally and uses no hosted RXN credential. `SERP_API_KEY` optionally enables WebSearch;
its absence is recorded as a capability limitation. Do not silently add excluded procurement or
literature tools. Place Judge credentials in a private file outside Git, mode 0600, following
[the example](reproduction/judge.env.example). Never include credentials in reports or submissions.

## Isolated services and real single-task test

Run IDs must be unique. `init` refuses an existing workspace. Use a different port base if another
run is active. The default ports are Rollout 18080, Gateway 18081, Evolution 18082 and RXN 18083.

```bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id smoke-001 --fixture-root "$PWD/.reproduction-fixtures"
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
```

`up` builds RXN from the locked public source, starts Redis/RabbitMQ/worker/MCP with health checks,
and starts the three Core processes. It is safe to repeat for a service that exited. Inspect private
`logs/*.log`, `service-health.json`, and `docker compose --project-name chemcrowrun-<run_id>
--file outputs/chemcrowrun/<run_id>/rxn.compose.yaml logs` for diagnosis. `down` stops owned
processes and containers without deleting datasets or evidence. `chemcrowrun logs --workspace
outputs/chemcrowrun/<run_id>` snapshots container image IDs and all RXN service/worker logs;
`down` attempts the same collection before stopping. See deployment notes for the
benchmark's retained v1 Core API and inline worker design.

The smoke run completes the first entire pair (G1, both internal assessments, three Reflectors,
G2 injection, blind final evaluation, reset). It cannot be exported as a 14-task submission.
Failed or ambiguous claims are never automatically replayed. An infrastructure repair may require
the existing explicit, evidence-bound recovery commands; `resume` alone cannot bypass claims.

## Full experiment and standard export

After smoke passes, create an independent workspace. No smoke or historical answers are copied.

```bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun init \
  --run-id reproduction-001 --fixture-root "$PWD/.reproduction-fixtures"
uv run --frozen --project benchmarks/chemcrow chemcrowrun up \
  --workspace outputs/chemcrowrun/reproduction-001
uv run --frozen --project benchmarks/chemcrow chemcrowrun experiment \
  --workspace outputs/chemcrowrun/reproduction-001 --allow-paid
uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/reproduction-001
```

Every workspace pins the clean source commit, configuration, topology and environment identity.
Keep that checkout unchanged throughout generation and export; use a new workspace after changes. This performs
aggregation, completed-run audit, submission export, schema validation, per-task paired analysis,
10,000-resample bootstrap confidence intervals and an exact two-sided sign-flip test. Raw answers
stay private. `chemcrowrun-report.json` explicitly reports missing stages; a report with missing
pairs is BLOCKED. Only a complete 14-task PASS audit can produce `chemcrowrun-submission.json`.
The report also writes `chemcrowrun-comparison.json`, with per-task old/new G1/G2 differences,
paired bootstrap intervals and sign-flip tests. Cross-run tests are exploratory, unadjusted
comparisons; model or service changes prevent causal claims. Comparison reads the immutable public
full-v8 scores only after submission validation. Optional calibrated comparisons require all 42
sealed Judge results. `chemcrowrun compare --submission <file> --paper-results <directory>
--output <file>` reproduces the same offline comparison.

The immutable task order is 01–10,12–15, seed 20260825. Each task starts bare S0, freezes three
independent Reflector inputs, creates exactly one memory/skill/system artifact, injects them into
G2 and seals then resets before the next task. Candidate/Reflector timeouts are 900 s; evaluators
600 s. There are 112 logical subscription invocations; managed readiness can consume additional
subscription turns, and internal agent/tool turns are not equivalent to one provider request.
Automatic experiment retries are zero. All explicit replacements retain failed attempt evidence.
The local seed does not control provider-side sampling. No temperature/top_p is invented for a
subscription harness. Server model weights can change under an unchanged model name; document
that limitation and never claim bit-identical model reproduction.

## Separate calibrated GPT-4 Judge

Only after the complete run audit passes, the evaluator reads historical notebook controls and
compares them with sealed new answers. It uses the existing PAPER_MINIMAL calibrated prompt,
OpenRouter `openai/gpt-4`, OpenAI-only, temperature 0.1, output cap 1200, no fallback or automatic
retry. This is not the original paper's unrecovered verbatim evaluator prompt.

```bash
uv run --frozen --project benchmarks/chemcrow chemcrowrun judge \
  --workspace outputs/chemcrowrun/reproduction-001 \
  --env-file /absolute/path/private-judge.env --budget-usd 12 --allow-paid
uv run --frozen --project benchmarks/chemcrow chemcrowrun report \
  --workspace outputs/chemcrowrun/reproduction-001
```

The 42-call frozen list-price ceiling is $11.83266 (historical observed cost was $2.65887 including
one invalid paid response). The tiny connectivity probe cost is not a realistic estimate for long
Judge prompts. The runner checks credits before dispatch, enforces the existing budget/claim
protocol, and never automatically performs replacements. Judge services use fixed ports 8180,
8110 and 8400 because the historical harness binds 8110; concurrent Judge stacks are rejected.
Per-call token/cost receipts remain in `judge/openrouter-receipts`; subscription billing cannot
be inferred from those receipts. Interrupted Judge execution reuses only fully validated sealed
results from **this** plan; ambiguous attempts fail closed.

## Offline checks and acceptance layers

```bash
export CHEMCROW_TEST_RUNS_ROOT="$PWD/.reproduction-fixtures/chemcrow-runs"
export CHEMCROW_TEST_PUBLIC_ROOT="$PWD/.reproduction-fixtures/chemcrow-public"
uv run --frozen --project benchmarks/chemcrow pytest benchmarks/chemcrow/tests -q
uv run --frozen --project benchmarks/chemcrow chemcrowrun verify-publication \
  --package-root benchmarks/chemcrow --no-model-calls
uv run --frozen --project benchmarks/chemcrow chemcrowrun validate-submission \
  --submission outputs/chemcrowrun/reproduction-001/chemcrowrun-submission.json --no-model-calls
```

L1 verifies public immutable evidence. L2 verifies installation, fixtures, tests and zero-model
preflight. L3 schema infrastructure can pass offline; a genuinely reproduced L3 result additionally
requires the fresh complete experiment and its audit. Public asset delivery and actual remote
fresh-clone acceptance must be tested after publication; a local clone cannot certify either.
