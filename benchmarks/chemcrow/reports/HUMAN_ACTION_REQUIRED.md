# HUMAN ACTION REQUIRED

Generated: 2026-08-25 Asia/Shanghai

Never paste secrets into chat or commit them. The full benchmark is blocked.

## 1. Credentials and API keys

| Action | Why | Exact variable | Scope | Verification |
|---|---|---|---|---|
| Decide whether to enable SerpAPI | WebSearch is currently an explicit unavailable observation and may be paid | `SERP_API_KEY` in ignored `.env`/secret manager | WebSearch only | One separately authorized call yields `source=live` and provider usage agrees |
| Decide whether hosted RXN is allowed as fallback | Local RXN works; hosted service is time-limited | `RXN4CHEM_API_KEY`, `RXN4CHEM_PROJECT_ID`, optional `RXN4CHEMISTRY_BASE_URL` | RXN tasks only | One separately authorized prediction returns a real provider ID/result |
| Keep excluded service keys absent unless protocol changes | ChemSpace procurement and hidden-model literature search are excluded | `CHEMSPACE_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY` | Does not block reduced profile | Inventory remains explicitly excluded |
| Fund the sealed paper judge key | The key authenticates, but available credits are below the frozen `$11.83266` worst-case ceiling | Add credit in OpenRouter; keep `OPENROUTER_API_KEY` only in `/home/lhy-h/work/chemcrowrun/.env.paper-evaluator` (mode 0600) | Paper-compatible evaluation only | Run `paper-credential-probe --no-model-calls`; require `status=VALID` and `sufficient_remaining_for_frozen_ceiling=true` |

Model role names and the Core-managed Codex subscription auth source are already detected. If auth expires, renew it through the normal Codex login flow on the host; do not copy auth contents into the repository. Verify with the zero-model preflight and one explicitly authorized Core canary.

## 2. Docker, GPU, and runtime services

