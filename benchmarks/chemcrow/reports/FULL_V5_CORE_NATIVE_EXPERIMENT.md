# Full-v5 Core-native three-Reflector experiment

## Verdict

`FULL_V5_CORE_NATIVE_EXPERIMENT_COMPLETE`

The 14-task run, 42 Core-native Reflector jobs, 42 unique artifacts, 14 exact three-artifact injections, 14 pair seals, 14 resets, and the isolated 42-call GPT-4 evaluation are complete. All model-derived scores remain provisional LLM judgments.

## Aggregate scores

| Layer | G1 | G2 | G2-G1 | wins/ties/losses |
|---|---:|---:|---:|---:|
| Evolution evaluator (/12) | 9.929 | 10.143 | 0.214 | 3/9/2 |
| Blind internal evaluator (/12) | 10.336 | 11.036 | 0.700 | 8/5/1 |
| Paper-compatible GPT-4 (/10) | 9.143 | 9.000 | -0.143 | 0/12/2 |

Evolution-evaluator dimension means:

- G1: chemical correctness 3.429, reasoning 3.500, task completion 3.000.
- G2: chemical correctness 3.571, reasoning 3.500, task completion 3.071.

## Per-task results

| Task | Internal G1->G2 | Blind G1->G2 | Paper G1->G2 | Artifact IDs | Reflector jobs | Conflict | Amplification |
|---|---:|---:|---:|---|---|---:|---:|
| chemcrow-01 | 8.000->8.000 (+0.0) | 8.000->8.000 (+0.0) | 9.000->8.000 (-1.0) | `art_0d111ad819aa4f0c`, `art_a7ec2a17032542df`, `art_cbea414b75e146c9` | `job_7ce2ad4ff9194473`, `job_c243437cf6b14e40`, `job_c13c493146654a52` | false | true |
| chemcrow-02 | 10.000->11.000 (+1.0) | 10.000->12.000 (+2.0) | 9.000->9.000 (+0.0) | `art_ab7874c561c84431`, `art_67ca5f42f4d84889`, `art_d0d6f608ce7b429b` | `job_33a33fc169604795`, `job_c85ec8a5a4eb4e81`, `job_1429bc2742704921` | false | true |
| chemcrow-03 | 12.000->12.000 (+0.0) | 12.000->12.000 (+0.0) | 10.000->10.000 (+0.0) | `art_15095e652d294ba9`, `art_8690135c43584f4a`, `art_4016a58d2f6f4329` | `job_a0f43ce91d3a4e53`, `job_6c8f467998344667`, `job_af7bd82cf0714160` | false | true |
| chemcrow-04 | 8.000->8.000 (+0.0) | 8.000->9.000 (+1.0) | 9.000->8.000 (-1.0) | `art_b2fd18b186ae40cc`, `art_4df98458273f4964`, `art_8c028fd3acc04c39` | `job_c1c0311d442340ee`, `job_f2e770d97470434a`, `job_5175f4c3a96449ca` | false | true |
| chemcrow-05 | 9.000->11.000 (+2.0) | 10.000->11.000 (+1.0) | 9.000->9.000 (+0.0) | `art_4f797b6b879747c4`, `art_f5b11b9c93e34d11`, `art_feff6632b0f74463` | `job_808d368c18af4311`, `job_bde1d943adb04420`, `job_8736685fae334df2` | true | true |
| chemcrow-06 | 12.000->12.000 (+0.0) | 11.000->12.000 (+1.0) | 9.000->9.000 (+0.0) | `art_20dbe2ac76354571`, `art_2d4bce4a58b348a9`, `art_32275ed39ced4699` | `job_37a84fefd82f43da`, `job_45ec1e5cb8934282`, `job_0f1cd817974446d8` | false | true |
| chemcrow-07 | 9.000->9.000 (+0.0) | 10.000->11.000 (+1.0) | 9.000->9.000 (+0.0) | `art_124ba9a4f7d649f0`, `art_9bca2c5a090a499e`, `art_e3290a2bb13b4800` | `job_58239ed01be44827`, `job_9a7532693c3b44ea`, `job_5a913710e35b47d8` | false | true |
| chemcrow-08 | 11.000->10.000 (-1.0) | 10.700->11.500 (+0.8) | 9.000->9.000 (+0.0) | `art_a9a2086d30764268`, `art_6fa8a55cf087418d`, `art_4f97b5c2946b4e6d` | `job_8d531dd701d84d3e`, `job_a5f3283528fd449d`, `job_9eeab9d3370f4a43` | false | true |
| chemcrow-09 | 10.000->12.000 (+2.0) | 12.000->12.000 (+0.0) | 9.000->9.000 (+0.0) | `art_0a6d74e770024871`, `art_5bf30f635315410a`, `art_a0bd224564e3470e` | `job_b1bdb48061db4409`, `job_6e6332b63c584199`, `job_8d9ed10d8544476a` | false | true |
| chemcrow-10 | 10.000->10.000 (+0.0) | 12.000->12.000 (+0.0) | 9.000->9.000 (+0.0) | `art_5ada00f573ed495a`, `art_8afb32f5f55d4c68`, `art_32d21957eca14d52` | `job_4a77d690f6fc4945`, `job_630fd3388a5b4d1e`, `job_daefa21632e044f0` | false | true |
| chemcrow-12 | 12.000->12.000 (+0.0) | 12.000->12.000 (+0.0) | 10.000->10.000 (+0.0) | `art_5845f8f3cd95470c`, `art_07517b7cd66c4b15`, `art_706ddc0db2844060` | `job_7d8ef0d1c86f42cc`, `job_7370af6ae95b4f90`, `job_724d5aedda8d4345` | true | true |
| chemcrow-13 | 8.000->7.000 (-1.0) | 9.000->8.000 (-1.0) | 8.000->8.000 (+0.0) | `art_7a1b6e227cc9424d`, `art_2765d8b7ae32404a`, `art_bfd4d620e1084e94` | `job_89b03402cb6c4a7d`, `job_030f4a9ee61647cb`, `job_f376592fe13e4914` | true | true |
| chemcrow-14 | 9.000->9.000 (+0.0) | 9.000->12.000 (+3.0) | 10.000->10.000 (+0.0) | `art_1b43e463d62f46f6`, `art_0985a5d032524a2e`, `art_defb1a9f27ea42fd` | `job_10f5988501e744cd`, `job_eea79b7e41ba4ed9`, `job_16aab24a93e04267` | false | true |
| chemcrow-15 | 11.000->11.000 (+0.0) | 11.000->12.000 (+1.0) | 9.000->9.000 (+0.0) | `art_285eafffc2b74754`, `art_51b8759af5a64df3`, `art_209330b09f054524` | `job_7822d259470247de`, `job_bb8316c842744fd9`, `job_70fb3119c02d448e` | false | true |

