from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from openevo_researchclawbench.baseline_equivalence import assess_baseline_equivalence
from openevo_researchclawbench.baseline_evidence_capsule import (
    BaselineEvidenceCapsuleAdmissionError,
    admit_baseline_evidence_capsule,
    build_reflector_capsule_view,
)
from openevo_researchclawbench.baseline_success_trace import (
    build_baseline_achievement_ledger,
    build_baseline_success_trace,
)
from openevo_researchclawbench.evaluation_feedback import (
    project_sanitized_evaluation_feedback,
)
from openevo_researchclawbench.task_specific_artifact_quality import (
    QUALITY_FAILURE,
    assess_task_specific_artifact_quality,
)


def _gt() -> list[dict[str, object]]:
    return [
        {
            "type": "image",
            "path": "private_target.png",
            "content": "Hidden target relation with private curvature.",
            "keywords": ["private curvature"],
            "weight": 3,
        }
    ]


def _candidate(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "candidate"
    (root / "code").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "outputs").mkdir()
    (root / "report/images").mkdir(parents=True)
    (root / "data/public.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (root / "code/analyze.py").write_text(
        "# candidate-designed public-data comparison\nprint('metrics')\n",
        encoding="utf-8",
    )
    (root / "outputs/metrics.csv").write_text("metric,value\ntrend,1\n", encoding="utf-8")
    (root / "report/images/trend.png").write_bytes(b"candidate figure")
    (root / "report/report.md").write_text(
        "# Modeling and Evaluation\n\n"
        "The candidate-designed public-data comparison uses `code/analyze.py`, "
        "`outputs/metrics.csv`, and `images/trend.png`. The trend figure supports "
        "the report discussion; verification compares the numeric table.\n",
        encoding="utf-8",
    )
    transcript = "\n".join(
        json.dumps(item)
        for item in (
            {"type": "command_execution", "status": "completed", "exit_code": 0, "command": "ls -la"},
            {"type": "command_execution", "status": "completed", "exit_code": 0, "command": "pwd"},
            {"type": "command_execution", "status": "failed", "exit_code": 1, "command": "python scratch.py"},
            {
                "type": "command_execution",
                "status": "completed",
                "exit_code": 0,
                "command": "python code/analyze.py data/public.csv outputs/metrics.csv report/images/trend.png",
            },
            {
                "type": "agent_message",
                "text": "Created code/analyze.py, generated outputs/metrics.csv and report/images/trend.png, then validated the report evidence.",
            },
        )
    )
    (root / "_agent_output.jsonl").write_text(
        json.dumps({"metadata": {"transcript": transcript}}) + "\n",
        encoding="utf-8",
    )
    return root, {
        "run_id": "preservation_test_a0",
        "session_id": "session-baseline",
        "core_task_id": "task-baseline",
        "core_attempt_id": "attempt-baseline",
        "candidate_output_root": str(root),
    }


