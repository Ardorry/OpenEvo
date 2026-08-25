# HUMAN ACTION REQUIRED

Current gate: **no foundation-model call has been authorized or executed**. Downloads, local
environments, OpenEvo Core services, immutable managed image binding, and local RXN setup are
complete. Never paste secrets into chat or Git.

## 1. Credentials and API keys

| Remaining action | Why | Exact variable/command | Scope | Verify |
|---|---|---|---|---|
| Decide whether WebSearch is formal | SerpAPI may be paid and changes the tool profile | If approved, set `SERP_API_KEY` in ignored `.env` or a secret manager | WebSearch only | One authorized query returns `source=live`; provider usage shows one call |
| Decide whether hosted RXN is a fallback | Local RXN now works; hosted RXN is optional and time-limited | If approved, set `RXN4CHEM_API_KEY`, `RXN4CHEM_PROJECT_ID`, optionally `RXN4CHEMISTRY_BASE_URL` | RXN tasks only | One separately authorized prediction returns a real ID/result |
| No ChemSpace/Semantic Scholar action for initial profile | Price procurement and legacy LiteratureSearch are excluded | Names are `CHEMSPACE_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY`; leave absent unless a new protocol is approved | Does not block current reduced profile | Inventory remains explicitly excluded |

The OpenEvo endpoint/model role names and a Core-managed Codex subscription credential source are
already detected. No credential value was inspected.

## 2. Docker, GPU, and runtime services

No setup action currently blocks CPU RXN or Core health. After a reboot, run:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo
docker compose -f benchmarks/chemcrow/configs/rxn_sandbox.services.yaml up -d
```

Start Rollout and Gateway using the exact commands in `READINESS_REPORT.md`. Verify:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/nodes
curl -fsS http://127.0.0.1:8300/openapi.json
```

Optional decision: enable GPU RXN. The RTX 4050 has only 6 GB VRAM; CPU mode is currently frozen
and passed smoke. GPU is not needed for the preflight. If a later GPU profile is approved, add a
separate Compose profile and verify `docker run --rm --gpus all <pinned-image> nvidia-smi` plus
both RXN canaries. This blocks only the optional GPU profile.

## 3. Deprecated or unavailable services

- Hosted RXN/API end-of-service is announced for **2026-10-28**. Human decision: use the local
  profile (recommended) or accept a time-bounded hosted profile. Verify the current notice in the
  official `rxn4chemistry` repository. This affects RXN tasks only.
- Legacy ChemCrow `rxnpred`/`retrosynthesis` images are mutable/unavailable and must remain
  excluded. No human action can make them a reproducible authority without exact historical
  digests.
- Paper-only restricted tools are absent from `chemcrow-public`. This blocks exact paper
  reproduction, not the declared public-source benchmark profile.

## 4. Tool licenses and accounts

| Action | Why | Command/file | Scope | Verify |
|---|---|---|---|---|
| Review and record acceptance of RXN-Sandbox OpenMDW 1.1 | Model/software use is governed by the repository license; download authorization is not a scientific license decision | Read `/home/lhy-h/work/chemcrowrun/vendor/rxn-sandbox/LICENSE.OpenMDW-1.1` and record the decision outside secrets | Blocks RXN use in formal metrics, not software smoke | Decision references commit `d56aad225...` and model hashes in `RXN_RUNTIME_MANIFEST.json` |
| Accept the MolBloom/SureChEMBL data/version | PatentCheck uses packaged probabilistic data | Review the installed MolBloom package/data terms | PatentCheck-dependent claims only | Record package `2.3.5` and one known canary |
| SerpAPI account/quota, only if selected | May be paid and drift over time | Configure `SERP_API_KEY` privately | WebSearch only | One-call canary and provider-side usage agree |

ChemSpace procurement remains excluded; do not create an account merely for this benchmark.

## 5. Model authentication and role design

- Human authorization is required for the first live OpenEvo Core model canary. It may consume
  subscription/provider quota. Do not run host `codex exec`; the only allowed route is the Core
  Rollout/Gateway managed Docker path.
- Decide whether the evolution evaluator and final evaluator may both use the currently frozen
  model name with separate prompts/configs, or must use distinct model instances/models. This
  blocks scientific interpretation, not code readiness.
- Verify the first authorized call by checking Core receipts: execution route is
  `openevo_core_rollout_gateway_managed_codex_v1`, the runtime image ID matches the report, and no
  host Codex process handled the task.

## 6. Final human chemistry evaluation

After the bounded paid preflight, a chemistry expert must review the blinded packets under
`runs/<run>/human_review/` without A/B identity. Record chemical correctness, reasoning quality,
task completion, winner/tie, confidence, and adjudication. This blocks definitive benchmark
claims. Until then every judge result must be labeled **PROVISIONAL LLM-JUDGED RESULT**.

For `chemcrow-12`, separately verify that preserved safety behavior—not operational assistance—is
scored correctly. A safety refusal must never be relabeled as execution success.

## 7. Potentially paid operations

Potentially billable/quota-consuming actions include Candidate, Reflector, evolution evaluator,
final evaluator, SerpAPI, and hosted RXN. The local RXN service itself has no per-call API charge.

Authorize only the fixed three-item model preflight first:

```bash
cd /home/lhy-h/work/chemcrowrun/openevo/benchmarks/chemcrow
set -a
source .env
set +a
export CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST
uv run openevo-chemcrow run --config configs/preflight.yaml --allow-paid
```

This runs `chemcrow-02`, `chemcrow-03`, `chemcrow-06`, fixed before any model outcome. Explicit
human approval of this paid command is still required. Absence of either `--allow-paid` or the
authorization variable fails before dispatch.

## 8. Tasks not currently faithful to the original paper setting

- `chemcrow-01`, `04`, `05`, `13`: original prompts require price/procurement evidence;
  `GetMoleculePrice` is intentionally excluded. They can run only as a declared reduced profile.
- `chemcrow-02`, `07`, `08`, `15`: literature/novelty/synthesis depth is reduced because legacy
  LiteratureSearch and restricted paper tools are absent. RXN single-step evidence is available.
- `chemcrow-10`: reaction prediction is available, but the original physical-property lookup
  surface is not faithfully reproduced.
- `chemcrow-12`: safety-sensitive scored task. It must be handled under a separately approved
  safety scoring policy and is excluded from the paid preflight.
- `chemcrow-03`, `06`, `09`: local forward prediction is available, but uses Pistachio2025Q2,
  not paper-era hosted RXN.
- `chemcrow-14`: PubChem safety evidence and local RXN are available, but synthesis planning is
  not paper-identical.
- `chemcrow-safety-nitroglycerin`: separate non-scored demonstration; never include in aggregate
  scored metrics.

Thus no claim should say “exact ChemCrow paper reproduction.” The faithful claim is a
public-source, tool-audited ChemCrow environment with documented reductions.

## 9. Decisions required before the full benchmark

1. Approve or reject RXN-Sandbox OpenMDW 1.1 for formal metrics.
2. Approve the fixed three-task paid Core preflight command above.
3. Choose the formal tool profile: local RXN plus no WebSearch, or add SerpAPI; keep excluded tools
   explicit.
4. Decide whether the full claim covers all 14 reduced-profile items or a preregistered runnable
   subset.
5. Freeze evaluator role/model separation and acceptable subscription/provider cost limits.
6. Approve the `chemcrow-12` safety scoring policy.
7. Accept the modern RDKit/RXN versions or request a separate historical-compatibility study.
8. Approve blinded expert review and the provisional-result wording.
9. Only after reviewing preflight receipts/results, explicitly authorize the full 14-item run.
