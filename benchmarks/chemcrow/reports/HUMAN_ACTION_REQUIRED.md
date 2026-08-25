# HUMAN ACTION REQUIRED

Updated: 2026-08-25 Asia/Shanghai

Never paste a credential into chat or commit it. The formal benchmark and 42-call paper evaluation
remain blocked. The OpenRouter key is valid and the frozen `$11.83266` ceiling is funded. The user
approved OpenAI-only `data_collection=allow`; the remaining OpenRouter blocker is that this revision
has not passed a separately authorized Core smoke. The historical v1 attempt remains a terminal 403.

## 1. Credentials and API keys

| Action | Why required | Exact variable/action | Scope | How to verify |
|---|---|---|---|---|
| Validate the approved OpenAI data-policy revision | `/key` and `/credits` passed; the v1 request combined OpenAI-only routing with `data_collection=deny` and failed before any model/provider/usage receipt. The user approved `data_collection=allow`. | Do not delete or retry claim `paper-chemcrow-smoke-core-v1`. A future v2 smoke requires literal `I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V2_20260825` and call ID `paper-chemcrow-smoke-core-v2`. | Paper evaluator only | A separately authorized v2 Core smoke returns HTTP 200, model `openai/gpt-4`, provider `OpenAI`, strict schema, and a usage receipt |
| Decide whether to configure WebSearch | `SERP_API_KEY` is absent and WebSearch fails explicitly | Put `SERP_API_KEY` only in an ignored 0600 env file if this paid/external service is approved | Tasks needing current web evidence | One separately authorized call has `source=live` and provider-side usage matches |
| Decide whether hosted RXN is an allowed fallback | Local RXN works; hosted RXN is optional and time-limited | `RXN4CHEM_API_KEY`, `RXN4CHEM_PROJECT_ID`, optional `RXN4CHEMISTRY_BASE_URL` | RXN-dependent tasks only | One separately authorized hosted prediction returns a real provider receipt |
| Keep excluded keys absent unless the protocol changes | ChemSpace procurement and hidden-model literature search are deliberately excluded | `CHEMSPACE_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`, `OPENAI_API_KEY` | Reduced-profile benchmark | Credential report continues to show these routes excluded |

The credential file `/home/lhy-h/work/chemcrowrun/.env.paper-evaluator` is mode 0600. The value-free
probe currently reports `status=VALID`, `auth_valid=true`, `credit_probe_success=true`, and
`sufficient_remaining_for_frozen_ceiling=true`.

## 2. Docker, GPU, and runtime services

The CPU profile is ready now. After a reboot, start or verify the following:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
docker compose -f benchmarks/chemcrow/configs/rxn_sandbox.services.yaml up -d

/home/lhy-h/work/chemcrowrun/core-state/evolution-backend-venv/bin/python \
  -m openevo.evolution.cli serve \
  --host 127.0.0.1 --port 8200 \
  --db /home/lhy-h/work/chemcrowrun/core-state/evolution/core.sqlite3 \
  --artifact-root /home/lhy-h/work/chemcrowrun/core-state/evolution/artifacts \
  --framework-lock /home/lhy-h/work/chemcrowrun/core-state/framework/framework-lock.json

uv run python -m openevo.rollout.server \
  --config benchmarks/chemcrow/configs/openevo_core_topology.yaml --log-level info

uv run python -m openevo.gateway.server \
  --config benchmarks/chemcrow/configs/openevo_core_topology.yaml \
  --node-id chemcrow-core-gateway-01 --log-level info
```

This blocks all model-driven tasks if Rollout/Gateway/Evolution is down and only RXN-dependent tasks
if `:8300` is down. Verify with:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/nodes
curl -fsS http://127.0.0.1:8100/health
curl -fsS http://127.0.0.1:8200/v1/health
curl -fsS http://127.0.0.1:8300/openapi.json
```

GPU RXN is optional. Do not switch to it without a separately frozen image/profile and new forward
and retrosynthesis canaries.

## 3. Deprecated or unavailable ChemCrow services

- The hosted RXN service is time-limited and reports an end-of-service date of 2026-10-28. Local
  RXN is the current frozen route. This blocks only a claim of hosted-RXN equivalence.
- Paper-only restricted tools are absent from `chemcrow-public`; exact paper reproduction is
  impossible from the public sources.
- `LiteratureSearch`, `GetMoleculePrice`, and `python_repl` remain excluded for provider fairness,
  benchmark-only safety, or reproducibility. Do not replace their failures with fake output.
- The local RXN-Sandbox model/service is not the original paper-era IBM RXN snapshot.

Verification is the explicit availability/deviation table in `CHEMCROW_TOOL_READINESS_MATRIX.md`;
there is no credential that repairs an absent public implementation.

## 4. Tool licenses and accounts

1. Review `/home/lhy-h/work/chemcrowrun/vendor/rxn-sandbox/LICENSE.OpenMDW-1.1` and record whether
   formal RXN-dependent metrics may use commit `d56aad22564a904a2fc737460adfbf0d4c4e9ac2` and the model
   hashes in `RXN_RUNTIME_MANIFEST.json`.
2. Review and record acceptance of MolBloom/SureChEMBL package/data terms for version `2.3.5`.
3. Create/approve a SerpAPI account and quota only if WebSearch is selected.

These decisions block formal claims involving the corresponding tools, not deterministic unit tests.

## 5. Model authentication

- Candidate, all three Reflectors, internal evaluator, and final evaluator use the native Codex
  harness with subscription auth. If `~/.codex/auth.json` expires, renew it through the normal Codex
  login flow; never copy it into the repository. Verify with the zero-model preflight.
