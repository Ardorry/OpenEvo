from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "chemcrow_short_long_artifact_similarity_comparison_v1"
COMPONENTS = ("combined", "general", "hybrid", "task_specific")
ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
SEMANTIC_FLAGS = (
    "TASK_CONFLICT",
    "CONSTRAINT_AMPLIFICATION",
    "UNSUPPORTED_GENERALIZATION",
    "INSTRUCTION_OVERLOAD",
    "POSITIVE_CORRECTION",
    "EVIDENCE_DISCIPLINE",
)
ROLE_ORDER = {artifact_type: index for index, artifact_type in enumerate(ARTIFACT_TYPES)}
ROLE_PAIR_LABELS = {
    ("text_memory", "skill_bundle"): "M-K",
    ("text_memory", "agent_system"): "M-A",
    ("skill_bundle", "agent_system"): "K-A",
}


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _role_pair_means(path: Path) -> dict[str, float]:
    values: dict[str, list[float]] = {label: [] for label in ROLE_PAIR_LABELS.values()}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["same_task"] != "True":
                continue
            pair = tuple(
                sorted((row["type_a"], row["type_b"]), key=ROLE_ORDER.__getitem__)
            )
            label = ROLE_PAIR_LABELS[pair]
            values[label].append(float(row["combined_coverage"]))
    _require(all(len(group) == 14 for group in values.values()), "role-pair coverage is incomplete")
    return {label: statistics.fmean(group) for label, group in values.items()}


def _semantic_audit_summary(payload: dict[str, Any]) -> dict[str, Any]:
    tasks = payload.get("tasks")
    _require(isinstance(tasks, dict) and len(tasks) == 14, "semantic audit must contain 14 tasks")
    flag_summary: dict[str, Any] = {}
    for flag in SEMANTIC_FLAGS:
        task_ids = [
            task_id
            for task_id, task in tasks.items()
            if bool(task["task_level"][flag])
        ]
        artifact_count = sum(
            bool(finding[flag])
            for task in tasks.values()
            for finding in task["artifact_findings"].values()
        )
        flag_summary[flag] = {
            "task_count": len(task_ids),
            "task_ids": task_ids,
            "artifact_count": artifact_count,
        }
    content_bytes: dict[str, Any] = {}
    for artifact_type in ARTIFACT_TYPES:
        values = [
            int(task["artifact_findings"][artifact_type]["content_bytes"])
            for task in tasks.values()
        ]
        content_bytes[artifact_type] = {
            "artifact_count": len(values),
            "mean": statistics.fmean(values),
            "total": sum(values),
            "minimum": min(values),
            "maximum": max(values),
        }
    return {"flags": flag_summary, "content_bytes": content_bytes}


def _delta(new: float | None, old: float | None) -> float | None:
    if new is None or old is None:
        return None
    return new - old


