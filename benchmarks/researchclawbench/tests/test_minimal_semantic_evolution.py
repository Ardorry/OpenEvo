from __future__ import annotations

import json
from pathlib import Path

import pytest
from openevo_researchclawbench.minimal_semantic_evolution import (
    MINIMAL_CONTEXT_SCHEMA,
    MINIMAL_TRACE_SCHEMA,
    MinimalSemanticFeedbackProjectionPort,
    admit_minimal_baseline_trace,
    assess_minimal_semantic_artifact_quality,
    build_minimal_baseline_trace,
    build_minimal_evolution_context,
)
from openevo_researchclawbench.training_state_store import canonical_sha256

from openevo.evolution import methods as methods_module
from openevo.evolution.framework.execution import (
    ReflectorInferenceRequest,
    ReflectorInferenceResponse,
    ReflectorRuntimeReceipt,
)
from openevo.evolution.managed_reflector import default_managed_reflector_runtime
from openevo.evolution.methods import run_method
from openevo.evolution.models import WorkerClaimedJob


def _workspace(tmp_path: Path, source: str) -> Path:
    for name in ("code", "data", "outputs", "report/images", "related_work"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "code/analysis.py").write_text(source, encoding="utf-8")
    (tmp_path / "data/input.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (tmp_path / "related_work/paper_000.pdf").write_bytes(b"candidate-public-pdf")
    (tmp_path / "outputs/metrics.csv").write_text("metric,value\na,1\n", encoding="utf-8")
    (tmp_path / "report/images/trend.png").write_bytes(b"candidate-image")
    (tmp_path / "report/report.md").write_text(
        "# Results\nThe analysis uses `metrics.csv` and `trend.png`.\n",
        encoding="utf-8",
    )
    transcript = {
        "metadata": {
            "transcript": "\n".join(
                [
                    json.dumps(
                        {
                            "item": {
                                "type": "command_execution",
                                "command": "pwd && ls",
                                "status": "completed",
                                "exit_code": 0,
                            }
                        }
                    ),
                    json.dumps(
                        {
                            "item": {
                                "type": "command_execution",
                                "command": "python code/analysis.py",
                                "status": "completed",
                                "exit_code": 0,
                            }
                        }
                    ),
                    json.dumps(
                        {
                            "item": {
                                "type": "command_execution",
                                "command": "python scratch.py",
                                "status": "failed",
                                "exit_code": 1,
                            }
                        }
                    ),
                ]
            )
        }
    }
    (tmp_path / "_agent_output.jsonl").write_text(json.dumps(transcript) + "\n", encoding="utf-8")
    return tmp_path


def _feedback(*dimensions: str) -> dict[str, object]:
    return {
        "feedback_class": "sanitized_evaluation_feedback_v1",
        "preserve_strengths": [
            {
                "dimension": "delivery_quality",
                "candidate_observation": "The Candidate completed an executable path.",
                "improvement_direction": "Preserve that path.",
                "evidence_refs": ["report/report.md"],
            }
        ],
        "diagnoses": [
            {
                "dimension": dimension,
                "severity": "medium",
                "candidate_observation": f"Candidate evidence needs {dimension}.",
                "improvement_direction": f"Add an independent {dimension} check.",
                "evidence_refs": ["report/report.md"],
            }
            for dimension in dimensions
        ],
    }


def _trace(
    *,
    summary: str,
    steps: list[str],
    parameters: list[str] | None = None,
    outputs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": MINIMAL_TRACE_SCHEMA,
        "successful_paths": [
            {
                "summary": summary,
                "why": "The baseline executed this path and used it in the report.",
                "inputs": ["data/input.csv"],
                "steps": steps,
                "parameters": parameters or [],
                "outputs": outputs or ["outputs/metrics.csv"],
                "report_role": "Supports the baseline scientific result.",
            }
        ],
        "projector_model_calls": 0,
    }


def _artifact_texts(memory: str, skill: str) -> dict[str, str]:
    return {
        "text_memory": memory,
        "skill_bundle": skill,
        "agent_system": (
            "Keep what worked and improve what sanitized feedback identified; "
            "do not discard an effective baseline analysis."
        ),
    }


def test_trace_keeps_successful_scientific_chain_and_filters_command_noise(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        """
from pathlib import Path

def read_public_table(path):
    return Path(path).read_text()

def fit_method(rows, probability_threshold=0.90):
    return [row for row in rows if row >= probability_threshold]

def confusion_analysis(values):
    return {"accepted": len(values)}

def plot_trend(values):
    Path("report/images/trend.png").write_bytes(b"plot")

def main():
    rows = read_public_table("data/input.csv")
    selected = fit_method([0.8, 0.95])
    confusion_analysis(selected)
    Path("outputs/metrics.csv").write_text(str(rows))
    plot_trend(selected)

if __name__ == "__main__":
    main()
""",
    )
    (root / "code/unused.py").write_text(
        "def unrelated_replacement_pipeline():\n    return 'not executed'\n",
        encoding="utf-8",
    )

    trace = build_minimal_baseline_trace(candidate_root=root)
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["projector_model_calls"] == 0
    assert 3 <= len(trace["successful_paths"]) <= 8
    assert "read_public_table" in serialized
    assert "fit_method" in serialized
    assert "confusion_analysis" in serialized
    assert "probability_threshold=0.90" in serialized
    assert "selected = fit_method([0.8, 0.95])" in serialized
    assert "pwd && ls" not in serialized
    assert "scratch.py" not in serialized
    assert "unrelated_replacement_pipeline" not in serialized
    assert admit_minimal_baseline_trace(trace, candidate_root=root)["status"] == "ADMITTED"


def test_trace_keeps_candidate_table_values_scoring_formula_and_report_reasoning(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        '''
from dataclasses import dataclass

@dataclass
class Candidate:
    name: str
    homo: float
    lumo: float

def candidate_table():
    return [
        Candidate("BNE", -7.55, -0.28),
        Candidate("DNE", -7.82, -0.42),
    ]

def sigmoid(x, midpoint, scale):
    return 1.0 / (1.0 + x)

def compute_descriptors(candidate):
    reduction_propensity = sigmoid(-candidate.lumo, 0.05, 0.18)
    oxidation_propensity = sigmoid(candidate.homo, -8.05, 0.22)
    additive_score = 0.34 * oxidation_propensity + 0.26 * reduction_propensity
    return additive_score

def main():
    for candidate in candidate_table():
        compute_descriptors(candidate)
''',
    )
    (root / "report/report.md").write_text(
        "# Results\n\n"
        "The candidate_descriptors.csv screen retained BNE as the leading baseline "
        "because the explicit frontier-orbital scoring route produced the strongest score.\n",
        encoding="utf-8",
    )

    trace = build_minimal_baseline_trace(candidate_root=root)
    serialized = json.dumps(trace, ensure_ascii=False)
    parameter_text = "\n".join(
        parameter
        for path in trace["successful_paths"]
        for parameter in path["parameters"]
    )

    assert 'Candidate("BNE", -7.55, -0.28)' in parameter_text
    assert "reduction_propensity = sigmoid(-candidate.lumo, 0.05, 0.18)" in parameter_text
    assert (
        "additive_score = 0.34 * oxidation_propensity + 0.26 * reduction_propensity"
        in parameter_text
    )
    assert "retained BNE as the leading baseline" in serialized


def test_trace_ignores_external_presentation_constructors(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        '''
class Candidate:
    def __init__(self, name, onset, scale):
        self.name = name
        self.onset = onset
        self.scale = scale

def main():
    return analyze_candidate()

def analyze_candidate():
    decision_threshold = 0.90
    candidate = Candidate("BNE", 0.05, 0.18)
    Rectangle((0, 0), 12, 8)
    return candidate, decision_threshold
''',
    )

    trace = build_minimal_baseline_trace(candidate_root=root)
    parameter_text = "\n".join(
        parameter
        for path in trace["successful_paths"]
        for parameter in path["parameters"]
    )

    assert 'Candidate("BNE", 0.05, 0.18)' in parameter_text
    assert "decision_threshold = 0.90" in parameter_text
    assert "Rectangle((0, 0), 12, 8)" not in parameter_text


def test_astronomy_semantic_diagnostics_do_not_block_dispatch() -> None:
    trace = _trace(
        summary="Select the quasar catalog, fit logistic predictions, and inspect confusion.",
        steps=[
            "Call `selection_rules` from `code/quasar_pipeline.py`.",
            "Call `train_logistic` from `code/quasar_pipeline.py`.",
            "Call `confusion` from `code/quasar_pipeline.py`.",
        ],
        parameters=['row["p_QSO"] >= 0.90 and row["p_WISE_QSO"] >= 0.97'],
        outputs=["outputs/metrics.csv"],
    )
    feedback = _feedback("quantitative_validation")
    generic = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            "Perform catalog selection and validation.",
            "Add quantitative validation to the catalog analysis.",
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )
    concrete = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            "Retain p_QSO >= 0.90 and p_WISE_QSO >= 0.97 as the baseline selection.",
            (
                "Run selection_rules, train_logistic, and confusion; write metrics.csv, "
                "then add an independent quantitative validation comparison."
            ),
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )

    assert generic["status"] == "PASS"
    assert generic["hard_safety"]["pass"] is True
    assert generic["dispatch_authority"]["pass"] is True
    assert generic["dispatch_authority"]["semantic_heuristics_hard_blocking"] is False
    assert generic["minimal_usability"] == {
        "authority": "diagnostic_only",
        "pass": False,
        "baseline_method_present": False,
        "feedback_action_present": True,
        "generic_only": False,
    }
    assert concrete["status"] == "PASS"
    assert concrete["minimal_usability"]["pass"] is True