After reboot, start local RXN:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
docker compose -f benchmarks/chemcrow/configs/rxn_sandbox.services.yaml up -d
```

Start the verified Evolution Backend:

```bash
/home/lhy-h/work/chemcrowrun/core-state/evolution-backend-venv/bin/python   -m openevo.evolution.cli serve   --host 127.0.0.1 --port 8200   --db /home/lhy-h/work/chemcrowrun/core-state/evolution/core.sqlite3   --artifact-root /home/lhy-h/work/chemcrowrun/core-state/evolution/artifacts   --framework-lock /home/lhy-h/work/chemcrowrun/core-state/framework/framework-lock.json
```

Start Rollout and Gateway in separate terminals:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
uv run python -m openevo.rollout.server   --config benchmarks/chemcrow/configs/openevo_core_topology.yaml --log-level info
```

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
uv run python -m openevo.gateway.server   --config benchmarks/chemcrow/configs/openevo_core_topology.yaml   --node-id chemcrow-core-gateway-01 --log-level info
```

Verify:

```bash
curl -fsS http://127.0.0.1:8200/v1/health
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/nodes
curl -fsS http://127.0.0.1:8100/sessions
curl -fsS http://127.0.0.1:8300/openapi.json
```

These services are currently running. GPU RXN is optional; CPU mode passed smoke and is frozen. Enabling the RTX 4050 6 GB profile requires a separate approved profile and both RXN canaries; it does not block current software readiness.

## 3. Deprecated or unavailable ChemCrow services

- Hosted RXN/API announces end-of-service on 2026-10-28. Choose local RXN (recommended) or accept a time-bounded hosted profile. This blocks only hosted RXN use.
- Legacy mutable `rxnpred`/retrosynthesis images are not reproducible and remain excluded.
- Paper-only restricted tools are absent from `chemcrow-public`; exact paper reproduction is impossible.
- LiteratureSearch, GetMoleculePrice, and python_repl remain excluded for fairness, benchmark-only safety, or reproducibility.

## 4. Tool licenses and accounts

1. Review `/home/lhy-h/work/chemcrowrun/vendor/rxn-sandbox/LICENSE.OpenMDW-1.1` and record acceptance/rejection for formal metrics. This blocks formal RXN-dependent scoring, not software smoke. Verify the decision references commit `d56aad22564a904a2fc737460adfbf0d4c4e9ac2` and the model hashes in `RXN_RUNTIME_MANIFEST.json`.
2. Review MolBloom/SureChEMBL package/data terms and record acceptance of version `2.3.5`. This affects PatentCheck-dependent claims.
3. Create/approve a SerpAPI account and quota only if WebSearch is selected. Verify with one authorized call and provider-side usage.

## 5. Model authentication and failed-preflight disposition

The paper evaluator defaults and API key are configured in the ignored 0600 file:
`OPENROUTER_BASE_URL`, `CHEMCROW_PAPER_EVALUATOR_MODEL`, and
`CHEMCROW_PAPER_EVALUATOR_MAX_USD`. The key is valid, but the zero-model credits probe reports
`VALID_INSUFFICIENT_CREDITS`. Keep
`CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION` empty until a complete 14-task preflight has passed.

After adding sufficient credit, validate it with the zero-model command documented in
`PAPER_EVALUATOR_READINESS.md`. It calls OpenRouter `GET /api/v1/key` and, when the key has no
spending limit, `GET /api/v1/credits`. It records no key, balance, limit, or usage values and emits
only the boolean capacity result for the frozen `$11.83266` ceiling.

Immediate decision required:

- `preflight-v1` already has five possible provider-effect phases for `chemcrow-02`.
- `preflight-v2` has one additional baseline provider effect for the same task.
- Neither is scientifically eligible, and no call was retried.

Choose one policy before another paid call:

1. **Authorize a deliberate fresh replacement** with a new experiment ID and an explicit exception allowing `chemcrow-02` to be called again after engineering invalidation. This preserves the original fixed three-task selection but creates another paid/provider effect.
2. **Close the preflight as incomplete** and do not repeat `chemcrow-02`. Running only `03/06` would be a new preregistered two-task diagnostic, not the original three-task preflight.
3. **Design an offline-only validation** from fixtures for engineering confidence. It cannot yield benchmark metrics.

Recommended scientific choice: do not silently resume or reuse either invalid run. If a fresh three-task replacement is desired, explicitly authorize the duplicate and a new `preflight-v3` receipt first. Verification must show zero reuse of v1/v2 run IDs and exactly one new phase sequence per selected item.

Also decide whether evolution and final evaluators may share the same model family with separate prompts/configs or must use distinct models/instances. This affects interpretation, not current code readiness.

## 6. Final human chemistry evaluation

For paper-comparable human evaluation, four independent expert chemists must review the sealed
offline packets. Each expert scores both blinded responses from 0 to 10 on `Chemically accurate`,
`Quality of reasoning`, and `Task completed`, then records A/B/tie preference, confidence, and
comments. Across 42 fixed comparisons this requires 168 completed response forms. Reviewers must
not see the private A/B mapping. A smaller panel is exploratory and blocks definitive paper-level
claims.

After the composite 14-task audit passes, generate the packets without model calls:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
uv run --project benchmarks/chemcrow openevo-chemcrow paper-human-review-prepare \
  --config benchmarks/chemcrow/configs/paper_human_review.composite-v1.yaml \
  --output /home/lhy-h/work/chemcrowrun/reports/PAPER_HUMAN_REVIEW_PREPARE.json \
  --no-model-calls
```

Verify it is fixed when the report says `READY_FOR_FOUR_EXPERT_REVIEWERS`, there are 42 blinded
packet files and 168 completed reviewer forms, and the private mapping remains mode 0600.

For `chemcrow-12`, separately approve a safety scoring policy that rewards intended safety behavior and never relabels a refusal as execution success.

## 7. Potentially paid operations

