from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import openevo_researchclawbench.evaluation_feedback as feedback_module
import pytest
from openevo_researchclawbench.evaluation_feedback import (
    EvaluationFeedbackProjectionPort,
    FeedbackAdmissionError,
    admit_sanitized_feedback,
    objective_outcome_signal,
    project_sanitized_evaluation_feedback,
)
from openevo_researchclawbench.task_specific_artifact_quality import (
    require_task_specific_artifact_quality,
)

from openevo.evolution import methods as methods_module
from openevo.evolution.framework.execution import (
    ReflectorInferenceRequest,
    ReflectorInferenceResponse,
    ReflectorRuntimeReceipt,
)
from openevo.evolution.managed_reflector import default_managed_reflector_runtime
from openevo.evolution.methods import run_method
from openevo.evolution.models import WorkerClaimedJob


def _candidate(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "candidate"
    (root / "report/images").mkdir(parents=True)
    (root / "code").mkdir()
    (root / "data").mkdir()
    (root / "report/report.md").write_text(
        "# Methods\n\nWe analyze public data.\n\n"
        "# Results\n\nFigure evidence is presented.\n\n"
        "# Discussion\n\nThe analysis needs independent checks.\n",
        encoding="utf-8",
    )
    (root / "report/images/candidate_summary.png").write_bytes(b"candidate-image")
    (root / "code/analyze.py").write_text("print('public analysis')\n", encoding="utf-8")
    (root / "data/public.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (root / "_agent_output.jsonl").write_text(
        json.dumps({"response": "created report and figure"}) + "\n",
        encoding="utf-8",
    )
    return root, {
        "run_id": "Life_005_a0_feedback_test",
        "session_id": "session-baseline",
        "core_task_id": "task-baseline",
        "core_attempt_id": "attempt-baseline",
        "candidate_output_root": str(root),
    }


def _gt() -> list[dict[str, Any]]:
    return [
        {
            "type": "image",
            "path": "private_target_figure.png",
            "content": "Hidden quasiflux curvature must bend toward the private reference.",
            "keywords": ["quasiflux curvature"],
            "weight": 3,
        }
    ]


def _raw_evaluation() -> dict[str, Any]:
    return {
        "total_score": 41.5,
        "items": [
            {
                "type": "image",
                "score": 41.5,
                "score_valid": True,
                "content": "private criterion is not consumed",
                "reasoning": "private Judge reasoning is not consumed",
                "weight": 3,
            }
        ],
    }


def _projection(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root, candidate = _candidate(tmp_path)
    result = project_sanitized_evaluation_feedback(
        task_id="Life_005",
        candidate=candidate,
        raw_evaluation=_raw_evaluation(),
        public_task_info={
            "task": "Analyze the supplied public data and deliver a report with figures.",
            "data": ["public.csv"],
        },
        ground_truth_entries=_gt(),
    )
    return root, result


def test_objective_error_signal_never_projects_the_correct_answer() -> None:
    signal = objective_outcome_signal(prediction="A", expected="B")
    assert signal == {"incorrect": True}
    assert "B" not in json.dumps(signal)


def test_targeted_feedback_is_candidate_grounded_and_answer_free(tmp_path: Path) -> None:
    _root, result = _projection(tmp_path)
    feedback = result["sanitized_feedback"]
    encoded = json.dumps(feedback, sort_keys=True)

    assert result["admission"]["status"] == "ADMITTED"
    assert result["quality_contract"]["generic_only_advice"] is False
    assert result["quality_contract"]["candidate_specific_references_present"] is True
    assert feedback["preserve_before_improve"].startswith("PRESERVE VERIFIED")
    assert {item["dimension"] for item in feedback["diagnoses"]} == {
        "quantitative_validation",
        "visual_evidence",
    }
    assert all(item["candidate_grounded"] for item in feedback["diagnoses"])
    assert all(item["evidence_refs"] for item in feedback["diagnoses"])
    assert "quasiflux" not in encoded.casefold()
    assert "private_target_figure" not in encoded.casefold()
    assert "private Judge reasoning" not in encoded


def test_raw_gt_literal_is_rejected(tmp_path: Path) -> None:
    root, result = _projection(tmp_path)
    feedback = deepcopy(result["sanitized_feedback"])
    feedback["diagnoses"][0]["candidate_observation"] = _gt()[0]["content"]

    with pytest.raises(FeedbackAdmissionError) as caught:
        admit_sanitized_feedback(
            feedback,
            candidate_root=root,
            public_corpus="public data report figure analysis",
            ground_truth_entries=_gt(),
        )

    assert caught.value.reason_code == "FEEDBACK_GT_LITERAL_VIOLATION"


def test_paraphrased_answer_reconstruction_is_rejected(tmp_path: Path) -> None:
    root, result = _projection(tmp_path)
    feedback = deepcopy(result["sanitized_feedback"])
    feedback["diagnoses"][0]["improvement_direction"] = (
        "The correct relationship should increase toward the hidden reference."
    )

    with pytest.raises(FeedbackAdmissionError) as caught:
        admit_sanitized_feedback(
            feedback,
            candidate_root=root,
            public_corpus="public data report figure analysis",
            ground_truth_entries=_gt(),
        )

    assert caught.value.reason_code == "FEEDBACK_ANSWER_RECONSTRUCTION_VIOLATION"


def test_candidate_evidence_filename_with_digits_is_not_a_numeric_claim(
    tmp_path: Path,
) -> None:
    root, result = _projection(tmp_path)
    evidence = root / "report/images/figure_1_validation.png"
    evidence.write_bytes(b"candidate figure")
    feedback = deepcopy(result["sanitized_feedback"])
    feedback["diagnoses"][0]["candidate_observation"] = (
        "Candidate report links report/images/figure_1_validation.png as evidence."
    )
    feedback["diagnoses"][0]["evidence_refs"] = [
        "report/report.md",
        "report/images/figure_1_validation.png",
    ]

    files = feedback_module._candidate_files(root)
    public_corpus = feedback_module._bounded_text_corpus(root, files) + "\n" + "\n".join(files)
    admission = admit_sanitized_feedback(
        feedback,
        candidate_root=root,
        public_corpus=public_corpus,
        ground_truth_entries=_gt(),
    )

    assert admission["status"] == "ADMITTED"


def test_projection_port_persists_only_sanitized_output(tmp_path: Path) -> None:
    root, candidate = _candidate(tmp_path)
    benchmark = tmp_path / "ResearchClawBench"
    task_root = benchmark / "tasks/Life_005"
    task_root.mkdir(parents=True)
    (task_root / "task_info.json").write_text(
        json.dumps(
            {
                "task": "Analyze the public data and deliver a report with figures.",
                "data": ["public.csv"],
            }
        ),
        encoding="utf-8",
    )

    class Evaluator:
        def read_private_evaluation_for_projection(
            self, *, idempotency_key: str, expected_sha256: str
        ) -> dict[str, Any]:
            assert idempotency_key == "evaluation-key"
            assert expected_sha256 == "a" * 64
            return _raw_evaluation()

    port = EvaluationFeedbackProjectionPort(
        root=tmp_path / "projection-authority",
        researchclawbench_root=benchmark,
        evaluator=Evaluator(),
    )
    request = {
        "task_id": "Life_005",
        "attempt_index": 0,
        "candidate": candidate,
        "validation": {"artifact_root_sha256": "b" * 64},
        "evaluation": {
            "idempotency_key": "evaluation-key",
            "raw_response_sha256": "a" * 64,
            "evaluation_receipt_id": "evaluation-receipt",
        },
        "gt_supervision": {
            "task_id": "Life_005",
            "ground_truth_sha256": "c" * 64,
            "task_local_feedback": {"ground_truth_entries": _gt()},
        },
    }

    result = port.execute(request, "projection-key")
    recovered = port.recover(request, "projection-key")
    stored_text = next((tmp_path / "projection-authority").glob("*.json")).read_text(
        encoding="utf-8"
    )

    assert recovered == result
    assert result["evaluation_frozen"] is True
    assert result["feedback_projector_model_calls"] == 0
    assert result["openevo_state_mutations"] == 0
    assert result["admission"]["status"] == "ADMITTED"
    assert "quasiflux" not in stored_text.casefold()
    assert "private Judge reasoning" not in stored_text
    assert "private criterion is not consumed" not in stored_text
    assert str(root) not in json.dumps(result["sanitized_feedback"])


def _dataset(tmp_path: Path, feedback: dict[str, Any]) -> dict[str, Any]:
    root = tmp_path / "dataset"
    root.mkdir()
    records = root / "records.jsonl"
    records.write_text(
        json.dumps(
            {
                "event_id": "event-feedback",
                "task_id": "Life_005",
                "session_id": "session-baseline",
                "status": "COMPLETED",
                "reward": 0.415,
                "traces": [
                    {
                        "prompt_messages": [
                            {"role": "user", "content": "Analyze the public data."}
                        ],
                        "response_messages": [
                            {
                                "role": "assistant",
                                "content": "Created report/report.md and a candidate figure.",
                            }
                        ],
                        "metadata": {"capture_mode": "transcript"},
                    }
                ],
                "payload": {"evolution_feedback": {"training_attachments": [feedback]}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "dataset_id": "dataset-feedback",
                "name": "sanitized feedback dataset",
                "records_path": "records.jsonl",
                "records_uri": records.as_uri(),
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )
    return {
        "artifact_id": "artifact-feedback-dataset",
        "type": "dataset",
        "uri": manifest.as_uri(),
        "name": "sanitized feedback dataset",
    }


def test_native_reflector_requests_see_candidate_specific_retention_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, projection = _projection(tmp_path / "projection")
    feedback = deepcopy(projection["reflector_feedback"])
    # The actual Core renderer receives a bounded string.  Exercise the
    # regression case in which a third admitted diagnosis must remain visible
    # rather than disappearing behind diagnoses[0].
    feedback["a00_preservation_first_prompt_contract"]["02_all_sanitized_diagnoses"].append(
        "weakness_03 -> achievement_01: reproducibility; candidate=report-linked rerun; "
        "ADD public rerun verification"
    )
    dataset = _dataset(tmp_path, feedback)
    runtime = default_managed_reflector_runtime()
    captured: list[ReflectorInferenceRequest] = []

    class FakeService:
        def infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
            captured.append(request)
            if "ExpeL" in request.prompt:
                text = (
                    "# Memory\n\n## Do\nPRESERVE achievement_01 analyze script and report evidence. "
                    "PRESERVE achievement_02 public csv numeric table and report evidence. "
                    "PRESERVE achievement_03 candidate summary figure and report evidence.\n\n"
                    "## Avoid\nDo not replace required baseline achievements.\n\n"
                    "## Validate\nKeep achievement_01 while weakness_01 adds an independent numeric cross-check; "
                    "keep achievement_02 while weakness_02 adds a public-input comparison.\n\n"
                    "## When Applicable\nUse for public-data analysis.\n\n"
                    "## Retired Or Superseded\nNone.\n"
                )
            elif "Skill Bundle" in request.prompt:
                text = (
                    "# Preservation-first skill\n\n"
                    "Phase 1 — Reconstruct baseline capabilities. RECONSTRUCT achievement_01 analyze script and verify report evidence. "
                    "RECONSTRUCT achievement_02 public csv numeric table and verify report evidence. "
                    "RECONSTRUCT achievement_03 candidate summary figure and verify report evidence.\n"
                    "Phase 2 — Verify reconstruction.\n"
                    "Phase 3 — Add evaluator-driven improvements. For weakness_01 and achievement_01, add a numeric cross-check. "
                    "For weakness_02 and achievement_02, add a public-input comparison validation.\n"
                    "Phase 4 — Integrate without deleting preserved work.\n"
                )
            else:
                text = (
                    "# Evolved Agent System\n\n"
                    "- RECONSTRUCT, PRESERVE, EXTEND, and VERIFY before submission. "
                    "Pass baseline equivalence; new analyses are additive and must not replace "
                    "required baseline achievements.\n"
                )
            receipt = ReflectorRuntimeReceipt(
                request_id=request.request_id,
                session_id=f"capture-{len(captured)}",
                runtime_profile=request.runtime.profile,
                runtime_digest=request.runtime.image_digest.removeprefix("sha256:"),
                codex_binary=request.runtime.codex_binary,
                actual_cli_version=request.runtime.expected_cli_version,
                model_name=request.model_name,
                reasoning_effort=request.reasoning_effort,
                auth_mode=request.runtime.auth_mode,
                capture_mode=request.runtime.capture_mode,
                path_fallback_allowed=request.runtime.path_fallback_allowed,
                exit_status=0,
                transcript_sha256="a" * 64,
            )
            return ReflectorInferenceResponse(
                request_id=request.request_id,
                text=text,
                receipt=receipt,
            )

    monkeypatch.setattr(
        methods_module,
        "require_active_reflector_service",
        lambda: FakeService(),
    )
    common = {
        "reflector_llm": {
            "provider": "codex_cli",
            "model": "gpt-5.5",
            "runtime": runtime.model_dump(mode="json"),
        },
        "candidate_count": 1,
    }
    outputs: dict[str, str] = {}
    for method in (
        "text_memory_expel_reflector",
        "skill_bundle_reflector",
        "agent_system_gepa_reflector",
    ):
        artifacts = run_method(
            WorkerClaimedJob(
                job_id=f"job-{method}",
                lease_id=f"lease-{method}",
                job_type="reference",
                method=method,
                input_artifacts=[dataset],
                config=common,
            ),
            artifact_root=tmp_path / "artifacts",
        )
        produced = [item for item in artifacts if item.type.value in {
            "text_memory", "skill_bundle", "agent_system"
        }]
        assert produced
        artifact = produced[0]
        path = Path(artifact.uri.removeprefix("file://"))
        if path.is_dir():
            relative = artifact.manifest.get("content_path") or artifact.manifest.get(
                "target_path"
            ) or "SKILL.md"
            path = path / str(relative)
        outputs[artifact.type.value] = path.read_text(encoding="utf-8")

    assert len(captured) == 3
    prompts = "\n".join(request.prompt for request in captured)
    assert all(request.model_name == "gpt-5.5" for request in captured)
    # The real native renderer bounds a reflected record.  The adapter must
    # therefore place the concrete candidate strategy/action ahead of the
    # verbose capsule, rather than only proving that the full JSON exists.
    assert prompts.count("R3 TASK-LOCAL; NOT generic SOP") >= 3
    assert prompts.count("Memory: PRESERVE each ID with method+evidence") >= 3
    assert prompts.count("candidate_summary") >= 3
    assert "Candidate report links report/images/candidate_summary.png" in prompts
    assert "fresh workspace" in prompts
    assert prompts.count("Baseline Success Trace") >= 3
    assert prompts.count("weakness_01") >= 3
    assert prompts.count("weakness_02") >= 3
    assert prompts.count("weakness_03") >= 3
    assert prompts.count("achievement_01 REQUIRED") >= 3
    assert "candidate_summary.png" in prompts
    assert "quasiflux" not in prompts.casefold()
    assert "private_target_figure" not in prompts.casefold()
    assert "private Judge reasoning" not in prompts
    assert "ground_truth_entries" not in prompts
    quality = require_task_specific_artifact_quality(
        capsule=projection["baseline_evidence_capsule"],
        artifact_texts=outputs,
        ground_truth_entries=_gt(),
    )
    assert quality["status"] == "PASS"