def test_score_percentile_grid_resolution_is_not_a_hard_scientific_parameter() -> None:
    trace = _trace(
        summary="Select score thresholds from candidate percentiles.",
        steps=["Call `select_threshold` from `code/quasar_pipeline.py`."],
        parameters=[
            "candidates = sorted(set(q(scores, [i / 100 for i in range(1, 100)])))"
        ],
        outputs=["outputs/threshold_selection.csv"],
    )
    report = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=_feedback("quantitative_validation"),
        artifact_texts=_artifact_texts(
            "Retain score-percentile threshold selection and threshold_selection.csv.",
            (
                "Run select_threshold over score percentiles, then add an independent "
                "quantitative validation comparison."
            ),
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )

    assert report["status"] == "PASS"
    assert report["dispatch_authority"]["pass"] is True
    assert report["diagnostics"]["parameter_literal_retention"]["ratio"] < 1.0
    assert "PARAMETER_LITERAL_RETENTION_INCOMPLETE" in report["diagnostics"]["warnings"]


def test_scientific_formula_parameters_are_not_erased_by_underscores() -> None:
    trace = _trace(
        summary="Descriptor scoring with explicit propensity and score formulas.",
        steps=["Call `compute_descriptors` from `code/screen_additives.py`."],
        parameters=[
            "reduction_propensity = sigmoid(-lumo, 0.05, 0.18)",
            "additive_score = 0.34 * oxidation_propensity + 0.26 * reduction_propensity",
        ],
        outputs=["outputs/candidate_descriptors.csv"],
    )
    feedback = _feedback("quantitative_validation")
    generic = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            "Retain compute_descriptors and candidate_descriptors.csv.",
            "Reconstruct compute_descriptors, then add quantitative validation.",
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )
    concrete = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            (
                "Retain compute_descriptors and candidate_descriptors.csv with the baseline "
                "sigmoid settings 0.05 and 0.18 and score weights 0.34 and 0.26."
            ),
            "Reconstruct compute_descriptors, then add quantitative validation.",
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )

    # Literal fidelity is diagnostic in R4.5.  The concrete method remains
    # present, so omission alone cannot terminate the native lifecycle.
    assert generic["status"] == "PASS"
    assert generic["diagnostics"]["parameter_literal_retention"]["ratio"] < 1.0
    assert concrete["status"] == "PASS"


