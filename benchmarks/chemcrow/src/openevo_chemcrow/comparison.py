"""Post-run comparison of sealed submissions against immutable public full-v8 scores."""

from __future__ import annotations

import json
from pathlib import Path

from .chemcrowrun import PACKAGE, paired_statistics
from .hashing import file_sha256
from .paper_evaluator import FROZEN_PAPER_TASK_IDS, PaperEvaluationResult
from .submission import validate_submission_file


def compare_submission(submission_path: Path, *, paper_results: Path | None = None) -> dict:
    submission = validate_submission_file(submission_path)
    aliases = json.loads((PACKAGE / "reproduction/legacy-aliases.json").read_text())

    def historical(name):
        relative = f"reports/full_v8_public_20260831/{name}"
        path = PACKAGE / relative
        if file_sha256(path) != aliases["immutable_files"][relative]:
            raise ValueError("historical comparison evidence differs from immutable identity")
        return json.loads(path.read_text())

    legacy = historical("per_task_evidence.json")
    ids = list(FROZEN_PAPER_TASK_IDS)
    if legacy["task_ids"] != ids or [r["task_id"] for r in legacy["results"]] != ids:
        raise ValueError("historical task order differs")
    old_g1 = [sum(r["blind_evaluation"]["baseline_scores"].values()) for r in legacy["results"]]
    old_g2 = [sum(r["blind_evaluation"]["evolved_scores"].values()) for r in legacy["results"]]
    new_g1 = [
        sum(r.blind_evaluation.baseline_scores.model_dump().values()) for r in submission.results
    ]
    new_g2 = [
        sum(r.blind_evaluation.evolved_scores.model_dump().values()) for r in submission.results
    ]

    def compare(old1, old2, new1, new2):
        return {
            "new_g1_g2": paired_statistics(new1, new2),
            "historical_g1_g2": paired_statistics(old1, old2),
            "new_minus_historical_g1": {
                "left": "historical",
                "right": "new",
                "statistics": paired_statistics(old1, new1),
            },
            "new_minus_historical_g2": {
                "left": "historical",
                "right": "new",
                "statistics": paired_statistics(old2, new2),
            },
            "per_task": [
                {
                    "task_id": task,
                    "historical_g1": a,
                    "historical_g2": b,
                    "new_g1": c,
                    "new_g2": d,
                    "new_g2_minus_g1": d - c,
                    "g1_change": c - a,
                    "g2_change": d - b,
                }
                for task, a, b, c, d in zip(ids, old1, old2, new1, new2, strict=True)
            ],
        }

    report = {
        "schema_version": "chemcrowrun_comparison_v1",
        "submission_sha256": file_sha256(submission_path),
        "source_commit": submission.execution.source_commit,
        "task_ids": ids,
        "historical_alias": "full-v8",
        "historical_model": "gpt-5.5",
        "new_models": {
            role: binding.model_dump()
            for role, binding in submission.execution.model_bindings.items()
        },
        "model_substitution": submission.execution.model_substitution.model_dump()
        if submission.execution.model_substitution
        else None,
        "internal_blind_12_point": compare(old_g1, old_g2, new_g1, new_g2),
        "paper_10_point": "PENDING",
        "model_calls": 0,
        "raw_answers_included": False,
        "interpretation": "Descriptive replication comparison. Model/service changes confound cross-run differences. Paired tests do not establish improvement from one task or replace human review. Multiple contrasts are exploratory and unadjusted.",
        "input_validation": "Submission schema and immutable historical hashes; no leaderboard certification",
    }
    if paper_results is not None:
        records = [
            PaperEvaluationResult.model_validate_json(p.read_text())
            for p in sorted(paper_results.glob("*.result.json"))
        ]
        grades = {(r.task_id, r.comparison): r.assessment.student_a.grade for r in records}
        expected = {
            (task, comparison)
            for task in ids
            for comparison in ("historical_control", "baseline", "evolved")
        }
        if len(records) != 42 or set(grades) != expected:
            raise ValueError("calibrated comparison requires all 42 distinct sealed assessments")
        old = historical("paper_judge_evidence.json")
        if [r["task_id"] for r in old["results"]] != ids:
            raise ValueError("historical paper task order differs")
        old_grades = {
            (r["task_id"], c["comparison"]): c["student_a_grade"]
            for r in old["results"]
            for c in r["comparisons"]
        }
        report["paper_10_point"] = compare(
            [old_grades[(t, "baseline")] for t in ids],
            [old_grades[(t, "evolved")] for t in ids],
            [grades[(t, "baseline")] for t in ids],
            [grades[(t, "evolved")] for t in ids],
        )
    return report
