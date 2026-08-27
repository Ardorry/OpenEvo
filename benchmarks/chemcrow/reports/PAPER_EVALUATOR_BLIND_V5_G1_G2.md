# Balanced Blind GPT-4 Comparison: full-v5 G1 vs G2

This project-added comparison uses the frozen calibrated ChemCrow-compatible judge. The judge received only the task and Student A/B answers.

- status: `PASS`
- calls: `14/14`
- G1 as A / G2 as A: `7/7`
- randomization seed: `20260827`
- mean G1: `9.143`
- mean G2: `9.286`
- mean G2 - G1: `+0.143`
- G2 wins/ties/G1 wins: `3/9/2`
- bootstrap 95% CI: `[-0.10714285714285714, 0.42857142857142855]`
- exact sign-test p: `1.000000`
- proven cost: `$1.00695`
- immutable prior ledgers unchanged: `True`
- Student A wins/ties/Student B wins: `4/9/1`

| task | G1 pos | G1 | G2 pos | G2 | G2 - G1 | winner |
|---|---|---:|---|---:|---:|---|
| chemcrow-01 | A | 9.0 | B | 8.5 | -0.5 | g1 |
| chemcrow-02 | A | 9.0 | B | 9.0 | +0.0 | tie |
| chemcrow-03 | B | 10.0 | A | 10.0 | +0.0 | tie |
| chemcrow-04 | B | 9.0 | A | 9.0 | +0.0 | tie |
| chemcrow-05 | A | 9.0 | B | 8.5 | -0.5 | g1 |
| chemcrow-06 | A | 10.0 | B | 10.0 | +0.0 | tie |
| chemcrow-07 | B | 8.0 | A | 9.0 | +1.0 | g2 |
| chemcrow-08 | A | 9.0 | B | 10.0 | +1.0 | g2 |
| chemcrow-09 | B | 9.0 | A | 9.0 | +0.0 | tie |
| chemcrow-10 | B | 9.0 | A | 9.0 | +0.0 | tie |
| chemcrow-12 | B | 10.0 | A | 10.0 | +0.0 | tie |
| chemcrow-13 | A | 9.0 | B | 9.0 | +0.0 | tie |
| chemcrow-14 | B | 9.0 | A | 10.0 | +1.0 | g2 |
| chemcrow-15 | A | 9.0 | B | 9.0 | +0.0 | tie |

Position diagnostic: Order was balanced across tasks, but Student A won 4 of the 5 non-ties; with N=14 and no within-task reversal, residual position/context effects cannot be separated from task composition.

Limitation: One call per task and one answer order per task. Order is randomized and exactly balanced across tasks, but no within-task A/B reversal replicate was run; N=14 and the GPT-4 judge is stochastic with discrete grades.
