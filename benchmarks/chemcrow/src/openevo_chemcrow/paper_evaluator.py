"""Sealed-output-only, paper-compatible ChemCrow EvaluatorGPT protocol.

The historical notebook answers handled here are deliberately outside the
sanitized benchmark manifest.  This module must never be imported by the
Candidate, Reflector, or evolution-evaluator execution paths.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem
from .three_artifact_models import ThreeArtifactPairResult

PAPER_EVALUATOR_PROTOCOL = "CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1"
PAPER_EVALUATOR_MODEL = "openai/gpt-4"
PAPER_EVALUATOR_TEMPERATURE = 0.1
PAPER_EVALUATOR_PROVIDER = "openai"
PAPER_EVALUATOR_DATA_COLLECTION = "allow"
PAPER_EVALUATOR_CONTEXT_TOKENS = 8191
PAPER_EVALUATOR_MAX_OUTPUT_TOKENS = 1200
PAPER_EVALUATOR_INPUT_USD_PER_TOKEN = 0.00003
PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN = 0.00006
PAPER_EVALUATOR_CALL_COUNT = 42
PAPER_EVALUATOR_AUTHORIZATION = "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FROZEN_PAPER_TASK_IDS = tuple(
    f"chemcrow-{number}"
    for number in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "12", "13", "14", "15")
)


class StudentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grade: float = Field(ge=0.0, le=10.0)
    strengths: list[str]
    weaknesses: list[str]
    justification: str
    feedback: list[str]


class DualStudentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_a: StudentAssessment
    student_b: StudentAssessment


class PaperEvaluationCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    task_id: str
    comparison: Literal["historical_control", "baseline", "evolved"]
    student_a_system: Literal["historical_chemcrow", "openevo_baseline", "openevo_evolved"]
    student_b_system: Literal["historical_gpt4"] = "historical_gpt4"
    prompt: str
    prompt_sha256: str
    source_pair_id: str
    source_pair_result_sha256: str
    source_output_id: str
    source_output_sha256: str
    historical_source_sha256: str
    target_answer_sha256: str
    historical_gpt4_answer_sha256: str
    estimated_input_tokens: int
    metric_classification: Literal[
        "paper_control_reconstruction",
        "project_added_openevo_metric",
    ]
    paper_comparable: bool

    @model_validator(mode="after")
    def _bind_prompt_and_sources(self) -> PaperEvaluationCall:
        if self.prompt_sha256 != canonical_sha256(self.prompt):
            raise ValueError("paper evaluator prompt hash differs from prompt bytes")
        if self.source_output_sha256 != self.target_answer_sha256:
            raise ValueError("paper evaluator source output hash differs from target answer")
        if self.comparison == "historical_control":
            if (
                self.metric_classification != "paper_control_reconstruction"
                or self.paper_comparable is not True
            ):
                raise ValueError("historical control classification differs")
        elif (
            self.metric_classification != "project_added_openevo_metric"
            or self.paper_comparable is not False
        ):
            raise ValueError("OpenEvo comparison must remain project-added")
        return self


@dataclass(frozen=True)
class HistoricalAnswers:
    task_id: str
    chemcrow_answer: str
    gpt4_answer: str
    notebook_path: Path
    notebook_sha256: str


@dataclass(frozen=True)
class HistoricalEvaluatorGrades:
    task_id: str
    chemcrow_grade: float
    gpt4_grade: float
    notebook_sha256: str


def paper_cost_ceiling() -> dict[str, Any]:
    """Return the exact list-price ceiling imposed by model context and output cap."""
    max_input_tokens = PAPER_EVALUATOR_CONTEXT_TOKENS - PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
    per_call = (
        max_input_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + PAPER_EVALUATOR_MAX_OUTPUT_TOKENS * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    total = per_call * PAPER_EVALUATOR_CALL_COUNT
    return {
        "model_context_tokens": PAPER_EVALUATOR_CONTEXT_TOKENS,
        "max_output_tokens_per_call": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "max_input_tokens_per_call": max_input_tokens,
        "input_usd_per_token": PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
        "output_usd_per_token": PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
        "call_count": PAPER_EVALUATOR_CALL_COUNT,
        "list_price_ceiling_usd_per_call": round(per_call, 8),
        "list_price_ceiling_usd_total": round(total, 8),
    }


def extract_historical_answers(
    *, runs_root: Path, task_ids: tuple[str, ...] = FROZEN_PAPER_TASK_IDS
) -> dict[str, HistoricalAnswers]:
    """Extract only the two historical student answers, never teacher output."""
    answers: dict[str, HistoricalAnswers] = {}
    for task_id in task_ids:
        number = task_id.rsplit("-", 1)[-1]
        matches = sorted((runs_root / "tasks").glob(f"{number}_*.ipynb"))
        if len(matches) != 1:
            raise ValueError(f"historical notebook authority is not unique for {task_id}")
        notebook_path = matches[0]
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        cells = notebook.get("cells")
        if not isinstance(cells, list):
            raise TypeError(f"invalid notebook cells: {notebook_path}")
        chemcrow_answer = _historical_student_answer(cells, "result_tools")
        gpt4_answer = _historical_student_answer(cells, "result_notools")
        if not chemcrow_answer or not gpt4_answer:
            raise ValueError(f"empty historical student answer: {notebook_path}")
        answers[task_id] = HistoricalAnswers(
            task_id=task_id,
            chemcrow_answer=chemcrow_answer,
            gpt4_answer=gpt4_answer,
            notebook_path=notebook_path,
            notebook_sha256=file_sha256(notebook_path),
        )
    return answers


def extract_historical_evaluator_grades(
    *, runs_root: Path, task_ids: tuple[str, ...] = FROZEN_PAPER_TASK_IDS
) -> dict[str, HistoricalEvaluatorGrades]:
    """Read old grades only for post-judge drift analysis, never as judge input."""
    grades: dict[str, HistoricalEvaluatorGrades] = {}
    pattern = re.compile(
        r"Student\s*([12])(?:'s)?\s+Grade\s*:\s*([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE,
    )
    for task_id in task_ids:
        number = task_id.rsplit("-", 1)[-1]
        matches = sorted((runs_root / "tasks").glob(f"{number}_*.ipynb"))
        if len(matches) != 1:
            raise ValueError(f"historical notebook authority is not unique for {task_id}")
        notebook_path = matches[0]
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        teacher_cells = [
            cell
            for cell in notebook.get("cells", [])
            if isinstance(cell, dict) and "Evaluator(" in "".join(cell.get("source", []))
        ]
        if len(teacher_cells) != 1:
            raise ValueError(f"historical teacher cell is not unique for {task_id}")
        observed = {key: float(value) for key, value in pattern.findall(_cell_stream_text(teacher_cells[0]))}
        if set(observed) != {"1", "2"}:
            raise ValueError(f"historical teacher grades are incomplete for {task_id}")
        grades[task_id] = HistoricalEvaluatorGrades(
            task_id=task_id,
            chemcrow_grade=observed["1"],
            gpt4_grade=observed["2"],
            notebook_sha256=file_sha256(notebook_path),
        )
    return grades


def assert_sealed_run_ready(
    *,
    run_root: Path,
    experiment_id: str,
    task_ids: list[str],
    completed_run_audit: Path,
) -> dict[str, ThreeArtifactPairResult]:
    """Fail closed unless all 14 task-local pairs and the aggregate are sealed."""
    if tuple(task_ids) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("paper evaluation requires the frozen 14-task ChemCrow order")
    for marker in ("STOPPED_BY_USER.json", "INVALIDATED.json"):
        if (run_root / marker).exists():
            raise ValueError(f"paper evaluation refuses run marker: {marker}")
    audit = json.loads(completed_run_audit.read_text(encoding="utf-8"))
    if (
        audit.get("status") != "PASS"
        or audit.get("experiment_id") != experiment_id
        or audit.get("task_ids") != task_ids
        or audit.get("task_count") != len(task_ids)
        or audit.get("artifact_protocol") != "chemcrow-three-isolated-artifacts-v1"
        or audit.get("unique_artifact_count") != len(task_ids) * 3
        or audit.get("independent_reflector_job_count") != len(task_ids) * 3
        or audit.get("core_evolved_injection_receipt_count") != len(task_ids)
        or audit.get("mock_or_fixture_observations") != 0
    ):
        raise ValueError(
            "completed-run audit does not bind the full three-artifact frozen task inventory"
        )
    aggregate_path = run_root / "aggregate.json"
    if not aggregate_path.is_file():
        raise ValueError("sealed aggregate.json is missing")
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if aggregate.get("task_count") != len(task_ids):
        raise ValueError("aggregate does not cover the frozen 14-task inventory")

    pairs: dict[str, ThreeArtifactPairResult] = {}
    for task_id in task_ids:
        item_root = run_root / f"{experiment_id}--{task_id}"
        required = (
            "baseline.trajectory.json",
            "evolved.trajectory.json",
            "artifacts.receipt.json",
            "injection.receipt.summary.json",
            "pair.result.json",
            "reset.receipt.json",
        )
        missing = [name for name in required if not (item_root / name).is_file()]
        if missing:
            raise ValueError(f"unsealed task {task_id}: missing {missing}")
        pair = ThreeArtifactPairResult.model_validate_json(
            (item_root / "pair.result.json").read_text(encoding="utf-8")
        )
        if pair.task_id != task_id or pair.pair_id != f"{experiment_id}--{task_id}":
            raise ValueError(f"pair task authority mismatch: {task_id}")
        pairs[task_id] = pair
    return pairs


def build_paper_evaluation_plan(
    *,
    tasks: list[TaskItem],
    pairs: dict[str, ThreeArtifactPairResult],
    historical: dict[str, HistoricalAnswers],
) -> dict[str, Any]:
    if [task.task_id for task in tasks] != list(FROZEN_PAPER_TASK_IDS):
        raise ValueError("sanitized task manifest differs from frozen paper order")
    calls: list[PaperEvaluationCall] = []
    for task in tasks:
        pair = pairs[task.task_id]
        if not isinstance(pair, ThreeArtifactPairResult):
            raise TypeError(
                "paper evaluation requires authoritative three-artifact pair results; "
                "legacy single-artifact pairs are provisional only"
            )
        old = historical[task.task_id]
        variants = (
            (
                "historical_control",
                "historical_chemcrow",
                old.chemcrow_answer,
                f"historical-notebook:{task.task_id}",
                old.notebook_sha256,
                f"{task.task_id}:historical_chemcrow",
            ),
            (
                "baseline",
                "openevo_baseline",
                pair.baseline.answer,
                pair.pair_id,
                canonical_sha256(pair.model_dump(mode="json")),
                pair.baseline.run_id,
            ),
            (
                "evolved",
                "openevo_evolved",
                pair.evolved.answer,
                pair.pair_id,
                canonical_sha256(pair.model_dump(mode="json")),
                pair.evolved.run_id,
            ),
        )
        for (
            comparison,
            student_a_system,
            answer_a,
            source_pair_id,
            source_pair_result_sha256,
            source_output_id,
        ) in variants:
            prompt = render_compatible_prompt(
                task_prompt=task.prompt,
                student_a=answer_a,
                student_b=old.gpt4_answer,
            )
            call_id = f"paper-{task.task_id}-{comparison}"
            estimated = estimate_chat_input_tokens(prompt)
            if estimated > paper_cost_ceiling()["max_input_tokens_per_call"]:
                raise ValueError(
                    f"paper prompt exceeds frozen input budget: {call_id} ({estimated} tokens)"
                )
            calls.append(
                PaperEvaluationCall(
                    call_id=call_id,
                    task_id=task.task_id,
                    comparison=comparison,
                    student_a_system=student_a_system,
                    prompt=prompt,
                    prompt_sha256=canonical_sha256(prompt),
                    source_pair_id=source_pair_id,
                    source_pair_result_sha256=source_pair_result_sha256,
                    source_output_id=source_output_id,
                    source_output_sha256=canonical_sha256(answer_a),
                    historical_source_sha256=old.notebook_sha256,
                    target_answer_sha256=canonical_sha256(answer_a),
                    historical_gpt4_answer_sha256=canonical_sha256(old.gpt4_answer),
                    estimated_input_tokens=estimated,
                    metric_classification=(
                        "paper_control_reconstruction"
                        if comparison == "historical_control"
                        else "project_added_openevo_metric"
                    ),
                    paper_comparable=comparison == "historical_control",
                )
            )
    if len(calls) != PAPER_EVALUATOR_CALL_COUNT:
        raise AssertionError("paper evaluation call inventory must contain exactly 42 calls")
    ceiling = paper_cost_ceiling()
    return {
        "schema_version": "chemcrow_paper_evaluator_plan_v1",
        "protocol": PAPER_EVALUATOR_PROTOCOL,
        "verbatim_historical_prompt_reproduction": False,
        "historical_prompt_availability": "not_published_in_public_source",
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "reflector_access": False,
        "evolution_feedback_access": False,
        "sealed_output_only": True,
        "source_pair_protocol": "chemcrow-three-isolated-artifacts-v1",
        "contains_historical_student_answers": True,
        "call_count": len(calls),
        "cost_ceiling": ceiling,
        "calls": [call.model_dump(mode="json") for call in calls],
    }


def render_compatible_prompt(*, task_prompt: str, student_a: str, student_b: str) -> str:
    """Render the recoverable ChemCrow evaluator semantics, not a claimed verbatim prompt."""
    return f"""You are an expert chemistry teacher evaluating two students' answers.