## Statistical limits

- Internal mean delta 95% paired bootstrap CI: [-0.21428571428571427, 0.7142857142857143]; Wilcoxon p=0.4375; sign-test p=1.0000.
- Blind mean delta 95% paired bootstrap CI: [0.21428571428571427, 1.2142857142857142]; Wilcoxon p=0.0312; sign-test p=0.0391.
- Paper mean delta 95% paired bootstrap CI: [-0.35714285714285715, 0.0]; Wilcoxon p=0.5000; sign-test p=0.5000.
- N=14 and the paper judge has 12 ties; no layer should be treated as a precise population estimate.

## Evidence locations

- Full-v5 run: `/home/lhy-h/work/chemcrowrun/runs/full-v5-core-native-three-pipeline`
- Paper production ledger: `/home/lhy-h/work/chemcrowrun/runs/paper-evaluator-full-v5-core-native-three-pipeline-r1`
- Completed-run audit: `FULL_V5_CORE_NATIVE_COMPLETED_RUN_AUDIT.json`
- Paper route audit: `FULL_V5_PAPER_EVALUATOR_AUDIT.json`
- Post-generation semantic audit: `FULL_V5_CORE_NATIVE_SEMANTIC_AUDIT.json`
- Machine-readable aggregate: `FULL_V5_CORE_NATIVE_EXPERIMENT.json`
