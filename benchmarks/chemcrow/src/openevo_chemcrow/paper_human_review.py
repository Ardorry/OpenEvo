"""Sealed-output human-review packets compatible with the ChemCrow paper.

This is an offline evaluation layer.  It is deliberately separate from the
Candidate, Reflector, evolution evaluator, and runtime protocol modules.
"""

from __future__ import annotations

import json
import os
import random
import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem
from .paper_evaluator import FROZEN_PAPER_TASK_IDS, HistoricalAnswers
from .three_artifact_models import ThreeArtifactPairResult

PAPER_HUMAN_REVIEW_PROTOCOL = "CHEMCROW_PAPER_HUMAN_REVIEW_COMPATIBLE_V1"
PAPER_HUMAN_REVIEWER_COUNT = 4
PAPER_HUMAN_COMPARISON_COUNT = 42

PAPER_HUMAN_DIMENSIONS = (
    {"key": "chemically_accurate", "label": "Chemically accurate"},
    {"key": "quality_of_reasoning", "label": "Quality of reasoning"},
    {"key": "task_completed", "label": "Task completed"},
)


class PaperHumanRubricScores(BaseModel):
    """The published ChemCrow expert-evaluation scale from Source Data Fig. 4."""

    model_config = ConfigDict(extra="forbid")

    chemically_accurate: float = Field(ge=0.0, le=10.0)
    quality_of_reasoning: float = Field(ge=0.0, le=10.0)
    task_completed: float = Field(ge=0.0, le=10.0)


class PaperHumanReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: str
    reviewer_slot: int = Field(ge=1, le=PAPER_HUMAN_REVIEWER_COUNT)
    scores_a: PaperHumanRubricScores
    scores_b: PaperHumanRubricScores
    preferred_response: Literal["A", "B", "tie"]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    strengths_a: list[str] = Field(default_factory=list)
    weaknesses_a: list[str] = Field(default_factory=list)
    strengths_b: list[str] = Field(default_factory=list)
    weaknesses_b: list[str] = Field(default_factory=list)
    comments: str = ""


def build_paper_human_review_bundle(
    *,
    tasks: list[TaskItem],
    pairs: dict[str, ThreeArtifactPairResult],
    historical: dict[str, HistoricalAnswers],
    randomization_secret: str,
) -> dict[str, Any]:
    """Build 42 blinded comparisons without exposing condition identity to reviewers."""
    _validate_randomization_secret(randomization_secret)
    task_ids = [task.task_id for task in tasks]
    if tuple(task_ids) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("paper human review requires the frozen 14-task ChemCrow order")
    if set(pairs) != set(task_ids) or set(historical) != set(task_ids):
        raise ValueError("paper human review inputs do not cover the frozen task inventory")

    packets: list[dict[str, Any]] = []
    private_mappings: list[dict[str, Any]] = []
    for task in tasks:
        pair = pairs[task.task_id]
        if not isinstance(pair, ThreeArtifactPairResult):
            raise TypeError(
                "formal human review requires authoritative three-artifact pair results; "
                "legacy single-artifact pairs are provisional only"
            )
        old = historical[task.task_id]
        comparisons = (
            ("historical_control", "historical_chemcrow", old.chemcrow_answer),
            ("baseline", "openevo_baseline", pair.baseline.answer),
            ("evolved", "openevo_evolved", pair.evolved.answer),
        )
        for comparison, target_system, target_answer in comparisons:
            opaque_id = canonical_sha256(
                {
                    "randomization_secret": randomization_secret,
                    "task_id": task.task_id,
                    "comparison": comparison,
                }
            )[:24]
            packet_id = f"human-{opaque_id}"
            rng = random.Random(f"{randomization_secret}:{packet_id}:answer-order")
            target_is_a = bool(rng.getrandbits(1))
            mapping = {
                "A": target_system if target_is_a else "historical_gpt4",
                "B": "historical_gpt4" if target_is_a else target_system,
            }
            response_a = target_answer if target_is_a else old.gpt4_answer
            response_b = old.gpt4_answer if target_is_a else target_answer
            mapping_seal = canonical_sha256(
                {
                    "packet_id": packet_id,
                    "comparison": comparison,
                    "mapping": mapping,
                    "answer_a_sha256": canonical_sha256(response_a),
                    "answer_b_sha256": canonical_sha256(response_b),
                }
            )
            packet = {
                "schema_version": "chemcrow_paper_human_review_packet_v1",
                "protocol": PAPER_HUMAN_REVIEW_PROTOCOL,
                "packet_id": packet_id,
                "task_id": task.task_id,
                "task_category": task.broad_category,
                "task_prompt": task.prompt,
                "response_a": response_a,
                "response_b": response_b,
                "scoring": {
                    "dimensions": list(PAPER_HUMAN_DIMENSIONS),
                    "minimum": 0,
                    "maximum": 10,
                    "preferred_response_choices": ["A", "B", "tie"],
                    "required_independent_reviewer_count": PAPER_HUMAN_REVIEWER_COUNT,
                },
                "review_instructions": [
                    "Evaluate each response independently on all three dimensions.",
                    "Choose the response you are more satisfied with, or tie.",
                    "Do not try to infer which system produced either response.",
                    "Treat appropriate safety refusal or caution as task completion for a safety case.",
                ],
                "condition_identity_included": False,
                "trajectory_included": False,
                "final_response_only": True,
                "mapping_seal_sha256": mapping_seal,
            }
            packets.append(packet)
            private_mappings.append(
                {
                    "packet_id": packet_id,
                    "task_id": task.task_id,
                    "comparison": comparison,
                    "mapping": mapping,
                    "mapping_seal_sha256": mapping_seal,
                    "answer_a_sha256": canonical_sha256(response_a),
                    "answer_b_sha256": canonical_sha256(response_b),
                    "historical_source_sha256": old.notebook_sha256,
                }
            )

    if len(packets) != PAPER_HUMAN_COMPARISON_COUNT:
        raise AssertionError("paper human review must contain exactly 42 comparisons")
    order_rng = random.Random(f"{randomization_secret}:packet-order")
    order_rng.shuffle(packets)
    return {
        "schema_version": "chemcrow_paper_human_review_bundle_v1",
        "protocol": PAPER_HUMAN_REVIEW_PROTOCOL,
        "official_source_data_scale": True,
        "verbatim_historical_review_sheet_reproduction": False,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "comparison_count": len(packets),
        "required_independent_reviewer_count": PAPER_HUMAN_REVIEWER_COUNT,
        "required_completed_review_count": (
            PAPER_HUMAN_COMPARISON_COUNT * PAPER_HUMAN_REVIEWER_COUNT
        ),
        "score_minimum": 0,
        "score_maximum": 10,
        "dimensions": list(PAPER_HUMAN_DIMENSIONS),
        "randomization_secret_sha256": canonical_sha256(randomization_secret),
        "randomized_answer_order": True,
        "source_pair_protocol": "chemcrow-three-isolated-artifacts-v1",
        "style_masking": "final_response_only_no_react_trajectory",
        "packets": packets,
        "private_mappings": private_mappings,
    }


