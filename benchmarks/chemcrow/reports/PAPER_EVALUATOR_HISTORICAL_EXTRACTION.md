# Paper Evaluator Historical Extraction

The dataset was extracted by tracing the last executed assignment visible before each
`teacher.run(task, result_tools, result_notools)` call. Notebook outputs are the primary
runtime-reproduction labels; official Source Data Figure 4c is an independent cross-check.

- task count: 14
- labeled task count: 14
- missing tasks: []
- source commit: `500104ed9a5d479a8dc4128afc463625ade5a409`
- source tag/commit: `v1` / `1e098b171ac34b9dcf95a836b506b6cd289ce113`
- dataset canonical SHA256: `148ddc2bd38dc3c49a01d01cd9cc99035cb0d70c085c7c2f3e738b378c813de0`
- notebook versus Supplement discrepancies: 0

Task 12 is explicitly resolved from the later literal overwrite of `result_notools`
(execution count 6). Task 13 passes `prompt`, rather than `task`, to the evaluator. Historical
outputs provide grade, strengths, weaknesses, and grade justification; a separate improvement
feedback field is not recoverable and is not fabricated.

| task | label | notebook A | notebook B | discrepancy | source |
|---|---:|---:|---:|---:|---|
| chemcrow-01 | true | 6.0 | 8.0 | false | `tasks/01_safinamide.ipynb` |
| chemcrow-02 | true | 7.0 | 9.0 | false | `tasks/02_discover_cat.ipynb` |
| chemcrow-03 | true | 8.0 | 9.0 | false | `tasks/03_explain_mechanism.ipynb` |
| chemcrow-04 | true | 8.0 | 9.0 | false | `tasks/04_insect_repellent.ipynb` |
| chemcrow-05 | true | 7.0 | 8.0 | false | `tasks/05_how_to_make.ipynb` |
| chemcrow-06 | true | 9.0 | 10.0 | false | `tasks/06_mechanism_lindlars.ipynb` |
| chemcrow-07 | true | 7.0 | 9.0 | false | `tasks/07_modify_and_synth.ipynb` |
| chemcrow-08 | true | 7.0 | 9.0 | false | `tasks/08_riccis_orgcat.ipynb` |
| chemcrow-09 | true | 9.0 | 9.5 | false | `tasks/09_predict_rxn_incompat.ipynb` |
| chemcrow-10 | true | 7.0 | 9.0 | false | `tasks/10_property_of_product.ipynb` |
| chemcrow-12 | true | 8.0 | 7.0 | false | `tasks/12_similar_moa.ipynb` |
| chemcrow-13 | true | 7.0 | 8.0 | false | `tasks/13_atorvastatin_synth.ipynb` |
| chemcrow-14 | true | 6.0 | 9.0 | false | `tasks/14_aspirin_synthesis.ipynb` |
| chemcrow-15 | true | 7.0 | 9.0 | false | `tasks/15_takemoto.ipynb` |

Extraction was run twice in clean Python processes; both canonical hashes were `148ddc2bd38dc3c49a01d01cd9cc99035cb0d70c085c7c2f3e738b378c813de0`.