- OpenRouter is restricted to the post-hoc paper evaluator. The key must remain only in the ignored
  0600 file. The current key/credit gate passes. OpenAI-only `data_collection=allow` is approved and
  implemented but still requires a separately authorized v2 Core smoke.
- Formal paper authorization must remain unconsumed until a protocol-compatible 14-task sealed set
  and frozen 42-call plan exist:

```text
CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS
```

## 6. Final human chemistry evaluation

Four independent expert chemists are required for the official-scale compatibility packet. Each
reviews 42 blinded conditions and scores both answers from 0 to 10 on `Chemically accurate`,
`Quality of reasoning`, and `Task completed`, plus A/B/tie and confidence. That is 168 completed
review forms. Reviewers must not see the private mapping. A smaller panel is exploratory.

For `chemcrow-12`, approve a safety scoring policy that rewards appropriate refusal/caution and never
relabels a refusal as physical execution success. Verification requires a completed blinded packet,
four responses per condition, and an untouched private mapping with mode 0600.

## 7. Potentially paid operations

Potentially quota-consuming operations are Candidate, three Reflectors, both internal diagnostic
evaluations, the final evaluator, SerpAPI, hosted RXN, and the GPT-4 paper evaluator. Local RXN has no
per-call API fee.

No further call is currently authorized. The v1 smoke claim is terminal and must never be retried or
deleted. The prepared v2 claim has not been submitted. Formal GPT-4 evaluation remains 42 calls with
a frozen list-price ceiling of `$11.83266`; it requires the formal authorization literal,
`--allow-paid`, a complete plan hash, and a successful new preflight.

## 8. Tasks that cannot currently run faithfully

The full capability mapping is in `CHEMCROW_TOOL_READINESS_MATRIX.md`.

- `chemcrow-01`, `04`, `05`, and `13`: live price/procurement evidence is unavailable.
- `chemcrow-02`: novelty/literature evidence is unavailable.
- `chemcrow-10`: no reliable dedicated boiling-point/property lookup is configured.
- `chemcrow-12`: literature, legality/activity, and purchasing claims are unavailable; safety refusal
  must be preserved.
- `chemcrow-03`, `06`, `08`, `09`, `14`, and `15`: local RXN works but differs materially from the
  paper-era service/model.
- `chemcrow-07`: the prompt's fallback synthesis path is runnable, but current purchasability is only
  approximated by MolBloom membership rather than vendor price/stock.

The valid label is a public-source, tool-audited reduced-profile ChemCrow environment, not
paper-identical ChemCrow.

## 9. Decisions required before the next benchmark stage

### A. Resolve the protocol incompatibility

The 12 sealed `full-v3` pairs used one Reflector and one `text_memory` artifact. They are immutable,
valid evidence for that historical protocol, but they do **not** satisfy the new required three-
Reflector protocol. A two-task three-artifact repair cannot be combined with those 12 pairs into one
scientifically homogeneous 14-task aggregate.

Choose one:

1. Authorize a fresh 14-task three-artifact experiment. This is the only path to a homogeneous
   14-task result under the new fixed protocol.
2. Preserve `full-v3` as a separate single-artifact experiment and run task 14/15 only as a new
   three-artifact pilot. Do not create a mixed aggregate.
3. Revert the repair to the old single-artifact protocol for comparability with the 12 pairs. This
   conflicts with the newly mandated mechanism and is not recommended.

Until this decision is explicit, readiness verdict is `BLOCKED`.

### B. Task 14 duplicate-call authorization

The interrupted evidence remains at
`runs/full-v3/claims/chemcrow-task-local-full-v3--chemcrow-14/` and must not be edited or deleted.
A fresh pair must use experiment/pair ID
`chemcrow-task-local-paper-repair-three-isolated-v1--chemcrow-14`, new run/cache/ledger roots, bare S0
hash `8f7113de469c92fff55b99b62e58d4c43d5e42f2b6485affe44b247c28306a5c`, and an empty baseline
artifact receipt. It may not import the interrupted G1, artifact, or claimed G2.

To authorize it in a later turn, send the exact literal:

```text
I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP
```

Only then may an authorization receipt be created at
`/home/lhy-h/work/chemcrowrun/manifests/CHEMCROW_14_DUPLICATE_AUTHORIZATION.json`, bound to prior claim
SHA256 `bfa5d6f880bb0f8b1f0149ceef364c51e2a0fe2e290734e0ca3827078fcae40b`.

After all blockers are resolved, the prepared task-14-only prefix command is:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
set -a
source benchmarks/chemcrow/.env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run --project benchmarks/chemcrow openevo-chemcrow run \
  --config benchmarks/chemcrow/configs/paper_repair.three-isolated-v1.yaml \
  --stop-after-task-id chemcrow-14 \
  --allow-paid
```

Do not run that command while this report says `BLOCKED`. After task 14 seals and is audited, task 15
would use the same config with `resume`, omitting `--stop-after-task-id`. A claim without a sealed pair
remains ambiguous and must never be automatically redispatched.

### C. Other required decisions

- Authorize at most one v2 Core smoke if the approved OpenAI-only `data_collection=allow` route is to
  be live-validated. Keep model, provider, fallback, strict-JSON, and Core restrictions frozen.
- Approve/reject RXN-Sandbox and MolBloom licenses for formal metrics.
- Freeze the formal tool profile with WebSearch absent or explicitly configured.
- Approve the `chemcrow-12` safety scoring policy.
- Accept modern RDKit/RXN deviations or commission a historical compatibility study.
- Approve the four-expert review scale and provisional-result wording.
- Do not authorize a 42-call plan until `PAPER_COMPARISON_MATRIX.json` and a homogeneous sealed
  14-task authority are both accepted.
