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
trace_sha256   = 49896780211c0048a11b4567fbde06272dda5fd97e3f1c7ae7b9daa3ec4df03c
context_sha256 = 54e05f53bb61c3f0564de5f4dc5bcb0dbcbead26e10147e42c716ece44a80173
context_bytes  = 9190
successful_paths = 4
parameter_entries = 8
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
trace_sha256   = 9a613b68b85e33f4782706eec4cb643cabe175898cc8503501573605aef2200e
context_sha256 = 531c72ce22846ee9201a90d2eaccf0597b73af3e17652e397afadc891f6f002f
context_bytes  = 12216
successful_paths = 4
parameter_entries = 48
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
trace_sha256   = a002ae960cd368610c5d8ab89ce87592bc627075408c28bdcb146a53a689902a
context_sha256 = f97cb9fffb9aaf93c81b97d403f0bd7aeb6302a944890a9c3835299b48c8e4b9
context_bytes  = 10779
successful_paths = 4
parameter_entries = 39
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
method signatures to six tokens. The important methods, Candidate-selected parameters, and
bounded report rationale remain verbatim in a 9–12 kB context, while feedback stays a separate additive view. Legacy
R3 capsule/ledger/equivalence records remain historical diagnostics and are not native
Reflector input or a Judge-dispatch hard gate.
