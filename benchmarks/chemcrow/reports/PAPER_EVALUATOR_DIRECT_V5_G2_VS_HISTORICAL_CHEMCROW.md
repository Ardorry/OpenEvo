# Direct GPT-4 Comparison: Historical ChemCrow vs full-v5 G2

This is a project-added, same-call comparison using the frozen calibrated ChemCrow-compatible judge. It is not an original-paper metric.

- status: `PASS`
- calls: `14/14`
- historical ChemCrow mean: `7.643`
- full-v5 G2 mean: `8.679`
- G2 minus historical ChemCrow: `+1.036`
- G2 wins/ties/historical wins: `9/1/4`
- paired bootstrap 95% CI: `[-0.14285714285714285, 2.25]`
- exact sign-test p: `0.266846`
- proven cost: `$0.84360`
- answer order: historical ChemCrow = Student A; full-v5 G2 = Student B
- original 42-call ledger unchanged: `True`

| task | historical ChemCrow | full-v5 G2 | G2 - historical | winner |
|---|---:|---:|---:|---|
| chemcrow-01 | 7.0 | 6.0 | -1.0 | historical_chemcrow |
| chemcrow-02 | 7.0 | 9.0 | +2.0 | v5_g2 |
| chemcrow-03 | 9.0 | 10.0 | +1.0 | v5_g2 |
| chemcrow-04 | 9.0 | 7.0 | -2.0 | historical_chemcrow |
| chemcrow-05 | 8.0 | 6.0 | -2.0 | historical_chemcrow |
| chemcrow-06 | 9.0 | 10.0 | +1.0 | v5_g2 |
| chemcrow-07 | 8.0 | 9.0 | +1.0 | v5_g2 |
| chemcrow-08 | 8.0 | 9.5 | +1.5 | v5_g2 |
| chemcrow-09 | 7.0 | 10.0 | +3.0 | v5_g2 |
| chemcrow-10 | 4.0 | 9.0 | +5.0 | v5_g2 |
| chemcrow-12 | 10.0 | 10.0 | +0.0 | tie |
| chemcrow-13 | 8.0 | 6.0 | -2.0 | historical_chemcrow |
| chemcrow-14 | 5.0 | 10.0 | +5.0 | v5_g2 |
| chemcrow-15 | 8.0 | 10.0 | +2.0 | v5_g2 |

Limitation: One GPT-4 call per task with fixed answer order (historical ChemCrow as Student A, v5 G2 as Student B); N=14, discrete grades, stochastic judge, and no answer-order reversal replicate.
