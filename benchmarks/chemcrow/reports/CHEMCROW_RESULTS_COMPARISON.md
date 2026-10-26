# ChemCrow 14-task results

This table compares the published ChemCrow baselines, the earlier OpenEvo G1/G2 run, and the
later ChemCrowRun reproduction. Scores remain on their original scales.

## How to read the run names

**OpenEvo G1/G2** is the earlier run with `gpt-5.5`. **ChemCrowRun Reproduction** is the later
run with `gpt-5.6-sol`. Within each run, G1 is the answer produced before evolution and G2 is the
answer produced after injecting three task-local Reflector artifacts. “Reproduction G1” and
“Reproduction G2” are therefore two stages of the same reproduction run, not two reproductions.

## Evaluation protocols

- **Paired blind internal judge `/12`**: for each task, G1 and G2 are randomly assigned to A/B and
  sent together in one judge call. The judge separately scores both answers from 0–4 on chemical
  correctness, reasoning quality and task completion. The A/B scores are then mapped back to G1
  and G2, so one paired evaluation produces both columns.
- **GPT-4-based evaluation `/10`**: G1 and G2 are evaluated in separate calls. In each call the
  OpenEvo answer is Student A and the same historical no-tool GPT-4 answer is Student B. The table
  reports Student A's grade. This is a historically calibrated compatible evaluator, not a direct
  G1-versus-G2 call and not an official leaderboard score.

## Results

| System | Candidate model | Stage | Human evaluation /10 | GPT-4-based evaluation /10 | Paired blind internal judge /12 | Judge cost |
|---|---|---|---:|---:|---:|---:|
| Official GPT-4 | GPT-4 | No-tool baseline | **7.696** | **8.750** | — | Not reported |
| Official ChemCrow | GPT-4 | Tool-augmented | **8.589** | **7.357** | — | Not reported |
| OpenEvo G1 | **gpt-5.5** | Before evolution | — | **9.357** | **9.929** | G1/G2 combined: **$2.65887** |
| OpenEvo G2 | **gpt-5.5** | After three Reflectors | — | **9.214** | **10.286** | Included above |
| ChemCrowRun Reproduction G1 | **gpt-5.6-sol** | Before evolution | — | **9.607** | **10.629** | G1/G2 combined: **$2.99460** |
| ChemCrowRun Reproduction G2 | **gpt-5.6-sol** | After three Reflectors | — | **9.571** | **11.014** | Included above |

## G1 to G2 change

| Run | Internal judge G1 | Internal judge G2 | Internal change | GPT-4 judge G1 | GPT-4 judge G2 | GPT-4 change |
|---|---:|---:|---:|---:|---:|---:|
| OpenEvo | 9.929 | 10.286 | **+0.357** | 9.357 | 9.214 | -0.143 |
| ChemCrowRun Reproduction | **10.629** | **11.014** | **+0.386** | **9.607** | **9.571** | -0.036 |

## Per-task calibrated GPT-4 evaluation

Each G1 and G2 value below comes from a separate fixed-reference judge call as described above.

| Task | Official ChemCrow re-evaluation | OpenEvo G1 | OpenEvo G2 | Reproduction G1 | Reproduction G2 |
|---|---:|---:|---:|---:|---:|
| 01 Safinamide | 7.0 | 9.0 | 9.0 | 9.5 | 9.0 |
| 02 Discover catalyst | 7.0 | 9.0 | 9.0 | 9.0 | 10.0 |
| 03 Explain mechanism | 9.0 | 10.0 | 10.0 | 10.0 | 10.0 |
| 04 Insect repellent | 9.0 | 9.0 | 8.0 | 9.0 | 9.0 |
| 05 How to make | 7.0 | 9.0 | 9.0 | 10.0 | 10.0 |
| 06 Lindlar mechanism | 9.0 | 10.0 | 9.0 | 10.0 | 10.0 |
| 07 Modify and synthesize | 9.0 | 9.0 | 9.0 | 10.0 | 9.0 |
| 08 Ricci organocatalyst | 7.0 | 10.0 | 10.0 | 9.0 | 10.0 |
| 09 Predict incompatibility | 9.0 | 9.0 | 9.0 | 10.0 | 10.0 |
| 10 Product property | 6.0 | 9.0 | 8.0 | 9.0 | 9.0 |
| 12 Similar mechanism of action | 10.0 | 10.0 | 10.0 | 10.0 | 10.0 |
| 13 Atorvastatin synthesis | 8.0 | 9.0 | 9.0 | 9.0 | 9.0 |
| 14 Aspirin synthesis | 3.0 | 10.0 | 10.0 | 10.0 | 9.0 |
| 15 Takemoto catalyst | 7.0 | 9.0 | 10.0 | 10.0 | 10.0 |
| **Mean** | **7.643** | **9.357** | **9.214** | **9.607** | **9.571** |

## Interpretation

The reproduction records higher absolute G1 and G2 averages than the earlier OpenEvo run, but
the candidate model versions differ, so that cross-run comparison is descriptive. Within both
runs, the paired internal judge gives G2 a slightly higher mean, while the calibrated GPT-4 judge
gives G2 a slightly lower mean. For the reproduction, both paired confidence intervals include
zero and the tests do not establish a statistically significant G2 improvement. These results
therefore show mixed evaluator evidence and do not by themselves prove that the evolution
artifacts improve performance.

**Pass@n** is not a primary metric for this benchmark because the tasks use continuous expert
and judge scores rather than binary executable tests.

## Data

- [ChemCrowRun reproduction results](CHEMCROWRUN_RESULTS.json)
- [OpenEvo historical results](OPEN_EVO_HISTORICAL_RESULTS.json)
- [ChemCrow paper](https://www.nature.com/articles/s42256-024-00832-8)
- [Official ChemCrow experiment notebooks](https://github.com/ur-whitelab/chemcrow-runs)
