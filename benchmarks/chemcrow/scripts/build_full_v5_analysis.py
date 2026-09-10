#!/usr/bin/env python3
"""Build a fail-closed, sealed-output-only analysis for a fresh ChemCrow v5 run.

The script is deliberately offline. It reads sealed pair results, aggregate/audit
receipts, optional post-generation annotations, and an optional completed paper
evaluator ledger. It has no model or network client and writes only new files.

Historical v4 raw artifacts are not assumed to exist. V4 values are accepted only
from an explicitly hash-bound tracked report, and that limitation is carried into
every generated report.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.paper_core import _audit_production_ledger
from openevo_chemcrow.paper_evaluator import (
    DualStudentAssessment,
    validate_paper_evaluation_plan,
)
from openevo_chemcrow.paper_recovery import load_paper_replacement_authority
from openevo_chemcrow.three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL,
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    ThreeArtifactPairResult,
)

TASK_IDS = tuple(
    f"chemcrow-{number}"
    for number in (
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "12",
        "13",
        "14",
        "15",
    )
)
DIMENSIONS = ("chemical_correctness", "reasoning_quality", "task_completion")
ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
AGGREGATE_AUDIT_ARTIFACT_PROTOCOL = CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL
SEALED_PAIR_ARTIFACT_PROTOCOL = CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL
AGGREGATE_SCHEMA_VERSION = "chemcrow_three_artifact_aggregate_v1"
COMPLETED_AUDIT_SCHEMA_VERSION = "chemcrow_three_artifact_completed_run_audit_v1"
PAIR_RESULT_SCHEMA_VERSION = "chemcrow_three_artifact_pair_v1"
INJECTION_RECEIPT_SCHEMA_VERSION = "3"
FROZEN_PAPER_PROMPT_SHA256 = "aa32cbc3190a53512bb121b167bcd68fb10cd92a5f07d30f68edcdda27ea0767"
BOOTSTRAP_SEED = 20260827
BOOTSTRAP_REPLICATES = 20_000

PROHIBITION_RE = re.compile(
    r"\b(do not|don't|never|avoid|must not|cannot|can't|withhold|"
    r"refus\w*|forbid\w*|no operational|no actionable)\b",
    re.IGNORECASE,
)
OUTPUT_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _close(left: float, right: float, *, tolerance: float = 1e-9) -> bool:
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)


def _load_pairs(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    _require(root.is_dir(), f"run root does not exist: {root}")
    all_paths = sorted(root.glob("*/pair.result.json"))
    _require(len(all_paths) == len(TASK_IDS), f"expected exactly 14 pair seals: {root}")
    pairs: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    for path in all_paths:
        pair = _load_json(path)
        task_id = pair.get("task_id")
        _require(task_id in TASK_IDS, f"unexpected pair task: {path}")
        _require(task_id not in pairs, f"duplicate pair authority: {task_id}")
        pairs[task_id] = pair
        paths[task_id] = path
    _require(set(pairs) == set(TASK_IDS), "pair task inventory differs from authoritative 14")
    return pairs, paths


def _score_total(scores: dict[str, Any]) -> float:
    _require(set(scores) == set(DIMENSIONS), "score dimension inventory differs")
    return sum(float(scores[dimension]) for dimension in DIMENSIONS)


def _validate_current_run(
    *,
    run_root: Path,
    aggregate_path: Path,
    completed_audit_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Path], dict[str, Any]]:
    pairs, pair_paths = _load_pairs(run_root)
    aggregate = _load_json(aggregate_path)
    audit = _load_json(completed_audit_path)

    _require(
        aggregate.get("schema_version") == AGGREGATE_SCHEMA_VERSION,
        "aggregate schema version differs",
    )
    _require(aggregate.get("task_count") == 14, "aggregate task_count must be 14")
    _require(aggregate.get("artifact_count") == 42, "aggregate artifact_count must be 42")
    _require(
        aggregate.get("artifact_protocol") == AGGREGATE_AUDIT_ARTIFACT_PROTOCOL,
        "aggregate artifact protocol differs",
    )
    _require(
        audit.get("schema_version") == COMPLETED_AUDIT_SCHEMA_VERSION,
        "completed-run audit schema version differs",
    )
    _require(audit.get("status") == "PASS", "completed-run audit is not PASS")
    _require(audit.get("answers_included") is False, "completed-run audit includes answers")
    _require(audit.get("task_count") == 14, "completed-run audit task_count must be 14")
    _require(set(audit.get("task_ids", [])) == set(TASK_IDS), "audit task IDs differ")
    _require(
        audit.get("aggregate_sha256") == file_sha256(aggregate_path),
        "completed-run audit is not bound to the supplied aggregate",
    )
    _require(
        audit.get("artifact_protocol") == AGGREGATE_AUDIT_ARTIFACT_PROTOCOL,
        "completed-run audit artifact protocol differs",
    )
    exact_counts = {
        "artifact_registration_count": 42,
        "unique_artifact_count": 42,
        "independent_reflector_job_count": 42,
        "independent_reflector_run_count": 42,
        "unique_reflector_job_count": 42,
        "unique_reflector_run_count": 42,
        "memory_reflector_job_count": 14,
        "skill_reflector_job_count": 14,
        "agent_system_reflector_job_count": 14,
        "core_evolved_injection_receipt_count": 14,
        "reset_receipt_count": 14,
        "sibling_isolation_evidence_count": 42,
        "mock_or_fixture_observations": 0,
    }
    for field, expected in exact_counts.items():
        _require(audit.get(field) == expected, f"completed-run audit {field} != {expected}")

    artifact_ids: list[str] = []
    reflector_job_ids: list[str] = []
    reflector_run_ids: list[str] = []
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        _require(
            pair.get("schema_version") == PAIR_RESULT_SCHEMA_VERSION,
            f"pair schema version differs: {task_id}",
        )
        bundle = pair.get("artifact_bundle", {})
        artifacts = bundle.get("artifacts", [])
        _require(bundle.get("task_id") == task_id, f"bundle task differs: {task_id}")
        _require(
            bundle.get("protocol") == SEALED_PAIR_ARTIFACT_PROTOCOL,
            f"bundle protocol differs: {task_id}",
        )
        _require(len(artifacts) == 3, f"artifact count differs: {task_id}")
        _require(
            [item.get("artifact_type") for item in artifacts] == list(ARTIFACT_TYPES),
            f"artifact type order differs: {task_id}",
        )
        _require(not bundle.get("byte_identical_pairs"), f"byte duplicate: {task_id}")
        _require(not bundle.get("normalized_identical_pairs"), f"normalized duplicate: {task_id}")
        _require(not bundle.get("near_duplicate_pairs"), f"near duplicate: {task_id}")
        for artifact in artifacts:
            _require(artifact.get("task_id") == task_id, f"artifact task differs: {task_id}")
            _require(
                artifact.get("sibling_outputs_visible") is False,
                f"sibling isolation differs: {task_id}",
            )
            artifact_ids.append(str(artifact["artifact_id"]))
            reflector_job_ids.append(str(artifact["reflector_job_id"]))
            reflector_run_ids.append(str(artifact["reflector_run_id"]))
        receipt = pair.get("injection_receipt", {})
        _require(
            receipt.get("schema_version") == INJECTION_RECEIPT_SCHEMA_VERSION,
            f"injection receipt schema version differs: {task_id}",
        )
        receipt_ids = {
            receipt.get("memory_artifact_id"),
            receipt.get("skill_artifact_id"),
            receipt.get("agent_system_artifact_id"),
        }
        _require(receipt.get("artifact_count") == 3, f"injection count differs: {task_id}")
        _require(
            receipt_ids == {item["artifact_id"] for item in artifacts},
            f"injection IDs differ: {task_id}",
        )
        _require(not receipt.get("unexpected_artifact_ids"), f"unexpected injection: {task_id}")
        _require(bool(pair.get("reset_receipt_sha256")), f"reset receipt absent: {task_id}")

    _require(len(set(artifact_ids)) == 42, "artifact IDs are not globally unique")
    _require(len(set(reflector_job_ids)) == 42, "reflector job IDs are not globally unique")
    _require(len(set(reflector_run_ids)) == 42, "reflector run IDs are not globally unique")

    # The aggregate contract is built from the sealed blind G1/G2 evaluator,
    # not the separate evolution-evaluator scores.
    computed_aggregate = _score_means(pairs, source="blind")
    for label, aggregate_key in (("g1", "mean_baseline"), ("g2", "mean_evolved")):
        for dimension in DIMENSIONS:
            _require(
                _close(computed_aggregate[label][dimension], aggregate[aggregate_key][dimension]),
                f"aggregate {aggregate_key}.{dimension} differs from sealed pairs",
            )

    evidence = {
        "run_root": str(run_root.resolve()),
        "aggregate_path": str(aggregate_path.resolve()),
        "aggregate_sha256": file_sha256(aggregate_path),
        "completed_audit_path": str(completed_audit_path.resolve()),
        "completed_audit_sha256": file_sha256(completed_audit_path),
        "pair_result_sha256": {task_id: file_sha256(pair_paths[task_id]) for task_id in TASK_IDS},
        "pair_count": 14,
        "artifact_count": 42,
        "unique_artifact_count": 42,
        "unique_reflector_job_count": 42,
        "unique_reflector_run_count": 42,
        "status": "PASS",
    }
    return pairs, pair_paths, evidence


def _artifact_content(artifact: dict[str, Any], *, artifacts_root: Path) -> str:
    root = artifacts_root.resolve()
    _require(root.is_dir(), f"Core artifact root does not exist: {root}")
    manifests = list(root.rglob(f"{artifact['artifact_id']}/manifest.json"))
    _require(
        len(manifests) == 1,
        f"artifact manifest authority is not unique: {artifact['artifact_id']}",
    )
    manifest = _load_json(manifests[0])
    uri = manifest.get("uri")
    _require(isinstance(uri, str) and uri.startswith("file://"), "artifact URI is not file://")
    payload = Path(uri.removeprefix("file://")).resolve()
    _require(payload.is_relative_to(root), f"artifact payload escapes supplied root: {payload}")
    if payload.is_dir():
        files = sorted(path for path in payload.rglob("*") if path.is_file())
        _require(bool(files), f"artifact payload directory is empty: {artifact['artifact_id']}")
        content = "\n\n".join(path.read_text(encoding="utf-8") for path in files)
    else:
        _require(payload.is_file(), f"artifact payload does not exist: {payload}")
        content = payload.read_text(encoding="utf-8")
    content_bytes = content.encode("utf-8")
    _require(len(content_bytes) == artifact.get("size_bytes"), "artifact size receipt differs")
    _require(
        hashlib.sha256(content_bytes).hexdigest() == artifact.get("artifact_hash"),
        "artifact content hash differs",
    )
    return content


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _trigram_jaccard(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    left_set = set(zip(left_tokens, left_tokens[1:], left_tokens[2:], strict=False))
    right_set = set(zip(right_tokens, right_tokens[1:], right_tokens[2:], strict=False))
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def _artifact_mechanics(
    pairs: dict[str, dict[str, Any]], *, artifacts_root: Path
) -> dict[str, Any]:
    tasks: dict[str, Any] = {}
    total_bytes = total_tokens = total_prohibitions = 0
    all_similarities: list[float] = []
    all_artifact_ids: list[str] = []
    byte_duplicates = normalized_duplicates = near_duplicates = 0
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        artifacts = pair["artifact_bundle"]["artifacts"]
        contents = [
            _artifact_content(artifact, artifacts_root=artifacts_root) for artifact in artifacts
        ]
        per_artifact = []
        for artifact, content in zip(artifacts, contents, strict=True):
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            per_artifact.append(
                {
                    "artifact_id": artifact["artifact_id"],
                    "artifact_type": artifact["artifact_type"],
                    "reflector_job_id": artifact["reflector_job_id"],
                    "content_sha256": content_sha256,
                    "content_bytes": len(content.encode("utf-8")),
                    "token_count": len(_tokens(content)),
                    "prohibition_line_count": sum(
                        bool(PROHIBITION_RE.search(line)) for line in content.splitlines()
                    ),
                }
            )
            all_artifact_ids.append(artifact["artifact_id"])
        similarities = [
            _trigram_jaccard(contents[left], contents[right])
            for left, right in itertools.combinations(range(3), 2)
        ]
        bundle = pair["artifact_bundle"]
        byte_duplicates += len(bundle["byte_identical_pairs"])
        normalized_duplicates += len(bundle["normalized_identical_pairs"])
        near_duplicates += len(bundle["near_duplicate_pairs"])
        task_bytes = sum(item["content_bytes"] for item in per_artifact)
        task_tokens = sum(item["token_count"] for item in per_artifact)
        task_prohibitions = sum(item["prohibition_line_count"] for item in per_artifact)
        total_bytes += task_bytes
        total_tokens += task_tokens
        total_prohibitions += task_prohibitions
        all_similarities.extend(similarities)
        tasks[task_id] = {
            "artifacts": per_artifact,
            "total_content_bytes": task_bytes,
            "total_token_count": task_tokens,
            "prohibition_line_count": task_prohibitions,
            "mean_cross_artifact_trigram_jaccard": statistics.mean(similarities),
        }
    return {
        "artifact_count": len(all_artifact_ids),
        "unique_artifact_count": len(set(all_artifact_ids)),
        "byte_duplicate_pair_count": byte_duplicates,
        "normalized_duplicate_pair_count": normalized_duplicates,
        "near_duplicate_pair_count": near_duplicates,
        "total_content_bytes": total_bytes,
        "total_token_count": total_tokens,
        "prohibition_line_count": total_prohibitions,
        "mean_cross_artifact_trigram_jaccard": statistics.mean(all_similarities),
        "tasks": tasks,
    }


def _score_means(pairs: dict[str, dict[str, Any]], *, source: str) -> dict[str, Any]:
    if source == "internal":
        baseline = [
            pairs[task_id]["baseline_internal_evaluation"]["scores"] for task_id in TASK_IDS
        ]
        evolved = [pairs[task_id]["evolved_internal_evaluation"]["scores"] for task_id in TASK_IDS]
    elif source == "blind":
        baseline = [pairs[task_id]["final_evaluation"]["baseline_scores"] for task_id in TASK_IDS]
        evolved = [pairs[task_id]["final_evaluation"]["evolved_scores"] for task_id in TASK_IDS]
    else:
        raise ValueError(source)
    means: dict[str, Any] = {}
    for label, rows in (("g1", baseline), ("g2", evolved)):
        dimension_means = {
            dimension: statistics.mean(float(row[dimension]) for row in rows)
            for dimension in DIMENSIONS
        }
        means[label] = dimension_means
        means[f"{label}_total"] = sum(dimension_means.values())
    means["delta"] = {
        dimension: means["g2"][dimension] - means["g1"][dimension] for dimension in DIMENSIONS
    }
    means["delta_total"] = means["g2_total"] - means["g1_total"]
    return means


def _pair_deltas(pairs: dict[str, dict[str, Any]], *, source: str) -> list[float]:
    output = []
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        if source == "internal":
            g1 = _score_total(pair["baseline_internal_evaluation"]["scores"])
            g2 = _score_total(pair["evolved_internal_evaluation"]["scores"])
        elif source == "blind":
            g1 = _score_total(pair["final_evaluation"]["baseline_scores"])
            g2 = _score_total(pair["final_evaluation"]["evolved_scores"])
        else:
            raise ValueError(source)
        output.append(g2 - g1)
    return output


def _sign_test(deltas: Sequence[float]) -> float:
    nonzero = [value for value in deltas if value != 0]
    if not nonzero:
        return 1.0
    positives = sum(value > 0 for value in nonzero)
    smaller = min(positives, len(nonzero) - positives)
    probability = (
        2
        * sum(math.comb(len(nonzero), value) for value in range(smaller + 1))
        / (2 ** len(nonzero))
    )
    return min(1.0, probability)


def _wilcoxon_exact(deltas: Sequence[float]) -> float:
    """Exact two-sided signed-rank sign-flip p-value, with zero deltas omitted."""

    nonzero = [value for value in deltas if value != 0]
    if not nonzero:
        return 1.0
    ordered = sorted((abs(value), index) for index, value in enumerate(nonzero))
    ranks = [0.0] * len(nonzero)
    start = 0
    while start < len(ordered):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[start][0]:
            end += 1
        rank = ((start + 1) + (end + 1)) / 2
        for cursor in range(start, end + 1):
            ranks[ordered[cursor][1]] = rank
        start = end + 1
    positive_rank = sum(rank for rank, value in zip(ranks, nonzero, strict=True) if value > 0)
    total_rank = sum(ranks)
    observed = abs(positive_rank - total_rank / 2)
    extreme = 0
    for mask in range(1 << len(nonzero)):
        permuted = sum(rank for index, rank in enumerate(ranks) if (mask >> index) & 1)
        extreme += abs(permuted - total_rank / 2) >= observed - 1e-12
    return extreme / (1 << len(nonzero))


def _paired_stats(deltas: Sequence[float]) -> dict[str, Any]:
    values = [float(value) for value in deltas]
    _require(bool(values), "paired statistics require at least one delta")
    randomizer = random.Random(BOOTSTRAP_SEED)
    bootstrap = sorted(
        statistics.mean(values[randomizer.randrange(len(values))] for _ in values)
        for _ in range(BOOTSTRAP_REPLICATES)
    )
    return {
        "n": len(values),
        "raw_paired_deltas": values,
        "mean_delta": statistics.mean(values),
        "median_delta": statistics.median(values),
        "wins": sum(value > 0 for value in values),
        "ties": sum(value == 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "paired_bootstrap_95_ci_mean_delta": [
            bootstrap[int(0.025 * len(bootstrap))],
            bootstrap[int(0.975 * len(bootstrap)) - 1],
        ],
        "wilcoxon_signed_rank_exact_two_sided_p": _wilcoxon_exact(values),
        "sign_test_exact_binomial_two_sided_p": _sign_test(values),
        "nonzero_pair_count": sum(value != 0 for value in values),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_replicates": BOOTSTRAP_REPLICATES,
        "limitations": (
            "N=14 and scores are discrete with ties. Bootstrap intervals and exact paired "
            "tests are descriptive and low-powered; zero deltas are omitted by the signed-rank "
            "and sign tests."
        ),
    }


def _load_historical_v4(
    path: Path, *, expected_sha256: str
) -> tuple[dict[str, Any], dict[str, dict[str, float]], dict[str, Any]]:
    _require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None, "invalid v4 SHA256")
    actual_sha256 = file_sha256(path)
    _require(actual_sha256 == expected_sha256, "historical v4 report SHA256 differs")
    report = _load_json(path)
    _require(
        report.get("schema_version") == "chemcrow_full_v5_core_native_experiment_analysis_v1",
        "unsupported historical report schema",
    )
    _require(
        report.get("status") == "FULL_V5_CORE_NATIVE_EXPERIMENT_COMPLETE",
        "historical source report is not complete",
    )
    _require(report.get("task_ids") == list(TASK_IDS), "historical task ordering differs")
    _require(len(report.get("per_task", [])) == 14, "historical per-task inventory differs")

    rows = {row["task_id"]: row for row in report["per_task"]}
    _require(set(rows) == set(TASK_IDS), "historical per-task IDs differ")
    delta_by_layer: dict[str, dict[str, float]] = {}
    layer_fields = {
        "internal_evaluator": "internal_delta",
        "blind_evaluator": "blind_delta",
        "paper_evaluator": "paper_delta",
    }
    for layer, field in layer_fields.items():
        changes = report["v4_v5_delta_comparison"][layer]["per_task"]
        _require(set(changes) == set(TASK_IDS), f"historical {layer} change IDs differ")
        v4_deltas = {
            task_id: float(rows[task_id][field]) - float(changes[task_id]) for task_id in TASK_IDS
        }
        summary_key = "delta_mean" if layer == "paper_evaluator" else "delta_total"
        _require(
            _close(
                statistics.mean(v4_deltas.values()),
                report["v4"][layer][summary_key],
            ),
            f"historical reconstructed {layer} mean differs",
        )
        delta_by_layer[layer] = v4_deltas

    for task_id in TASK_IDS:
        direct = float(rows[task_id]["v4_paper_g2"]) - float(rows[task_id]["v4_paper_g1"])
        _require(
            _close(direct, delta_by_layer["paper_evaluator"][task_id]),
            f"historical v4 paper delta differs: {task_id}",
        )

    evidence = {
        "mode": "tracked_immutable_historical_report_only",
        "report_path": str(path.resolve()),
        "report_sha256": actual_sha256,
        "raw_v4_pair_root_available": False,
        "raw_v4_artifacts_available": False,
        "live_v4_recomputation_performed": False,
        "limitations": (
            "The raw v4 run/artifact root is unavailable. V4 aggregate values, artifact "
            "mechanics, and reconstructed paired deltas come only from this exact archived "
            "tracked report. They were consistency-checked but cannot be re-derived from raw v4."
        ),
    }
    return deepcopy(report["v4"]), delta_by_layer, evidence


def _load_semantic_audit(
    path: Path | None,
    *,
    pairs: dict[str, dict[str, Any]],
    pair_paths: dict[str, Path],
    completed_audit_sha256: str,
) -> dict[str, Any]:
    if path is None:
        return {
            "status": "NOT_PROVIDED",
            "task_count": 0,
            "artifact_count": 0,
            "limitations": (
                "No fresh, artifact-bound post-generation semantic annotations were supplied."
            ),
        }
    annotations = _load_json(path)
    _require(
        annotations.get("created_after_all_pairs_sealed") is True,
        "semantic annotations were not created after pair sealing",
    )
    _require(
        annotations.get("fed_to_candidate_or_reflector") is False,
        "semantic annotations entered evolution",
    )
    _require(
        annotations.get("source_completed_run_audit_sha256") == completed_audit_sha256,
        "semantic annotations are not bound to the supplied completed-run audit",
    )
    tasks = annotations.get("tasks", {})
    _require(set(tasks) == set(TASK_IDS), "semantic annotation task inventory differs")
    output_tasks: dict[str, Any] = {}
    for task_id in TASK_IDS:
        annotation = tasks[task_id]
        _require(
            annotation.get("source_pair_result_sha256") == file_sha256(pair_paths[task_id]),
            f"semantic annotation pair binding differs: {task_id}",
        )
        pair_artifacts = {
            artifact["artifact_type"]: artifact
            for artifact in pairs[task_id]["artifact_bundle"]["artifacts"]
        }
        findings = annotation.get("artifact_findings", {})
        _require(
            set(findings) == set(ARTIFACT_TYPES),
            f"semantic artifact types differ: {task_id}",
        )
        clean_findings = {}
        for artifact_type in ARTIFACT_TYPES:
            finding = findings[artifact_type]
            _require(
                finding.get("artifact_id") == pair_artifacts[artifact_type]["artifact_id"],
                f"semantic artifact binding differs: {task_id}/{artifact_type}",
            )
            clean_findings[artifact_type] = finding
        task_level = annotation.get("task_level", {})
        required_labels = {
            "TASK_CONFLICT",
            "CONSTRAINT_AMPLIFICATION",
            "UNSUPPORTED_GENERALIZATION",
            "INSTRUCTION_OVERLOAD",
            "POSITIVE_CORRECTION",
            "EVIDENCE_DISCIPLINE",
        }
        _require(set(task_level) == required_labels, f"semantic labels differ: {task_id}")
        _require(
            all(isinstance(task_level[label], bool) for label in required_labels),
            f"semantic labels are not booleans: {task_id}",
        )
        output_tasks[task_id] = {
            "source_pair_result_sha256": annotation["source_pair_result_sha256"],
            "task_level": task_level,
            "artifact_findings": clean_findings,
            "summary": annotation.get("summary"),
        }
    return {
        "schema_version": "chemcrow_full_v5_core_native_semantic_audit_v2",
        "status": "POST_GENERATION_AUDIT_COMPLETE",
        "source_path": str(path.resolve()),
        "source_sha256": file_sha256(path),
        "source_completed_run_audit_sha256": completed_audit_sha256,
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "task_count": 14,
        "artifact_count": 42,
        "task_conflict_count": sum(
            item["task_level"]["TASK_CONFLICT"] for item in output_tasks.values()
        ),
        "constraint_amplification_task_count": sum(
            item["task_level"]["CONSTRAINT_AMPLIFICATION"] for item in output_tasks.values()
        ),
        "instruction_overload_task_count": sum(
            item["task_level"]["INSTRUCTION_OVERLOAD"] for item in output_tasks.values()
        ),
        "tasks": output_tasks,
    }


def _paper_records(root: Path) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    aggregate = _load_json(root / "results" / "aggregate.json")
    grades: dict[str, dict[str, float]] = {}
    paths = sorted((root / "results").glob("*.result.json"))
    _require(len(paths) == 42, "paper result count must be 42")
    for path in paths:
        result = _load_json(path)
        assessment = DualStudentAssessment.model_validate(result["assessment"])
        task_id = result.get("task_id")
        comparison = result.get("comparison")
        _require(task_id in TASK_IDS, f"paper task ID differs: {path}")
        _require(
            comparison in {"historical_control", "baseline", "evolved"},
            "paper comparison differs",
        )
        _require(comparison not in grades.setdefault(task_id, {}), "duplicate paper result")
        grades[task_id][comparison] = float(assessment.student_a.grade)
    _require(set(grades) == set(TASK_IDS), "paper task inventory differs")
    _require(all(len(values) == 3 for values in grades.values()), "paper inventory is incomplete")
    return aggregate, grades


def _canonical_pair_sha256(pair: dict[str, Any]) -> str:
    validated = ThreeArtifactPairResult.model_validate(pair)
    return canonical_sha256(validated.model_dump(mode="json"))


def _audit_paper(
    root: Path | None,
    *,
    pairs: dict[str, dict[str, Any]],
    source_run_root: Path | None = None,
    completed_run_audit: Path | None = None,
    replacement_authority_path: Path | None = None,
    core_completion_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, float]] | None, dict[str, Any] | None]:
    recovery_arguments = (replacement_authority_path, core_completion_root)
    _require(
        all(value is None for value in recovery_arguments)
        or all(value is not None for value in recovery_arguments),
        "paper recovery requires both replacement authority and Core completion root",
    )
    if root is None:
        _require(
            all(value is None for value in recovery_arguments),
            "paper recovery arguments require a paper production root",
        )
        return (
            {
                "status": "NOT_PROVIDED",
                "call_count": 0,
                "limitations": "No fresh v5 paper-evaluator production root was supplied.",
            },
            None,
            None,
        )
    plan_path = root / "private" / "plan.json"
    plan = _load_json(plan_path)
    calls = plan.get("calls", [])
    _require(len(calls) == 42, "paper plan must contain 42 calls")
    validated_calls = validate_paper_evaluation_plan(plan)
    _require(len(validated_calls) == 42, "validated paper plan must contain 42 calls")
    _require(
        plan.get("prompt_candidate_sha256") == FROZEN_PAPER_PROMPT_SHA256,
        "paper prompt hash differs",
    )
    plan_calls = {call["call_id"]: call for call in calls}
    _require(len(plan_calls) == 42, "paper call IDs are not unique")
    comparison_counts = Counter(call.get("comparison") for call in calls)
    _require(
        comparison_counts == Counter({"historical_control": 14, "baseline": 14, "evolved": 14}),
        "paper comparison families differ",
    )
    _require(
        {(call.get("task_id"), call.get("comparison")) for call in calls}
        == {
            (task_id, comparison)
            for task_id in TASK_IDS
            for comparison in ("historical_control", "baseline", "evolved")
        },
        "paper task/comparison inventory differs",
    )
    for call in calls:
        task_id = call.get("task_id")
        _require(task_id in TASK_IDS, "paper plan task ID differs")
        if call["comparison"] in {"baseline", "evolved"}:
            pair = pairs[task_id]
            output = pair[call["comparison"]]
            _require(call.get("source_pair_id") == pair.get("pair_id"), "paper pair ID differs")
            _require(
                call.get("source_pair_result_sha256") == _canonical_pair_sha256(pair),
                "paper canonical pair result hash differs",
            )
            _require(
                call.get("source_output_id") == output.get("run_id"),
                "paper source output ID differs",
            )
            _require(
                call.get("source_output_sha256") == canonical_sha256(output.get("answer")),
                "paper source output hash differs",
            )
            _require(
                call.get("target_answer_sha256") == call.get("source_output_sha256"),
                "paper target/source output hash differs",
            )

    replacement_authority = None
    if replacement_authority_path is not None:
        _require(source_run_root is not None, "paper recovery source run root is absent")
        _require(completed_run_audit is not None, "paper recovery completed-run audit is absent")
        _require(core_completion_root is not None, "paper recovery Core completion root is absent")
        replacement_authority = load_paper_replacement_authority(
            authority_path=replacement_authority_path,
            plan_path=plan_path,
            result_root=root / "results",
            receipt_root=root / "openrouter-receipts",
            source_run_root=source_run_root,
            completed_run_audit=completed_run_audit,
            core_completion_root=core_completion_root,
        )
    ledger_audit = _audit_production_ledger(
        calls=validated_calls,
        result_root=root / "results",
        shim_receipt_root=root / "openrouter-receipts",
        replacement_authority=replacement_authority,
        replacement_authority_path=replacement_authority_path,
    )
    aggregate, grades = _paper_records(root)
    _require(
        aggregate.get("plan_sha256") == file_sha256(plan_path),
        "paper aggregate plan binding differs",
    )
    expected_aggregate_fields = {
        "schema_version": "chemcrow_paper_evaluator_aggregate_v1",
        "status": "PROVISIONAL_LLM_JUDGED_RESULT",
        "protocol": plan.get("protocol"),
        "model": "openai/gpt-4",
        "task_count": 14,
        "result_count": 42,
        "reflector_access": False,
        "final_evaluation_only": True,
    }
    for field, expected in expected_aggregate_fields.items():
        _require(aggregate.get(field) == expected, f"paper aggregate {field} differs")
    for field, expected in ledger_audit.items():
        observed = aggregate.get(field)
        if field in {"actual_openrouter_reported_cost_usd", "list_price_cost_usd"}:
            _require(
                not isinstance(observed, bool)
                and isinstance(observed, (int, float))
                and _close(observed, expected),
                f"paper aggregate {field} differs",
            )
        else:
            _require(observed == expected, f"paper aggregate {field} differs")

    actual_attempt_count = int(ledger_audit["actual_upstream_call_count"])
    audit = {
        "schema_version": "chemcrow_full_v5_paper_evaluator_audit_v2",
        "status": "PASS",
        "root": str(root.resolve()),
        "protocol": plan.get("protocol"),
        "prompt_sha256": plan["prompt_candidate_sha256"],
        "plan_sha256": file_sha256(plan_path),
        "source_pair_protocol": plan.get("source_pair_protocol"),
        "call_count": 42,
        **ledger_audit,
        "result_count": 42,
        "model_counts": {"openai/gpt-4": actual_attempt_count},
        "provider_counts": {"OpenAI": actual_attempt_count},
        "replacement_authority_used": replacement_authority is not None,
        "replacement_authority_path": (
            str(replacement_authority_path.resolve())
            if replacement_authority_path is not None
            else None
        ),
        "replacement_authority_sha256": (
            file_sha256(replacement_authority_path)
            if replacement_authority_path is not None
            else None
        ),
        "calibration_ledger_included": False,
        "v4_paper_ledger_included": False,
    }
    return audit, grades, aggregate


def _delta_comparison(
    *, current: dict[str, float], historical: dict[str, float]
) -> dict[str, Any]:
    _require(set(current) == set(TASK_IDS), "current delta IDs differ")
    _require(set(historical) == set(TASK_IDS), "historical delta IDs differ")
    changes = [current[task_id] - historical[task_id] for task_id in TASK_IDS]
    return {
        "mean_delta_change": statistics.mean(changes),
        "median_delta_change": statistics.median(changes),
        "improved": sum(value > 0 for value in changes),
        "same": sum(value == 0 for value in changes),
        "worsened": sum(value < 0 for value in changes),
        "paired": _paired_stats(changes),
        "per_task": {
            task_id: {
                "delta_v4": historical[task_id],
                "delta_v5": current[task_id],
                "delta_v5_minus_delta_v4": current[task_id] - historical[task_id],
            }
            for task_id in TASK_IDS
        },
    }


def _fmt(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.3f}"


def _main_report(analysis: dict[str, Any]) -> str:
    internal = analysis["v5"]["internal_evaluator"]
    blind = analysis["v5"]["blind_evaluator"]
    paper = analysis["v5"]["paper_evaluator"]
    lines = [
        "# Fresh full-v5 Core-native three-Reflector analysis",
        "",
        "## Status",
        "",
        f"`{analysis['status']}`",
        "",
        "The engineering result is bound to the explicitly supplied run root, aggregate, and completed-run audit. No model call is made by this analysis script.",
        "",
        "## Aggregate scores",
        "",
        "| Layer | G1 | G2 | G2-G1 | wins/ties/losses |",
        "|---|---:|---:|---:|---:|",
        f"| Evolution evaluator (/12) | {_fmt(internal['g1_total'])} | {_fmt(internal['g2_total'])} | {_fmt(internal['delta_total'])} | {internal['paired']['wins']}/{internal['paired']['ties']}/{internal['paired']['losses']} |",
        f"| Blind internal evaluator (/12) | {_fmt(blind['g1_total'])} | {_fmt(blind['g2_total'])} | {_fmt(blind['delta_total'])} | {blind['paired']['wins']}/{blind['paired']['ties']}/{blind['paired']['losses']} |",
    ]
    if paper["status"] == "PASS":
        lines.append(
            f"| Paper-compatible GPT-4 (/10) | {_fmt(paper['g1_mean'])} | {_fmt(paper['g2_mean'])} | {_fmt(paper['delta_mean'])} | {paper['paired']['wins']}/{paper['paired']['ties']}/{paper['paired']['losses']} |"
        )
    else:
        lines.append(
            "| Paper-compatible GPT-4 (/10) | unavailable | unavailable | unavailable | unavailable |"
        )
    lines.extend(
        [
            "",
            "## Statistical reporting",
            "",
            f"- Internal: mean {internal['paired']['mean_delta']:.3f}, median {internal['paired']['median_delta']:.3f}, bootstrap 95% CI {internal['paired']['paired_bootstrap_95_ci_mean_delta']}, Wilcoxon p={internal['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}, exact sign/binomial p={internal['paired']['sign_test_exact_binomial_two_sided_p']:.4f}.",
            f"- Blind: mean {blind['paired']['mean_delta']:.3f}, median {blind['paired']['median_delta']:.3f}, bootstrap 95% CI {blind['paired']['paired_bootstrap_95_ci_mean_delta']}, Wilcoxon p={blind['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}, exact sign/binomial p={blind['paired']['sign_test_exact_binomial_two_sided_p']:.4f}.",
        ]
    )
    if paper["status"] == "PASS":
        lines.append(
            f"- Paper: mean {paper['paired']['mean_delta']:.3f}, median {paper['paired']['median_delta']:.3f}, bootstrap 95% CI {paper['paired']['paired_bootstrap_95_ci_mean_delta']}, Wilcoxon p={paper['paired']['wilcoxon_signed_rank_exact_two_sided_p']:.4f}, exact sign/binomial p={paper['paired']['sign_test_exact_binomial_two_sided_p']:.4f}."
        )
    else:
        lines.append("- Paper: unavailable; no paper production root was supplied.")
    lines.extend(
        [
            "",
            "Raw paired deltas and every task-paired v4/v5 delta are preserved in the machine-readable analysis. N=14 and tied discrete scores limit inferential power.",
            "",
            "## Historical v4 evidence limitation",
            "",
            analysis["historical_v4_evidence"]["limitations"],
            "",
            "## Component status",
            "",
            f"- Completed-run audit: `{analysis['components']['engineering']}`",
            f"- Fresh semantic audit: `{analysis['components']['semantic']}`",
            f"- Fresh paper evaluator: `{analysis['components']['paper']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _mechanism_report(analysis: dict[str, Any]) -> str:
    v4 = analysis["v4"]
    v5 = analysis["v5"]
    comparison = analysis["v4_v5_delta_comparison"]
    lines = [
        "# Fresh v4-v5 Core-native mechanism comparison",
        "",
        "## Evidence boundary",
        "",
        analysis["historical_v4_evidence"]["limitations"],
        "",
        "V5 is recomputed from the explicitly supplied fresh sealed pairs. V4 is not recomputed from raw evidence.",
        "",
        "## Architecture",
        "",
        "| Version | Reflectors | Adapter semantic shaping |",
        "|---|---|---|",
        "| v4 | three isolated jobs | ChemCrow role/responsibility shaping |",
        "| v5 | three isolated Core-native jobs | none; engineering boundary checks only |",
        "",
        "## Task-paired evolution-delta comparison",
        "",
        "| Layer | v4 delta | v5 delta | mean change | improved/same/worsened |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, label in (
        ("internal_evaluator", "Evolution evaluator /12"),
        ("blind_evaluator", "Blind internal evaluator /12"),
    ):
        left, right, change = v4[key], v5[key], comparison[key]
        lines.append(
            f"| {label} | {left['delta_total']:.3f} | {right['delta_total']:.3f} | {change['mean_delta_change']:.3f} | {change['improved']}/{change['same']}/{change['worsened']} |"
        )
    if v5["paper_evaluator"]["status"] == "PASS":
        change = comparison["paper_evaluator"]
        lines.append(
            f"| Paper-compatible GPT-4 /10 | {v4['paper_evaluator']['delta_mean']:.3f} | {v5['paper_evaluator']['delta_mean']:.3f} | {change['mean_delta_change']:.3f} | {change['improved']}/{change['same']}/{change['worsened']} |"
        )
    else:
        lines.append(
            f"| Paper-compatible GPT-4 /10 | {v4['paper_evaluator']['delta_mean']:.3f} | unavailable | unavailable | unavailable |"
        )
    lines.extend(
        [
            "",
            "## Artifact mechanics",
            "",
            "| Measure | archived v4 | fresh v5 |",
            "|---|---:|---:|",
            f"| Artifact count | {v4['artifact_mechanics']['artifact_count']} | {v5['artifact_mechanics']['artifact_count']} |",
            f"| Unique artifact count | {v4['artifact_mechanics']['unique_artifact_count']} | {v5['artifact_mechanics']['unique_artifact_count']} |",
            f"| Artifact bytes | {v4['artifact_mechanics']['total_content_bytes']} | {v5['artifact_mechanics']['total_content_bytes']} |",
            f"| Approximate word tokens | {v4['artifact_mechanics']['total_token_count']} | {v5['artifact_mechanics']['total_token_count']} |",
            f"| Mechanically counted prohibition lines | {v4['artifact_mechanics']['prohibition_line_count']} | {v5['artifact_mechanics']['prohibition_line_count']} |",
            f"| Mean cross-artifact trigram Jaccard | {v4['artifact_mechanics']['mean_cross_artifact_trigram_jaccard']:.4f} | {v5['artifact_mechanics']['mean_cross_artifact_trigram_jaccard']:.4f} |",
            "",
        ]
    )
    semantic = analysis["semantic_audit"]
    if semantic["status"] == "POST_GENERATION_AUDIT_COMPLETE":
        lines.extend(
            [
                "## Fresh post-generation semantic audit",
                "",
                f"- Task-obligation conflicts: {semantic['task_conflict_count']}/14 tasks.",
                f"- Constraint amplification: {semantic['constraint_amplification_task_count']}/14 tasks.",
                f"- Instruction overload: {semantic['instruction_overload_task_count']}/14 tasks.",
                "- Detailed task-bound findings are in the machine-readable semantic audit. Causal conclusions still require human interpretation because artifact count and artifact content are not separately randomized.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Fresh post-generation semantic audit",
                "",
                "Unavailable. No mechanism conclusion about task conflict, constraint amplification, or instruction overload is claimed.",
                "",
            ]
        )
    return "\n".join(lines)


def _output_paths(output_dir: Path, prefix: str) -> dict[str, Path]:
    _require(OUTPUT_PREFIX_RE.fullmatch(prefix) is not None, "invalid output prefix")
    _require(output_dir.is_dir(), f"output directory does not exist: {output_dir}")
    paths = {
        "analysis": output_dir / f"{prefix}_EXPERIMENT.json",
        "main_md": output_dir / f"{prefix}_EXPERIMENT.md",
        "mechanism_md": output_dir / f"{prefix}_V4_V5_MECHANISM_COMPARISON.md",
        "semantic": output_dir / f"{prefix}_SEMANTIC_AUDIT.json",
        "paper": output_dir / f"{prefix}_PAPER_EVALUATOR_AUDIT.json",
    }
    resolved = [path.resolve() for path in paths.values()]
    _require(len(set(resolved)) == len(resolved), "output paths are not unique")
    existing = [str(path) for path in paths.values() if path.exists()]
    _require(not existing, f"refusing to overwrite existing output(s): {existing}")
    return paths


def _write_new(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)


def build_analysis(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path]]:
    outputs = _output_paths(args.output_dir, args.output_prefix)
    pairs, pair_paths, current_evidence = _validate_current_run(
        run_root=args.run_root,
        aggregate_path=args.aggregate,
        completed_audit_path=args.completed_audit,
    )
    v4, v4_deltas, historical_evidence = _load_historical_v4(
        args.historical_v4_report,
        expected_sha256=args.historical_v4_report_sha256,
    )
    internal = _score_means(pairs, source="internal")
    blind = _score_means(pairs, source="blind")
    internal_deltas = _pair_deltas(pairs, source="internal")
    blind_deltas = _pair_deltas(pairs, source="blind")
    internal["paired"] = _paired_stats(internal_deltas)
    blind["paired"] = _paired_stats(blind_deltas)
    v5_mechanics = _artifact_mechanics(pairs, artifacts_root=args.core_artifacts_root)

    semantic_audit = _load_semantic_audit(
        args.semantic_annotations,
        pairs=pairs,
        pair_paths=pair_paths,
        completed_audit_sha256=current_evidence["completed_audit_sha256"],
    )
    paper_audit, paper_grades, paper_aggregate = _audit_paper(
        args.paper_root,
        pairs=pairs,
        source_run_root=args.run_root,
        completed_run_audit=args.completed_audit,
        replacement_authority_path=args.paper_replacement_authority,
        core_completion_root=args.paper_core_completions,
    )
    if paper_grades is None or paper_aggregate is None:
        paper: dict[str, Any] = {"status": "NOT_PROVIDED"}
        paper_deltas_by_task: dict[str, float] | None = None
    else:
        paper_deltas_by_task = {
            task_id: paper_grades[task_id]["evolved"] - paper_grades[task_id]["baseline"]
            for task_id in TASK_IDS
        }
        paper_deltas = [paper_deltas_by_task[task_id] for task_id in TASK_IDS]
        paper = {
            "status": "PASS",
            "g1_mean": statistics.mean(paper_grades[task_id]["baseline"] for task_id in TASK_IDS),
            "g2_mean": statistics.mean(paper_grades[task_id]["evolved"] for task_id in TASK_IDS),
            "delta_mean": statistics.mean(paper_deltas),
            "historical_control_mean": statistics.mean(
                paper_grades[task_id]["historical_control"] for task_id in TASK_IDS
            ),
            "paired": _paired_stats(paper_deltas),
            "actual_cost_usd": paper_audit["actual_openrouter_reported_cost_usd"],
        }
        for field, computed in (
            ("mean_openevo_baseline_grade", paper["g1_mean"]),
            ("mean_openevo_evolved_grade", paper["g2_mean"]),
            ("mean_evolved_minus_baseline", paper["delta_mean"]),
            ("mean_historical_chemcrow_grade", paper["historical_control_mean"]),
        ):
            _require(_close(paper_aggregate[field], computed), f"paper aggregate {field} differs")

    current_delta_maps = {
        "internal_evaluator": dict(zip(TASK_IDS, internal_deltas, strict=True)),
        "blind_evaluator": dict(zip(TASK_IDS, blind_deltas, strict=True)),
    }
    comparisons = {
        layer: _delta_comparison(current=current_delta_maps[layer], historical=v4_deltas[layer])
        for layer in current_delta_maps
    }
    if paper_deltas_by_task is not None:
        comparisons["paper_evaluator"] = _delta_comparison(
            current=paper_deltas_by_task,
            historical=v4_deltas["paper_evaluator"],
        )

    per_task = []
    for task_id in TASK_IDS:
        pair = pairs[task_id]
        row: dict[str, Any] = {
            "task_id": task_id,
            "internal_g1": _score_total(pair["baseline_internal_evaluation"]["scores"]),
            "internal_g2": _score_total(pair["evolved_internal_evaluation"]["scores"]),
            "internal_delta": current_delta_maps["internal_evaluator"][task_id],
            "blind_g1": _score_total(pair["final_evaluation"]["baseline_scores"]),
            "blind_g2": _score_total(pair["final_evaluation"]["evolved_scores"]),
            "blind_delta": current_delta_maps["blind_evaluator"][task_id],
            "artifact_ids": [item["artifact_id"] for item in pair["artifact_bundle"]["artifacts"]],
            "reflector_job_ids": [
                item["reflector_job_id"] for item in pair["artifact_bundle"]["artifacts"]
            ],
            "v4_deltas": {layer: values[task_id] for layer, values in v4_deltas.items()},
        }
        if paper_grades is not None:
            row["paper_g1"] = paper_grades[task_id]["baseline"]
            row["paper_g2"] = paper_grades[task_id]["evolved"]
            row["paper_delta"] = paper_deltas_by_task[task_id]  # type: ignore[index]
        per_task.append(row)

    components = {
        "engineering": "PASS",
        "semantic": semantic_audit["status"],
        "paper": paper_audit["status"],
    }
    complete = (
        components["engineering"] == "PASS"
        and components["semantic"] == "POST_GENERATION_AUDIT_COMPLETE"
        and components["paper"] == "PASS"
    )
    analysis = {
        "schema_version": "chemcrow_full_v5_core_native_experiment_analysis_v2",
        "status": (
            "FULL_V5_CORE_NATIVE_EXPERIMENT_COMPLETE"
            if complete
            else "ANALYSIS_INCOMPLETE_MISSING_REQUIRED_EVIDENCE"
        ),
        "task_ids": list(TASK_IDS),
        "task_count": 14,
        "components": components,
        "current_run_evidence": current_evidence,
        "historical_v4_evidence": historical_evidence,
        "v4": v4,
        "v5": {
            "internal_evaluator": internal,
            "blind_evaluator": blind,
            "paper_evaluator": paper,
            "artifact_mechanics": v5_mechanics,
        },
        "v4_v5_delta_comparison": comparisons,
        "semantic_audit": semantic_audit,
        "paper_audit": paper_audit,
        "per_task": per_task,
        "statistical_limitations": (
            "All comparisons are task-paired. N=14 and discrete tied judge scores limit power; "
            "failure to reject is not evidence of equivalence. V4 raw evidence is unavailable."
        ),
    }
    return analysis, outputs


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root", type=Path, required=True, help="fresh sealed 14-task run root"
    )
    parser.add_argument("--aggregate", type=Path, required=True, help="fresh aggregate.json")
    parser.add_argument(
        "--completed-audit",
        type=Path,
        required=True,
        help="fresh PASS completed-run audit bound to --aggregate",
    )
    parser.add_argument(
        "--core-artifacts-root",
        type=Path,
        required=True,
        help="Core managed root containing artifacts/ and workers/",
    )
    parser.add_argument(
        "--historical-v4-report",
        type=Path,
        required=True,
        help="tracked immutable historical analysis JSON (raw v4 is not used)",
    )
    parser.add_argument("--historical-v4-report-sha256", required=True)
    parser.add_argument("--semantic-annotations", type=Path)
    parser.add_argument("--paper-root", type=Path)
    parser.add_argument(
        "--paper-replacement-authority",
        "--replacement-authority",
        dest="paper_replacement_authority",
        type=Path,
        help="sealed failed-attempt replacement authority for a 43-attempt paper ledger",
    )
    parser.add_argument(
        "--paper-core-completions",
        "--core-completions",
        dest="paper_core_completions",
        type=Path,
        help="durable Core completion root used to revalidate paper replacement evidence",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-prefix", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    analysis, outputs = build_analysis(args)
    payloads = {
        "analysis": json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        "main_md": _main_report(analysis),
        "mechanism_md": _mechanism_report(analysis),
        "semantic": json.dumps(analysis["semantic_audit"], indent=2, sort_keys=True) + "\n",
        "paper": json.dumps(analysis["paper_audit"], indent=2, sort_keys=True) + "\n",
    }
    for name, path in outputs.items():
        _write_new(path, payloads[name])
    print(
        json.dumps(
            {
                "status": analysis["status"],
                "task_count": analysis["task_count"],
                "paper_call_count": analysis["paper_audit"]["call_count"],
                "outputs": {name: str(path) for name, path in outputs.items()},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