def _projection(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root, candidate = _candidate(tmp_path)
    return root, project_sanitized_evaluation_feedback(
        task_id="Astronomy_004",
        candidate=candidate,
        raw_evaluation={
            "total_score": 41.0,
            "items": [{"type": "image", "score": 41.0, "score_valid": True}],
        },
        public_task_info={"task": "Analyze supplied public data and report figures."},
        ground_truth_entries=_gt(),
    )


def _output_words(achievement: dict[str, Any]) -> str:
    words = {
        "script": "script code",
        "numeric_output": "numeric table csv metrics",
        "figure": "figure plot image",
        "report_section": "report discussion section",
    }
    return " ".join(words[item] for item in achievement["required_output_classes"])


def _complete_artifacts(capsule: dict[str, Any]) -> dict[str, str]:
    achievements = capsule["baseline_achievement_ledger"]["achievements"]
    actions = capsule["improvement_actions"]
    memory = "\n".join(
        f"PRESERVE {item['achievement_id']}: retain the { _output_words(item) } evidence role."
        for item in achievements
    )
    skill = ["Phase 1 — Reconstruct baseline capabilities."]
    skill.extend(
        f"RECONSTRUCT {item['achievement_id']} with { _output_words(item) }, then verify its report evidence."
        for item in achievements
    )
    skill.append("Phase 2 — Verify reconstruction.")
    skill.append("Phase 3 — Add evaluator-driven improvements.")
    skill.extend(
        f"For {item['addresses']} and {item['achievement_id']}, add a public-data cross-check without replacing the baseline path."
        for item in actions
    )
    skill.append("Phase 4 — Integrate without deleting preserved work.")
    return {
        "text_memory": memory,
        "skill_bundle": "\n".join(skill),
        "agent_system": (
            "RECONSTRUCT required baseline work, PRESERVE every required achievement, "
            "EXTEND only additively, and VERIFY baseline equivalence before submission. "
            "New analyses are additive and must not replace required baseline achievements."
        ),
    }


def test_success_trace_extracts_real_successes_and_filters_noise(tmp_path: Path) -> None:
    root, _candidate_record = _candidate(tmp_path)
    trace = build_baseline_success_trace(candidate_root=root)
    actions = "\n".join(event["action"] for event in trace["events"]).casefold()

    assert trace["projector_model_calls"] == 0
    assert "python code/analyze.py" in actions
    assert "metrics.csv" in actions
    assert "ls -la" not in actions
    assert "pwd" not in actions
    assert "scratch.py" not in actions


def test_achievement_ledger_uses_trajectory_capability_not_report_heading(tmp_path: Path) -> None:
    root, _candidate_record = _candidate(tmp_path)
    trace = build_baseline_success_trace(candidate_root=root)
    ledger = build_baseline_achievement_ledger(trace=trace, candidate_root=root)
    encoded = json.dumps(ledger).casefold()

    assert ledger["projector_model_calls"] == 0
    assert "code/analyze.py" in encoded
    assert "modeling and evaluation" not in {item["capability"].casefold() for item in ledger["achievements"]}
    assert all(item["reconstruction_steps"] for item in ledger["achievements"])


def test_reflector_view_keeps_all_three_admitted_diagnoses(tmp_path: Path) -> None:
    root, projection = _projection(tmp_path)
    capsule = deepcopy(projection["baseline_evidence_capsule"])
    extra = deepcopy(capsule["observed_weaknesses"][0])
    extra["weakness_id"] = "weakness_03"
    extra["dimension"] = "reproducibility"
    extra["candidate_observation"] = "Candidate code/analyze.py has candidate-produced outputs with limited documented rerun verification."
    extra["improvement_direction"] = "After reconstruction, add a public-data rerun verification without replacing the baseline path."
    capsule["observed_weaknesses"].append(extra)
    action = deepcopy(capsule["improvement_actions"][0])
    action["action_id"] = "action_03"
    action["addresses"] = "weakness_03"
    action["action"] = "Reconstruct the required path, then add a public-data rerun verification additively."
    capsule["improvement_actions"].append(action)

    admission = admit_baseline_evidence_capsule(capsule, candidate_root=root, ground_truth_entries=_gt())
    view = build_reflector_capsule_view(capsule)

    assert admission["status"] == "ADMITTED"
    assert [item["weakness_id"] for item in view["weakness_to_action"]] == [
        "weakness_01", "weakness_02", "weakness_03"
    ]


def test_memory_gate_requires_unique_required_achievement_coverage(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    first = capsule["baseline_achievement_ledger"]["achievements"][0]
    artifacts["text_memory"] = (
        f"PRESERVE {first['achievement_id']}: retain the {_output_words(first)} evidence role."
    )

    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["artifact_metrics"]["text_memory"]["required_achievement_memory_coverage"] < 0.80


def test_skill_gate_requires_reconstruction_and_every_additive_mapping(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    artifacts["skill_bundle"] = "Phase 3 — Add more validation."

    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["artifact_metrics"]["skill_bundle"]["required_achievement_reconstruction_coverage"] == 0
    assert report["artifact_metrics"]["skill_bundle"]["feedback_action_coverage"] == 0


def test_earth_style_bundle_pass_cannot_bypass_r3_per_artifact_gate(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = {
        "text_memory": "Inspect files, validate outputs, and ensure reproducibility.",
        "skill_bundle": "Run scripts and check results before adding more validation.",
        "agent_system": "Follow instructions and link evidence before submission.",
    }
    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["aggregate"]["overall_retention_ratio"] == 0
    assert report["artifact_role_contract"]["two_stem_overlap_is_not_sufficient"] is True


def test_duplicate_achievement_mentions_count_once(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    first = capsule["baseline_achievement_ledger"]["achievements"][0]
    artifacts["text_memory"] = " ".join(
        f"PRESERVE {first['achievement_id']}: retain the {_output_words(first)} evidence role."
        for _ in range(20)
    )

    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["artifact_metrics"]["text_memory"]["unique_required_achievements"] == [first["achievement_id"]]
    assert report["aggregate"]["memory_required_achievement_coverage"] < 0.80


def test_candidate_detail_is_allowed_but_hidden_detail_is_rejected(tmp_path: Path) -> None:
    root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    prompt_contract = projection["reflector_feedback"]["a00_preservation_first_prompt_contract"]
    assert "analyze" in json.dumps(prompt_contract).casefold()
    assert "trend.png" in json.dumps(prompt_contract)

    poisoned = deepcopy(capsule)
    poisoned["candidate_concepts"][0]["text"] = "Hidden target relation with private curvature."
    with pytest.raises(BaselineEvidenceCapsuleAdmissionError):
        admit_baseline_evidence_capsule(poisoned, candidate_root=root, ground_truth_entries=_gt())


def _equivalence_ledger() -> dict[str, Any]:
    return {
        "schema_version": "openevo.researchclawbench.baseline_achievement_ledger.v2",
        "achievements": [
            {
                "achievement_id": "achievement_alpha",
                "capability": "Reconstruct alpha route script evidence.",
                "method_signature": ["alpha", "route", "script"],
                "required_output_classes": ["script", "numeric_output", "figure", "report_section"],
                "preservation_priority": "required",
            },
            {
                "achievement_id": "achievement_beta",
                "capability": "Reconstruct beta comparison evidence.",
                "method_signature": ["beta", "comparison", "route"],
                "required_output_classes": ["script", "numeric_output", "figure", "report_section"],
                "preservation_priority": "required",
            },
            {
                "achievement_id": "achievement_gamma",
                "capability": "Reconstruct gamma validation evidence.",
                "method_signature": ["gamma", "validation", "route"],
                "required_output_classes": ["script", "numeric_output", "figure", "report_section"],
                "preservation_priority": "required",
            },
        ],
    }


def _evolved_workspace(tmp_path: Path, *, include: list[str]) -> Path:
    root = tmp_path / "evolved"
    (root / "code").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "report/images").mkdir(parents=True)
    (root / "code/rebuilt.py").write_text("# reconstructed method\n", encoding="utf-8")
    (root / "outputs/metrics.csv").write_text("metric,value\na,1\n", encoding="utf-8")
    (root / "report/images/plot.png").write_bytes(b"plot")
    report_lines = ["# Evolved report", "Verification check completed from public data."]
    for identifier in include:
        token = identifier.removeprefix("achievement_")
        report_lines.append(f"{identifier}: {token} route script comparison validation evidence.")
    (root / "report/report.md").write_text("\n".join(report_lines), encoding="utf-8")
    return root


def test_baseline_equivalence_accepts_reconstructed_path_with_corrected_conclusion(tmp_path: Path) -> None:
    ledger = _equivalence_ledger()
    root = _evolved_workspace(
        tmp_path,
        include=["achievement_alpha", "achievement_beta", "achievement_gamma"],
    )
    report_path = root / "report/report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8") + "\nCorrected conclusion after the reconstructed public-data check.\n",
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger, evolved_candidate_root=root, validation={"artifact_valid": True}
    )

    assert report["status"] == "PASS"
    assert report["reconstructed_required_achievement_count"] == 3
    assert all(item["candidate_conclusion_revised"] for item in report["findings"])
    assert report["provider_calls"] == 0
    assert report["raw_gt_visible"] is False


def test_baseline_equivalence_rejects_additive_replacement_when_capability_missing(tmp_path: Path) -> None:
    ledger = _equivalence_ledger()
    root = _evolved_workspace(tmp_path, include=["achievement_alpha", "achievement_gamma"])

    report = assess_baseline_equivalence(
        ledger=ledger, evolved_candidate_root=root, validation={"artifact_valid": True}
    )

    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"
    assert "achievement_beta" in report["dropped_required_achievement_ids"]


def test_final_artifact_gate_accepts_complete_r3_roles_without_provider_calls(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == "PASS"
    assert report["aggregate"]["memory_required_achievement_coverage"] >= 0.80
    assert report["aggregate"]["skill_required_achievement_reconstruction_coverage"] >= 0.80
    assert report["aggregate"]["feedback_action_coverage"] == 1
    assert report["gt_leakage_findings"] == []