def compare(
    *,
    short_summary: dict[str, Any],
    long_summary: dict[str, Any],
    short_pairs_path: Path,
    long_pairs_path: Path,
    short_audit: dict[str, Any],
    long_audit: dict[str, Any],
) -> dict[str, Any]:
    for label, summary in (("short", short_summary), ("long", long_summary)):
        _require(summary.get("artifact_count") == 42, f"{label} summary must contain 42 artifacts")
        _require(summary.get("pair_count") == 861, f"{label} summary must contain 861 pairs")
    short_tasks = short_summary["composition"]["by_task"]
    long_tasks = long_summary["composition"]["by_task"]
    _require(short_tasks.keys() == long_tasks.keys(), "task inventories differ")

    composition: dict[str, Any] = {}
    for scope in ("general", "hybrid", "task_specific"):
        composition[scope] = {}
        for basis in ("unit", "word"):
            field = f"{scope}_{basis}_fraction"
            short_value = float(short_summary["composition"]["overall"][field])
            long_value = float(long_summary["composition"]["overall"][field])
            composition[scope][basis] = {
                "short": short_value,
                "long": long_value,
                "long_minus_short": long_value - short_value,
            }

    by_artifact_type: dict[str, Any] = {}
    for artifact_type in ARTIFACT_TYPES:
        by_artifact_type[artifact_type] = {}
        for scope in ("general", "hybrid", "task_specific"):
            field = f"{scope}_unit_fraction"
            short_value = float(
                short_summary["composition"]["by_artifact_type"][artifact_type][field]
            )
            long_value = float(
                long_summary["composition"]["by_artifact_type"][artifact_type][field]
            )
            by_artifact_type[artifact_type][scope] = {
                "short": short_value,
                "long": long_value,
                "long_minus_short": long_value - short_value,
            }

    groups: dict[str, Any] = {}
    for group_name in (
        "all_pairs",
        "same_task_siblings",
        "same_type_cross_task",
        "different_type_cross_task",
    ):
        groups[group_name] = {}
        for component in COMPONENTS:
            short_value = short_summary["groups"][group_name][component]["mean"]
            long_value = long_summary["groups"][group_name][component]["mean"]
            groups[group_name][component] = {
                "short": short_value,
                "long": long_value,
                "long_minus_short": _delta(long_value, short_value),
            }

    cross_task_same_role: dict[str, Any] = {}
    for artifact_type in ARTIFACT_TYPES:
        cross_task_same_role[artifact_type] = {}
        for component in COMPONENTS:
            short_value = short_summary["cross_task_same_role"][artifact_type][component]["mean"]
            long_value = long_summary["cross_task_same_role"][artifact_type][component]["mean"]
            cross_task_same_role[artifact_type][component] = {
                "short": short_value,
                "long": long_value,
                "long_minus_short": _delta(long_value, short_value),
            }

    short_pair_means = _role_pair_means(short_pairs_path)
    long_pair_means = _role_pair_means(long_pairs_path)
    within_task_role_pairs = {
        label: {
            "short": short_pair_means[label],
            "long": long_pair_means[label],
            "long_minus_short": long_pair_means[label] - short_pair_means[label],
        }
        for label in ROLE_PAIR_LABELS.values()
    }

    per_task: dict[str, Any] = {}
    for task_id in short_tasks:
        short_task = short_tasks[task_id]
        long_task = long_tasks[task_id]
        short_sibling = float(short_summary["per_task"][task_id]["same_task_combined_mean"])
        long_sibling = float(long_summary["per_task"][task_id]["same_task_combined_mean"])
        per_task[task_id] = {
            "scope_unit_fraction": {
                scope: {
                    "short": float(short_task[f"{scope}_unit_fraction"]),
                    "long": float(long_task[f"{scope}_unit_fraction"]),
                    "long_minus_short": float(long_task[f"{scope}_unit_fraction"])
                    - float(short_task[f"{scope}_unit_fraction"]),
                }
                for scope in ("general", "hybrid", "task_specific")
            },
            "same_task_combined_mean": {
                "short": short_sibling,
                "long": long_sibling,
                "long_minus_short": long_sibling - short_sibling,
            },
        }

    short_semantic = _semantic_audit_summary(short_audit)
    long_semantic = _semantic_audit_summary(long_audit)
    semantic_audit: dict[str, Any] = {"flags": {}, "content_bytes": {}}
    for flag in SEMANTIC_FLAGS:
        semantic_audit["flags"][flag] = {
            "short": short_semantic["flags"][flag],
            "long": long_semantic["flags"][flag],
            "long_minus_short_task_count": long_semantic["flags"][flag]["task_count"]
            - short_semantic["flags"][flag]["task_count"],
            "long_minus_short_artifact_count": long_semantic["flags"][flag]["artifact_count"]
            - short_semantic["flags"][flag]["artifact_count"],
        }
    for artifact_type in ARTIFACT_TYPES:
        short_values = short_semantic["content_bytes"][artifact_type]
        long_values = long_semantic["content_bytes"][artifact_type]
        semantic_audit["content_bytes"][artifact_type] = {
            "short": short_values,
            "long": long_values,
            "long_minus_short_mean": long_values["mean"] - short_values["mean"],
            "long_to_short_mean_ratio": long_values["mean"] / short_values["mean"],
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "comparison_scope": "post_generation_descriptive_only",
        "causal_claim_authorized": False,
        "same_candidate_outputs": False,
        "same_manual_scope_rubric": True,
        "artifact_count_per_run": 42,
        "semantic_unit_count": {
            "short": short_summary["composition"]["overall"]["semantic_unit_count"],
            "long": long_summary["composition"]["overall"]["semantic_unit_count"],
        },
        "composition": composition,
        "composition_by_artifact_type": by_artifact_type,
        "similarity_groups": groups,
        "cross_task_same_role": cross_task_same_role,
        "within_task_role_pairs": within_task_role_pairs,
        "alignment_effect": {
            "short": short_summary["same_task_alignment_effect"],
            "long": long_summary["same_task_alignment_effect"],
            "long_minus_short": long_summary["same_task_alignment_effect"]
            - short_summary["same_task_alignment_effect"],
        },
        "duplicates": {
            "short_exact": short_summary["exact_content_duplicate_count"],
            "long_exact": long_summary["exact_content_duplicate_count"],
            "short_semantic_at_or_above_threshold": short_summary[
                "semantic_near_duplicate_pair_count"
            ],
            "long_semantic_at_or_above_threshold": long_summary[
                "semantic_near_duplicate_pair_count"
            ],
            "threshold": long_summary["semantic_near_duplicate_threshold"],
        },
        "per_task": per_task,
        "semantic_audit": semantic_audit,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--short-summary", type=Path, required=True)
    parser.add_argument("--long-summary", type=Path, required=True)
    parser.add_argument("--short-pairs", type=Path, required=True)
    parser.add_argument("--long-pairs", type=Path, required=True)
    parser.add_argument("--short-semantic-audit", type=Path, required=True)
    parser.add_argument("--long-semantic-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    paths = {
        "short_summary": args.short_summary.resolve(),
        "long_summary": args.long_summary.resolve(),
        "short_pairs": args.short_pairs.resolve(),
        "long_pairs": args.long_pairs.resolve(),
        "short_semantic_audit": args.short_semantic_audit.resolve(),
        "long_semantic_audit": args.long_semantic_audit.resolve(),
    }
    result = compare(
        short_summary=_load_json(paths["short_summary"]),
        long_summary=_load_json(paths["long_summary"]),
        short_pairs_path=paths["short_pairs"],
        long_pairs_path=paths["long_pairs"],
        short_audit=_load_json(paths["short_semantic_audit"]),
        long_audit=_load_json(paths["long_semantic_audit"]),
    )
    result["sources"] = {
        name: {"path": str(path), "sha256": _sha256(path)} for name, path in paths.items()
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)
    print(json.dumps({"status": "COMPLETE", "output": str(output), "sha256": _sha256(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
