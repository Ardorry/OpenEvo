"""Sealed-output-only, paper-compatible ChemCrow EvaluatorGPT protocol.

The historical notebook answers handled here are deliberately outside the
sanitized benchmark manifest.  This module must never be imported by the
Candidate, Reflector, or evolution-evaluator execution paths.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem
from .three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    LEGACY_THREE_ARTIFACT_PROTOCOL_LABEL,
    ThreeArtifactPairResult,
    three_artifact_protocol_label,
)

PAPER_EVALUATOR_PROTOCOL = "CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2"
PAPER_EVALUATOR_PROMPT_CANDIDATE = "PAPER_MINIMAL"
PAPER_EVALUATOR_PROMPT_SHA256 = "aa32cbc3190a53512bb121b167bcd68fb10cd92a5f07d30f68edcdda27ea0767"
PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256 = (
    "a901f03a5e5a58c7d64b7e5dea36c5f6c0d1634f0432f024f69d1394240983e2"
)
PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256 = (
    "148ddc2bd38dc3c49a01d01cd9cc99035cb0d70c085c7c2f3e738b378c813de0"
)
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
PAPER_EVALUATOR_PLAN_SCHEMA = "chemcrow_paper_evaluator_plan_v2"
_MAX_SEALED_AUTHORITY_BYTES = 4 * 1024 * 1024
SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS = frozenset(
    {
        LEGACY_THREE_ARTIFACT_PROTOCOL_LABEL,
        CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    }
)

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FROZEN_PAPER_TASK_IDS = tuple(
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


class PaperEvaluationResult(BaseModel):
    """Strict durable result envelope for one production paper call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chemcrow_paper_evaluator_result_v1"]
    call_id: str
    task_id: str
    comparison: Literal["historical_control", "baseline", "evolved"]
    student_a_system: Literal["historical_chemcrow", "openevo_baseline", "openevo_evolved"]
    student_b_system: Literal["historical_gpt4"]
    prompt_sha256: str
    assessment: DualStudentAssessment
    assessment_sha256: str
    openrouter_receipt_sha256: str
    core_terminal_payload_sha256: str
    execution_route: str
    reflector_access: Literal[False]
    evolution_feedback_access: Literal[False]
    provisional_llm_judged: Literal[True]

    @model_validator(mode="after")
    def _bind_assessment(self) -> PaperEvaluationResult:
        if self.assessment_sha256 != canonical_sha256(self.assessment.model_dump(mode="json")):
            raise ValueError("paper result assessment hash differs")
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


