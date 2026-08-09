# R4 Reflector Input Preview

This is a zero-provider-call reconstruction from three sealed historical Candidate runs. It
contains only Candidate/public evidence and the already-admitted sanitized feedback. Raw GT,
checklists, target-image semantics, Judge reasoning, and Judge raw responses are absent.

Every preview has the same two top-level views:

```text
WHAT ALREADY WORKED = MinimalBaselineTrace
WHAT NEEDS IMPROVEMENT = admitted sanitized evaluator feedback
```

The production instruction says to reconstruct the former before adding the latter. It allows
a method or parameter change only when public or Candidate-produced evidence records the old
value, new value, evidence, and reason.

## Life_005 — positive V2 reference

```text
trace_sha256   = 93a66d5c61d4c13dd7c150bf6d486e9654cfa9b3cf5da65000c425a1fa1d67b6
context_sha256 = 0ac362bd1497b2bf2e4fff4f562355db56b1285e19b44e65bcfe8b30bfb61138
context_bytes  = 5189
successful_paths = 4
diagnoses = 1
```

WHAT ALREADY WORKED:

1. Read the seven supplied probability/matrix CSVs with `read_table`,
   `read_labeled_matrix`, and `read_numeric_matrix`.
2. Compute triplet trends and matrix contrasts with `triplets`, `lin_slope`, and
   `matrix_stats`; retain the orthogonality metrics used by the report.
3. Produce the independent probability-mass validation with `plot_validation`,
   `outputs/probability_mass_validation.csv`, and
   `report/images/probability_mass_validation.png`.
4. Produce the complete evidence set with `plot_line_panels`, `plot_fig12`, `plot_fig13`,
   `plot_orthogonality_bars`, and the report-linked figures/tables.

WHAT NEEDS IMPROVEMENT:

- `visual_evidence`: preserve the existing figures, then add an independent quantitative
  public-data check and connect conclusions to Candidate-produced evidence.

Deterministic checks:

```text
baseline methods visible = true
public input roles visible = true
report-linked outputs visible = true
all feedback visible = 1/1
private markers visible = false
```

## Astronomy_004 — negative R3.14 reference

```text
trace_sha256   = 05ea10e96ba0abebbb88d5e517d546228ca157c5b14b09753e11626b67b80cfa
context_sha256 = e9749d78293a8709c2e9a920970a378fd3f3d57da045097a30e930fafa05d2ca
context_bytes  = 8117
successful_paths = 4
diagnoses = 2
```

WHAT ALREADY WORKED:

1. Read `data/QZO.csv` with `read_catalog` and retain the selected catalog evidence.
2. Run the Candidate's feature/selection path with `add_features`, `decisive_label`,
   `standardize_matrix`, `percentile`, `selection_rules`, `entropy3`, and `pr_curve`.
   The exact Candidate-produced selection definition remains visible:

   ```text
   p_QSO >= 0.90
   p_WISE_QSO >= 0.97
   wise_margin >= 0.90
   ```

   The trace also preserves the other explicit Candidate comparisons rather than reducing
   them to “catalog selection analysis.”
3. Run the modeling/validation path with `train_logistic`, `predict_logistic`,
   `best_threshold`, `confusion`, `auc_roc`, and `roc_curve`, retaining model coefficients,
   model metrics, and `model_validation.png`.
4. Reproduce the six report-linked views for probability, selection, model validation, sky
   density, and redshift coverage.

WHAT NEEDS IMPROVEMENT:

- `visual_evidence`: reconstruct the existing figure-producing path, then add an independent
  public-data quantitative cross-check.
- `quantitative_validation`: keep the existing route, then add one independent public-input
  validation/comparison that extends its evidence chain.

Deterministic checks:

```text
read_catalog visible = true
selection_rules visible = true
train_logistic visible = true
confusion visible = true
p_QSO >= 0.90 visible = true
p_WISE_QSO >= 0.97 visible = true
all feedback visible = 2/2
private markers visible = false
```

## Chemistry_004 — negative R3.14 reference

```text
trace_sha256   = 3d0ad9a0722753588feed9371ec87a5ed45ddd38d71207c483e60d19c6aa8cce
context_sha256 = 20ee48e7c378e3562accff1fe347423182745f47b6af23737a798f2b07df0376
context_bytes  = 6558
successful_paths = 4
diagnoses = 2
```

WHAT ALREADY WORKED:

1. Perform real text extraction over the four supplied PDFs with
   `extract_pdf_metadata_and_text` and `pdf_literal_to_text`, retaining
   `outputs/related_work_extraction.json`. This is not represented as generic “public
   structured synthesis.”
2. Run the Candidate's additive-screening path with `normalize_scores`, `summarize`,
   `make_radar_proxy`, `clamp`, and `triangular`.
3. Produce the descriptor comparison with `make_descriptor_correlation`, the descriptor
   table, and its report-linked heatmap.
4. Produce the ranking, redox, descriptor, and validation evidence through the Candidate's
   actual chart/output functions and retain the screening JSON/CSV outputs.

WHAT NEEDS IMPROVEMENT:

- `visual_evidence`: reconstruct the figure-producing path, then add an independent
  public-data quantitative cross-check.
- `quantitative_validation`: keep the existing route, then add one independent public-input
  validation/comparison that extends its evidence chain.

Deterministic checks:

```text
extract_pdf_metadata_and_text visible = true
pdf_literal_to_text visible = true
four PDF public inputs visible = true
related_work_extraction.json visible = true
all feedback visible = 2/2
private markers visible = false
```

## Differential conclusion

R4's preview does not multiply one script into 7–12 achievement rows and does not truncate
method signatures to six tokens. The important methods and Candidate-selected parameters
remain verbatim in a 5–8 kB context, while feedback stays a separate additive view. Legacy
R3 capsule/ledger/equivalence records remain historical diagnostics and are not native
Reflector input or a Judge-dispatch hard gate.
