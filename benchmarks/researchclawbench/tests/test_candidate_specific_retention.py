from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import openevo_researchclawbench.production_operation_ports as operation_ports
import pytest
from openevo_researchclawbench.baseline_evidence_capsule import (
    BaselineEvidenceCapsuleAdmissionError,
    admit_baseline_evidence_capsule,
)
from openevo_researchclawbench.evaluation_feedback import (
    project_sanitized_evaluation_feedback,
)
from openevo_researchclawbench.task_specific_artifact_quality import (
    QUALITY_FAILURE,
    TaskSpecificArtifactQualityError,
    assess_task_specific_artifact_quality,
    require_task_specific_artifact_quality,
)


def _gt() -> list[dict[str, object]]:
    return [
        {
            "type": "image",
            "path": "private_target_structure.png",
            "content": "Hidden target relationship has a private expected curvature.",
            "keywords": ["private curvature"],
            "weight": 3,
        }
    ]


def _candidate(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "candidate"
    (root / "report/images").mkdir(parents=True)
    (root / "code").mkdir()
    (root / "outputs").mkdir()
    (root / "report/report.md").write_text(
        "# Methods\nCandidate analysis and public figures.\n",
        encoding="utf-8",
    )
    (root / "report/images/custom_trend.png").write_bytes(b"image")
    (root / "code/custom_analysis.py").write_text("print('public data')\n", encoding="utf-8")
    (root / "outputs/summary.csv").write_text("value\n1\n", encoding="utf-8")
    (root / "_agent_output.jsonl").write_text(
        json.dumps({"response": "created custom analysis and trend figure"}) + "\n",
        encoding="utf-8",
    )
    return root, {
        "run_id": "Life_005_a0_capsule_test",
        "session_id": "session-baseline",
        "core_task_id": "task-baseline",
        "core_attempt_id": "attempt-baseline",
        "candidate_output_root": str(root),
    }


def _projection(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root, candidate = _candidate(tmp_path)
    result = project_sanitized_evaluation_feedback(
        task_id="Life_005",
        candidate=candidate,
        raw_evaluation={
            "total_score": 42.0,
            "items": [{"type": "image", "score": 42.0, "score_valid": True}],
        },
        public_task_info={"task": "Analyze public data and deliver a report with figures."},
        ground_truth_entries=_gt(),
    )
    return root, result


def _artifact_texts() -> dict[str, str]:
    return {
        "text_memory": (
            "# Memory\n"
            "Reconstruct the baseline custom analysis route before changing the plan. "
            "The visual evidence weakness needs an independent numeric aggregation. "
            "Add that aggregation to validate the custom analysis conclusion.\n"
        ),
        "skill_bundle": (
            "# Skill\n"
            "1. Reconstruct custom analysis from public inputs.\n"
            "2. Generate the custom trend figure from that analysis.\n"
            "3. Add a numeric aggregation to validate the custom trend evidence.\n"
        ),
        "agent_system": (
            "# Agent system\n"
            "In a fresh workspace, reconstruct the proven custom analysis strategy. "
            "Preserve that baseline strategy before additions. "
            "For each visual evidence weakness, add a validation step tied to the custom trend output.\n"
        ),
    }


def test_capsule_extracts_candidate_specific_evidence_without_gt(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    encoded = json.dumps(capsule, sort_keys=True).casefold()

    assert projection["baseline_evidence_capsule_admission"]["status"] == "ADMITTED"
    assert projection["baseline_evidence_capsule_admission"]["projector_model_calls"] == 0
    assert "custom analysis" in encoded
    assert "custom trend" in encoded
    assert "weakness_01" in encoded
    assert "reconstruct" in encoded
    assert "private expected curvature" not in encoded
    assert "private_target_structure" not in encoded


def test_capsule_extracts_candidate_report_labels_with_candidate_provenance(
    tmp_path: Path,
) -> None:
    root, candidate = _candidate(tmp_path)
    (root / "report/report.md").write_text(
        "# Symmetry-aware custom trend study\n\n"
        "![Custom trend evidence](images/custom_trend.png)\n",
        encoding="utf-8",
    )
    result = project_sanitized_evaluation_feedback(
        task_id="Life_005",
        candidate=candidate,
        raw_evaluation={
            "total_score": 42.0,
            "items": [{"type": "image", "score": 42.0, "score_valid": True}],
        },
        public_task_info={"task": "Analyze public data and deliver a report with figures."},
        ground_truth_entries=_gt(),
    )
    concepts = [item["text"] for item in result["baseline_evidence_capsule"]["candidate_concepts"]]
    assert any("symmetry aware custom trend" in item for item in concepts)
    assert any("custom trend" in item for item in concepts)
    assert result["baseline_evidence_capsule_admission"]["status"] == "ADMITTED"


def test_capsule_requires_fresh_workspace_reconstruction_semantics(tmp_path: Path) -> None:
    root, projection = _projection(tmp_path)
    capsule = deepcopy(projection["baseline_evidence_capsule"])
    capsule["improvement_actions"][0]["action"] = "Preserve existing files."

    with pytest.raises(BaselineEvidenceCapsuleAdmissionError) as caught:
        admit_baseline_evidence_capsule(
            capsule,
            candidate_root=root,
            ground_truth_entries=_gt(),
        )

    assert caught.value.reason_code == "CAPSULE_FRESH_WORKSPACE_SEMANTICS_INVALID"


def test_capsule_rejects_hidden_only_concept_provenance(tmp_path: Path) -> None:
    root, projection = _projection(tmp_path)
    capsule = deepcopy(projection["baseline_evidence_capsule"])
    capsule["candidate_concepts"][0]["text"] = "private expected curvature"
    capsule["candidate_concepts"][0]["provenance"] = "hidden_gt"

    with pytest.raises(BaselineEvidenceCapsuleAdmissionError) as caught:
        admit_baseline_evidence_capsule(
            capsule,
            candidate_root=root,
            ground_truth_entries=_gt(),
        )

    assert caught.value.reason_code == "CAPSULE_SCHEMA_INVALID"


def test_generic_artifact_triple_is_rejected(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    generic = {
        artifact_type: "Inspect the task. Validate results. Ensure reproducibility."
        for artifact_type in _artifact_texts()
    }
    report = assess_task_specific_artifact_quality(
        capsule=projection["baseline_evidence_capsule"],
        artifact_texts=generic,
        ground_truth_entries=_gt(),
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["aggregate"]["candidate_specific_reference_count"] == 0


def test_candidate_specific_artifact_triple_is_accepted(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    report = require_task_specific_artifact_quality(
        capsule=projection["baseline_evidence_capsule"],
        artifact_texts=_artifact_texts(),
        ground_truth_entries=_gt(),
    )

    assert report["status"] == "PASS"
    assert report["aggregate"]["candidate_specific_reference_count"] >= 2
    assert report["aggregate"]["weakness_to_action_mapping_count"] >= 1
    assert report["bundle_requirements"]["per_artifact_role_prescription"] is False


def test_quality_accepts_semantic_retention_of_candidate_report_strategy(
    tmp_path: Path,
) -> None:
    """A substantive paraphrase of Candidate report terminology is retained.

    The gate must not require a reflector to repeat a figure filename or an
    inflected heading literally.  The strategy is still candidate-grounded:
    both the report heading and figure label originate in the candidate
    workspace, while the artifacts contain only an executable reconstruction
    lesson and no private target fact.
    """

    root, candidate = _candidate(tmp_path)
    (root / "report/report.md").write_text(
        "# Repeat pattern probability studies\n\n"
        "![Probability trends](images/custom_trend.png)\n",
        encoding="utf-8",
    )
    projection = project_sanitized_evaluation_feedback(
        task_id="Life_005",
        candidate=candidate,
        raw_evaluation={
            "total_score": 42.0,
            "items": [{"type": "image", "score": 42.0, "score_valid": True}],
        },
        public_task_info={"task": "Analyze public data and deliver a report with figures."},
        ground_truth_entries=_gt(),
    )
    artifacts = {
        "text_memory": (
            "Reconstruct the repeat-pattern probability study in a fresh workspace. "
            "The visual evidence weakness needs an independent numeric aggregation."
        ),
        "skill_bundle": (
            "Rebuild the probability-trend analysis from public inputs, then add a "
            "numeric aggregation to validate the visual evidence."
        ),
        "agent_system": (
            "Preserve the prior repeat-pattern strategy before additions; each visual "
            "evidence weakness needs a concrete validation action."
        ),
    }

    report = require_task_specific_artifact_quality(
        capsule=projection["baseline_evidence_capsule"],
        artifact_texts=artifacts,
        ground_truth_entries=_gt(),
    )

    assert report["status"] == "PASS"
    assert report["aggregate"]["candidate_specific_reference_count"] >= 2
    assert report["gt_leakage_findings"] == []


def test_gt_specific_artifact_is_rejected(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    artifacts = _artifact_texts()
    artifacts["text_memory"] += "Hidden target relationship has a private expected curvature."

    with pytest.raises(TaskSpecificArtifactQualityError) as caught:
        require_task_specific_artifact_quality(
            capsule=projection["baseline_evidence_capsule"],
            artifact_texts=artifacts,
            ground_truth_entries=_gt(),
        )

    assert caught.value.reason_code == QUALITY_FAILURE
    assert caught.value.report is not None
    assert caught.value.report["gt_leakage_findings"] == ["GT_LITERAL"]


def test_artifact_requires_weakness_to_action_mapping(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    artifacts = _artifact_texts()
    artifacts = {
        artifact_type: (
            "The custom analysis route has a visual evidence weakness without a next step."
        )
        for artifact_type in artifacts
    }
    report = assess_task_specific_artifact_quality(
        capsule=projection["baseline_evidence_capsule"],
        artifact_texts=artifacts,
        ground_truth_entries=_gt(),
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["aggregate"]["weakness_to_action_mapping_count"] == 0


def test_low_signal_concepts_do_not_turn_generic_advice_into_fidelity(
    tmp_path: Path,
) -> None:
    _root, projection = _projection(tmp_path)
    capsule = deepcopy(projection["baseline_evidence_capsule"])
    capsule["candidate_concepts"] = [
        {
            "concept_id": "concept_01",
            "text": "validation",
            "kind": "figure",
            "evidence_refs": ["report/images/custom_trend.png"],
            "provenance": "candidate_workspace",
        }
    ]
    artifacts = {
        artifact_type: (
            "In a fresh workspace, preserve validation and add a validation step "
            "for the visual evidence weakness."
        )
        for artifact_type in _artifact_texts()
    }
    with pytest.raises(TaskSpecificArtifactQualityError) as caught:
        require_task_specific_artifact_quality(
            capsule=capsule,
            artifact_texts=artifacts,
            ground_truth_entries=_gt(),
        )
    assert caught.value.reason_code == "ARTIFACT_QUALITY_CAPSULE_INVALID"


def test_production_quality_port_reads_only_bounded_core_snapshots_and_archives_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real adapter port with the Core boundary as the only fake.

    The fake represents the lowest Core HTTP executor.  The test still covers
    the production quality port's successor/type/hash checks, its native
    snapshot request paths, its deterministic gate, and the no-text durable
    receipt policy.
    """

    _root, projection = _projection(tmp_path)
    texts = _artifact_texts()
    identifiers = {
        "text_memory": "art-memory",
        "skill_bundle": "art-skill",
        "agent_system": "art-agent-system",
    }
    manifests = {artifact_type: sha256(artifact_type.encode()).hexdigest() for artifact_type in texts}
    observed_paths: list[str] = []

    class _CoreSnapshotClient:
        def __init__(self, _authority: object) -> None:
            pass

        def close(self) -> None:
            return None

        def json(self, method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
            assert method == "GET"
            observed_paths.append(path)
            matched = [
                artifact_type
                for artifact_type, artifact_id in identifiers.items()
                if f"/artifacts/{artifact_id}/text-snapshot" in path
            ]
            assert matched
            artifact_type = matched[0]
            text = texts[artifact_type]
            return {
                "schema_version": "openevo.internal.artifact_text_snapshot.v1",
                "artifact_id": identifiers[artifact_type],
                "artifact_type": artifact_type,
                "payload_manifest_sha256": manifests[artifact_type],
                "documents": [
                    {
                        "relative_path": "SKILL.md" if artifact_type == "skill_bundle" else "memory.md",
                        "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
                        "utf8_byte_size": len(text.encode("utf-8")),
                        "text": text,
                    }
                ],
                "total_utf8_bytes": len(text.encode("utf-8")),
            }

    monkeypatch.setattr(operation_ports, "CoreControlV2Client", _CoreSnapshotClient)
    monkeypatch.setattr(
        operation_ports,
        "load_current_task_gt_supervision",
        lambda _config, *, task_id: {
            "task_id": task_id,
            "task_local_feedback": {"ground_truth_entries": _gt()},
        },
    )
    port = operation_ports.CoreArtifactQualityPort(
        object(),  # type: ignore[arg-type]
        root=tmp_path / "quality",
        core_authority=object(),  # type: ignore[arg-type]
    )
    result = port.execute(
        {
            "task_id": "Life_005",
            "evolution": {
                "successor_transition_id": "successor-v2",
                "jobs": [
                    {
                        "artifact_type": artifact_type,
                        "successor_registry_id": artifact_id,
                        "successor_sha256": manifests[artifact_type],
                    }
                    for artifact_type, artifact_id in identifiers.items()
                ],
            },
            "baseline_evidence_capsule": projection["baseline_evidence_capsule"],
        },
        "artifact-quality-key",
    )

    report_path = Path(result["quality_report_path"])
    archived = report_path.read_text(encoding="utf-8")
    assert result["quality_gate_status"] == "PASS"
    assert len(observed_paths) == 3
    assert all(path.startswith("/v2/internal/training-successors/successor-v2/") for path in observed_paths)
    assert result["artifact_text_persisted"] is False
    assert result["raw_gt_persisted"] is False
    assert "Reconstruct the baseline custom analysis route" not in archived
    assert "Hidden target relationship" not in archived
