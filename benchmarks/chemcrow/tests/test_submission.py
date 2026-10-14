from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import ArtifactKind, RubricScores
from openevo_chemcrow.paper_evaluator import FROZEN_PAPER_TASK_IDS
from openevo_chemcrow.submission import (
    PROTOCOL_VERSION,
    SUBMISSION_SCHEMA_VERSION,
    ChemCrowSubmission,
    SubmissionArtifact,
    SubmissionBlindEvaluation,
    SubmissionEvidence,
    SubmissionExecution,
    SubmissionModelBinding,
    SubmissionRunRef,
    SubmissionTaskResult,
    aggregate_submission_results,
    aggregate_submissions,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _hash(value: str) -> str:
    return canonical_sha256({"value": value})


def _submission(*, experiment_id: str = "third-party-test") -> ChemCrowSubmission:
    scores = RubricScores(
        chemical_correctness=3.0,
        reasoning_quality=3.0,
        task_completion=3.0,
    )
    evolved_scores = RubricScores(
        chemical_correctness=4.0,
        reasoning_quality=3.0,
        task_completion=4.0,
    )
    rows: list[SubmissionTaskResult] = []
    for task_id in FROZEN_PAPER_TASK_IDS:
        artifacts = [
            SubmissionArtifact(
                artifact_type=artifact_type,
                artifact_id=f"{task_id}-{artifact_type.value}",
                artifact_sha256=_hash(f"artifact:{task_id}:{artifact_type.value}"),
                artifact_bytes=100,
                reflector_job_id=f"job:{task_id}:{artifact_type.value}",
                reflector_run_id=f"run:{task_id}:{artifact_type.value}",
                prompt_sha256=_hash(f"prompt:{task_id}:{artifact_type.value}"),
                input_evidence_sha256=_hash(f"input:{task_id}:{artifact_type.value}"),
            )
            for artifact_type in (
                ArtifactKind.TEXT_MEMORY,
                ArtifactKind.SKILL_BUNDLE,
                ArtifactKind.AGENT_SYSTEM,
            )
        ]
        rows.append(
            SubmissionTaskResult(
                task_id=task_id,
                pair_id=f"{experiment_id}--{task_id}",
                task_prompt_sha256=_hash(f"task:{task_id}"),
                s0_sha256=_hash("s0"),
                baseline=SubmissionRunRef(
                    run_id=f"baseline:{task_id}", answer_sha256=_hash(f"g1:{task_id}")
                ),
                evolved=SubmissionRunRef(
                    run_id=f"evolved:{task_id}", answer_sha256=_hash(f"g2:{task_id}")
                ),
                baseline_internal_scores=scores,
                evolved_internal_scores=evolved_scores,
                artifacts=artifacts,
                injection_receipt_sha256=_hash(f"inject:{task_id}"),
                reset_receipt_sha256=_hash(f"reset:{task_id}"),
                blind_evaluation=SubmissionBlindEvaluation(
                    baseline_scores=scores,
                    evolved_scores=evolved_scores,
                    winner="evolved",
                    confidence=0.8,
                    mapping_seal_sha256=_hash(f"mapping:{task_id}"),
                ),
            )
        )
    roles = {
        role: SubmissionModelBinding(provider="test-provider", model="test-model")
        for role in (
            "candidate",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
            "evolution_evaluator",
            "final_evaluator",
        )
    }
    payload = {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_kind": "third_party_reproduction",
        "experiment_id": experiment_id,
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "execution": SubmissionExecution(
            source_commit="1" * 40,
            config_sha256="2" * 64,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 2, tzinfo=UTC),
            random_seed=20260825,
            model_bindings=roles,
        ).model_dump(mode="json"),
        "judge_protocol": "chemcrow-final-blind-evaluator-v1",
        "scoring_rule": "three_axes_0_to_4_sum_12_v1",
        "results": [item.model_dump(mode="json") for item in rows],
        "aggregate": aggregate_submission_results(rows).model_dump(mode="json"),
        "evidence": SubmissionEvidence(
            audit_sha256="3" * 64,
            aggregate_sha256="4" * 64,
        ).model_dump(mode="json"),
        "human_review_status": "PROVISIONAL_NOT_COMPLETED",
    }
    return ChemCrowSubmission.model_validate(
        {"submission_id": canonical_sha256(payload), **payload}
    )


def test_submission_schema_is_checked_in_without_drift() -> None:
    expected = json.dumps(
        ChemCrowSubmission.model_json_schema(), indent=2, sort_keys=True
    ) + "\n"
    assert (
        PACKAGE_ROOT / "reproduction/submission.schema.json"
    ).read_text(encoding="utf-8") == expected


def test_submission_round_trip_and_offline_aggregation() -> None:
    first = _submission(experiment_id="run-a")
    second = _submission(experiment_id="run-b")

    assert ChemCrowSubmission.model_validate_json(
        first.model_dump_json()
    ) == first
    aggregate = aggregate_submissions([first, second])
    assert aggregate["status"] == "PASS"
    assert aggregate["submission_count"] == 2
    assert aggregate["entries"][0]["mean_evolved_total"] == 11.0


def test_submission_rejects_tampering_and_duplicate_entries() -> None:
    submission = _submission()
    payload = submission.model_dump(mode="json")
    payload["results"][0]["blind_evaluation"]["evolved_scores"][
        "chemical_correctness"
    ] = 0.0

    with pytest.raises(ValidationError, match="aggregate differs"):
        ChemCrowSubmission.model_validate(payload)
    with pytest.raises(ValueError, match="duplicate submission ID"):
        aggregate_submissions([submission, submission])
