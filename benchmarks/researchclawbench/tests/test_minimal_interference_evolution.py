from __future__ import annotations

import json
from pathlib import Path

from openevo_researchclawbench.minimal_interference_evolution import (
    MINIMAL_CONTEXT_SCHEMA,
    MinimalInterferenceFeedbackProjectionPort,
    admit_selected_baseline_trajectory,
    assess_minimal_interference_artifacts,
    build_minimal_interference_context,
    build_selected_baseline_trajectory,
)


def _workspace(tmp_path: Path, *, source: str, messages: list[str]) -> Path:
    for name in ("code", "data", "outputs", "report/images", "related_work"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "code/analysis.py").write_text(source, encoding="utf-8")
    (tmp_path / "data/input.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (tmp_path / "related_work/paper_000.pdf").write_bytes(b"public-pdf")
    (tmp_path / "outputs/metrics.csv").write_text("metric,value\nauc,.9\n", encoding="utf-8")
    (tmp_path / "report/images/result.png").write_bytes(b"png")
    (tmp_path / "report/report.md").write_text(
        "# Results\n\nThe baseline used p_QSO >= 0.90 and p_WISE_QSO >= 0.95, "
        "then reported logistic AUC, Brier calibration, and confusion analysis from "
        "`outputs/metrics.csv`.\n",
        encoding="utf-8",
    )
    items: list[dict[str, object]] = [
        {
            "item": {
                "type": "command_execution",
                "command": "pwd && ls",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "code data outputs report",
            }
        }
    ]
    items.extend({"item": {"type": "agent_message", "text": text}} for text in messages)
    items.append(
        {
            "item": {
                "type": "command_execution",
                "command": "python code/analysis.py",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": (
                    "AUC=0.91 Brier=0.12 ECE=0.04; wrote outputs/metrics.csv "
                    "and report/images/result.png"
                ),
            }
        }
    )
    outer = {
        "metadata": {
            "transcript": "\n".join(json.dumps(item) for item in items),
        }
    }
    (tmp_path / "_agent_output.jsonl").write_text(
        json.dumps(outer) + "\n", encoding="utf-8"
    )
    return tmp_path


def _feedback() -> dict[str, object]:
    return {
        "feedback_class": "sanitized_evaluation_feedback_v1",
        "preserve_strengths": [{"dimension": "delivery", "candidate_observation": "Executable evidence exists."}],
        "diagnoses": [
            {
                "dimension": "quantitative_validation",
                "candidate_observation": "The Candidate comparison needs an independent check.",
                "improvement_direction": "Add an independent public-data comparison.",
            },
            {
                "dimension": "coverage",
                "candidate_observation": "The report coverage is narrow.",
                "improvement_direction": "Extend coverage without deleting baseline analyses.",
            },
        ],
    }


def test_selected_trajectory_preserves_scientific_decisions_without_taxonomy(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        source="""
def select(rows):
    return [r for r in rows if r["p_QSO"] >= 0.90 and r["p_WISE_QSO"] >= 0.95]

def fit_logistic(rows, l2=0.001):
    return rows

def confusion_analysis(rows):
    return {"accepted": len(rows)}

def calibration(rows):
    return {"brier": 0.12, "ece": 0.04}

def main():
    selected = select([])
    fit_logistic(selected)
    confusion_analysis(selected)
    calibration(selected)

if __name__ == "__main__":
    main()
""",
        messages=[
            (
                "I define the WISE silver reference as p_WISE_QSO >= 0.95 and keep "
                "p_QSO >= 0.90 as the optical selection before logistic comparison."
            ),
            "The fitted model will retain confusion analysis, Brier score, and ECE calibration.",
        ],
    )

    trace = build_selected_baseline_trajectory(candidate_root=root)
    rendered = json.dumps(trace, ensure_ascii=False)

    assert "p_WISE_QSO >= 0.95" in rendered
    assert "p_QSO >= 0.90" in rendered
    assert "fit_logistic" in rendered
    assert "confusion_analysis" in rendered
    assert "Brier" in rendered
    assert "pwd && ls" not in rendered
    assert admit_selected_baseline_trajectory(trace, candidate_root=root)["status"] == "ADMITTED"


def test_selected_trajectory_keeps_real_pdf_extraction_and_candidate_values(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path,
        source='''
def pdf_literal_to_text(path):
    return open(path, "rb").read().decode("latin1", errors="ignore")

def candidate_table():
    return {"BNE": {"score": 0.853}, "DNE": {"score": 0.533}}

def main():
    documents = [pdf_literal_to_text(f"related_work/paper_{i:03d}.pdf") for i in range(4)]
    return documents, candidate_table()
''',
        messages=[
            "I will extract text from all four supplied PDFs before literature synthesis.",
            "The baseline candidate ranking is BNE 0.853 ahead of DNE 0.533.",
        ],
    )

    trace = build_selected_baseline_trajectory(candidate_root=root)
    rendered = "\n".join(item["text"] for item in trace["selected_trajectory_excerpts"])

    assert "pdf_literal_to_text" in rendered
    assert "range(4)" in rendered
    assert '"BNE": {"score": 0.853}' in rendered
    assert "BNE 0.853 ahead of DNE 0.533" in rendered


def test_context_keeps_all_feedback_and_only_short_objective(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        source="def analyze_threshold(x=0.90):\n    return x\n\ndef main():\n    analyze_threshold()\n",
        messages=["I chose threshold 0.90 because it defines the baseline selection."],
    )
    trace = build_selected_baseline_trajectory(candidate_root=root)
    context = build_minimal_interference_context(trace=trace, sanitized_feedback=_feedback())
    rendered = json.dumps(context, ensure_ascii=False)

    assert context["schema_version"] == MINIMAL_CONTEXT_SCHEMA
    assert len(context["sanitized_evaluator_feedback"]["diagnoses"]) == 2
    assert "threshold 0.90" in rendered
    assert "Do not silently replace baseline scientific definitions" in rendered
    assert "achievement" not in rendered.casefold()


def test_projection_and_quality_observations_do_not_block_registered_artifacts(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path / "candidate",
        source="def analyze_threshold(x=0.90):\n    return x\n\ndef main():\n    analyze_threshold()\n",
        messages=["I chose threshold 0.90 for the baseline model."],
    )

    class FrozenProjector:
        def execute(self, request, idempotency_key):
            del request, idempotency_key
            return {
                "task_id": "Astronomy_004",
                "reflector_sanitized_feedback": _feedback(),
                "reflector_feedback_sha256": "a" * 64,
                "content_sha256": "b" * 64,
            }

    port = MinimalInterferenceFeedbackProjectionPort(
        root=tmp_path / "projection", frozen_projector=FrozenProjector()
    )
    result = port.execute(
        {"task_id": "Astronomy_004", "candidate": {"candidate_output_root": str(root)}},
        "r5",
    )
    trace = result["selected_baseline_trajectory"]
    report = assess_minimal_interference_artifacts(
        trace=trace,
        sanitized_feedback=_feedback(),
        artifact_texts={
            "text_memory": "Remember the baseline threshold model.",
            "skill_bundle": "Rebuild it and add an independent public-data comparison.",
            "agent_system": "Preserve useful baseline work.",
        },
        content_admission_findings=["POSSIBLE_LITERAL_OVERLAP"],
        provenance_violations=["POSSIBLE_LINEAGE_WARNING"],
    )

    assert result["schema_version"].endswith(".r5")
    assert report["status"] == "PASS"
    assert report["dispatch_authority"]["pass"] is True
    assert report["diagnostics"]["warnings"]
