# Two-Order Blind GPT-4 Comparison: full-v5 G1 vs G2

Each task was judged twice with Student A/B exactly reversed in the second round.

- calls: `28/28`
- two-order mean G1: `9.071`
- two-order mean G2: `9.232`
- two-order mean G2 - G1: `+0.161`
- G2 wins/ties/G1 wins after per-task order averaging: `6/7/1`
- bootstrap 95% CI: `[0.017857142857142856, 0.32142857142857145]`
- exact sign-test p: `0.125000`
- cross-order preference agreement: `7/14`
- Student A wins/ties/Student B wins across 28 calls: `11/16/1`
- Student A - B mean grade: `+0.232`
- proven two-round cost: `$1.99830`

| task | R1 G1 pos | R1 delta | R2 G1 pos | R2 delta | two-order G1 | two-order G2 | mean delta | result |
|---|---|---:|---|---:|---:|---:|---:|---|
| chemcrow-01 | A | -0.5 | B | +0.5 | 8.75 | 8.75 | +0.00 | tie |
| chemcrow-02 | A | +0.0 | B | +1.0 | 8.50 | 9.00 | +0.50 | g2 |
| chemcrow-03 | B | +0.0 | A | +0.0 | 10.00 | 10.00 | +0.00 | tie |
| chemcrow-04 | B | +0.0 | A | +0.0 | 9.00 | 9.00 | +0.00 | tie |
| chemcrow-05 | A | -0.5 | B | +0.0 | 9.00 | 8.75 | -0.25 | g1 |
| chemcrow-06 | A | +0.0 | B | +0.0 | 10.00 | 10.00 | +0.00 | tie |
| chemcrow-07 | B | +1.0 | A | -0.5 | 8.50 | 8.75 | +0.25 | g2 |
| chemcrow-08 | A | +1.0 | B | +1.0 | 8.50 | 9.50 | +1.00 | g2 |
| chemcrow-09 | B | +0.0 | A | +0.0 | 9.00 | 9.00 | +0.00 | tie |
| chemcrow-10 | B | +0.0 | A | +0.0 | 9.00 | 9.00 | +0.00 | tie |
| chemcrow-12 | B | +0.0 | A | +0.0 | 10.00 | 10.00 | +0.00 | tie |
| chemcrow-13 | A | +0.0 | B | +0.5 | 8.75 | 9.00 | +0.25 | g2 |
| chemcrow-14 | B | +1.0 | A | -0.5 | 9.25 | 9.50 | +0.25 | g2 |
| chemcrow-15 | A | +0.0 | B | +0.5 | 8.75 | 9.00 | +0.25 | g2 |

Interpretation: Averaging the two exact opposite answer orders gives the order-balanced descriptive G2-G1 estimate. Strong Student-A preference and only partial cross-order preference agreement show that position/context sensitivity is material; N=14 and one call per order remain too small for a definitive effect claim.
