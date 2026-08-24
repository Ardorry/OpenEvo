# HUMAN ACTION REQUIRED

Current gate: **BLOCKED BEFORE MODEL CALLS**. Preparation used zero Candidate, Reflector,
evolution-evaluator, or final-evaluator calls and performed no paid operation. Never paste a
secret into chat, a command history, a report, or Git; load it from an ignored `.env`, a secret
manager, or the existing OpenEvo service environment.

## 1. Credentials and API keys

| Action | Why | Exact variable/command | Scope | Verification |
|---|---|---|---|---|
| Bind the OpenEvo rollout endpoint and four model roles | Candidate, one native Reflector step, evolution feedback, and blinded final judgment all need an admitted provider route | Set `OPENEVO_ROLLOUT_BASE_URL`, `OPENEVO_CANDIDATE_MODEL`, `OPENEVO_REFLECTOR_MODEL`, `OPENEVO_EVOLUTION_EVALUATOR_MODEL`, and `OPENEVO_FINAL_EVALUATOR_MODEL` in a private environment | Blocks every paid task pair | `uv run openevo-chemcrow preflight --config configs/preflight.yaml --no-model-calls` reports all five names present; values are never printed |
| Configure hosted IBM RXN, if that path is selected | Hosted forward prediction and retrosynthesis require account/project authority | Set `RXN4CHEM_API_KEY`, `RXN4CHEM_PROJECT_ID`, and optionally `RXN4CHEMISTRY_BASE_URL`; do not use the hard-coded project ID in the vendor source | Blocks only tasks that require RXN | Use a separate, explicitly authorized single tool probe and require a real prediction ID/result; never accept a stub |
| Configure SerpAPI only if WebSearch remains in the formal profile | `WebSearch` is credentialed and may incur usage | Set `SERP_API_KEY` privately | Blocks WebSearch only | Run a one-query tool canary and confirm `source=live`, no error, and account usage matches one query |
| ChemSpace and Semantic Scholar | ChemSpace price/procurement is excluded; legacy LiteratureSearch is excluded because it adds hidden model calls | No action for the initial profile. Variables are `CHEMSPACE_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY` if a later approved profile is designed | Does not block the initial profile | Preflight inventory continues to mark `GetMoleculePrice` and `LiteratureSearch` excluded |

## 2. Docker, GPU, and runtime services

| Action | Why | Exact variable/command | Scope | Verification |
|---|---|---|---|---|
| Bind an immutable Candidate runtime image | `configs/*.yaml` deliberately contains `HUMAN_ACTION_REQUIRED_IMMUTABLE_RUNTIME_IMAGE`; a mutable tag is not acceptable for paired fairness | Copy `configs/full.example.yaml` to ignored/local `configs/full.yaml`, replace the placeholder with a digest-qualified image such as `registry/repo@sha256:...` | Blocks all Candidate tasks | `docker image inspect <digest-qualified-image>` succeeds and preflight reports `candidate_runtime_image_bound=true` |
| Start/verify the existing OpenEvo rollout and Gateway services | The adapter submits native `TaskRequest` objects and must not fall back to host `codex exec` | Start services with the checkout's approved deployment procedure, then run `curl -fsS "$OPENEVO_ROLLOUT_BASE_URL/health"` | Blocks all Candidate/evaluator calls | Health reports a registered, schedulable Gateway; a no-model admission probe succeeds |
| Validate GPU-in-container only if GPU RXN is selected | Host RTX 4050 and Docker `nvidia` runtime were detected, but no CUDA image was pulled for a container canary | After choosing a pinned image already approved for download: `docker run --rm --gpus all <pinned-cuda-image> nvidia-smi` | Blocks GPU RXN only | The container reports the GPU and exits 0 |
| Decide whether to deploy official RXN-Sandbox | Official self-hosting requires Docker/Compose, Git LFS, about 10 GB free disk, 8 GB RAM minimum and 16 GB recommended for tree search | Only after approving the substantial download: `git clone https://github.com/rxn4chemistry/rxn-sandbox.git vendor/rxn-sandbox`; then follow its pinned `compose.yaml` or `compose-cuda.yaml` build/up commands | Blocks local RXN only | Pin commit `d56aad22564a904a2fc737460adfbf0d4c4e9ac2`, record model hashes/license, run one forward and one retrospective canary, and bind `CHEMCROW_RXN_PREDICT_URL`/`CHEMCROW_RXN_RETRO_URL` |

## 3. Deprecated or unavailable ChemCrow services

- IBM's official `rxn4chemistry` README says hosted RXN and its APIs reach end of service on
  **2026-10-28**. Select hosted RXN only as a time-bounded profile and preserve an exit plan.
  This affects RXN-dependent tasks, not local RDKit tasks. Verify the current notice at
  <https://github.com/rxn4chemistry/rxn4chemistry>.