def test_chemistry_pdf_extraction_diagnostic_does_not_block_dispatch() -> None:
    trace = _trace(
        summary="Extract text from four supplied PDFs before literature screening.",
        steps=[
            "Call `extract_pdf_metadata_and_text` from `code/screen_additives.py`.",
            "Call `pdf_literal_to_text` from `code/screen_additives.py`.",
        ],
        outputs=["outputs/related_work_extraction.json"],
    )
    feedback = _feedback("evidence_coverage")
    generic = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            "Retain literature synthesis.",
            "Perform literature synthesis and add evidence coverage.",
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )
    concrete = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=feedback,
        artifact_texts=_artifact_texts(
            "Retain real text extraction from the four supplied PDFs.",
            (
                "Extract text from the supplied PDFs before synthesis, save "
                "related_work_extraction.json, and add an evidence coverage comparison."
            ),
        ),
        content_admission_findings=[],
        provenance_violations=[],
    )

    assert generic["status"] == "PASS"
    assert generic["minimal_usability"]["pass"] is False
    assert concrete["status"] == "PASS"


def test_context_preserves_parameters_multiple_paths_and_all_feedback() -> None:
    trace = _trace(
        summary="Method M with explicit parameter P.",
        steps=["Call `method_M` from `code/analysis.py`."],
        parameters=["P=0.90"],
    )
    trace["successful_paths"].extend(
        [
            {
                **trace["successful_paths"][0],
                "summary": "Independent baseline path B.",
                "steps": ["Call `method_B` from `code/analysis.py`."],
            },
            {
                **trace["successful_paths"][0],
                "summary": "Independent baseline path C.",
                "steps": ["Call `method_C` from `code/analysis.py`."],
            },
        ]
    )
    feedback = _feedback("quantitative_validation", "visual_evidence", "coverage")

    context = build_minimal_evolution_context(trace=trace, sanitized_feedback=feedback)
    serialized = json.dumps(context, ensure_ascii=False)

    assert context["schema_version"] == MINIMAL_CONTEXT_SCHEMA
    assert all(name in serialized for name in ("method_M", "method_B", "method_C"))
    assert "P=0.90" in serialized
    assert len(context["what_needs_improvement"]["diagnoses"]) == 3
    assert "ground_truth_entries" not in serialized
    assert "judge reasoning" not in serialized.casefold()


