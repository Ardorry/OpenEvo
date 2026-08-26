# ChemCrow EvaluatorGPT-Compatible Historical Calibration

## Prompt recovery

- `EXACT_PROMPT_RECOVERED=false`
- authoritative search result: no verbatim implementation recovered
- prompt uncertainty remains; see `PAPER_EVALUATOR_PROMPT_ARCHAEOLOGY.md`

## Historical dataset

- task count: 14
- labeled task count: 14
- missing tasks: none
- source commit: `500104ed9a5d479a8dc4128afc463625ade5a409`
- dataset SHA256: `148ddc2bd38dc3c49a01d01cd9cc99035cb0d70c085c7c2f3e738b378c813de0`

## Candidate prompts

Exactly three candidates and their hashes were frozen before paid results existed. Full text
and paper-grounding evidence are in [PAPER_EVALUATOR_PROMPT_CANDIDATES.json](PAPER_EVALUATOR_PROMPT_CANDIDATES.json).

## Results

| candidate | MAE | RMSE | bias | Pearson | Spearman | pair agreement | delta MAE | delta RMSE | delta Spearman | parse failures | schema failures | provider failures | proven cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_V1 | 0.886 | 1.251 | 0.421 | 0.726 | 0.784 | 0.929 | 0.800 | 1.501 | 0.873 | 0 | 0 | 0 | $0.71976 |
| PAPER_MINIMAL | 0.732 | 1.228 | 0.304 | 0.738 | 0.758 | 0.929 | 0.750 | 1.518 | 0.817 | 0 | 0 | 0 | $0.67224 |
| PAPER_OUTPUT_FAITHFUL | 0.768 | 1.243 | 0.339 | 0.704 | 0.696 | 0.857 | 0.821 | 1.631 | 0.649 | 0 | 0 | 0 | $0.67050 |

Proven OpenRouter-reported cost: `$2.307120` across 48 paid calls.
One already-successful first call required local result-seal recovery after a Rollout-envelope
adapter bug; its existing HTTP 200 receipt and Core trajectory were reused with no provider
redispatch. The regression is covered by the test suite.

## Selection

Selected `PAPER_MINIMAL` by the preregistered lexicographic rule: pairwise preference agreement,
delta MAE, absolute-score MAE, Spearman, parse/schema/provider reliability, then simplicity.
The complete ranking and numeric selection keys are preserved in
`PAPER_EVALUATOR_CALIBRATION_RESULTS.json`.

- LOO folds won: `{'CURRENT_V1': 1, 'PAPER_MINIMAL': 12, 'PAPER_OUTPUT_FAITHFUL': 1}`
- LOO held-out pairwise agreement: 0.857
- LOO held-out score error: 0.857
- LOO held-out delta error: 1.000
- bootstrap seed/samples: 20260826 / 10000

| selected-prompt bootstrap metric | 95% CI lower | 95% CI upper |
|---|---:|---:|
| score_mae | 0.339 | 1.196 |
| pairwise_preference_agreement | 0.786 | 1.000 |
| delta_mae | 0.179 | 1.536 |
| score_spearman | 0.468 | 0.938 |

## Drift interpretation

- Prompt uncertainty: nonzero because the authoritative exact prompt was not recovered.
- Model/API drift: inseparable from prompt uncertainty in the compatible-prompt branch.
- Judge stochasticity: all three task preferences were stable across the primary call and two
  additional repetitions.

| task | Student A grade range | Student B grade range | delta range | preference stable |
|---|---|---|---|---:|
| chemcrow-02 | [7.0, 7.0] | [9.0, 9.0] | [-2.0, -2.0] | true |
| chemcrow-06 | [9.0, 9.0] | [10.0, 10.0] | [-1.0, -1.0] | true |
| chemcrow-12 | [10.0, 10.0] | [10.0, 10.0] | [0.0, 0.0] | true |

Repeat calls are reported separately and are not averaged into primary calibration metrics.

## Final label

`CALIBRATED_COMPATIBLE_HIGH`

Project diagnostic agreement category: `HIGH`. Even HIGH agreement would not prove
exact prompt recovery. Historical controls must report original and modern judge values before
cross-era absolute-score interpretation.

Selected score MAE 0.732; pairwise agreement 0.929; delta MAE 0.750.
