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
    FeedbackProjectionError,
    build_balanced_evolution_context,
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
        "# candidate-designed public-data comparison\n"
        "def build_public_features():\n    return 'features'\n"
        "def compare_trends():\n    return 'outputs/metrics.csv', 'report/images/trend.png'\n"
        "def verify_numeric_evidence():\n    return build_public_features(), compare_trends()\n"
        "print(verify_numeric_evidence())\n",
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
        f"PRESERVE {item['achievement_id']}: retain candidate method "
        f"{' '.join(item['method_signature'])} and its "
        f"{' '.join(item['evidence_role_signature'])} {_output_words(item)} evidence role; "
        "do not drop this baseline capability."
        for item in achievements
    )
    skill = ["Phase 1 — Reconstruct baseline capabilities."]
    skill.extend(
        f"RECONSTRUCT {item['achievement_id']} using candidate method "
        f"{' '.join(item['method_signature'])} with "
        f"{' '.join(item['evidence_role_signature'])} {_output_words(item)}, then verify its report evidence."
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


def _four_required_achievements(capsule: dict[str, Any]) -> dict[str, Any]:
    """Exercise coverage independently of how many real scripts a fixture has."""

    expanded = deepcopy(capsule)
    original = expanded["baseline_achievement_ledger"]["achievements"][0]
    achievements = []
    for index in range(1, 5):
        item = deepcopy(original)
        item["achievement_id"] = f"achievement_{index:02d}"
        item["method_signature"] = [f"method_{index}", "public", "evidence"]
        item["evidence_role_signature"] = [f"role_{index}", "coverage"]
        item["capability"] = f"Reconstruct candidate method_{index} from public evidence."
        achievements.append(item)
    expanded["baseline_achievement_ledger"]["achievements"] = achievements
    expanded["baseline_achievement_ledger"]["achievement_count"] = len(achievements)
    expanded["baseline_achievement_ledger"]["required_achievement_count"] = len(achievements)
    expanded["successful_work"] = [
        {**deepcopy(expanded["successful_work"][0]), "achievement_id": item["achievement_id"]}
        for item in achievements
    ]
    expanded["improvement_actions"] = [
        {**deepcopy(action), "achievement_id": achievements[index % len(achievements)]["achievement_id"]}
        for index, action in enumerate(expanded["improvement_actions"])
    ]
    return expanded


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
    assert "candidate-produced numeric output `data/public.csv`" not in actions
    assert trace["events"][0]["action"].startswith(
        "Reconstruct and execute candidate-created `code/analyze.py`"
    )


def test_achievement_ledger_uses_trajectory_capability_not_report_heading(tmp_path: Path) -> None:
    root, _candidate_record = _candidate(tmp_path)
    trace = build_baseline_success_trace(candidate_root=root)
    ledger = build_baseline_achievement_ledger(trace=trace, candidate_root=root)
    encoded = json.dumps(ledger).casefold()

    assert ledger["projector_model_calls"] == 0
    assert "code/analyze.py" in encoded
    assert "build_public_features" in encoded
    assert "compare_trends" in encoded
    assert "verify_numeric_evidence" in encoded
    assert "data/public.csv" in encoded
    assert "outputs/metrics.csv" in encoded
    assert "report/images/trend.png" in encoded
    assert "modeling and evaluation" not in {item["capability"].casefold() for item in ledger["achievements"]}
    assert all(item["reconstruction_steps"] for item in ledger["achievements"])
    assert all(
        "data/public.csv" not in item["candidate_evidence_refs"]
        for item in ledger["achievements"]
    )
    assert all(
        "data/public.csv" in item["public_inputs"]
        for item in ledger["achievements"]
    )


def test_achievement_ledger_excludes_administrative_outputs(tmp_path: Path) -> None:
    root, _candidate_record = _candidate(tmp_path)
    (root / "outputs/data_inventory.csv").write_text("path\npublic.csv\n", encoding="utf-8")
    (root / "outputs/data_access_status.json").write_text(
        '{"available": true}\n', encoding="utf-8"
    )
    (root / "outputs/analysis_metadata.json").write_text("{}\n", encoding="utf-8")
    script = root / "code/analyze.py"
    script.write_text(
        script.read_text(encoding="utf-8")
        + "\nADMIN = ('outputs/data_inventory.csv', 'outputs/data_access_status.json', "
        "'outputs/analysis_metadata.json')\n",
        encoding="utf-8",
    )
    report = root / "report/report.md"
    report.write_text(
        report.read_text(encoding="utf-8")
        + "\nBookkeeping: outputs/data_inventory.csv, outputs/data_access_status.json, "
        "outputs/analysis_metadata.json.\n",
        encoding="utf-8",
    )

    trace = build_baseline_success_trace(candidate_root=root)
    ledger = build_baseline_achievement_ledger(trace=trace, candidate_root=root)
    evidence = {
        ref
        for item in ledger["achievements"]
        for ref in item["candidate_evidence_refs"]
    }

    assert "outputs/metrics.csv" in evidence
    assert "report/images/trend.png" in evidence
    assert "outputs/data_inventory.csv" not in evidence
    assert "outputs/data_access_status.json" not in evidence
    assert "outputs/analysis_metadata.json" not in evidence


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
    assert view["baseline_success_trace"]["events"][0]["action"].startswith(
        "Reconstruct and execute candidate-created `code/analyze.py`"
    )


def test_memory_gate_requires_unique_required_achievement_coverage(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = _four_required_achievements(projection["baseline_evidence_capsule"])
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
    capsule = _four_required_achievements(projection["baseline_evidence_capsule"])
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
    capsule = _four_required_achievements(projection["baseline_evidence_capsule"])
    artifacts = _complete_artifacts(capsule)
    first = capsule["baseline_achievement_ledger"]["achievements"][0]
    artifacts["text_memory"] = " ".join(
        f"PRESERVE {first['achievement_id']}: retain candidate method "
        f"{' '.join(first['method_signature'])} and its "
        f"{' '.join(first['evidence_role_signature'])} {_output_words(first)} evidence role; "
        "do not drop this baseline capability."
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
    prompt_contract = projection["reflector_feedback"]["a00_balanced_evolution_context"]
    assert "analyze" in json.dumps(prompt_contract).casefold()
    assert "figure" in json.dumps(prompt_contract).casefold()
    assert "required" in json.dumps(prompt_contract).casefold()
    assert "never replace the baseline plan" in json.dumps(prompt_contract).casefold()
    assert any(
        "trend.png" in ref
        for item in capsule["baseline_achievement_ledger"]["achievements"]
        for ref in item["candidate_evidence_refs"]
    )

    poisoned = deepcopy(capsule)
    poisoned["candidate_concepts"][0]["text"] = "Hidden target relation with private curvature."
    with pytest.raises(BaselineEvidenceCapsuleAdmissionError):
        admit_baseline_evidence_capsule(poisoned, candidate_root=root, ground_truth_entries=_gt())


def test_balanced_context_contains_strengths_achievements_and_all_actions(
    tmp_path: Path,
) -> None:
    _root, projection = _projection(tmp_path)
    context = projection["reflector_feedback"]["a00_balanced_evolution_context"]
    capsule = projection["reflector_baseline_evidence_capsule"]

    assert context["baseline_successes"]
    assert len(context["baseline_achievements"]) == len(
        capsule["baseline_achievement_ledger"]["achievements"]
    )
    assert len(context["sanitized_feedback"]) == len(capsule["weakness_to_action"])
    assert len(context["additive_improvement_targets"]) == len(
        capsule["weakness_to_action"]
    )
    assert all("ADDITIVE" in item for item in context["additive_improvement_targets"])


def test_balanced_context_keeps_every_bounded_success_trace_event(
    tmp_path: Path,
) -> None:
    _root, projection = _projection(tmp_path)
    context = projection["reflector_feedback"]["a00_balanced_evolution_context"]
    capsule = projection["reflector_baseline_evidence_capsule"]

    assert len(capsule["baseline_success_trace"]["events"]) > 2
    assert len(context["baseline_success_trace"]) == len(
        capsule["baseline_success_trace"]["events"]
    )
    assert all(
        event["trace_id"] in rendered
        for event, rendered in zip(
            capsule["baseline_success_trace"]["events"],
            context["baseline_success_trace"],
            strict=True,
        )
    )


def test_balanced_context_rejects_weakness_only_or_success_only(
    tmp_path: Path,
) -> None:
    _root, projection = _projection(tmp_path)
    sanitized = deepcopy(projection["reflector_sanitized_feedback"])
    capsule = deepcopy(projection["reflector_baseline_evidence_capsule"])

    weakness_only = deepcopy(sanitized)
    weakness_only["preserve_strengths"] = []
    with pytest.raises(FeedbackProjectionError, match="BALANCED_EVOLUTION_CONTEXT_FAILED"):
        build_balanced_evolution_context(
            sanitized_feedback=weakness_only,
            capsule=capsule,
        )

    success_only = deepcopy(sanitized)
    success_only["diagnoses"] = []
    with pytest.raises(FeedbackProjectionError, match="BALANCED_EVOLUTION_CONTEXT_FAILED"):
        build_balanced_evolution_context(
            sanitized_feedback=success_only,
            capsule={**capsule, "weakness_to_action": []},
        )


def _equivalence_ledger() -> dict[str, Any]:
    return {
        "schema_version": "openevo.researchclawbench.baseline_achievement_ledger.v2",
        "required_output_class_counts": {
            "script": 1,
            "numeric_output": 1,
            "figure": 1,
            "report_section": 1,
        },
        "achievements": [
            {
                "achievement_id": "achievement_alpha",
                "capability": "Reconstruct alpha orbit regression evidence.",
                "method_signature": ["alpha", "orbit", "regression"],
                "evidence_role_signature": ["alpha", "orbit"],
                "evidence_output_class": "figure",
                "required_output_classes": ["script", "numeric_output", "figure", "report_section"],
                "preservation_priority": "required",
            },
            {
                "achievement_id": "achievement_beta",
                "capability": "Reconstruct beta classifier comparison evidence.",
                "method_signature": ["beta", "classifier", "comparison"],
                "evidence_role_signature": ["beta", "classifier"],
                "evidence_output_class": "figure",
                "required_output_classes": ["script", "numeric_output", "figure", "report_section"],
                "preservation_priority": "required",
            },
            {
                "achievement_id": "achievement_gamma",
                "capability": "Reconstruct gamma entropy calibration evidence.",
                "method_signature": ["gamma", "entropy", "calibration"],
                "evidence_role_signature": ["gamma", "entropy"],
                "evidence_output_class": "figure",
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
    signatures = {
        "achievement_alpha": "alpha orbit regression",
        "achievement_beta": "beta classifier comparison",
        "achievement_gamma": "gamma entropy calibration",
    }
    (root / "code/rebuilt.py").write_text(
        "\n".join(f"# {signatures[item]}" for item in include) + "\n",
        encoding="utf-8",
    )
    (root / "outputs/metrics.csv").write_text("metric,value\na,1\n", encoding="utf-8")
    report_lines = ["# Evolved report", "Verification check completed from public data."]
    for identifier in include:
        token = identifier.removeprefix("achievement_")
        figure = f"report/images/{token}_{token}.png"
        (root / figure).write_bytes(b"plot")
        report_lines.append(
            f"{identifier}: verify the {signatures[identifier]} {token} script with a numeric table and "
            f"figure at images/{token}_{token}.png; retain this report evidence discussion."
        )
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
        report_path.read_text(encoding="utf-8").replace(
            "report evidence discussion.",
            "report evidence discussion with a corrected conclusion after the public-data check.",
        ),
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger, evolved_candidate_root=root, validation={"artifact_valid": True}
    )

    assert report["status"] == "PASS", report
    assert report["reconstructed_required_achievement_count"] == 3
    assert all(item["candidate_conclusion_revised"] for item in report["findings"])
    assert report["provider_calls"] == 0
    assert report["raw_gt_visible"] is False


def test_baseline_equivalence_accepts_evidence_chain_across_report_sections(
    tmp_path: Path,
) -> None:
    ledger = {
        "schema_version": "openevo.researchclawbench.baseline_achievement_ledger.v2",
        "required_output_class_counts": {
            "script": 1,
            "numeric_output": 1,
            "figure": 1,
            "report_section": 1,
        },
        "achievements": [
            {
                "achievement_id": "achievement_01",
                "capability": "Reconstruct catalog clamp logit sigmoid route.",
                "method_signature": ["catalog", "clamp", "logit", "sigmoid"],
                "evidence_role_signature": ["catalog", "distribution"],
                "evidence_output_class": "figure",
                "required_output_classes": [
                    "script",
                    "numeric_output",
                    "figure",
                    "report_section",
                ],
                "preservation_priority": "required",
            }
        ],
    }
    root = tmp_path / "evolved"
    (root / "code").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "report/images").mkdir(parents=True)
    (root / "code/rebuilt_pipeline.py").write_text(
        "# catalog clamp logit sigmoid route\n", encoding="utf-8"
    )
    (root / "outputs/summary.json").write_text('{"passes": true}\n', encoding="utf-8")
    (root / "report/images/overview.png").write_bytes(b"plot")
    (root / "report/report.md").write_text(
        """# Report

## Method
The catalog clamp, logit, and sigmoid route reconstructs the catalog distribution in the analysis code.

## Results
The numeric summary table records the derived values.

## Figure
The catalog distribution figure at images/overview.png shows the reconstructed distribution.

## Independent validation
The split comparison check passes on the public data.

## Reproducibility
The report evidence is regenerated by the script.
""",
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger,
        evolved_candidate_root=root,
        validation={"artifact_valid": True},
    )

    assert report["status"] == "PASS"
    assert report["findings"][0]["report_output_roles_present"] is True
    assert report["findings"][0]["verification_assertion_present"] is True


def test_baseline_equivalence_rejects_additive_replacement_when_capability_missing(tmp_path: Path) -> None:
    ledger = _equivalence_ledger()
    root = _evolved_workspace(tmp_path, include=["achievement_alpha", "achievement_gamma"])

    report = assess_baseline_equivalence(
        ledger=ledger, evolved_candidate_root=root, validation={"artifact_valid": True}
    )

    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"
    assert "achievement_beta" in report["dropped_required_achievement_ids"]


def test_baseline_equivalence_rejects_public_input_as_numeric_output(
    tmp_path: Path,
) -> None:
    ledger = {
        "schema_version": "openevo.researchclawbench.baseline_achievement_ledger.v2",
        "required_output_class_counts": {
            "script": 1,
            "numeric_output": 1,
            "figure": 0,
            "report_section": 1,
        },
        "achievements": [
            {
                "achievement_id": "achievement_01",
                "capability": "Reconstruct catalog clamp logit route.",
                "method_signature": ["catalog", "clamp", "logit"],
                "evidence_role_signature": ["catalog", "summary"],
                "evidence_output_class": "numeric_output",
                "required_output_classes": ["script", "numeric_output", "report_section"],
                "preservation_priority": "required",
            }
        ],
    }
    root = tmp_path / "evolved"
    (root / "code").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "report").mkdir()
    (root / "code/rebuilt.py").write_text("# catalog clamp logit\n", encoding="utf-8")
    (root / "data/public.csv").write_text("x\n1\n", encoding="utf-8")
    (root / "report/report.md").write_text(
        "Catalog clamp logit method. Validation check in this report section.",
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger,
        evolved_candidate_root=root,
        validation={"artifact_valid": True},
    )

    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"
    assert report["findings"][0]["required_output_classes_present"]["numeric_output"] is False


def test_baseline_equivalence_rejects_achievement_name_without_method(
    tmp_path: Path,
) -> None:
    ledger = _equivalence_ledger()
    root = _evolved_workspace(tmp_path, include=["achievement_alpha"])
    (root / "code/rebuilt.py").write_text("# unrelated implementation\n", encoding="utf-8")
    report_path = root / "report/report.md"
    report_path.write_text(
        "achievement_alpha: verify a numeric table, figure, script, and report evidence.",
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger,
        evolved_candidate_root=root,
        validation={"artifact_valid": True},
    )

    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"
    assert report["findings"][0]["method_reconstructed"] is False


def test_baseline_equivalence_enforces_baseline_output_count_floor(
    tmp_path: Path,
) -> None:
    ledger = _equivalence_ledger()
    ledger["required_output_class_counts"]["figure"] = 4
    root = _evolved_workspace(
        tmp_path,
        include=["achievement_alpha", "achievement_beta", "achievement_gamma"],
    )

    report = assess_baseline_equivalence(
        ledger=ledger,
        evolved_candidate_root=root,
        validation={"artifact_valid": True},
    )

    assert report["reconstructed_required_achievement_count"] == 3
    assert report["output_class_floor_passed"] is False
    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"


def test_baseline_equivalence_does_not_borrow_role_across_output_classes(
    tmp_path: Path,
) -> None:
    ledger = {
        "schema_version": "openevo.researchclawbench.baseline_achievement_ledger.v2",
        "required_output_class_counts": {
            "script": 1,
            "numeric_output": 1,
            "figure": 1,
            "report_section": 1,
        },
        "achievements": [
            {
                "achievement_id": "achievement_figure",
                "capability": "Reconstruct topk precision comparison figure.",
                "method_signature": ["topk", "precision", "comparison"],
                "evidence_role_signature": ["topk", "precision"],
                "evidence_output_class": "figure",
                "required_output_classes": ["script", "figure", "report_section"],
                "preservation_priority": "required",
            },
            {
                "achievement_id": "achievement_numeric",
                "capability": "Reconstruct topk precision comparison table.",
                "method_signature": ["topk", "precision", "comparison"],
                "evidence_role_signature": ["topk", "precision"],
                "evidence_output_class": "numeric_output",
                "required_output_classes": [
                    "script",
                    "numeric_output",
                    "report_section",
                ],
                "preservation_priority": "required",
            },
        ],
    }
    root = tmp_path / "evolved"
    (root / "code").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "report/images").mkdir(parents=True)
    (root / "code/rebuilt.py").write_text(
        "# topk precision comparison\n", encoding="utf-8"
    )
    (root / "report/images/topk_precision.png").write_bytes(b"plot")
    (root / "outputs/unrelated.csv").write_text("value\n1\n", encoding="utf-8")
    (root / "report/report.md").write_text(
        "Topk precision comparison is verified by the figure "
        "images/topk_precision.png. An unrelated numeric table is stored at "
        "outputs/unrelated.csv. The script and report discussion are reproducible.",
        encoding="utf-8",
    )

    report = assess_baseline_equivalence(
        ledger=ledger,
        evolved_candidate_root=root,
        validation={"artifact_valid": True},
    )

    assert report["output_class_floor_passed"] is True
    assert report["findings"][0]["status"] == "PASS"
    assert report["findings"][1]["evidence_role_signature_present"] is False
    assert report["status"] == "BASELINE_EQUIVALENCE_FAILED"


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
    assert report["aggregate"]["required_achievement_memory_skill_union_coverage"] == 1
    assert report["aggregate"]["artifact_role_redundancy_ratio"] < 0.65
    assert report["gt_leakage_findings"] == []


def test_artifact_role_redundancy_rejects_three_copies(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    complete = _complete_artifacts(capsule)
    repeated = "\n".join(complete.values())
    report = assess_task_specific_artifact_quality(
        capsule=capsule,
        artifact_texts={kind: repeated for kind in complete},
        ground_truth_entries=_gt(),
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["aggregate"]["artifact_role_redundancy_ratio"] == 1


def test_artifact_roles_reject_memory_workflow_and_agent_handbook(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    artifacts["text_memory"] += "\nPhase 1 inventory workspace; Phase 2 run scripts."
    artifacts["agent_system"] += "\n" + ("generic operating handbook rule " * 400)
    report = assess_task_specific_artifact_quality(
        capsule=capsule,
        artifact_texts=artifacts,
        ground_truth_entries=_gt(),
    )

    assert report["status"] == QUALITY_FAILURE
    assert report["artifact_metrics"]["text_memory"][
        "retained_knowledge_not_full_workflow"
    ] is False
    assert report["artifact_metrics"]["agent_system"]["constraint_only_role"] is False


def test_gate_keeps_multisentence_markdown_anchor_as_one_semantic_unit(tmp_path: Path) -> None:
    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    achievements = capsule["baseline_achievement_ledger"]["achievements"]
    anchors = "\n".join(
        f"- PRESERVE {achievement['achievement_id']}: retain candidate method "
        f"{' '.join(achievement['method_signature'])}. It produces the "
        f"{' '.join(achievement['evidence_role_signature'])} "
        f"{_output_words(achievement)} evidence chain. Do not drop this baseline capability."
        for achievement in achievements
    )
    artifacts["text_memory"] = "# Preservation\n\n" + anchors

    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == "PASS"
    assert report["artifact_metrics"]["text_memory"]["required_achievement_memory_coverage"] == 1


def test_skill_gate_accepts_one_achievement_across_bounded_markdown_blocks(
    tmp_path: Path,
) -> None:
    """Regression for the first R3.12 native Astronomy skill.

    Method, output roles, reconstruction, and verification are intentionally
    separate bullets.  They belong to one explicit achievement and must not be
    mistaken for missing reconstruction fidelity.
    """

    _root, projection = _projection(tmp_path)
    capsule = projection["baseline_evidence_capsule"]
    artifacts = _complete_artifacts(capsule)
    achievements = capsule["baseline_achievement_ledger"]["achievements"]
    reconstruction_blocks = "\n\n".join(
        f"`{achievement['achievement_id']}`:\n"
        f"- Candidate/public method: {' '.join(achievement['method_signature'])}.\n"
        f"- Evidence role: {' '.join(achievement['evidence_role_signature'])} "
        f"{_output_words(achievement)}.\n"
        "- Reconstruction steps: reconstruct the public-data method and regenerate evidence."
        for achievement in achievements
    )
    verification_blocks = "\n\n".join(
        f"`{achievement['achievement_id']}`:\n"
        "- Verification steps: run the method, confirm outputs, and verify report evidence."
        for achievement in achievements
    )
    artifacts["skill_bundle"] = (
        "Phase 1 — Reconstruct baseline capabilities\n\n"
        f"{reconstruction_blocks}\n\n"
        "Phase 2 — Verify reconstruction\n\n"
        f"{verification_blocks}\n\n"
        "Phase 3 — Add evaluator-driven improvements\n"
        + "\n".join(
            f"- For {item['addresses']} and {item['achievement_id']}, add a public-data cross-check without replacing the baseline path."
            for item in capsule["improvement_actions"]
        )
        + "\n\nPhase 4 — Integrate without deleting preserved work.\n"
    )

    report = assess_task_specific_artifact_quality(
        capsule=capsule, artifact_texts=artifacts, ground_truth_entries=_gt()
    )

    assert report["status"] == "PASS", report
    assert report["artifact_metrics"]["skill_bundle"][
        "required_achievement_reconstruction_coverage"
    ] == 1