Potentially quota-consuming operations: Candidate, Reflector, evolution evaluator, final evaluator, SerpAPI, and hosted RXN. Local RXN has no per-call API fee.

No further paid command is authorized by this report. After choosing the failed-preflight policy, create a new immutable authorization receipt and config before execution. The full command exists in `READINESS_REPORT.md` but remains blocked.

The paper evaluator is a separate potential charge: 42 `openai/gpt-4` calls have a frozen
list-price ceiling of `$11.83266`. It requires both `--allow-paid` and
`CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION=I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS`. Verify it is fixed
only when the sealed-output preflight reports 42 calls and the exact plan hash; do not set the
authorization merely to test credentials.

## 8. Tasks that cannot currently run faithfully to the paper

`full-v3` is not a complete 14-task result: 12 pairs are sealed/reset, task 14 was interrupted, and
task 15 was never started. Without additional Candidate work, only a 12-task exploratory paper
evaluation is possible and it must not be reported as the official 14-task mean.

- `chemcrow-01/04/05/13`: price/procurement evidence requires the excluded price tool.
- `chemcrow-02/07/08/15`: literature/novelty depth is reduced because LiteratureSearch and restricted tools are absent.
- `chemcrow-10`: reaction prediction exists, but the original physical-property lookup surface is incomplete.
- `chemcrow-12`: safety-sensitive; requires approved safety scoring.
- `chemcrow-03/06/09/14`: local RXN is available but differs from paper-era hosted RXN.
- `chemcrow-safety-nitroglycerin`: non-scored demonstration only.

The valid claim is a public-source, tool-audited reduced ChemCrow environment, not an exact paper reproduction.

## 9. Decisions required before the full benchmark

1. Resolve the `chemcrow-02` duplicate/replacement policy above.
2. Approve or reject RXN-Sandbox OpenMDW 1.1 for formal metrics.
3. Freeze the formal tool profile: local RXN with WebSearch unavailable, or add SerpAPI.
4. Decide whether all 14 reduced-profile tasks are in scope or preregister a runnable subset before outcomes.
5. Freeze evaluator model/instance separation and a cost/quota ceiling.
6. Approve the `chemcrow-12` safety scoring policy.
7. Accept modern RDKit/RXN versions or request a historical compatibility study.
8. Approve blinded expert review and provisional-result wording.
9. Explicitly authorize the full run only after a valid bounded preflight is reviewed.
10. Choose how to close the paper-evaluator task inventory: (a) preregister a fresh isolated
    two-task repair for tasks 14 and 15, explicitly accepting a duplicate task-14 baseline caused
    by the interrupted pair, then create a composite 14-task audit; (b) run no more Candidate calls
    and accept a clearly labeled 12-task exploratory judge; or (c) authorize a fresh 14-task run.
    Option (a) is the smallest path compatible with a 14-task aggregate, but it requires explicit
    duplicate-call authorization and a passing composite audit before paper evaluation.

The composite audit and two-task repair configuration are now implemented. The first 12 pairs pass
their sealed-subset audit and will not be rerun. To choose option (a), explicitly authorize the
literal `I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP`. That authorization will be written to
`/home/lhy-h/work/chemcrowrun/manifests/CHEMCROW_14_DUPLICATE_AUTHORIZATION.json`, bound to prior
claim SHA256 `bfa5d6f880bb0f8b1f0149ceef364c51e2a0fe2e290734e0ca3827078fcae40b`. Until then the file
must remain absent and both repair preflight and composite audit remain blocked.

How to verify readiness after decisions:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
uv run openevo-chemcrow preflight   --config configs/preflight.v2.yaml   --no-model-calls   --output /home/lhy-h/work/chemcrowrun/runs/<new-run-id>/preflight.json
```

Do not use `preflight.v2.yaml` for a paid retry because its experiment ID and claims are already invalidated. Create a new frozen config/authorization receipt after the human policy decision.
