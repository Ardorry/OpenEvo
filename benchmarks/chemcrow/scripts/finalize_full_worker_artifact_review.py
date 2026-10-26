from __future__ import annotations

import argparse
import json
from pathlib import Path

from openevo_chemcrow.artifact_similarity import ARTIFACT_TYPES, SEMANTIC_FLAGS


def _object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def finalize(*, draft_path: Path, decisions_path: Path, output_path: Path) -> dict:
    draft = _object(draft_path.resolve(strict=True))
    decisions = _object(decisions_path.resolve(strict=True))
    draft_tasks = draft.get("tasks")
    decision_tasks = decisions.get("tasks")
    if not isinstance(draft_tasks, dict) or not isinstance(decision_tasks, dict):
        raise TypeError("artifact review task maps are absent")
    if set(draft_tasks) != set(decision_tasks):
        raise ValueError("artifact review decision task inventory differs")
    tasks: dict[str, dict] = {}
    for task_id, source in draft_tasks.items():
        decision = decision_tasks[task_id]
        task_level = decision.get("task_level")
        findings = decision.get("artifact_findings")
        if (
            not isinstance(task_level, dict)
            or set(task_level) != set(SEMANTIC_FLAGS)
            or not all(isinstance(task_level[flag], bool) for flag in SEMANTIC_FLAGS)
        ):
            raise ValueError(f"task-level semantic flags differ: {task_id}")
        if not isinstance(findings, dict) or set(findings) != set(ARTIFACT_TYPES):
            raise ValueError(f"artifact finding inventory differs: {task_id}")
        for artifact_type, finding in findings.items():
            if (
                not isinstance(finding, dict)
                or set(finding) != {*SEMANTIC_FLAGS, "reason"}
                or not all(isinstance(finding[flag], bool) for flag in SEMANTIC_FLAGS)
                or not isinstance(finding["reason"], str)
                or not finding["reason"].strip()
            ):
                raise ValueError(f"artifact semantic decision differs: {task_id}/{artifact_type}")
        summary = decision.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(f"task summary is absent: {task_id}")
        tasks[task_id] = {
            "task_prompt": source["task_prompt"],
            "task_obligations": source["task_obligations"],
            "task_level": task_level,
            "summary": summary,
            "artifact_findings": findings,
        }
    result = {
        "schema_version": "chemcrow_core_full_worker_manual_semantic_annotations_v1",
        "status": "FULL_CORPUS_ARTIFACT_AUDIT_COMPLETE",
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "criterion_note": (
            "Manual post-generation semantic audit of all 42 exact native artifacts; "
            "no annotation was used by a Candidate, evaluator, or Reflector."
        ),
        "tasks": tasks,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": result["status"],
        "task_count": len(tasks),
        "artifact_count": len(tasks) * len(ARTIFACT_TYPES),
        "output": str(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            finalize(
                draft_path=args.draft,
                decisions_path=args.decisions,
                output_path=args.output,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