def test_native_r4_reflector_prompts_keep_methods_parameters_and_all_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = _trace(
        summary="Select the catalog and validate the fitted model.",
        steps=[
            "Call `selection_rules` from `code/analysis.py`.",
            "Call `train_logistic` from `code/analysis.py`.",
            "Call `confusion` from `code/analysis.py`.",
        ],
        parameters=[
            'row["p_QSO"] >= 0.90 and row["p_WISE_QSO"] >= 0.97',
            "reduction_propensity = sigmoid(-lumo, 0.05, 0.18)",
            "additive_score = 0.34 * oxidation_propensity + 0.26 * reduction_propensity",
        ],
        outputs=["outputs/metrics.csv"],
    )
    feedback = _feedback("visual_evidence", "quantitative_validation", "coverage")
    context = build_minimal_evolution_context(trace=trace, sanitized_feedback=feedback)
    attachment = {
        "a00_minimal_semantic_context": context,
        "schema_version": MINIMAL_CONTEXT_SCHEMA,
        "status": "available_for_evolution",
        "feedback_class": "minimal_semantic_evolution_r4",
    }
    dataset_root = tmp_path / "dataset"
    dataset_root.mkdir()
    records_path = dataset_root / "records.jsonl"
    records_path.write_text(
        json.dumps(
            {
                "event_id": "event-r4",
                "task_id": "task-r4",
                "session_id": "session-r4",
                "status": "COMPLETED",
                "reward": 1.0,
                "traces": [
                    {
                        "prompt_messages": [{"role": "user", "content": "Analyze public inputs."}],
                        "response_messages": [
                            {"role": "assistant", "content": "Completed the analysis."}
                        ],
                        "metadata": {"capture_mode": "transcript"},
                    }
                ],
                "payload": {"evolution_feedback": {"training_attachments": [attachment]}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = dataset_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "dataset_id": "dataset-r4",
                "name": "minimal semantic dataset",
                "records_path": "records.jsonl",
                "records_uri": records_path.as_uri(),
                "event_count": 1,
            }
        ),
        encoding="utf-8",
    )
    dataset = {
        "artifact_id": "artifact-r4-dataset",
        "type": "dataset",
        "uri": manifest_path.as_uri(),
        "name": "minimal semantic dataset",
    }
    captured: list[ReflectorInferenceRequest] = []

    class FakeService:
        def infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
            captured.append(request)
            if "ExpeL" in request.prompt:
                text = (
                    "# Memory\n\n## Do\nKeep selection_rules at p_QSO 0.90 and "
                    "p_WISE_QSO 0.97, plus train_logistic and confusion; retain sigmoid "
                    "settings 0.05 and 0.18 and score weights 0.34 and 0.26.\n\n"
                    "## Avoid\nDo not replace the fitted-model path.\n\n"
                    "## Validate\nAdd the independent quantitative comparison.\n\n"
                    "## When Applicable\nUse for this catalog analysis.\n\n"
                    "## Retired Or Superseded\nNone.\n"
                )
            elif "Skill Bundle Reflection Context" in request.prompt:
                text = (
                    "# Catalog skill\n\nRun selection_rules with p_QSO 0.90 and "
                    "p_WISE_QSO 0.97, then train_logistic and inspect confusion; "
                    "rebuild sigmoid settings 0.05 and 0.18 and score weights 0.34 and 0.26; "
                    "add the independent quantitative comparison without dropping coverage.\n"
                )
            else:
                text = (
                    "# Task-local constraints\n\n- Before finalizing, run selection_rules, "
                    "train_logistic, and confusion; check broad task coverage after adding "
                    "the feedback-requested comparison.\n"
                )
            runtime = request.runtime
            return ReflectorInferenceResponse(
                request_id=request.request_id,
                text=text,
                receipt=ReflectorRuntimeReceipt(
                    request_id=request.request_id,
                    session_id=f"capture-{len(captured)}",
                    runtime_profile=runtime.profile,
                    runtime_digest=runtime.image_digest.removeprefix("sha256:"),
                    codex_binary=runtime.codex_binary,
                    actual_cli_version=runtime.expected_cli_version,
                    model_name=request.model_name,
                    reasoning_effort=request.reasoning_effort,
                    auth_mode=runtime.auth_mode,
                    capture_mode=runtime.capture_mode,
                    path_fallback_allowed=runtime.path_fallback_allowed,
                    exit_status=0,
                    transcript_sha256="a" * 64,
                ),
            )

    monkeypatch.setattr(methods_module, "require_active_reflector_service", lambda: FakeService())
    common = {
        "training_feedback_required": True,
        "task_local_preservation": {
            "schema_version": "openevo.task_local_preservation.v1",
            "scope": "next_session_only",
        },
        "reflector_llm": {
            "provider": "codex_cli",
            "model": "gpt-5.5",
            "runtime": default_managed_reflector_runtime().model_dump(mode="json"),
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
        produced = [
            item
            for item in artifacts
            if item.type.value in {"text_memory", "skill_bundle", "agent_system"}
        ]
        assert produced
        artifact = produced[0]
        path = Path(artifact.uri.removeprefix("file://"))
        if path.is_dir():
            path /= str(
                artifact.manifest.get("content_path")
                or artifact.manifest.get("target_path")
                or "SKILL.md"
            )
        outputs[artifact.type.value] = path.read_text(encoding="utf-8")

    assert len(captured) == 3
    for request in captured:
        prompt = request.prompt
        for expected in (
            "selection_rules",
            "train_logistic",
            "confusion",
            "0.90",
            "0.97",
            "0.05",
            "0.18",
            "0.34",
            "0.26",
            "visual_evidence",
            "quantitative_validation",
            "coverage",
        ):
            assert expected in prompt
        assert "required achievement" not in prompt.casefold()
        assert "baseline equivalence" not in prompt.casefold()
        assert "ground_truth_entries" not in prompt
        assert "judge reasoning" not in prompt.casefold()
    assert "selection_rules" in outputs["text_memory"]
    assert "selection_rules" in outputs["skill_bundle"]
    assert all(
        value in outputs["text_memory"] and value in outputs["skill_bundle"]
        for value in ("0.05", "0.18", "0.34", "0.26")
    )
    assert "feedback-requested comparison" in outputs["agent_system"]


def test_trace_is_bounded_without_dropping_method_parameter_fidelity(
    tmp_path: Path,
) -> None:
    methods = "\n".join(
        f"def analyze_{index}(rows, threshold_{index}=0.{index + 1}):\n"
        f"    return [row for row in rows if row >= threshold_{index}]\n"
        for index in range(8)
    )
    calls = "\n".join(f"    analyze_{index}([0.1, 0.9])" for index in range(8))
    root = _workspace(
        tmp_path,
        methods
        + "\ndef write_plot():\n    return 'report/images/trend.png'\n"
        + "\ndef main():\n"
        + calls
        + "\n    write_plot()\n",
    )

    trace = build_minimal_baseline_trace(candidate_root=root)
    serialized = json.dumps(trace, ensure_ascii=False)

    assert len(trace["successful_paths"]) <= 8
    assert len(serialized.encode("utf-8")) <= 65_536
    assert "analyze_0" in serialized
    assert "threshold_0=0.1" in serialized


def test_projection_wrapper_keeps_frozen_feedback_and_seals_r4_content(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path / "candidate",
        """
def read_input(path):
    return path

def validate_result(values, threshold=0.90):
    return values, threshold

def main():
    validate_result(read_input("data/input.csv"))
""",
    )
    feedback = _feedback("quantitative_validation", "visual_evidence")

    class FrozenProjector:
        def execute(self, request, idempotency_key):
            del request, idempotency_key
            return {
                "task_id": "Astronomy_004",
                "reflector_sanitized_feedback": feedback,
                "reflector_feedback_sha256": "a" * 64,
                "content_sha256": "b" * 64,
            }

    port = MinimalSemanticFeedbackProjectionPort(
        root=tmp_path / "projection", frozen_projector=FrozenProjector()
    )
    request = {
        "task_id": "Astronomy_004",
        "candidate": {"candidate_output_root": str(root)},
    }
    result = port.execute(request, "r4-projection")
    content_sha256 = result["content_sha256"]
    body = {key: value for key, value in result.items() if key != "content_sha256"}

    assert result["schema_version"].endswith(".r4")
    assert (
        len(
            result["reflector_feedback"]["a00_minimal_semantic_context"]["what_needs_improvement"][
                "diagnoses"
            ]
        )
        == 2
    )
    assert content_sha256 == canonical_sha256(body)
    assert port.recover(request, "r4-projection") == result


def test_artifact_quality_requires_core_leakage_admission() -> None:
    trace = _trace(
        summary="Execute candidate method M.",
        steps=["Call `method_M` from `code/analysis.py`."],
    )
    report = assess_minimal_semantic_artifact_quality(
        trace=trace,
        sanitized_feedback=_feedback("quantitative_validation"),
        artifact_texts=_artifact_texts(
            "Retain method_M and metrics.csv. hidden evaluator answer phrase",
            "Run method_M and add an independent quantitative validation.",
        ),
        content_admission_findings=["GT_LITERAL"],
        provenance_violations=[],
    )

    assert report["status"] == "HARD_SAFETY_FAILED"
    assert report["hard_safety"] == {
        "pass": False,
        "findings": ["content_admission:GT_LITERAL"],
    }
    assert report["gt_leakage_findings"] == ["GT_LITERAL"]