def write_paper_human_review_bundle(
    *, bundle: dict[str, Any], output_root: Path
) -> dict[str, Any]:
    """Write blinded packets, private mappings, and four response forms per packet."""
    packets = bundle.get("packets")
    mappings = bundle.get("private_mappings")
    if not isinstance(packets, list) or not isinstance(mappings, list):
        raise TypeError("paper human review bundle is incomplete")
    packet_root = output_root / "packets"
    response_root = output_root / "responses"
    private_root = output_root / "private"
    for path in (packet_root, response_root, private_root):
        path.mkdir(parents=True, exist_ok=True)

    packet_receipts: list[dict[str, str]] = []
    for packet in packets:
        packet_id = str(packet["packet_id"])
        packet_path = packet_root / f"{packet_id}.blinded.json"
        _write_private_json(packet_path, packet)
        packet_receipts.append(
            {
                "packet_id": packet_id,
                "path": str(packet_path.resolve()),
                "sha256": file_sha256(packet_path),
            }
        )
        for reviewer_slot in range(1, PAPER_HUMAN_REVIEWER_COUNT + 1):
            template = {
                "schema_version": "chemcrow_paper_human_review_response_template_v1",
                "packet_id": packet_id,
                "reviewer_slot": reviewer_slot,
                "scores_a": {key["key"]: None for key in PAPER_HUMAN_DIMENSIONS},
                "scores_b": {key["key"]: None for key in PAPER_HUMAN_DIMENSIONS},
                "preferred_response": None,
                "confidence": None,
                "strengths_a": [],
                "weaknesses_a": [],
                "strengths_b": [],
                "weaknesses_b": [],
                "comments": "",
            }
            _write_private_json(
                response_root / f"{packet_id}.reviewer-{reviewer_slot}.json", template
            )

    mapping_payload = {
        "schema_version": "chemcrow_paper_human_review_private_mapping_v1",
        "protocol": PAPER_HUMAN_REVIEW_PROTOCOL,
        "mappings": mappings,
    }
    mapping_path = private_root / "mapping.json"
    _write_private_json(mapping_path, mapping_payload)
    manifest = {
        "schema_version": "chemcrow_paper_human_review_manifest_v1",
        "status": "READY_FOR_FOUR_EXPERT_REVIEWERS",
        "protocol": PAPER_HUMAN_REVIEW_PROTOCOL,
        "task_ids": bundle["task_ids"],
        "task_count": bundle["task_count"],
        "comparison_count": bundle["comparison_count"],
        "required_independent_reviewer_count": PAPER_HUMAN_REVIEWER_COUNT,
        "required_completed_review_count": bundle["required_completed_review_count"],
        "score_minimum": 0,
        "score_maximum": 10,
        "dimensions": bundle["dimensions"],
        "randomized_answer_order": True,
        "randomization_secret_sha256": bundle["randomization_secret_sha256"],
        "style_masking": bundle["style_masking"],
        "packet_receipts": packet_receipts,
        "private_mapping_path": str(mapping_path.resolve()),
        "private_mapping_sha256": file_sha256(mapping_path),
        "answers_in_manifest": False,
        "condition_identities_in_packets": False,
    }
    manifest_path = output_root / "manifest.json"
    _write_private_json(manifest_path, manifest)
    return manifest


def validate_paper_human_review_response(text: str) -> PaperHumanReviewResponse:
    return PaperHumanReviewResponse.model_validate_json(text)


def load_or_create_human_review_secret(output_root: Path) -> str:
    """Return a private stable secret without exposing it in reports or packet names."""
    private_root = output_root / "private"
    private_root.mkdir(parents=True, exist_ok=True)
    path = private_root / "randomization-secret.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        _validate_randomization_secret(value)
        os.chmod(path, 0o600)
        return value
    value = secrets.token_hex(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def _validate_randomization_secret(value: str) -> None:
    if len(value) != 64:
        raise ValueError("paper human review randomization secret is invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("paper human review randomization secret is invalid") from exc


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError(f"existing paper human review file differs: {path}")
        os.chmod(path, 0o600)
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(serialized)
