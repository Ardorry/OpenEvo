from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import artifact_similarity as base
from .hashing import canonical_sha256, file_sha256

OUTPUT_SCHEMA_VERSION = "chemcrow_artifact_manual_scope_similarity_v1"
ANNOTATION_SCHEMA_VERSION = "chemcrow_manual_semantic_scope_annotations_v1"
SCOPE_LABELS = ("General", "Hybrid", "Task-specific")
SCOPE_COLORS = {
    "General": "#4C78A8",
    "Hybrid": "#ECA82C",
    "Task-specific": "#E45756",
}
MISSING_COLOR = "#E6E6E6"


@dataclass(frozen=True)
class ManualScopeResult:
    matrices: dict[str, Any]
    metadata: dict[str, Any]
    artifact_composition: tuple[dict[str, Any], ...]
    unit_inventory: tuple[dict[str, Any], ...]
    pair_rows: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


def _canonical_unit_inventory(
    records: Sequence[base.ArtifactRecord],
    units_by_artifact: Sequence[Sequence[base.TextChunk]],
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    global_index = 0
    for record, units in zip(records, units_by_artifact, strict=True):
        for local_index, unit in enumerate(units):
            inventory.append(
                {
                    "global_unit_index": global_index,
                    "artifact_id": record.artifact_id,
                    "local_unit_index": local_index,
                    "word_count": unit.word_count,
                    "content": unit.text,
                }
            )
            global_index += 1
    return inventory


def load_manual_scope_labels(
    annotation_path: Path,
    *,
    source_path: Path,
    records: Sequence[base.ArtifactRecord],
    units_by_artifact: Sequence[Sequence[base.TextChunk]],
) -> tuple[tuple[str, ...], dict[str, Any], str]:
    """Load a source-bound, exhaustive human adjudication of semantic-unit scope."""

    annotation_path = annotation_path.resolve()
    payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    base._require(isinstance(payload, dict), "manual annotation root must be an object")
    base._require(
        payload.get("schema_version") == ANNOTATION_SCHEMA_VERSION,
        "manual annotation schema differs",
    )
    base._require(
        payload.get("status") == "FULL_CORPUS_RUBRIC_ADJUDICATION_COMPLETE",
        "manual annotation is not complete",
    )
    source = payload.get("source")
    base._require(isinstance(source, dict), "manual annotation source binding is absent")
    actual_source_sha256 = file_sha256(source_path.resolve())
    base._require(
        source.get("sha256") == actual_source_sha256,
        "manual annotation source SHA256 differs",
    )
    canonical_units = _canonical_unit_inventory(records, units_by_artifact)
    unit_inventory_sha256 = canonical_sha256(canonical_units)
    base._require(
        payload.get("semantic_unit_inventory_sha256") == unit_inventory_sha256,
        "manual annotation semantic-unit inventory SHA256 differs",
    )
    base._require(
        payload.get("reviewed_unit_count") == len(canonical_units),
        "manual annotation reviewed unit count differs",
    )
    task_annotations = payload.get("task_annotations")
    base._require(isinstance(task_annotations, dict), "task annotation map is absent")
    expected_tasks = sorted({record.task_id for record in records}, key=base._task_sort_key)
    base._require(
        sorted(task_annotations, key=base._task_sort_key) == expected_tasks,
        "manual annotation task inventory differs",
    )

    unit_task_ids = [
        record.task_id for record, units in zip(records, units_by_artifact, strict=True) for _ in units
    ]
    labels: list[str | None] = [None] * len(canonical_units)
    for task_id in expected_tasks:
        annotation = task_annotations[task_id]
        base._require(isinstance(annotation, dict), f"task annotation is invalid: {task_id}")
        task_indices = [
            index for index, observed_task_id in enumerate(unit_task_ids) if observed_task_id == task_id
        ]
        base._require(bool(task_indices), f"task has no semantic units: {task_id}")
        expected_range = [task_indices[0], task_indices[-1]]
        base._require(
            annotation.get("unit_range") == expected_range,
            f"manual annotation unit range differs: {task_id}",
        )
        hybrid = annotation.get("hybrid_indices")
        task_specific = annotation.get("task_specific_indices")
        base._require(
            isinstance(hybrid, list) and all(isinstance(value, int) for value in hybrid),
            f"hybrid index inventory is invalid: {task_id}",
        )
        base._require(
            isinstance(task_specific, list)
            and all(isinstance(value, int) for value in task_specific),
            f"task-specific index inventory is invalid: {task_id}",
        )
        hybrid_set = set(hybrid)
        task_specific_set = set(task_specific)
        base._require(len(hybrid_set) == len(hybrid), f"duplicate hybrid index: {task_id}")
        base._require(
            len(task_specific_set) == len(task_specific),
            f"duplicate task-specific index: {task_id}",
        )
        base._require(
            hybrid_set.isdisjoint(task_specific_set),
            f"manual scope labels overlap: {task_id}",
        )
        base._require(
            hybrid_set | task_specific_set <= set(task_indices),
            f"manual scope index escapes its task: {task_id}",
        )
        for index in task_indices:
            labels[index] = (
                "Hybrid"
                if index in hybrid_set
                else "Task-specific"
                if index in task_specific_set
                else "General"
            )
    base._require(all(label in SCOPE_LABELS for label in labels), "manual scope coverage is incomplete")
    return tuple(str(label) for label in labels), payload, unit_inventory_sha256


def _safe_component_coverage(values: Any) -> float:
    import numpy as np

    matrix = np.asarray(values, dtype=np.float64)
    if matrix.ndim != 2 or 0 in matrix.shape:
        return math.nan
    return base._symmetric_best_match_coverage(matrix)


def _describe_finite(values: Sequence[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "available_pair_count": 0,
            "missing_pair_count": len(values),
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
        }
    return {
        "available_pair_count": len(finite),
        "missing_pair_count": len(values) - len(finite),
        "mean": statistics.fmean(finite),
        "median": statistics.median(finite),
        "minimum": min(finite),
        "maximum": max(finite),
    }


def _compute_scope_result(
    records: Sequence[base.ArtifactRecord],
    *,
    annotation_path: Path,
    source_path: Path,
    model_name: str,
    model_cache: Path,
    min_words: int,
    batch_size: int,
    threads: int,
) -> ManualScopeResult:
    import numpy as np
    from fastembed import TextEmbedding

    units_by_artifact = [
        base.extract_semantic_units(record.content, min_words=min_words) for record in records
    ]
    labels, annotation_payload, unit_inventory_sha256 = load_manual_scope_labels(
        annotation_path,
        source_path=source_path,
        records=records,
        units_by_artifact=units_by_artifact,
    )
    all_units = [unit.text for units in units_by_artifact for unit in units]
    owners = [index for index, units in enumerate(units_by_artifact) for _ in units]
    owner_array = np.asarray(owners, dtype=np.int64)
    label_array = np.asarray(labels, dtype=object)
    vector_indices = [np.flatnonzero(owner_array == index) for index in range(len(records))]

    model = TextEmbedding(
        model_name=model_name,
        cache_dir=str(model_cache.resolve()),
        threads=threads,
        cuda=False,
    )
    vectors = np.asarray(list(model.embed(all_units, batch_size=batch_size)), dtype=np.float64)
    base._require(
        vectors.ndim == 2 and vectors.shape[0] == len(all_units),
        "embedding backend returned the wrong semantic-unit inventory",
    )
    vectors = np.asarray([base._l2_normalize(vector) for vector in vectors], dtype=np.float64)
    raw_unit_cosines = np.clip(vectors @ vectors.T, -1.0, 1.0)

    background_parts = []
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            background_parts.append(raw_unit_cosines[np.ix_(vector_indices[left], vector_indices[right])].ravel())
    background = np.concatenate(background_parts)
    background_floor = float(np.quantile(background, 0.5))
    high_match_cap = float(np.quantile(background, 0.99))
    base._require(high_match_cap > background_floor, "embedding calibration range is invalid")
    calibrated = np.clip(
        (raw_unit_cosines - background_floor) / (high_match_cap - background_floor),
        0.0,
        1.0,
    )

    matrix_names = ("combined", "general", "hybrid", "task_specific")
    matrices = {
        name: np.full((len(records), len(records)), np.nan, dtype=np.float64)
        for name in matrix_names
    }
    for artifact_index, indices in enumerate(vector_indices):
        matrices["combined"][artifact_index, artifact_index] = 1.0
        for name, label in (
            ("general", "General"),
            ("hybrid", "Hybrid"),
            ("task_specific", "Task-specific"),
        ):
            if np.any(label_array[indices] == label):
                matrices[name][artifact_index, artifact_index] = 1.0

    pair_rows: list[dict[str, Any]] = []
    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            left_indices = vector_indices[left]
            right_indices = vector_indices[right]
            row: dict[str, Any] = {
                "artifact_a": records[left].artifact_id,
                "artifact_b": records[right].artifact_id,
                "label_a": records[left].label,
                "label_b": records[right].label,
                "task_a": records[left].task_id,
                "task_b": records[right].task_id,
                "type_a": records[left].artifact_type,
                "type_b": records[right].artifact_type,
                "same_task": records[left].task_id == records[right].task_id,
                "same_type": records[left].artifact_type == records[right].artifact_type,
            }
            for name, label in (
                ("combined", None),
                ("general", "General"),
                ("hybrid", "Hybrid"),
                ("task_specific", "Task-specific"),
            ):
                selected_left = (
                    left_indices if label is None else left_indices[label_array[left_indices] == label]
                )
                selected_right = (
                    right_indices
                    if label is None
                    else right_indices[label_array[right_indices] == label]
                )
                score = _safe_component_coverage(calibrated[np.ix_(selected_left, selected_right)])
                matrices[name][left, right] = matrices[name][right, left] = score
                row[f"{name}_coverage"] = None if not math.isfinite(score) else score
            pair_rows.append(row)

    artifact_composition: list[dict[str, Any]] = []
    unit_inventory: list[dict[str, Any]] = []
    global_index = 0
    for record, units, indices in zip(records, units_by_artifact, vector_indices, strict=True):
        counts = Counter(labels[index] for index in indices)
        words = Counter()
        for index, unit in zip(indices, units, strict=True):
            words[labels[index]] += unit.word_count
        total_words = sum(words.values())
        row = {
            "artifact_index": record.index,
            "label": record.label,
            "task_id": record.task_id,
            "artifact_type": record.artifact_type,
            "artifact_id": record.artifact_id,
            "semantic_unit_count": len(units),
            "semantic_unit_word_count": total_words,
        }
        for scope in SCOPE_LABELS:
            key = scope.lower().replace("-", "_")
            row[f"{key}_unit_count"] = counts[scope]
            row[f"{key}_unit_fraction"] = counts[scope] / len(units)
            row[f"{key}_word_count"] = words[scope]
            row[f"{key}_word_fraction"] = words[scope] / total_words
        row["general_equivalent_unit_fraction"] = (
            counts["General"] + (0.5 * counts["Hybrid"])
        ) / len(units)
        row["general_equivalent_word_fraction"] = (
            words["General"] + (0.5 * words["Hybrid"])
        ) / total_words
        artifact_composition.append(row)
        for local_index, unit in enumerate(units):
            label = labels[global_index]
            unit_inventory.append(
                {
                    "global_unit_index": global_index,
                    "artifact_index": record.index,
                    "artifact_label": record.label,
                    "artifact_id": record.artifact_id,
                    "task_id": record.task_id,
                    "artifact_type": record.artifact_type,
                    "local_unit_index": local_index,
                    "word_count": unit.word_count,
                    "manual_scope": label,
                    "scope_basis": {
                        "General": "transferable_chemistry_or_work_behavior",
                        "Hybrid": "mixed_transferable_and_current_task_content",
                        "Task-specific": "depends_on_current_task_domain_or_obligation",
                    }[label],
                    "content": unit.text,
                }
            )
            global_index += 1
    base._require(global_index == len(labels), "manual unit inventory indexing differs")

    summary = _summarize(records, pair_rows, artifact_composition)
    model_dir = getattr(getattr(model, "model", None), "_model_dir", None)
    base._require(model_dir is not None, "fastembed did not expose its model snapshot")
    metadata = {
        "implementation": "manual-scope order-invariant bidirectional semantic-unit coverage",
        "annotation": {
            "path": str(annotation_path.resolve()),
            "sha256": file_sha256(annotation_path.resolve()),
            "schema_version": annotation_payload["schema_version"],
            "status": annotation_payload["status"],
            "reviewed_unit_count": len(labels),
            "semantic_unit_inventory_sha256": unit_inventory_sha256,
            "frequency_used_as_label_rule": False,
            "rubric": annotation_payload["adjudication_method"],
            "task_review_notes": {
                task_id: annotation["review_note"]
                for task_id, annotation in annotation_payload["task_annotations"].items()
            },
        },
        "embedding": {
            "model_name": model_name,
            "embedding_dimension": int(vectors.shape[1]),
            "device": "CPU",
            "batch_size": batch_size,
            "threads": threads,
            "model_snapshot": base._tree_manifest(Path(model_dir).resolve()),
        },
        "semantic_units": {
            "minimum_words": min_words,
            "total_count": len(labels),
            "per_artifact_count": [len(units) for units in units_by_artifact],
        },
        "background_calibration": {
            "population": "all cross-artifact semantic-unit cosine cells",
            "value_count": int(background.size),
            "floor_quantile": 0.5,
            "floor_cosine": background_floor,
            "cap_quantile": 0.99,
            "cap_cosine": high_match_cap,
            "transform": "clip((cosine-floor)/(cap-floor), 0, 1)",
        },
        "matching": {
            "method": "harmonic mean of both directed mean-best-match coverages",
            "order_sensitive": False,
            "combined_view_preserved": True,
            "component_missing_value": (
                "NA when either artifact has no semantic unit in the selected manual scope"
            ),
        },
    }
    return ManualScopeResult(
        matrices=matrices,
        metadata=metadata,
        artifact_composition=tuple(artifact_composition),
        unit_inventory=tuple(unit_inventory),
        pair_rows=tuple(pair_rows),
        summary=summary,
    )


def _summarize(
    records: Sequence[base.ArtifactRecord],
    pair_rows: Sequence[dict[str, Any]],
    composition: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    component_fields = {
        "combined": "combined_coverage",
        "general": "general_coverage",
        "hybrid": "hybrid_coverage",
        "task_specific": "task_specific_coverage",
    }
    groups = {
        "all_pairs": list(pair_rows),
        "same_task_siblings": [row for row in pair_rows if row["same_task"]],
        "same_type_cross_task": [
            row for row in pair_rows if not row["same_task"] and row["same_type"]
        ],
        "different_type_cross_task": [
            row for row in pair_rows if not row["same_task"] and not row["same_type"]
        ],
    }
    group_summary = {
        group_name: {
            component: _describe_finite(
                [
                    math.nan if row[field] is None else float(row[field])
                    for row in rows
                ]
            )
            for component, field in component_fields.items()
        }
        for group_name, rows in groups.items()
    }

    role_cross_task = {}
    for artifact_type in base.ARTIFACT_TYPES:
        rows = [
            row
            for row in groups["same_type_cross_task"]
            if row["type_a"] == artifact_type == row["type_b"]
        ]
        role_cross_task[artifact_type] = {
            component: _describe_finite(
                [math.nan if row[field] is None else float(row[field]) for row in rows]
            )
            for component, field in component_fields.items()
        }

    tasks = sorted({record.task_id for record in records}, key=base._task_sort_key)
    per_task = {}
    for task_id in tasks:
        sibling_rows = [
            row for row in groups["same_task_siblings"] if row["task_a"] == task_id
        ]
        cross_rows = [
            row
            for row in pair_rows
            if not row["same_task"] and task_id in (row["task_a"], row["task_b"])
        ]
        sibling_combined = [float(row["combined_coverage"]) for row in sibling_rows]
        cross_combined = [float(row["combined_coverage"]) for row in cross_rows]
        per_task[task_id] = {
            "same_task_combined_mean": statistics.fmean(sibling_combined),
            "cross_task_combined_mean": statistics.fmean(cross_combined),
            "alignment_effect": statistics.fmean(sibling_combined)
            - statistics.fmean(cross_combined),
            "components": {
                component: _describe_finite(
                    [
                        math.nan if row[field] is None else float(row[field])
                        for row in sibling_rows
                    ]
                )
                for component, field in component_fields.items()
            },
        }

    def composition_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        total_units = sum(int(row["semantic_unit_count"]) for row in rows)
        total_words = sum(int(row["semantic_unit_word_count"]) for row in rows)
        result = {
            "artifact_count": len(rows),
            "semantic_unit_count": total_units,
            "semantic_unit_word_count": total_words,
        }
        for scope in SCOPE_LABELS:
            key = scope.lower().replace("-", "_")
            count = sum(int(row[f"{key}_unit_count"]) for row in rows)
            words = sum(int(row[f"{key}_word_count"]) for row in rows)
            result[f"{key}_unit_count"] = count
            result[f"{key}_unit_fraction"] = count / total_units
            result[f"{key}_word_count"] = words
            result[f"{key}_word_fraction"] = words / total_words
        return result

    composition_by_type = {
        artifact_type: composition_summary(
            [row for row in composition if row["artifact_type"] == artifact_type]
        )
        for artifact_type in base.ARTIFACT_TYPES
    }
    composition_by_task = {
        task_id: composition_summary([row for row in composition if row["task_id"] == task_id])
        for task_id in tasks
    }

    ranked_pairs = sorted(pair_rows, key=lambda row: float(row["combined_coverage"]), reverse=True)
    exact_content_duplicate_count = sum(
        records[left].content_sha256 == records[right].content_sha256
        for left in range(len(records))
        for right in range(left + 1, len(records))
    )
    same_task_mean = group_summary["same_task_siblings"]["combined"]["mean"]
    cross_role_mean = group_summary["different_type_cross_task"]["combined"]["mean"]
    base._require(
        same_task_mean is not None and cross_role_mean is not None,
        "combined group summary is unexpectedly empty",
    )
    return {
        "artifact_count": len(records),
        "pair_count": len(pair_rows),
        "groups": group_summary,
        "composition": {
            "overall": composition_summary(composition),
            "by_artifact_type": composition_by_type,
            "by_task": composition_by_task,
        },
        "cross_task_same_role": role_cross_task,
        "same_task_alignment_effect": float(same_task_mean) - float(cross_role_mean),
        "per_task": per_task,
        "highest_combined_pairs": ranked_pairs[:30],
        "exact_content_duplicate_count": exact_content_duplicate_count,
        "semantic_near_duplicate_threshold": 0.95,
        "semantic_near_duplicate_pair_count": sum(
            float(row["combined_coverage"]) >= 0.95 for row in pair_rows
        ),
    }


def _write_matrix_csv(path: Path, records: Sequence[base.ArtifactRecord], matrix: Any) -> None:
    rows = []
    for index, record in enumerate(records):
        row: dict[str, Any] = {"artifact": record.label}
        for other in records:
            value = float(matrix[index, other.index])
            row[other.label] = "" if not math.isfinite(value) else f"{value:.10f}"
        rows.append(row)
    base._write_csv(path, rows, ["artifact", *[record.label for record in records]])


def _plot_matrix(
    matrix: Any,
    records: Sequence[base.ArtifactRecord],
    *,
    title: str,
    output_path: Path,
    order: Sequence[int] | None = None,
    boundaries: Sequence[int] = (),
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    indices = list(range(len(records))) if order is None else list(order)
    values = np.asarray(matrix, dtype=np.float64)[np.ix_(indices, indices)]
    masked = np.ma.masked_invalid(values)
    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(MISSING_COLOR)
    labels = [records[index].label for index in indices]
    fig, ax = plt.subplots(figsize=(15, 13))
    image = ax.imshow(masked, cmap=cmap, vmin=0.0, vmax=1.0, interpolation="nearest")
    ax.set_title(title, pad=12)
    ax.set_xticks(range(len(labels)), labels=labels, rotation=90, fontsize=6)
    ax.set_yticks(range(len(labels)), labels=labels, fontsize=6)
    for boundary in boundaries:
        ax.axhline(boundary - 0.5, color="white", linewidth=1.2)
        ax.axvline(boundary - 0.5, color="white", linewidth=1.2)
    colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.035)
    colorbar.set_label("Background-calibrated semantic-unit coverage")
    ax.set_xlabel("Artifact (task:type; M=memory, K=skill, A=agent system)")
    ax.set_ylabel("Artifact (task:type)")
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _plot_artifact_composition(
    composition: Sequence[dict[str, Any]],
    *,
    basis: str,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    base._require(basis in {"unit", "word"}, "composition basis is invalid")
    labels = [str(row["label"]) for row in composition]
    positions = np.arange(len(composition))
    fig, ax = plt.subplots(figsize=(12, 14))
    left = np.zeros(len(composition), dtype=np.float64)
    for scope in SCOPE_LABELS:
        key = scope.lower().replace("-", "_")
        values = np.asarray(
            [float(row[f"{key}_{basis}_fraction"]) for row in composition],
            dtype=np.float64,
        )
        ax.barh(
            positions,
            values,
            left=left,
            color=SCOPE_COLORS[scope],
            label=scope,
            height=0.78,
        )
        left += values
    ax.set_yticks(positions, labels=labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlim(0.0, 1.0)
    ax.set_xlabel(f"Share of artifact semantic-unit {basis}s")
    ax.set_title(
        "Manual semantic scope by semantic-unit count"
        if basis == "unit"
        else "Manual semantic scope by semantic-unit word count"
    )
    ax.grid(axis="x", alpha=0.22)
    ax.legend(loc="lower right", ncols=3)
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _plot_composition_summary(
    summary: dict[str, Any],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    roles = list(base.ARTIFACT_TYPES)
    tasks = sorted(summary["composition"]["by_task"], key=base._task_sort_key)
    fig, axes = plt.subplots(1, 2, figsize=(15, 7), gridspec_kw={"width_ratios": [1, 2.6]})
    for ax, labels, values_by_key, title in (
        (
            axes[0],
            [base.ARTIFACT_TYPE_SHORT[role] for role in roles],
            summary["composition"]["by_artifact_type"],
            "Pooled by artifact type",
        ),
        (
            axes[1],
            [task.removeprefix("chemcrow-") for task in tasks],
            summary["composition"]["by_task"],
            "Pooled by task",
        ),
    ):
        keys = roles if ax is axes[0] else tasks
        positions = np.arange(len(keys))
        bottom = np.zeros(len(keys), dtype=np.float64)
        for scope in SCOPE_LABELS:
            key = scope.lower().replace("-", "_")
            values = np.asarray(
                [float(values_by_key[item][f"{key}_unit_fraction"]) for item in keys]
            )
            ax.bar(positions, values, bottom=bottom, color=SCOPE_COLORS[scope], label=scope)
            bottom += values
        ax.set_xticks(positions, labels=labels)
        ax.set_ylim(0.0, 1.0)
        ax.set_title(title)
        ax.set_ylabel("Share by semantic-unit count")
        ax.grid(axis="y", alpha=0.22)
    axes[1].legend(loc="upper center", bbox_to_anchor=(0.5, -0.11), ncols=3)
    fig.suptitle("Manual scope composition: role expectation and task specialization")
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _plot_unit_word_delta(
    composition: Sequence[dict[str, Any]],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    labels = [str(row["label"]) for row in composition]
    unit = np.asarray(
        [float(row["general_equivalent_unit_fraction"]) for row in composition]
    )
    word = np.asarray(
        [float(row["general_equivalent_word_fraction"]) for row in composition]
    )
    positions = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(12, 14))
    for y, left, right in zip(positions, unit, word, strict=True):
        ax.plot([left, right], [y, y], color="#9E9E9E", linewidth=1.0, zorder=1)
    ax.scatter(unit, positions, color="#4C78A8", s=24, label="By unit count", zorder=2)
    ax.scatter(word, positions, color="#E45756", s=24, label="By unit words", zorder=2)
    ax.axvline(0.5, color="black", linewidth=0.7, linestyle="--", alpha=0.5)
    ax.set_yticks(positions, labels=labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlim(0.0, 1.0)
    ax.set_xlabel("General-equivalent share (General + 0.5 x Hybrid)")
    ax.set_title("Why unit-count and word-count composition can differ")
    ax.grid(axis="x", alpha=0.22)
    ax.legend(loc="lower right")
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _task_pair_values(
    records: Sequence[base.ArtifactRecord],
    matrix: Any,
) -> tuple[list[str], Any]:
    return base._task_pair_matrix(records, matrix)


def _plot_within_task_components(
    records: Sequence[base.ArtifactRecord],
    matrices: dict[str, Any],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    components = ("combined", "general", "hybrid", "task_specific")
    titles = ("Combined", "General only", "Hybrid only", "Task-specific only")
    fig, axes = plt.subplots(1, 4, figsize=(20, 10), sharey=True)
    tasks: list[str] | None = None
    for ax, component, title in zip(axes, components, titles, strict=True):
        observed_tasks, values = _task_pair_values(records, matrices[component])
        if tasks is None:
            tasks = observed_tasks
        base._require(tasks == observed_tasks, "task order differs between components")
        masked = np.ma.masked_invalid(values)
        cmap = plt.get_cmap("viridis").copy()
        cmap.set_bad(MISSING_COLOR)
        image = ax.imshow(masked, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")
        ax.set_title(title)
        ax.set_xticks(range(3), labels=["M-K", "M-A", "K-A"])
        ax.set_xlabel("Within-task role pair")
        for row in range(values.shape[0]):
            for column in range(values.shape[1]):
                value = float(values[row, column])
                text = "NA" if not math.isfinite(value) else f"{value:.2f}"
                color = "#777777" if not math.isfinite(value) else "white" if value < 0.5 else "black"
                ax.text(column, row, text, ha="center", va="center", fontsize=6.5, color=color)
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.035)
    base._require(tasks is not None, "task inventory is empty")
    axes[0].set_yticks(
        range(len(tasks)), labels=[task.removeprefix("chemcrow-") for task in tasks]
    )
    axes[0].set_ylabel("ChemCrow task")
    fig.suptitle("Within-task sibling similarity after manual semantic-scope decomposition")
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _plot_cross_task_roles(
    pair_rows: Sequence[dict[str, Any]],
    *,
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    components = ("combined", "general", "hybrid", "task_specific")
    component_labels = ("Combined", "General", "Hybrid", "Task-specific")
    fig, ax = plt.subplots(figsize=(13, 7))
    centers = np.arange(len(base.ARTIFACT_TYPES), dtype=float)
    width = 0.17
    colors = ("#59A14F", SCOPE_COLORS["General"], SCOPE_COLORS["Hybrid"], SCOPE_COLORS["Task-specific"])
    for offset_index, (component, label, color) in enumerate(
        zip(components, component_labels, colors, strict=True)
    ):
        datasets = []
        positions = []
        for role_index, artifact_type in enumerate(base.ARTIFACT_TYPES):
            values = [
                float(row[f"{component}_coverage"])
                for row in pair_rows
                if not row["same_task"]
                and row["type_a"] == artifact_type == row["type_b"]
                and row[f"{component}_coverage"] is not None
            ]
            datasets.append(values)
            positions.append(centers[role_index] + (offset_index - 1.5) * width)
        box = ax.boxplot(
            datasets,
            positions=positions,
            widths=width * 0.82,
            patch_artist=True,
            showfliers=False,
            manage_ticks=False,
        )
        for patch in box["boxes"]:
            patch.set_facecolor(color)
            patch.set_alpha(0.72)
        for median in box["medians"]:
            median.set_color("black")
        ax.plot([], [], color=color, linewidth=7, alpha=0.72, label=label)
    ax.set_xticks(centers, labels=["Memory", "Skill", "Agent System"])
    ax.set_ylim(0.0, 1.0)
    ax.set_ylabel("Cross-task same-role semantic coverage")
    ax.set_title("Role commonality decomposed by manually reviewed semantic scope")
    ax.grid(axis="y", alpha=0.22)
    ax.legend(ncols=4, loc="upper center", bbox_to_anchor=(0.5, -0.1))
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _plot_task_alignment(summary: dict[str, Any], *, output_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    tasks = sorted(summary["per_task"], key=base._task_sort_key)
    sibling = np.asarray([summary["per_task"][task]["same_task_combined_mean"] for task in tasks])
    cross = np.asarray([summary["per_task"][task]["cross_task_combined_mean"] for task in tasks])
    positions = np.arange(len(tasks))
    fig, ax = plt.subplots(figsize=(12, 8))
    for x, lower, upper in zip(positions, cross, sibling, strict=True):
        ax.plot([x, x], [lower, upper], color="#9E9E9E", linewidth=1.5)
    ax.scatter(positions, cross, color="#9C755F", s=48, label="Cross-task mean")
    ax.scatter(positions, sibling, color="#59A14F", s=48, label="Three sibling-pair mean")
    ax.set_xticks(positions, labels=[task.removeprefix("chemcrow-") for task in tasks])
    ax.set_ylim(0.0, 1.0)
    ax.set_xlabel("ChemCrow task")
    ax.set_ylabel("Combined semantic-unit coverage")
    ax.set_title("Does each task's three artifacts align more than its cross-task comparisons?")
    ax.grid(axis="y", alpha=0.22)
    ax.legend()
    fig.tight_layout()
    base._save_figure(fig, output_path)
    plt.close(fig)


def _markdown_report(
    *,
    source_path: Path,
    annotation_path: Path,
    result: ManualScopeResult,
) -> str:
    summary = result.summary
    overall = summary["composition"]["overall"]
    groups = summary["groups"]
    role = summary["cross_task_same_role"]
    highest = summary["highest_combined_pairs"][:10]
    by_task = summary["composition"]["by_task"]
    zero_task_specific = [
        task_id.removeprefix("chemcrow-")
        for task_id in sorted(by_task, key=base._task_sort_key)
        if by_task[task_id]["task_specific_unit_count"] == 0
    ]
    most_task_specific = max(
        by_task,
        key=lambda task_id: by_task[task_id]["task_specific_unit_fraction"],
    )
    zero_task_specific_text = ", ".join(zero_task_specific) or "none"
    most_task_specific_text = most_task_specific.removeprefix("chemcrow-")
    lines = [
        "# ChemCrow native artifact manual-scope similarity analysis",
        "",
        "Status: `COMPLETE`",
        "",
        "This is a post-generation descriptive analysis. It was not used by Candidate, Reflector, evaluator, injection, or selection.",
        "",
        "## What changed",
        "",
        f"- All {overall['semantic_unit_count']} semantic units were reviewed in the context of the exact task prompt, task obligations, artifact role, and neighboring units.",
        "- Corpus recurrence is not used to decide whether a unit is General.",
        "- General means transferable chemistry background or work behavior; Task-specific means dependence on the current target, reaction, requested property, safety category, or obligation; Hybrid records a material mixture in one unit.",
        "- Markdown labels such as Trigger, Action, or Validation do not determine the label.",
        "- The combined similarity view is retained unchanged; the three component views are diagnostics and are not additive.",
        "",
        "## Provenance and method",
        "",
        f"- Source: `{source_path}`",
        f"- Source SHA256: `{file_sha256(source_path)}`",
        f"- Manual annotation: `{annotation_path}`",
        f"- Annotation SHA256: `{file_sha256(annotation_path)}`",
        f"- Semantic units: `{overall['semantic_unit_count']}` across `{summary['artifact_count']}` artifacts.",
        f"- Embedding: `{result.metadata['embedding']['model_name']}`, `{result.metadata['embedding']['embedding_dimension']}` dimensions.",
        "- Unit-set comparison: order-invariant, bidirectional best-match coverage; ABC and BAC therefore match when their propositions match.",
        f"- Background cosine floor: `{result.metadata['background_calibration']['floor_cosine']:.4f}`; 99th-percentile cap: `{result.metadata['background_calibration']['cap_cosine']:.4f}`.",
        "- Training performed: no. Paid/model API calls: no.",
        "",
        "## Manual scope composition",
        "",
        "| Scope | Units | Unit share | Words | Word share |",
        "|---|---:|---:|---:|---:|",
    ]
    for scope in SCOPE_LABELS:
        key = scope.lower().replace("-", "_")
        lines.append(
            f"| {scope} | {overall[f'{key}_unit_count']} | "
            f"{overall[f'{key}_unit_fraction']:.3f} | {overall[f'{key}_word_count']} | "
            f"{overall[f'{key}_word_fraction']:.3f} |"
        )
    lines.extend(
        [
            "",
            "By artifact type (unit fractions):",
            "",
            "| Type | General | Hybrid | Task-specific |",
            "|---|---:|---:|---:|",
        ]
    )
    for artifact_type in base.ARTIFACT_TYPES:
        values = summary["composition"]["by_artifact_type"][artifact_type]
        lines.append(
            f"| {artifact_type} | {values['general_unit_fraction']:.3f} | "
            f"{values['hybrid_unit_fraction']:.3f} | "
            f"{values['task_specific_unit_fraction']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Similarity results",
            "",
            "| Pair group | Combined | General | Hybrid | Task-specific |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for group_name in (
        "all_pairs",
        "same_task_siblings",
        "same_type_cross_task",
        "different_type_cross_task",
    ):
        values = groups[group_name]
        formatted = []
        for component in ("combined", "general", "hybrid", "task_specific"):
            mean = values[component]["mean"]
            formatted.append("NA" if mean is None else f"{mean:.3f}")
        lines.append(f"| {group_name} | {' | '.join(formatted)} |")
    lines.extend(
        [
            "",
            f"Same-task siblings exceed different-role cross-task pairs by `{summary['same_task_alignment_effect']:.3f}` on the combined metric.",
            "",
            "Cross-task same-role means:",
            "",
            "| Role | Combined | General | Hybrid | Task-specific |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for artifact_type in base.ARTIFACT_TYPES:
        formatted = []
        for component in ("combined", "general", "hybrid", "task_specific"):
            mean = role[artifact_type][component]["mean"]
            formatted.append("NA" if mean is None else f"{mean:.3f}")
        lines.append(f"| {artifact_type} | {' | '.join(formatted)} |")
    lines.extend(
        [
            "",
            "## Distinctness audit",
            "",
            f"- Exact duplicate native contents: `{summary['exact_content_duplicate_count']}`.",
            f"- Combined semantic pairs at or above `{summary['semantic_near_duplicate_threshold']:.2f}`: `{summary['semantic_near_duplicate_pair_count']}`.",
            "- High similarity means shared propositions, not identity: every artifact remains bound to a unique content hash, artifact ID, and Reflector job ID.",
            "",
            "Highest combined semantic pairs:",
            "",
            "| Pair | Same task | Same role | Coverage |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in highest:
        lines.append(
            f"| {row['label_a']} / {row['label_b']} | {row['same_task']} | "
            f"{row['same_type']} | {float(row['combined_coverage']):.3f} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The expected hierarchy is a hypothesis, not a labeling rule: Agent System should carry more reusable organization, while Skill and Memory may specialize. The role-composition and cross-task-role plots show where that expectation holds and where it fails.",
            "",
            f"Tasks {zero_task_specific_text} contain no strictly Task-specific unit after review. This is not missing annotation: their native artifacts contain only reusable or mixed workflow/domain statements under the stated rubric. Task {most_task_specific_text} has the largest Task-specific unit share ({by_task[most_task_specific]['task_specific_unit_fraction']:.3f}).",
            "",
            "Hybrid is reported separately because forcing mixed units into General would inflate background commonality, while forcing them into Task-specific would erase genuinely reusable workflow content. Missing component cells are marked NA rather than imputed.",
            "",
            "Cosine similarity remains descriptive. It can show overlap after ignoring wording and order, but it cannot establish that advice is correct, non-conflicting, or causally responsible for G2 behavior; those questions still require the sealed semantic and mechanism audits.",
            "",
            "## Figures",
            "",
            "1. `01_scope_composition_by_unit.png`",
            "2. `02_scope_composition_by_words.png`",
            "3. `03_scope_composition_role_and_task.png`",
            "4. `04_unit_vs_word_general_equivalent.png`",
            "5. `05_combined_similarity_task_order.png`",
            "6. `06_combined_similarity_role_order.png`",
            "7. `07_general_similarity_role_order.png`",
            "8. `08_task_specific_similarity_task_order.png`",
            "9. `09_within_task_component_similarity.png`",
            "10. `10_cross_task_role_component_distribution.png`",
            "11. `11_per_task_alignment_effect.png`",
            "",
        ]
    )
    return "\n".join(lines)


def run_analysis(
    *,
    source_path: Path,
    annotation_path: Path,
    output_dir: Path,
    model_cache: Path,
    model_name: str = base.DEFAULT_EMBEDDING_MODEL,
    min_semantic_unit_words: int = base.DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
    batch_size: int = 16,
    threads: int = 1,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    annotation_path = annotation_path.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        base._require(not any(output_dir.iterdir()), f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    records, source_payload = base.load_artifacts(source_path)
    result = _compute_scope_result(
        records,
        annotation_path=annotation_path,
        source_path=source_path,
        model_name=model_name,
        model_cache=model_cache,
        min_words=min_semantic_unit_words,
        batch_size=batch_size,
        threads=threads,
    )

    index_rows = []
    for record in records:
        row = record.index_row(
            chunk_count=0,
            semantic_unit_count=int(result.artifact_composition[record.index]["semantic_unit_count"]),
        )
        row.pop("embedding_chunk_count")
        index_rows.append(row)
    base._write_csv(output_dir / "artifact_index.csv", index_rows, list(index_rows[0]))
    base._write_csv(
        output_dir / "semantic_unit_manual_adjudication.csv",
        result.unit_inventory,
        list(result.unit_inventory[0]),
    )
    base._write_csv(
        output_dir / "artifact_scope_composition.csv",
        result.artifact_composition,
        list(result.artifact_composition[0]),
    )
    base._write_csv(
        output_dir / "pairwise_scope_similarity.csv",
        result.pair_rows,
        list(result.pair_rows[0]),
    )
    for name, matrix in result.matrices.items():
        _write_matrix_csv(output_dir / f"{name}_coverage_matrix.csv", records, matrix)
    base._write_json(output_dir / "summary.json", result.summary)

    _plot_artifact_composition(
        result.artifact_composition,
        basis="unit",
        output_path=output_dir / "01_scope_composition_by_unit.png",
    )
    _plot_artifact_composition(
        result.artifact_composition,
        basis="word",
        output_path=output_dir / "02_scope_composition_by_words.png",
    )
    _plot_composition_summary(
        result.summary,
        output_path=output_dir / "03_scope_composition_role_and_task.png",
    )
    _plot_unit_word_delta(
        result.artifact_composition,
        output_path=output_dir / "04_unit_vs_word_general_equivalent.png",
    )
    task_boundaries = list(range(3, len(records), 3))
    _plot_matrix(
        result.matrices["combined"],
        records,
        title="All content: order-invariant semantic similarity (task order)",
        output_path=output_dir / "05_combined_similarity_task_order.png",
        boundaries=task_boundaries,
    )
    role_order = [
        record.index
        for artifact_type in base.ARTIFACT_TYPES
        for record in records
        if record.artifact_type == artifact_type
    ]
    task_count = len(records) // len(base.ARTIFACT_TYPES)
    _plot_matrix(
        result.matrices["combined"],
        records,
        title="All content: semantic similarity grouped by artifact role",
        output_path=output_dir / "06_combined_similarity_role_order.png",
        order=role_order,
        boundaries=[task_count, task_count * 2],
    )
    _plot_matrix(
        result.matrices["general"],
        records,
        title="Manually adjudicated General content only (role order)",
        output_path=output_dir / "07_general_similarity_role_order.png",
        order=role_order,
        boundaries=[task_count, task_count * 2],
    )
    _plot_matrix(
        result.matrices["task_specific"],
        records,
        title="Manually adjudicated Task-specific content only (task order; grey=NA)",
        output_path=output_dir / "08_task_specific_similarity_task_order.png",
        boundaries=task_boundaries,
    )
    _plot_within_task_components(
        records,
        result.matrices,
        output_path=output_dir / "09_within_task_component_similarity.png",
    )
    _plot_cross_task_roles(
        result.pair_rows,
        output_path=output_dir / "10_cross_task_role_component_distribution.png",
    )
    _plot_task_alignment(
        result.summary,
        output_path=output_dir / "11_per_task_alignment_effect.png",
    )
    readme = _markdown_report(
        source_path=source_path,
        annotation_path=annotation_path,
        result=result,
    )
    base._atomic_write_text(output_dir / "README.md", readme)
    receipt_name = "manual_scope_similarity_receipt.json"
    receipt = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "generated_at": datetime.now(UTC).isoformat(),
        "post_generation_only": True,
        "fed_to_candidate_reflector_or_evaluator": False,
        "training_performed": False,
        "paid_model_api_calls": 0,
        "source": {
            "path": str(source_path),
            "sha256": file_sha256(source_path),
            "schema_version": source_payload.get("schema_version"),
            "run_id": source_payload.get("run_id"),
        },
        "manual_annotation": result.metadata["annotation"],
        "embedding": result.metadata["embedding"],
        "semantic_units": result.metadata["semantic_units"],
        "background_calibration": result.metadata["background_calibration"],
        "matching": result.metadata["matching"],
        "summary_sha256": file_sha256(output_dir / "summary.json"),
        "output_manifest": base._output_manifest(output_dir, excluded={receipt_name}),
    }
    base._write_json(output_dir / receipt_name, receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate manual-scope semantic analysis for ChemCrow native artifacts."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--embedding-model", default=base.DEFAULT_EMBEDDING_MODEL)
    parser.add_argument(
        "--min-semantic-unit-words",
        type=int,
        default=base.DEFAULT_MIN_SEMANTIC_UNIT_WORDS,
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--threads", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = run_analysis(
        source_path=args.source,
        annotation_path=args.annotations,
        output_dir=args.output_dir,
        model_cache=args.model_cache,
        model_name=args.embedding_model,
        min_semantic_unit_words=args.min_semantic_unit_words,
        batch_size=args.batch_size,
        threads=args.threads,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
