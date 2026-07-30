# Artifact Validator Specification

Status: `DESIGN_FROZEN_FOR_PHASE_0`

## Purpose and trust boundary

The validator decides whether a completed candidate run has a structurally safe, non-placeholder, reviewable ResearchClawBench deliverable. It runs after the candidate process and namespace are gone, before the official scorer, and without access to `target_study`, checklist, judge credentials, scores, or other task results.

It is not a quality scorer and does not compare the report with a target paper. Every rule is task-independent and therefore may produce bounded operational feedback on community Dev.

## Inputs

Required inputs are immutable references to:

- run ID and task ID;
- candidate-visible workspace root;
- process receipt containing status, exit code, timeout state, and start/end times;
- prompt/input inventory receipt;
- frozen protocol and artifact identities;
- configured resource and file-size bounds.

The validator opens paths relative to a pinned workspace file descriptor with no-follow semantics. It never trusts paths supplied by the report without resolving them under that root.

## Terminal preconditions

Validation fails immediately when:

- the candidate process group or namespace is still alive;
- the process timed out or was interrupted;
- the candidate exit code is missing or nonzero;
- the workspace binding differs from the construction receipt;
- input `data/`, `related_work/`, `INSTRUCTIONS.md`, or frozen artifact identity changed;
- an unsafe path, special file, symlink, or unexpected hard link is present.

A successful exit code is necessary but not sufficient.

## Required structure

The following must exist as real directories/files below the workspace, without links:

```text
code/                   directory, at least one regular source/script/notebook file
outputs/                directory, at least one regular result/provenance file
report/                 directory
report/report.md        UTF-8 regular file, non-empty
report/images/          directory
report/images/*.png     at least one valid PNG
```

Input directories may be present in the sealed run folder but are excluded from candidate artifact hashes. `_meta.json`, `_agent_output.jsonl`, `_score.json`, and `audit/` are supervisor/evaluator files and are not candidate deliverables.

## Report substance

`report/report.md` must satisfy all of the following:

- valid UTF-8 with no NUL bytes;
- at least 1,000 non-whitespace characters and at least 200 word-like tokens;
- at least three non-empty substantive sections covering method/approach, results, and discussion/limitations; headings may vary and are classified by a fixed synonym table;
- at least one local PNG reference and at least one explicit result statement;
- not dominated by repeated lines, generated filler, stack traces, raw logs, or copied task instructions;
- not an outline, plan, or instructions-only document.

Length alone never passes a report. A long report still fails when required sections, results, valid images, output traceability, or completed content are absent.

The thresholds are generic artifact hygiene gates, not paper-derived score thresholds. A future threshold change requires a validator version change and a new frozen protocol before any comparative run.

## Placeholder and incompleteness detection

The validator scans report, code, and output text with bounded reads for obvious unresolved markers, including:

- `TODO`, `TBD`, `FIXME`, `XXX`;
- `placeholder`, `lorem ipsum`, `insert figure/table/result here`;
- empty Markdown headings or tables;
- template variables such as `<...>`, `{{...}}`, or `${...}` in deliverable prose;
- statements that the analysis/result/report remains to be done;
- an error traceback presented as the final result.

Matches inside quoted source code, citations, or a limitations discussion are not automatically fatal. The validator records context-free location hashes, applies a fixed parser/classifier, and fails only when an unresolved marker affects a required deliverable. Ambiguity is `REVIEW_REQUIRED` and blocks formal scoring until resolved by a predeclared, score-blind review rule.

## PNG validation

At least one `.png` under `report/images/` must pass all checks:

- regular file, one link, nonzero, within configured size limit;
- correct PNG signature and decodable by the pinned image library;
- positive width/height, sane dimension/pixel bounds, supported color mode;
- complete image stream with no truncation;
- not a uniform/near-empty canvas under a fixed entropy/variance heuristic;
- no absolute/external path dependency required to render it.

