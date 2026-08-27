# Reversed Blind GPT-4 Comparison: full-v5 G1 vs G2

This project-added comparison uses the frozen calibrated ChemCrow-compatible judge. The judge received only the task and Student A/B answers.

- status: `PASS`
- calls: `14/14`
- G1 as A / G2 as A: `7/7`
- randomization seed: `20260827`
- mean G1: `9.000`
- mean G2: `9.179`
- mean G2 - G1: `+0.179`
- G2 wins/ties/G1 wins: `5/7/2`
- bootstrap 95% CI: `[-0.03571428571428571, 0.42857142857142855]`
- exact sign-test p: `0.453125`
- proven cost: `$0.99135`
- immutable prior ledgers unchanged: `True`
- Student A wins/ties/Student B wins: `7/7/0`

| task | G1 pos | G1 | G2 pos | G2 | G2 - G1 | winner |
|---|---|---:|---|---:|---:|---|
| chemcrow-01 | B | 8.5 | A | 9.0 | +0.5 | g2 |
| chemcrow-02 | B | 8.0 | A | 9.0 | +1.0 | g2 |
| chemcrow-03 | A | 10.0 | B | 10.0 | +0.0 | tie |
| chemcrow-04 | A | 9.0 | B | 9.0 | +0.0 | tie |
| chemcrow-05 | B | 9.0 | A | 9.0 | +0.0 | tie |
| chemcrow-06 | B | 10.0 | A | 10.0 | +0.0 | tie |
| chemcrow-07 | A | 9.0 | B | 8.5 | -0.5 | g1 |
| chemcrow-08 | B | 8.0 | A | 9.0 | +1.0 | g2 |
| chemcrow-09 | A | 9.0 | B | 9.0 | +0.0 | tie |
| chemcrow-10 | A | 9.0 | B | 9.0 | +0.0 | tie |
| chemcrow-12 | A | 10.0 | B | 10.0 | +0.0 | tie |
| chemcrow-13 | B | 8.5 | A | 9.0 | +0.5 | g2 |
| chemcrow-14 | A | 9.5 | B | 9.0 | -0.5 | g1 |
| chemcrow-15 | B | 8.5 | A | 9.0 | +0.5 | g2 |

Position diagnostic: Student A won 7 of 7 non-ties in this round. Use the exact reversed round and the paired two-round analysis to distinguish position sensitivity from task composition.

Limitation: This is the exact A/B-reversed replicate of round one. Each round remains one stochastic GPT-4 call per task; order effects should be interpreted from the paired two-round analysis rather than either round alone.
