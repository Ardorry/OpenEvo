# Evolution Feedback Policy

Status: `DESIGN_FROZEN_FOR_PHASE_0`

Policy ID: `researchclaw-openevo-feedback-v1`

Machine contract: `schemas/evolution_feedback.schema.json`

## Goal

Feedback must improve a general research workflow on community Dev without leaking hidden target details, turning Validation into training data, or adapting to the official 40-task test set. Every evaluator-to-OpenEvo transfer goes through one closed schema and a phase gate. Raw scorer output is never a valid evolution input.

## Phase 1: Community Dev

Community Dev is the only split on which OpenEvo may update an artifact. A post-run evaluator may score a sealed run, but `feedback_filter.py` releases only a schema-valid `community_dev_feedback` record.

Allowed fields:

- task/run identity;
- candidate artifact SHA-256;
- completion status and exit code;
- artifact-validator pass/fail and fixed generic error tags;
- rounded wall-clock runtime;
- total metered/estimated cost;
- one scalar task total score in the official 0–100 range.

Not allowed:

- per-checklist-item scores, weights, types, content, keywords, or reasoning;
- report-vs-target comparisons;
- target-derived metrics, values, plots, images, or paper statements;
- scorer prompt, response, raw exception, or provider trace;
- free-form failure messages containing task/evaluator detail.

The scalar total score is allowed because these tasks are the declared training/Dev set. It is still a coarse optimization signal, not permission to expose the rubric. The evolution dataset must record that the score is community-Dev-only.

## Phase 2: Community Validation

Validation does not update or reflect an artifact. Each candidate is evaluated with the same frozen runtime and policy. Only after all six tasks for all declared candidates are sealed may the selection service consume one aggregate `community_validation_summary` per candidate.

Allowed aggregate fields:

- evaluated/completed/artifact-valid/scored task counts;
- mean, median, and minimum total score without task labels;
- total runtime and total cost;
- candidate artifact SHA-256.

The summary is marked `selection_only: true` and `released_to_reflector: false`. It may select one final artifact from a predeclared candidate set; it may not synthesize, edit, prompt-tune, or rerun a candidate. Per-task Validation results remain sealed evaluation evidence and never enter OpenEvo datasets.

If a tie rule is needed, freeze it before evaluation. Recommended ordering is artifact-valid count, completed count, mean score, minimum score, then lower cost, then artifact SHA-256 lexical order. Do not add a new metric after results are visible.

## Phase 3: Official 40 tasks

During formal candidate execution, no scorer is called. The adapter produces only an `official_test_withheld_receipt` with `feedback_released: false`. OpenEvo, its reflector, candidate memory, and later official candidate runs receive no completion, validity, runtime, cost, score, judge, or task-result signal from earlier official tasks.

After every formal run is sealed, the separate evaluator may score them for reporting/submission. Those scores remain outside evolution inputs. They cannot choose, revise, or retry the artifact.

## Permanently forbidden feedback

The following are forbidden in every stage, including community Dev:

- checklist content;
- checklist keywords;
- checklist item weights or modes;
- target paper text, values, tables, citations, identity, or extracted facts;
- target images or their embeddings/descriptions;
- per-item judge reasoning;
- rubric mode or whether an item was classified objective/subjective;
- official `_score.json` or any field derived from it during formal execution;
- target-paper metrics or comparisons;
- other official test-task results, traces, completion state, validity, runtime, or cost;
- raw judge request/response, provider log, or hidden evaluator exception.

The Markdown bullet spelling in upstream notes is not normative; this list and the JSON schema are.

## Decision table

| Signal | Community Dev | Community Validation | Official 40 | Reason |
| --- | --- | --- | --- | --- |
| Scalar total score | Allowed | Aggregate selection summary only | Forbidden | Coarse Dev reward; no hidden item detail |
| Artifact validity | Allowed as boolean + fixed tags | Aggregate count only | Withheld from OpenEvo | General deliverable quality signal |
| Completion | Allowed | Aggregate count only | Withheld from OpenEvo | Operational, not target-derived |
| Runtime | Allowed, rounded | Aggregate total only | Withheld from OpenEvo | Resource optimization without content |
| Cost | Allowed, aggregate | Aggregate total only | Withheld from OpenEvo | Budget control without content |
| Generic error label | Allowed from closed enum | Not released | Forbidden | Prevents free-text leakage |
| Per-item score | Forbidden | Forbidden | Forbidden | Encodes hidden rubric structure |
| Judge reasoning | Forbidden | Forbidden | Forbidden | Direct hidden evaluator feedback |
| Raw report critique | Forbidden | Forbidden | Forbidden | May paraphrase target/rubric content |

## Filter operation

1. Accept a phase identifier from the signed/frozen run manifest, never from caller free text.
2. Verify source run, artifact, scorer, and phase identities.
3. Construct a new object from allowed source fields; never delete forbidden keys from a raw object and pass the remainder.
4. Map operational errors into the fixed enum. If mapping is ambiguous, use `UNKNOWN_OPERATIONAL_FAILURE`; do not include raw text.
5. Validate the new object against `schemas/evolution_feedback.schema.json` with format-independent JSON Schema validation.
6. Canonicalize JSON with sorted keys, finite numbers, and no insignificant whitespace; record SHA-256.
7. Store raw evaluator evidence only in evaluator-only storage. Publish the allowlisted object to OpenEvo only when the stage permits it.
8. Record release/withhold decision without secret or hidden content.

Unknown fields, non-finite numbers, over-bounds values, task IDs outside the frozen split, official-test records using a community branch, or attempts to include free text are `FEEDBACK_POLICY_VIOLATION` and fail closed.

## Score and validator separation

Artifact validity is computed without the checklist or target study. The validator may inspect only the candidate-visible workspace and process receipt. The scorer runs separately and may read hidden material. A validator result is safe operational feedback only because its rule set is task-independent and frozen in `design/artifact_validator_spec.md`.

The total score comes from the official community-task scorer but is reduced to one scalar after the scorer process exits. No scorer metadata enters the filter.

## Cross-run and cross-task rules

- Community Dev records may be accumulated across Dev tasks by the evolution method.
- Each Dev update applies only to a later, fresh task/session; no in-session streaming of score feedback is allowed.
- Validation summaries select among already frozen candidates and cannot feed another evolution round.
- Official runs use one artifact and no cross-task state. Even operational official feedback is withheld until the experiment ends, and it is never used for evolution.
- Pass@5, if maintainer-approved, means five independent runs of the same frozen artifact/config. Runs do not exchange memory or feedback.

## Version changes

Any new field or relaxation requires:

1. a new policy/schema version;
2. a new frozen protocol manifest;
3. re-review before any result is observed under the new policy;
4. maintainer confirmation if it affects official tasks, Pass@5, cross-task state, network, or judge use.

Existing result sets are never reinterpreted under a later, more permissive policy.