The validator records dimensions, byte size, MIME, and SHA-256. It does not interpret scientific correctness.

## Report references

Parse Markdown image/link syntax and supported HTML `img` tags without executing HTML. For every local report image reference:

- percent-decode once and reject control/NUL characters;
- require a relative path;
- reject `..`, absolute POSIX/Windows paths, `file:`, `data:`, UNC paths, and drive letters;
- resolve below `report/` and require a real regular file;
- require PNG for report figures;
- match path case exactly;
- require every PNG intended as a report figure to be referenced, unless explicitly marked auxiliary in a task-independent manifest.

HTTP(S) citations may remain as bibliographic links. Remote images are not accepted as required figures. Filesystem-looking absolute paths anywhere in prose/code blocks are reported; absolute paths in reproducibility commands fail unless replaced with workspace-relative forms.

## Workspace escape checks

The complete candidate output tree is traversed with no-follow operations. Fail on:

- symlink, FIFO, socket, block/character device, mount point, or hard link with unexpected link count;
- a path resolving outside `code/`, `outputs/`, or `report/`;
- absolute paths in output manifests;
- archive members with absolute/traversal paths when archives are allowed and inspected;
- Markdown/HTML links to files outside the workspace;
- generated launcher/config content pointing to host home, benchmark source, hidden task paths, or sibling workspaces.

Unsafe files are preserved as quarantined evidence but are never followed, executed, rendered, or passed to the scorer.

## Code and output checks

`code/` must contain at least one non-empty regular source/script/notebook file in a bounded allowlist of text or notebook formats. Notebook output cells are not treated as the sole reproducibility evidence. The validator does not execute code.

`outputs/` must contain at least one non-empty regular output or a machine-readable provenance index. Raw caches, downloaded packages, model weights, and logs alone do not satisfy this requirement.

Recommended optional candidate file:

```text
outputs/provenance.json
```

If present, it must be closed JSON mapping named report claims/figures to workspace-relative code and output paths plus hashes. It is not required by the upstream benchmark, so absence alone is not fatal; the fallback traceability checks below apply.

## Numeric traceability

The validator extracts prominent numeric result claims from Results/Conclusion sections and Markdown tables using a fixed parser. It ignores dates, citations, section numbers, and configuration values. For each key claim it seeks evidence in:

1. `outputs/provenance.json`, if present;
2. report links or explicit workspace-relative output references;
3. exact/normalized values in bounded CSV, TSV, JSON, YAML, or plain-text output files;
4. a machine-readable CSV/JSON summary adjacent to binary NPY/NPZ/MAT/HDF5 output;
5. source code that writes the referenced output path.

At least one primary numeric result must trace to a non-report output when the report makes quantitative claims. If a task/report is genuinely qualitative and contains no primary numeric result, traceability may be `NOT_APPLICABLE`, but report figures and qualitative evidence must still reference generated outputs and code. A claim is never judged against a target value.

The validator records claim hashes and evidence path hashes, not report text, in the evolution-safe receipt. Detailed mappings remain in the sealed audit folder.

## Process-status consistency

The final status is computed as follows:

| Process receipt | Deliverable validation | Validator result |
| --- | --- | --- |
| completed, exit 0 | pass | `PASS` |
| completed, exit 0 | fail/review required | `FAIL` |
| failed/nonzero | any | `FAIL` |
| timeout/interrupted | any | `FAIL` |
| missing/ambiguous | any | `FAIL` |

An existing report cannot turn a timeout/nonzero run into success. Exit zero cannot turn an invalid report into success. `_meta.json.status` is written by the supervisor from this combined result; the candidate cannot set it.

## Resource and bounded-read rules

The frozen protocol must cap:

- total candidate artifact bytes and file count;
- maximum single file, report, image, archive, notebook, and text-scan bytes;
- directory depth and relative path length;
- total numeric claims and report references parsed;
- validation wall time and memory.