- The legacy `doncamilom/rxnpred:latest` manifest was not found and
  `doncamilom/retrosynthesis:latest` returned registry authorization denial. They are mutable,
  unpinned, and must not be used as formal infrastructure. Verify with
  `docker manifest inspect --verbose <image>`; success alone is insufficient without a digest.
- The official ChemCrow README states that some paper tools are absent because of API usage
  restrictions. Those capabilities cannot be reconstructed faithfully from the public repo.
  Verify at <https://github.com/ur-whitelab/chemcrow-public>.

## 4. Tool licenses and accounts

- Review and approve RXN-Sandbox's `LICENSE.OpenMDW-1.1` and model/data terms before cloning its
  Git LFS assets. This blocks local RXN only. Verification is a recorded license decision plus
  hashes for the exact model files.
- If SerpAPI is enabled, create/approve the account and quota outside Git. This blocks WebSearch
  only. Verify with a one-query canary and provider-side usage.
- ChemSpace purchasing/price lookup remains excluded by the software-benchmark and
  no-procurement boundary. Do not create an account merely to unblock this benchmark.

## 5. Model authentication

- Authenticate Candidate, Reflector, evolution evaluator, and final evaluator through the
  existing OpenEvo managed runtime/provider path. Do not place provider credentials in
  Candidate-visible MCP configuration or trajectory metadata.
- Verify authentication with the existing zero/one-call managed canary and confirm Candidate
  and Reflector receive no evaluator-only credentials. This blocks all model phases.
- Decide whether evolution and final evaluators use distinct models or at least distinct
  independently frozen configurations. The code enforces separate roles/prompts/config IDs but
  cannot choose the scientific design.

## 6. Final human chemistry evaluation

- After a bounded preflight, a chemistry expert must review `runs/<run>/human_review/*.blinded.json`
  without access to the A/B mapping. This is required before any LLM-judged delta is called a
  scientific result. It does not block software smoke tests but blocks definitive reporting.
- Record rubric scores for chemical correctness, reasoning quality, and task completion, plus
  adjudication/conflicts. Do not relabel a safety refusal as execution success.

## 7. Potentially paid operations

- Every Candidate, Reflector, evolution-evaluator, final-evaluator, SerpAPI, hosted RXN, or
  paid literature request is potentially billable. The launcher requires both `--allow-paid`
  and `CHEMCROW_FULL_RUN_AUTHORIZATION=I_UNDERSTAND_THIS_MAY_INCUR_COST`.
- Authorize the fixed three-item preflight before the full set. The preselected IDs are
  `chemcrow-03`, `chemcrow-06`, and `chemcrow-12`; do not change them after seeing outcomes.
- Verify the gate by running without either authority: it must stop before dispatch. Provider-side
  usage should remain zero.

## 8. Tasks that cannot currently run faithfully

- `chemcrow-01`, `02`, `04`, `05`, `07`, `08`, `09`, `10`, `13`, `14`, and `15` may require
  reaction prediction, retrosynthesis, literature/web evidence, or synthesis-planning services
  to approximate the original tool-rich setting. They are blocked or materially limited until
  the RXN/search profile is chosen and canaried.
- `chemcrow-03` and `chemcrow-06` are lower-infrastructure mechanism/reasoning tasks;
  `chemcrow-12` has a local RDKit similarity path. They are the preregistered preflight set, but
  still need model runtime/authentication.
- `chemcrow-safety-nitroglycerin` is a separate, non-scored safety demonstration and is not in
  `tasks.jsonl`. Its intended safety behavior must be reviewed separately.
- `LiteratureSearch`, `python_repl`, and `GetMoleculePrice` are intentionally unavailable in the
  initial real profile. No silent fake result is permitted.

## 9. Decisions required before the full benchmark

1. Choose hosted RXN (time-limited), official RXN-Sandbox (substantial download/license), or a
   reduced open-source profile. Record the choice as part of the benchmark identity.
2. Choose and freeze Candidate, Reflector, evolution-evaluator, and final-evaluator models,
   temperatures, timeouts, and provider authentication.
3. Decide whether WebSearch is in scope and whether the legacy hidden-model LiteratureSearch
   remains excluded (recommended).
4. Decide whether formal claims target the 14 public scored notebooks or only a reproducible
   open-source subset; neither is automatically comparable to the paper.
5. Approve the fixed 3-task paid preflight only after preflight reports `READY` and all important
   tools fail explicitly or pass their canaries.
6. Approve a human-review plan and the wording `PROVISIONAL LLM-JUDGED RESULT` for interim output.
7. Decide whether the locked modern RDKit 2025.9.6 profile is accepted or whether a separate
   historically closer environment must be built and validated.