@dataclass(frozen=True)
class CompletedRunAuditBinding:
    """Content and source-run identity pinned into a production paper plan."""

    completed_run_audit_sha256: str
    aggregate_sha256: str
    experiment_id: str
    artifact_protocol: str


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
        observed = {
            key: float(value)
            for key, value in pattern.findall(_cell_stream_text(teacher_cells[0]))
        }
        if set(observed) != {"1", "2"}:
            raise ValueError(f"historical teacher grades are incomplete for {task_id}")
        grades[task_id] = HistoricalEvaluatorGrades(
            task_id=task_id,
            chemcrow_grade=observed["1"],
            gpt4_grade=observed["2"],
            notebook_sha256=file_sha256(notebook_path),
        )
    return grades


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized path without resolving symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _opened_path_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _stable_file_snapshot(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular_file_no_follow(path: Path, *, label: str) -> bytes:
    """Read one authority file while binding every pathname component to an FD.

    This deliberately avoids ``Path.resolve`` and rejects symlinks in any
    component.  The opened file, its directory chain, and the pathname are
    rechecked after the bounded read so a concurrent rename/replacement cannot
    silently change the authority being validated.
    """

    absolute = _lexical_absolute(path)
    parts = absolute.parts
    if (
        not absolute.is_absolute()
        or len(parts) < 2
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise ValueError(f"{label} path is invalid")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    opened_fds: list[int] = []
    directory_bindings: list[tuple[int, str, int]] = []
    file_descriptor: int | None = None
    try:
        current_directory = os.open(os.sep, directory_flags)
        opened_fds.append(current_directory)
        for component in parts[1:-1]:
            pathname_stat = os.stat(
                component,
                dir_fd=current_directory,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(pathname_stat.st_mode):
                raise ValueError(f"{label} path contains a non-directory or symlink")
            child = os.open(component, directory_flags, dir_fd=current_directory)
            opened_fds.append(child)
            opened_stat = os.fstat(child)
            if _opened_path_identity(pathname_stat) != _opened_path_identity(opened_stat):
                raise ValueError(f"{label} directory identity changed while opening")
            directory_bindings.append((current_directory, component, child))
            current_directory = child

        filename = parts[-1]
        pathname_before = os.stat(
            filename,
            dir_fd=current_directory,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(pathname_before.st_mode) or pathname_before.st_nlink != 1:
            raise ValueError(f"{label} must be a link-count-one regular file")
        if pathname_before.st_size > _MAX_SEALED_AUTHORITY_BYTES:
            raise ValueError(f"{label} exceeds the sealed authority byte limit")
        file_descriptor = os.open(filename, file_flags, dir_fd=current_directory)
        opened_fds.append(file_descriptor)
        opened_before = os.fstat(file_descriptor)
        if _stable_file_snapshot(pathname_before) != _stable_file_snapshot(opened_before):
            raise ValueError(f"{label} pathname identity changed while opening")

        chunks: list[bytes] = []
        remaining = opened_before.st_size
        while remaining:
            chunk = os.read(file_descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError(f"{label} was truncated while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            raise ValueError(f"{label} grew while reading")

        opened_after = os.fstat(file_descriptor)
        pathname_after = os.stat(
            filename,
            dir_fd=current_directory,
            follow_symlinks=False,
        )
        if _stable_file_snapshot(opened_before) != _stable_file_snapshot(
            opened_after
        ) or _stable_file_snapshot(opened_after) != _stable_file_snapshot(pathname_after):
            raise ValueError(f"{label} identity or content changed while reading")
        for parent_fd, component, child_fd in directory_bindings:
            current_path_stat = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if _opened_path_identity(current_path_stat) != _opened_path_identity(
                os.fstat(child_fd)
            ):
                raise ValueError(f"{label} pathname was replaced while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"{label} cannot be read without following symlinks") from exc
    finally:
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_regular_json_no_follow(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    raw = _read_regular_file_no_follow(path, label=label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{label} must contain a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def load_completed_run_audit_binding(
    *,
    run_root: Path,
    experiment_id: str,
    task_ids: list[str],
    completed_run_audit: Path,
    expected_completed_run_audit_sha256: str | None = None,
    expected_aggregate_sha256: str | None = None,
) -> CompletedRunAuditBinding:
    """Validate and bind the exact completed-run audit and sealed aggregate."""

    if tuple(task_ids) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("paper evaluation requires the frozen 14-task ChemCrow order")
    lexical_run_root = _lexical_absolute(run_root)
    lexical_audit = _lexical_absolute(completed_run_audit)
    if lexical_audit != lexical_run_root / "completed_run.audit.json":
        raise ValueError("configured completed-run audit path differs from the source run root")
    audit, audit_sha256 = _read_regular_json_no_follow(
        lexical_audit,
        label="completed-run audit",
    )
    if expected_completed_run_audit_sha256 is not None:
        if not _is_sha256(expected_completed_run_audit_sha256):
            raise ValueError("plan completed-run audit SHA256 is missing or invalid")
        if audit_sha256 != expected_completed_run_audit_sha256:
            raise ValueError("completed-run audit SHA256 differs from the frozen plan")

    expected_reflector_count = len(task_ids) * 3
    expected_role_count = len(task_ids)
    if (
        audit.get("status") != "PASS"
        or audit.get("experiment_id") != experiment_id
        or audit.get("task_ids") != task_ids
        or audit.get("task_count") != len(task_ids)
        or audit.get("artifact_protocol") not in SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS
        or audit.get("unique_artifact_count") != expected_reflector_count
        or audit.get("artifact_registration_count") != expected_reflector_count
        or audit.get("unique_reflector_job_count") != expected_reflector_count
        or audit.get("unique_reflector_run_count") != expected_reflector_count
        or audit.get("independent_reflector_job_count") != expected_reflector_count
        or audit.get("independent_reflector_run_count") != expected_reflector_count
        or audit.get("memory_reflector_job_count") != expected_role_count
        or audit.get("skill_reflector_job_count") != expected_role_count
        or audit.get("agent_system_reflector_job_count") != expected_role_count
        or audit.get("sibling_isolation_evidence_count") != expected_reflector_count
        or audit.get("core_evolved_injection_receipt_count") != len(task_ids)
        or audit.get("reset_receipt_count") != len(task_ids)
        or audit.get("mock_or_fixture_observations") != 0
    ):
        raise ValueError(
            "completed-run audit does not bind the full three-artifact frozen task inventory"
        )

    aggregate_sha256 = audit.get("aggregate_sha256")
    if not _is_sha256(aggregate_sha256):
        raise ValueError("completed-run audit aggregate SHA256 is missing or invalid")
    if expected_aggregate_sha256 is not None:
        if not _is_sha256(expected_aggregate_sha256):
            raise ValueError("plan aggregate SHA256 is missing or invalid")
        if aggregate_sha256 != expected_aggregate_sha256:
            raise ValueError("completed-run audit aggregate SHA256 differs from the frozen plan")
    aggregate, observed_aggregate_sha256 = _read_regular_json_no_follow(
        lexical_run_root / "aggregate.json",
        label="sealed aggregate",
    )
    if observed_aggregate_sha256 != aggregate_sha256:
        raise ValueError("sealed aggregate SHA256 differs from the completed-run audit")
    if (
        aggregate.get("task_count") != len(task_ids)
        or aggregate.get("artifact_count") != expected_reflector_count
        or aggregate.get("artifact_protocol") != audit["artifact_protocol"]
    ):
        raise ValueError("sealed aggregate does not cover the frozen three-artifact inventory")
    return CompletedRunAuditBinding(
        completed_run_audit_sha256=audit_sha256,
        aggregate_sha256=aggregate_sha256,
        experiment_id=experiment_id,
        artifact_protocol=str(audit["artifact_protocol"]),
    )


def assert_sealed_run_ready(
    *,
    run_root: Path,
    experiment_id: str,
    task_ids: list[str],
    completed_run_audit: Path,
    expected_completed_run_audit_sha256: str | None = None,
) -> dict[str, ThreeArtifactPairResult]:
    """Fail closed unless all 14 task-local pairs and the aggregate are sealed."""
    if tuple(task_ids) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("paper evaluation requires the frozen 14-task ChemCrow order")
    for marker in ("STOPPED_BY_USER.json", "INVALIDATED.json"):
        if (run_root / marker).exists():
            raise ValueError(f"paper evaluation refuses run marker: {marker}")
    binding = load_completed_run_audit_binding(
        run_root=run_root,
        experiment_id=experiment_id,
        task_ids=task_ids,
        completed_run_audit=completed_run_audit,
        expected_completed_run_audit_sha256=expected_completed_run_audit_sha256,
    )

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
        pair_protocol = three_artifact_protocol_label(pair.artifact_bundle.protocol)
        if pair_protocol != binding.artifact_protocol:
            raise ValueError(f"pair artifact protocol differs from audit: {task_id}")
        pairs[task_id] = pair
    return pairs


def build_paper_evaluation_plan(
    *,
    tasks: list[TaskItem],
    pairs: dict[str, ThreeArtifactPairResult],
    historical: dict[str, HistoricalAnswers],
    call_id_prefix: str = "paper",
    expected_source_pair_protocol: str | None = None,
    source_completed_run_audit_sha256: str | None = None,
    source_aggregate_sha256: str | None = None,
    source_experiment_id: str | None = None,
) -> dict[str, Any]:
    assert_historical_answers_match_frozen_calibration(historical=historical)
    if [task.task_id for task in tasks] != list(FROZEN_PAPER_TASK_IDS):
        raise ValueError("sanitized task manifest differs from frozen paper order")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", call_id_prefix):
        raise ValueError("paper evaluator call ID prefix is invalid")
    if not _is_sha256(source_completed_run_audit_sha256):
        raise ValueError("paper evaluator completed-run audit SHA256 is missing or invalid")
    if not _is_sha256(source_aggregate_sha256):
        raise ValueError("paper evaluator aggregate SHA256 is missing or invalid")
    if not isinstance(source_experiment_id, str) or not source_experiment_id:
        raise ValueError("paper evaluator source experiment ID is missing")
    if any(not isinstance(pair, ThreeArtifactPairResult) for pair in pairs.values()):
        raise TypeError(
            "paper evaluation requires authoritative three-artifact pair results; "
            "legacy single-artifact pairs are provisional only"
        )
    source_pair_protocols = {
        three_artifact_protocol_label(pair.artifact_bundle.protocol) for pair in pairs.values()
    }
    if len(source_pair_protocols) != 1:
        raise ValueError("paper evaluation requires one homogeneous source pair protocol")
    source_pair_protocol = source_pair_protocols.pop()
    if source_pair_protocol not in SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS:
        raise ValueError("paper evaluation source pair protocol is unsupported")
    if (
        expected_source_pair_protocol is not None
        and expected_source_pair_protocol != source_pair_protocol
    ):
        raise ValueError("paper evaluation source pair protocol differs from config")
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
            call_id = f"{call_id_prefix}-{task.task_id}-{comparison}"
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
    plan = {
        "schema_version": PAPER_EVALUATOR_PLAN_SCHEMA,
        "protocol": PAPER_EVALUATOR_PROTOCOL,
        "prompt_candidate_id": PAPER_EVALUATOR_PROMPT_CANDIDATE,
        "prompt_candidate_sha256": PAPER_EVALUATOR_PROMPT_SHA256,
        "historically_calibrated_compatible_prompt": True,
        "historical_agreement": "HIGH",
        "calibration_results_sha256": PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256,
        "verbatim_historical_prompt_reproduction": False,
        "historical_prompt_availability": "not_recovered_from_authoritative_public_source",
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "reflector_access": False,
        "evolution_feedback_access": False,
        "sealed_output_only": True,
        "source_pair_protocol": source_pair_protocol,
        "source_completed_run_audit_sha256": source_completed_run_audit_sha256,
        "source_aggregate_sha256": source_aggregate_sha256,
        "source_experiment_id": source_experiment_id,
        "call_id_prefix": call_id_prefix,
        "ledger_class": "production",
        "environment_proxy_required": True,
        "automatic_provider_retries": False,
        "failed_call_id_reuse": False,
        "historical_control_reuse_policy": "frozen_hash_match_only",
        "historical_calibration_dataset_sha256": (PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256),
        "contains_historical_student_answers": True,
        "call_count": len(calls),
        "cost_ceiling": ceiling,
        "calls": [call.model_dump(mode="json") for call in calls],
    }
    validate_paper_evaluation_plan(plan)
    return plan


def assert_historical_answers_match_frozen_calibration(
    *, historical: dict[str, HistoricalAnswers]
) -> None:
    """Bind reusable controls to the exact frozen calibration source hashes."""

    records = _frozen_historical_records()
    if set(historical) != set(FROZEN_PAPER_TASK_IDS):
        raise ValueError("historical answer inventory differs from frozen controls")
    for record in records:
        task_id = str(record["repo_task_id"])
        old = historical[task_id]
        if (
            old.notebook_sha256 != record.get("source_notebook_sha256")
            or canonical_sha256(old.chemcrow_answer)
            != record.get("historical_chemcrow_answer_sha256")
            or canonical_sha256(old.gpt4_answer)
            != record.get("historical_no_tools_gpt4_answer_sha256")
        ):
            raise ValueError(f"historical control hash differs: {task_id}")


def validate_paper_evaluation_plan(
    plan: dict[str, Any],
) -> list[PaperEvaluationCall]:
    """Validate the complete frozen 14 x 3 production authority."""

    template = render_compatible_prompt(
        task_prompt="<<TASK>>",
        student_a="<<STUDENT_A>>",
        student_b="<<STUDENT_B>>",
    )
    if canonical_sha256(template) != PAPER_EVALUATOR_PROMPT_SHA256:
        raise ValueError("frozen paper evaluator template hash differs")
    calibration_results = (
        Path(__file__).resolve().parents[2]
        / "reports"
        / "PAPER_EVALUATOR_CALIBRATION_RESULTS.json"
    )
    if file_sha256(calibration_results) != PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256:
        raise ValueError("frozen paper evaluator calibration results hash differs")

    expected_top_level = {
        "schema_version": PAPER_EVALUATOR_PLAN_SCHEMA,
        "protocol": PAPER_EVALUATOR_PROTOCOL,
        "prompt_candidate_id": PAPER_EVALUATOR_PROMPT_CANDIDATE,
        "prompt_candidate_sha256": PAPER_EVALUATOR_PROMPT_SHA256,
        "historically_calibrated_compatible_prompt": True,
        "historical_agreement": "HIGH",
        "calibration_results_sha256": PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256,
        "verbatim_historical_prompt_reproduction": False,
        "historical_prompt_availability": ("not_recovered_from_authoritative_public_source"),
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "reflector_access": False,
        "evolution_feedback_access": False,
        "sealed_output_only": True,
        "ledger_class": "production",
        "environment_proxy_required": True,
        "automatic_provider_retries": False,
        "failed_call_id_reuse": False,
        "historical_control_reuse_policy": "frozen_hash_match_only",
        "historical_calibration_dataset_sha256": (PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256),
        "contains_historical_student_answers": True,
        "call_count": PAPER_EVALUATOR_CALL_COUNT,
        "cost_ceiling": paper_cost_ceiling(),
    }
    expected_keys = set(expected_top_level) | {
        "source_pair_protocol",
        "source_completed_run_audit_sha256",
        "source_aggregate_sha256",
        "source_experiment_id",
        "call_id_prefix",
        "calls",
    }
    if set(plan) != expected_keys:
        raise ValueError("paper evaluator plan schema fields differ")
    if any(plan.get(key) != value for key, value in expected_top_level.items()):
        raise ValueError("paper evaluator plan frozen authority differs")
    if plan.get("source_pair_protocol") not in SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS:
        raise ValueError("paper evaluator source pair protocol is unsupported")
    if not _is_sha256(plan.get("source_completed_run_audit_sha256")):
        raise ValueError("paper evaluator completed-run audit SHA256 is missing or invalid")
    if not _is_sha256(plan.get("source_aggregate_sha256")):
        raise ValueError("paper evaluator aggregate SHA256 is missing or invalid")
    source_experiment_id = plan.get("source_experiment_id")
    if not isinstance(source_experiment_id, str) or not source_experiment_id:
        raise ValueError("paper evaluator source experiment ID is missing")
    call_id_prefix = plan.get("call_id_prefix")
    if not isinstance(call_id_prefix, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,47}", call_id_prefix
    ):
        raise ValueError("paper evaluator call ID prefix is invalid")
    raw_calls = plan.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != PAPER_EVALUATOR_CALL_COUNT:
        raise ValueError("paper evaluator plan must contain exactly 42 calls")
    calls = [PaperEvaluationCall.model_validate(item) for item in raw_calls]
    comparisons = ("historical_control", "baseline", "evolved")
    expected_call_ids = [
        f"{call_id_prefix}-{task_id}-{comparison}"
        for task_id in FROZEN_PAPER_TASK_IDS
        for comparison in comparisons
    ]
    if [call.call_id for call in calls] != expected_call_ids:
        raise ValueError("paper evaluator call inventory or order differs")
    if len({call.call_id for call in calls}) != PAPER_EVALUATOR_CALL_COUNT:
        raise ValueError("paper evaluator call IDs are not unique")

    by_task: dict[str, dict[str, PaperEvaluationCall]] = {}
    for call in calls:
        by_task.setdefault(call.task_id, {})[call.comparison] = call
    if list(by_task) != list(FROZEN_PAPER_TASK_IDS) or any(
        set(group) != set(comparisons) for group in by_task.values()
    ):
        raise ValueError("paper evaluator task/comparison inventory differs")
    systems = {
        "historical_control": "historical_chemcrow",
        "baseline": "openevo_baseline",
        "evolved": "openevo_evolved",
    }
    historical_records = {
        str(record["repo_task_id"]): record for record in _frozen_historical_records()
    }
    for task_id, group in by_task.items():
        historical_record = historical_records[task_id]
        expected_historical_source = historical_record["source_notebook_sha256"]
        expected_chemcrow_answer = historical_record["historical_chemcrow_answer_sha256"]
        expected_gpt4_answer = historical_record["historical_no_tools_gpt4_answer_sha256"]
        if any(
            group[comparison].student_a_system != systems[comparison] for comparison in comparisons
        ):
            raise ValueError(f"paper evaluator student mapping differs: {task_id}")
        if any(
            call.historical_source_sha256 != expected_historical_source
            or call.historical_gpt4_answer_sha256 != expected_gpt4_answer
            for call in group.values()
        ):
            raise ValueError(f"paper evaluator historical binding differs: {task_id}")
        sentinel = "<<CHEMCROW_TARGET_ANSWER_SENTINEL>>"
        prompt_template = render_compatible_prompt(
            task_prompt=str(historical_record["task_text"]),
            student_a=sentinel,
            student_b=str(historical_record["historical_no_tools_gpt4_final_answer"]),
        )
        prompt_prefix, prompt_suffix = prompt_template.split(sentinel, 1)
        for call in group.values():
            if not call.prompt.startswith(prompt_prefix) or not call.prompt.endswith(
                prompt_suffix
            ):
                raise ValueError(f"paper evaluator prompt source framing differs: {call.call_id}")
            target_answer = call.prompt[len(prompt_prefix) : len(call.prompt) - len(prompt_suffix)]
            if canonical_sha256(target_answer) != call.source_output_sha256:
                raise ValueError(f"paper evaluator prompt/source hash differs: {call.call_id}")
        control = group["historical_control"]
        baseline = group["baseline"]
        evolved = group["evolved"]
        if (
            control.source_pair_id != f"historical-notebook:{task_id}"
            or control.source_pair_result_sha256 != control.historical_source_sha256
            or control.source_output_id != f"{task_id}:historical_chemcrow"
            or control.source_output_sha256 != expected_chemcrow_answer
            or baseline.source_pair_id != evolved.source_pair_id
            or baseline.source_pair_id != f"{source_experiment_id}--{task_id}"
            or baseline.source_pair_result_sha256 != evolved.source_pair_result_sha256
            or baseline.source_output_id == evolved.source_output_id
        ):
            raise ValueError(f"paper evaluator sealed source binding differs: {task_id}")
    return calls


def validate_paper_plan_source_audit(
    *,
    plan: dict[str, Any],
    run_root: Path,
    completed_run_audit: Path,
) -> CompletedRunAuditBinding:
    """Re-read the configured source authority before any production dispatch."""

    calls = validate_paper_evaluation_plan(plan)
    task_ids = list(dict.fromkeys(call.task_id for call in calls))
    return load_completed_run_audit_binding(
        run_root=run_root,
        experiment_id=str(plan["source_experiment_id"]),
        task_ids=task_ids,
        completed_run_audit=completed_run_audit,
        expected_completed_run_audit_sha256=str(plan["source_completed_run_audit_sha256"]),
        expected_aggregate_sha256=str(plan["source_aggregate_sha256"]),
    )


def _frozen_historical_records() -> list[dict[str, Any]]:
    dataset_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "paper_evaluator_historical_calibration.json"
    )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if canonical_sha256(dataset) != PAPER_EVALUATOR_HISTORICAL_DATASET_SHA256:
        raise ValueError("frozen historical calibration dataset hash differs")
    records = dataset.get("tasks")
    if not isinstance(records, list) or [
        item.get("repo_task_id") for item in records if isinstance(item, dict)
    ] != list(FROZEN_PAPER_TASK_IDS):
        raise ValueError("frozen historical calibration task inventory differs")
    if any(not isinstance(item, dict) for item in records):
        raise TypeError("frozen historical calibration records are invalid")
    return records


def render_compatible_prompt(*, task_prompt: str, student_a: str, student_b: str) -> str:
    """Render the historically calibrated compatible prompt, never an official-prompt claim."""
    return f"""You are a teacher evaluating two students' answers to the same chemistry task.

For each student, give a grade from 0 to 10 based only on whether the task is addressed and whether the overall thought process is correct. Report strengths, weaknesses, a grade justification, and improvement feedback. Apply the same standard to both students.

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