Exceeding a cap returns a stable resource-limit error and fails closed. The validator hashes files sequentially or with very low bounded concurrency.

## Artifact inventory and root hash

For each regular candidate artifact file, record:

```text
relative_path, size_bytes, mode, sha256, media_type
```

Sort entries by normalized relative path and compute:

```text
artifact_root_sha256 = SHA256(canonical JSON {
  "contract_version": "1",
  "entries": [...]
})
```

The inventory excludes inputs, supervisor/evaluator files, filesystem timestamps, absolute paths, UIDs, and secrets. Mode is normalized to executable/non-executable rather than host-specific bits.

## Post-validation freeze

After a pass or fail receipt is durably written:

1. revalidate the workspace root and every output file identity;
2. fsync files/directories where supported;
3. atomically publish the inventory and receipt;
4. remove candidate write access by unmounting/destroying the candidate namespace;
5. make candidate artifact paths read-only to subsequent adapter operations;
6. verify the root hash immediately before evaluator start and after scoring.

The evaluator writes `_score.json` through a separate overlay/output path. Its addition does not alter `artifact_root_sha256`.

## Error codes

Stable validator error codes include:

- `PROCESS_NOT_TERMINAL`
- `CANDIDATE_TIMEOUT`
- `CANDIDATE_NONZERO_EXIT`
- `INPUT_IDENTITY_CHANGED`
- `UNSAFE_PATH`
- `SYMLINK_OR_SPECIAL_FILE`
- `REPORT_MISSING`
- `REPORT_EMPTY`
- `REPORT_TOO_SHORT`
- `REPORT_STRUCTURE_INCOMPLETE`
- `REPORT_PLACEHOLDER_CONTENT`
- `CODE_MISSING_OR_EMPTY`
- `OUTPUTS_MISSING_OR_EMPTY`
- `PNG_MISSING`
- `PNG_INVALID`
- `REPORT_IMAGE_REFERENCE_INVALID`
- `ABSOLUTE_PATH_REFERENCE`
- `NUMERIC_TRACEABILITY_MISSING`
- `ARTIFACT_RESOURCE_LIMIT`
- `ARTIFACT_IDENTITY_CHANGED`
- `REVIEW_REQUIRED`

Only a coarser mapping from these codes enters community Dev feedback.

## Suggested validator return JSON

```json
{
  "schema_version": "1.0.0",
  "validator_id": "researchclawbench-artifact-validator-v1",
  "validator_source_sha256": "<sha256>",
  "run_id": "<run-id>",
  "task_id": "<task-id>",
  "protocol_id": "<protocol-id>",
  "process_receipt_sha256": "<sha256>",
  "started_at": "2026-07-27T00:00:00Z",
  "finished_at": "2026-07-27T00:00:01Z",
  "status": "PASS",
  "checks": [
    {
      "id": "report_structure",
      "status": "PASS",
      "severity": "error",
      "evidence": {
        "report_sha256": "<sha256>",
        "non_whitespace_chars": 12000,
        "word_like_tokens": 2100,
        "substantive_section_count": 5
      }
    }
  ],
  "errors": [],
  "warnings": [],
  "metrics": {
    "candidate_file_count": 27,
    "candidate_total_bytes": 1234567,
    "png_count": 3,
    "referenced_png_count": 3,
    "key_numeric_claim_count": 8,
    "traced_numeric_claim_count": 8
  },
  "artifact_inventory": {
    "contract_version": "1",
    "inventory_path": "audit/candidate_artifact_files.json",
    "inventory_sha256": "<sha256>",
    "artifact_root_sha256": "<sha256>"
  },
  "freeze": {
    "candidate_namespace_gone": true,
    "candidate_write_access_removed": true,
    "sealed_at": "2026-07-27T00:00:01Z"
  }
}
```

The actual implementation must replace examples, use canonical UTC timestamps, reject extra fields, and validate its own receipt against a future closed JSON Schema before Phase 3.