This is a compatibility reconstruction of ChemCrow EvaluatorGPT. Grade each student from 0 to 10, including decimals when useful. Base the grade only on: (1) whether the given task is addressed, and (2) whether the overall chemistry thought process is correct. Identify concise strengths and weaknesses, justify the grade, and give actionable feedback. Apply the same standard to both students. Do not infer a hidden reference answer and do not reward verbosity by itself.

Return one JSON object only, with exactly this structure:
{{"student_a":{{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]}},"student_b":{{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]}}}}

TASK:
{task_prompt}

STUDENT A ANSWER:
{student_a}

STUDENT B ANSWER:
{student_b}
"""


def estimate_chat_input_tokens(prompt: str) -> int:
    """Estimate the exact serialized two-message Chat Completions token count.

    The provider's final billing receipt remains authoritative.  This local
    count is used only as a fail-closed context preflight.
    """
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - dependency check is explicit
        raise RuntimeError("tiktoken is required for paper evaluator preflight") from exc
    encoding = tiktoken.get_encoding("cl100k_base")
    system = "ChemCrow sealed-output paper evaluator. Return strict JSON only."
    messages = (("system", system), ("user", prompt))
    # OpenAI's cl100k Chat Completions framing: 3 tokens/message plus 3 reply tokens.
    return 3 + sum(
        3 + len(encoding.encode(role)) + len(encoding.encode(content))
        for role, content in messages
    )


def validate_assessment_text(text: str) -> DualStudentAssessment:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        raise ValueError("paper evaluator returned a Markdown fence instead of strict JSON")
    return DualStudentAssessment.model_validate_json(cleaned)


def _historical_student_answer(cells: list[Any], variable: str) -> str:
    """Resolve the last student-variable assignment before the teacher cell.

    Task 12 intentionally overwrites the no-tools answer with a safety refusal;
    taking the earlier printed output would silently judge the wrong student.
    Only literal strings and captured output from the known agent-call cell are
    accepted; arbitrary notebook code is never executed.
    """
    teacher_indexes = [
        index
        for index, cell in enumerate(cells)
        if isinstance(cell, dict) and "Evaluator(" in "".join(cell.get("source", []))
    ]
    if len(teacher_indexes) != 1:
        raise ValueError("historical teacher cell is not unique")
    assignments: list[tuple[dict[str, Any], ast.AST]] = []
    for cell in cells[: teacher_indexes[0]]:
        if not isinstance(cell, dict):
            continue
        source = "".join(cell.get("source", []))
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ValueError("historical notebook source is not valid Python") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == variable for target in targets):
                assignments.append((cell, node.value))
    if not assignments:
        raise ValueError(f"historical student assignment is missing: {variable}")
    cell, value = assignments[-1]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return _clean_text(value.value)
    if not isinstance(value, ast.Call):
        raise TypeError(f"unsupported historical student assignment: {variable}")
    output = _cell_stream_text(cell)
    return _extract_chemcrow_output(output) if variable == "result_tools" else _clean_text(output)


def _cell_stream_text(cell: dict[str, Any]) -> str:
    parts: list[str] = []
    for output in cell.get("outputs", []):
        if isinstance(output, dict) and output.get("output_type") == "stream":
            parts.append("".join(output.get("text", [])))
    return "\n".join(parts)


def _extract_chemcrow_output(text: str) -> str:
    cleaned = _clean_text(text)
    marker = "ChemCrow output:"
    if marker not in cleaned:
        raise ValueError("historical ChemCrow final-answer marker is missing")
    return cleaned.rsplit(marker, 1)[-1].strip()


def _clean_text(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text).strip()
