"""Historical calibration for the ChemCrow EvaluatorGPT-compatible judge.

This module is post-hoc and benchmark-only.  It must never be imported by the
Candidate, Reflector, or internal evolution-evaluator paths.  Official grades
are kept outside every model request and are joined only during local metric
calculation.
"""

from __future__ import annotations

import ast
import json
import math
import os
import random
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import canonical_sha256, file_sha256
from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    DualStudentAssessment,
    estimate_chat_input_tokens,
    render_compatible_prompt,
)
from .runtime import _message_content, _poll_rollout_until_terminal

CALIBRATION_AUTHORIZATION = "I_AUTHORIZE_CHEMCROW_EVALUATOR_HISTORICAL_CALIBRATION"
CALIBRATION_MAX_USD = 15.0
CALIBRATION_MAX_INITIAL_CALLS = 42
CALIBRATION_MAX_REPEATABILITY_CALLS = 6
CALIBRATION_MAX_CALLS = 48
CALIBRATION_PROTOCOL = "CHEMCROW_EVALUATORGPT_HISTORICAL_CALIBRATION_V1"
CALIBRATION_PLAN_SCHEMA = "chemcrow_paper_calibration_plan_v1"
CALIBRATION_RESULTS_SCHEMA = "chemcrow_paper_calibration_results_v1"
CALIBRATION_CALL_PREFIX = "paper-chemcrow-cal-v1-"
CALIBRATION_RUNTIME_GATEWAY_BASE_URL = "http://host.docker.internal:8210/v1"
CALIBRATION_CORE_ROUTE = (
    "paper_calibration_harness_dedicated_openevo_rollout_gateway_openrouter_shim_v1"
)

CHEMCROW_RUNS_REPOSITORY = "https://github.com/ur-whitelab/chemcrow-runs"
CHEMCROW_RUNS_COMMIT = "500104ed9a5d479a8dc4128afc463625ade5a409"
CHEMCROW_RUNS_V1_COMMIT = "1e098b171ac34b9dcf95a836b506b6cd289ce113"
CHEMCROW_RUNS_ZENODO_DOI = "10.5281/zenodo.10884645"
CHEMCROW_RUNS_ZENODO_ARCHIVE_SHA256 = (
    "806dcb13314a43ee07a66c2751ab5fc04bc4df4b67a6588023e55d7539ed567b"
)
NATURE_SUPPLEMENT_SHA256 = "ac14f6ee5d5c00736eeb8d4bf4e25bbe8311763cc7b5935d5132f36d07eec02b"
NATURE_SOURCE_DATA_SHA256 = "9e3383177c975662b7e76e54f490a811bda690cb2b869ca47ad24439f5367b8d"
ARXIV_SOURCE_SHA256 = "21c607b0c71631e362d318e2424cac73dacb38fd528e463f02a9f030bef5ed23"

BOOTSTRAP_SEED = 20260826
BOOTSTRAP_SAMPLES = 10_000
REPEATABILITY_TASK_IDS = ("chemcrow-02", "chemcrow-06", "chemcrow-12")

PROMPT_CANDIDATE_IDS = ("CURRENT_V1", "PAPER_MINIMAL", "PAPER_OUTPUT_FAITHFUL")
SELECTION_RULE = (
    "highest_pairwise_preference_agreement",
    "lowest_delta_mae",
    "lowest_absolute_score_mae",
    "highest_spearman_correlation",
    "lowest_parse_schema_failure_rate",
    "simplest_closest_to_published_paper_semantics",
)
SIMPLICITY_TIE_ORDER = ("PAPER_MINIMAL", "PAPER_OUTPUT_FAITHFUL", "CURRENT_V1")

_CURRENT_V1_TEMPLATE = """You are an expert chemistry teacher evaluating two students' answers.

This is a compatibility reconstruction of ChemCrow EvaluatorGPT. Grade each student from 0 to 10, including decimals when useful. Base the grade only on: (1) whether the given task is addressed, and (2) whether the overall chemistry thought process is correct. Identify concise strengths and weaknesses, justify the grade, and give actionable feedback. Apply the same standard to both students. Do not infer a hidden reference answer and do not reward verbosity by itself.

Return one JSON object only, with exactly this structure:
{"student_a":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]},"student_b":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]}}

TASK:
<<TASK>>

STUDENT A ANSWER:
<<STUDENT_A>>

STUDENT B ANSWER:
<<STUDENT_B>>
"""

_PAPER_MINIMAL_TEMPLATE = """You are a teacher evaluating two students' answers to the same chemistry task.

For each student, give a grade from 0 to 10 based only on whether the task is addressed and whether the overall thought process is correct. Report strengths, weaknesses, a grade justification, and improvement feedback. Apply the same standard to both students.

Return one JSON object only, with exactly this structure:
{"student_a":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]},"student_b":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]}}

TASK:
<<TASK>>

STUDENT A ANSWER:
<<STUDENT_A>>

STUDENT B ANSWER:
<<STUDENT_B>>
"""

_PAPER_OUTPUT_FAITHFUL_TEMPLATE = """You are a teacher evaluating two students' answers to the same chemistry task.

For each student, give a grade from 0 to 10 based only on whether the task is addressed and whether the overall thought process is correct. Evaluate Student 1 (Student A) and then Student 2 (Student B). For each student report, in this order: grade, strengths, weaknesses, grade justification, and improvement feedback. Apply the same standard to both students.

The historical report headings are represented by the following strict JSON transport schema. Return one JSON object only, with exactly this structure:
{"student_a":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]},"student_b":{"grade":0,"strengths":["..."],"weaknesses":["..."],"justification":"...","feedback":["..."]}}

TASK:
<<TASK>>

STUDENT A ANSWER (STUDENT 1):
<<STUDENT_A>>

STUDENT B ANSWER (STUDENT 2):
<<STUDENT_B>>
"""

_CANDIDATE_TEMPLATES = {
    "CURRENT_V1": _CURRENT_V1_TEMPLATE,
    "PAPER_MINIMAL": _PAPER_MINIMAL_TEMPLATE,
    "PAPER_OUTPUT_FAITHFUL": _PAPER_OUTPUT_FAITHFUL_TEMPLATE,
}

# Figure 4c in the official Source Data stores the 14 EvaluatorGPT values in
# repository order 01-10,12-15 (the unscored display task 11 is absent).
_SUPPLEMENT_EVALUATOR_GRADES = {
    task_id: {"student_a": a, "student_b": b}
    for task_id, a, b in zip(
        FROZEN_PAPER_TASK_IDS,
        (6, 7, 8, 8, 7, 9, 7, 7, 9, 7, 8, 7, 6, 7),
        (8, 9, 9, 9, 8, 10, 9, 9, 9.5, 9, 7, 8, 9, 9),
        strict=True,
    )
}

_HISTORICAL_ASSESSMENT = re.compile(
    r"Student\s*([12])(?:'s)?\s+Grade\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*\n"
    r"Strengths\s*:\s*(.*?)\s*\n"
    r"Weaknesses\s*:\s*(.*?)\s*\n"
    r"Grade\s+Justification\s*:\s*(.*?)"
    r"(?=\n\s*Student\s*[12](?:'s)?\s+Grade\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class CalibrationCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    task_id: str
    prompt_candidate_id: Literal["CURRENT_V1", "PAPER_MINIMAL", "PAPER_OUTPUT_FAITHFUL"]
    prompt_candidate_sha256: str
    prompt: str
    prompt_sha256: str
    input_sha256: str
    estimated_input_tokens: int = Field(gt=0)
    phase: Literal["initial", "repeatability"]
    repetition: int = Field(ge=0, le=2)

    @model_validator(mode="after")
    def _bind_call(self) -> CalibrationCall:
        if not self.call_id.startswith(CALIBRATION_CALL_PREFIX):
            raise ValueError("calibration call ID has the wrong namespace")
        if not all(character.isalnum() or character in "-_" for character in self.call_id):
            raise ValueError("calibration call ID contains an invalid character")
        if self.prompt_sha256 != canonical_sha256(self.prompt):
            raise ValueError("calibration prompt hash differs from prompt bytes")
        if self.prompt_candidate_sha256 != canonical_sha256(
            _CANDIDATE_TEMPLATES[self.prompt_candidate_id]
        ):
            raise ValueError("calibration prompt candidate hash differs")
        return self


class SelectedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str
    candidate_id: Literal["CURRENT_V1", "PAPER_MINIMAL", "PAPER_OUTPUT_FAITHFUL"]
    instruction_template: str
    prompt_sha256: str
    exact_prompt_recovered: bool

    @model_validator(mode="after")
    def _bind_hash(self) -> SelectedPrompt:
        if self.prompt_sha256 != canonical_sha256(self.instruction_template):
            raise ValueError("selected prompt hash differs from prompt text")
        if self.instruction_template != _CANDIDATE_TEMPLATES[self.candidate_id]:
            raise ValueError("selected prompt text differs from frozen candidate")
        if self.exact_prompt_recovered is not False:
            raise ValueError("compatible calibration cannot claim exact prompt recovery")
        return self


@dataclass(frozen=True)
class CandidateOutput:
    task_id: str
    candidate_id: str
    assessment: dict[str, Any] | None
    parse_valid: bool
    schema_valid: bool
    provider_valid: bool = True


@dataclass(frozen=True)
class _ResolvedNotebookValue:
    text: str
    variable_name: str
    cell_index: int
    cell_id: str | None
    cell_execution_count: int | None
    cell_source_sha256: str
    cell_output_sha256: str | None
    resolution: str

    def source_record(self) -> dict[str, Any]:
        return {
            "variable_name": self.variable_name,
            "cell_index": self.cell_index,
            "cell_id": self.cell_id,
            "cell_execution_count": self.cell_execution_count,
            "cell_source_sha256": self.cell_source_sha256,
            "cell_output_sha256": self.cell_output_sha256,
            "resolution": self.resolution,
        }


def parse_historical_assessment(text: str) -> dict[str, Any] | None:
    """Parse the released four-line student reports without inventing fields."""
    cleaned = _clean_text(text)
    if not cleaned:
        return None
    matches = _HISTORICAL_ASSESSMENT.findall(cleaned)
    if not matches:
        return None
    observed: dict[str, dict[str, Any]] = {}
    for number, grade, strengths, weaknesses, justification in matches:
        observed[number] = {
            "grade": float(grade),
            "strengths": strengths.strip(),
            "weaknesses": weaknesses.strip(),
            "justification": justification.strip(),
            "improvement_feedback_available": False,
        }
    if set(observed) != {"1", "2"}:
        raise ValueError("historical evaluator output has an incomplete student pair")
    return {"student_a": observed["1"], "student_b": observed["2"]}


def extract_historical_calibration_dataset(
    *, runs_root: Path, task_ids: tuple[str, ...] = FROZEN_PAPER_TASK_IDS
) -> dict[str, Any]:
    """Trace the actual notebook values passed to ``teacher.run``."""
    records: list[dict[str, Any]] = []
    for task_id in task_ids:
        number = task_id.rsplit("-", 1)[-1]
        matches = sorted((runs_root / "tasks").glob(f"{number}_*.ipynb"))
        if len(matches) != 1:
            raise ValueError(f"historical notebook authority is not unique for {task_id}")
        path = matches[0]
        notebook = json.loads(path.read_text(encoding="utf-8"))
        cells = notebook.get("cells")
        if not isinstance(cells, list):
            raise TypeError(f"invalid notebook cells: {path}")
        teacher_index, task_variable = _teacher_call(cells)
        task = _resolve_notebook_value(
            cells, variable=task_variable, before_index=teacher_index, role="task"
        )
        student_a = _resolve_notebook_value(
            cells, variable="result_tools", before_index=teacher_index, role="student_a"
        )
        student_b = _resolve_notebook_value(
            cells, variable="result_notools", before_index=teacher_index, role="student_b"
        )
        teacher_cell = cells[teacher_index]
        if not isinstance(teacher_cell, dict):
            raise TypeError("historical teacher cell must be an object")
        raw_assessment = _cell_stream_text(teacher_cell)
        parsed = parse_historical_assessment(raw_assessment)
        supplement = _SUPPLEMENT_EVALUATOR_GRADES.get(task_id)
        discrepancy = False
        if parsed is not None and supplement is not None:
            discrepancy = any(
                parsed[student]["grade"] != supplement[student]
                for student in ("student_a", "student_b")
            )
        teacher_source = "".join(teacher_cell.get("source", []))
        records.append(
            {
                "repo_task_id": task_id,
                "task_text": task.text,
                "historical_chemcrow_final_answer": student_a.text,
                "historical_no_tools_gpt4_final_answer": student_b.text,
                "historical_evaluator_label_available": parsed is not None,
                "historical_evaluator": parsed,
                "supplement_evaluator": supplement,
                "supplement_discrepancy": discrepancy,
                "source_notebook": f"tasks/{path.name}",
                "source_repository": CHEMCROW_RUNS_REPOSITORY,
                "source_commit": CHEMCROW_RUNS_COMMIT,
                "source_tag": "v1",
                "source_tag_commit": CHEMCROW_RUNS_V1_COMMIT,
                "source_notebook_sha256": file_sha256(path),
                "task_sha256": canonical_sha256(task.text),
                "historical_chemcrow_answer_sha256": canonical_sha256(student_a.text),
                "historical_no_tools_gpt4_answer_sha256": canonical_sha256(student_b.text),
                "historical_evaluator_output_sha256": (
                    canonical_sha256(_clean_text(raw_assessment)) if raw_assessment else None
                ),
                "task_source": task.source_record(),
                "student_a_source": student_a.source_record(),
                "student_b_source": student_b.source_record(),
                "evaluator_source": {
                    "cell_index": teacher_index,
                    "cell_id": teacher_cell.get("id"),
                    "cell_execution_count": teacher_cell.get("execution_count"),
                    "cell_source_sha256": canonical_sha256(teacher_source),
                    "cell_output_sha256": (
                        canonical_sha256(raw_assessment) if raw_assessment else None
                    ),
                },
            }
        )
    missing = [
        record["repo_task_id"]
        for record in records
        if record["historical_evaluator_label_available"] is not True
    ]
    return {
        "schema_version": "chemcrow_paper_evaluator_historical_calibration_v1",
        "exact_prompt_recovered": False,
        "task_ids": list(task_ids),
        "task_count": len(records),
        "labeled_task_count": len(records) - len(missing),
        "missing_label_task_ids": missing,
        "student_order": {
            "student_a": "historical_chemcrow",
            "student_b": "historical_no_tools_gpt4",
        },
        "source_authority": "AUTHORITATIVE_AUTHOR_CONTROLLED",
        "source_repository": CHEMCROW_RUNS_REPOSITORY,
        "source_commit": CHEMCROW_RUNS_COMMIT,
        "source_tag": "v1",
        "source_tag_commit": CHEMCROW_RUNS_V1_COMMIT,
        "zenodo_doi": CHEMCROW_RUNS_ZENODO_DOI,
        "zenodo_archive_sha256": CHEMCROW_RUNS_ZENODO_ARCHIVE_SHA256,
        "supplement_source_data_sha256": NATURE_SOURCE_DATA_SHA256,
        "official_notebook_value_is_primary": True,
        "official_grades_never_enter_model_prompt": True,
        "tasks": records,
    }


def render_prompt_candidate(
    candidate_id: str, *, task_prompt: str, student_a: str, student_b: str
) -> str:
    if candidate_id not in _CANDIDATE_TEMPLATES:
        raise ValueError(f"unknown calibration prompt candidate: {candidate_id}")
    if candidate_id == "CURRENT_V1":
        rendered = render_compatible_prompt(
            task_prompt=task_prompt, student_a=student_a, student_b=student_b
        )
    else:
        rendered = (
            _CANDIDATE_TEMPLATES[candidate_id]
            .replace("<<TASK>>", task_prompt)
            .replace("<<STUDENT_A>>", student_a)
            .replace("<<STUDENT_B>>", student_b)
        )
    return rendered


def build_prompt_candidate_manifest(*, dataset_sha256: str) -> dict[str, Any]:
    example = {
        "task": "Explain the chemistry task.",
        "student_a": "Student A response.",
        "student_b": "Student B response.",
    }
    evidence = {
        "CURRENT_V1": [
            "existing CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1 unchanged",
        ],
        "PAPER_MINIMAL": [
            "Nature article: teacher, task addressed, overall thought process, strengths, weaknesses, improvement feedback",
        ],
        "PAPER_OUTPUT_FAITHFUL": [
            "same paper semantics plus official notebook Student 1/2 output ordering",
        ],
    }
    candidates = []
    for candidate_id in PROMPT_CANDIDATE_IDS:
        template = _CANDIDATE_TEMPLATES[candidate_id]
        rendered = render_prompt_candidate(
            candidate_id,
            task_prompt=example["task"],
            student_a=example["student_a"],
            student_b=example["student_b"],
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "instruction_template": template,
                "prompt_candidate_sha256": canonical_sha256(template),
                "rendered_example": rendered,
                "rendered_example_sha256": canonical_sha256(rendered),
                "paper_evidence": evidence[candidate_id],
                "labels_or_historical_grades_included": False,
                "future_g1_g2_identity_included": False,
            }
        )
    manifest = {
        "schema_version": "chemcrow_paper_evaluator_prompt_candidates_v1",
        "protocol": CALIBRATION_PROTOCOL,
        "exact_prompt_recovered": False,
        "candidate_count": 3,
        "candidate_ids": list(PROMPT_CANDIDATE_IDS),
        "dataset_sha256": dataset_sha256,
        "result_data_seen_when_frozen": False,
        "selection_rule": list(SELECTION_RULE),
        "simplicity_tie_order": list(SIMPLICITY_TIE_ORDER),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "repeatability_task_ids": list(REPEATABILITY_TASK_IDS),
        "repeatability_additional_calls_per_task": 2,
        "historical_agreement_thresholds": {
            "HIGH": {
                "pairwise_preference_agreement_min": 0.85,
                "score_mae_max": 1.0,
                "delta_mae_max": 1.0,
                "parse_schema_failures": 0,
            },
            "MODERATE": {
                "pairwise_preference_agreement_min": 0.70,
                "score_mae_max": 1.5,
                "parse_schema_failures": 0,
            },
        },
        "example_inputs": example,
        "candidates": candidates,
    }
    return validate_prompt_candidate_manifest(manifest)


def validate_prompt_candidate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if (
        manifest.get("schema_version")
        != "chemcrow_paper_evaluator_prompt_candidates_v1"
        or manifest.get("protocol") != CALIBRATION_PROTOCOL
        or manifest.get("exact_prompt_recovered") is not False
        or manifest.get("candidate_count") != 3
        or manifest.get("candidate_ids") != list(PROMPT_CANDIDATE_IDS)
        or manifest.get("result_data_seen_when_frozen") is not False
        or manifest.get("selection_rule") != list(SELECTION_RULE)
        or manifest.get("simplicity_tie_order") != list(SIMPLICITY_TIE_ORDER)
        or manifest.get("bootstrap_seed") != BOOTSTRAP_SEED
        or manifest.get("bootstrap_samples") != BOOTSTRAP_SAMPLES
        or manifest.get("repeatability_task_ids") != list(REPEATABILITY_TASK_IDS)
        or manifest.get("repeatability_additional_calls_per_task") != 2
    ):
        raise ValueError("prompt candidate manifest frozen metadata differs")
    example = manifest.get("example_inputs")
    if not isinstance(example, dict) or set(example) != {"task", "student_a", "student_b"}:
        raise ValueError("prompt candidate example inputs differ")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 3:
        raise ValueError("prompt candidate inventory differs")
    for expected_id, candidate in zip(PROMPT_CANDIDATE_IDS, candidates, strict=True):
        if not isinstance(candidate, dict) or candidate.get("candidate_id") != expected_id:
            raise ValueError("prompt candidate order differs")
        template = candidate.get("instruction_template")
        if template != _CANDIDATE_TEMPLATES[expected_id]:
            raise ValueError("prompt candidate text differs from frozen bytes")
        if candidate.get("prompt_candidate_sha256") != canonical_sha256(template):
            raise ValueError("prompt candidate hash differs")
        rendered = render_prompt_candidate(
            expected_id,
            task_prompt=str(example["task"]),
            student_a=str(example["student_a"]),
            student_b=str(example["student_b"]),
        )
        if (
            candidate.get("rendered_example") != rendered
            or candidate.get("rendered_example_sha256") != canonical_sha256(rendered)
            or candidate.get("labels_or_historical_grades_included") is not False
            or candidate.get("future_g1_g2_identity_included") is not False
        ):
            raise ValueError("prompt candidate rendered example differs")
    return manifest


def calibration_cost_ceiling() -> dict[str, Any]:
    max_input_tokens = 8191 - PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
    per_call = (
        max_input_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + PAPER_EVALUATOR_MAX_OUTPUT_TOKENS * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    return {
        "model_context_tokens": 8191,
        "max_input_tokens_per_call": max_input_tokens,
        "max_output_tokens_per_call": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "max_initial_calls": CALIBRATION_MAX_INITIAL_CALLS,
        "max_repeatability_calls": CALIBRATION_MAX_REPEATABILITY_CALLS,
        "max_total_calls": CALIBRATION_MAX_CALLS,
        "list_price_ceiling_usd_per_call": round(per_call, 8),
        "list_price_ceiling_usd_initial": round(
            per_call * CALIBRATION_MAX_INITIAL_CALLS, 8
        ),
        "list_price_ceiling_usd_repeatability": round(
            per_call * CALIBRATION_MAX_REPEATABILITY_CALLS, 8
        ),
        "list_price_ceiling_usd_total": round(per_call * CALIBRATION_MAX_CALLS, 8),
        "authorized_max_usd": CALIBRATION_MAX_USD,
    }


def build_calibration_plan(
    *, dataset: dict[str, Any], candidate_manifest: dict[str, Any]
) -> dict[str, Any]:
    validate_prompt_candidate_manifest(candidate_manifest)
    dataset_sha256 = canonical_sha256(dataset)
    if candidate_manifest.get("dataset_sha256") != dataset_sha256:
        raise ValueError("prompt candidates are not bound to the calibration dataset")
    calls: list[CalibrationCall] = []
    for record in dataset.get("tasks", []):
        if record.get("historical_evaluator_label_available") is not True:
            continue
        task_id = str(record["repo_task_id"])
        input_value = {
            "task": record["task_text"],
            "student_a": record["historical_chemcrow_final_answer"],
            "student_b": record["historical_no_tools_gpt4_final_answer"],
            "student_order": ["historical_chemcrow", "historical_no_tools_gpt4"],
        }
        for candidate_id in PROMPT_CANDIDATE_IDS:
            prompt = render_prompt_candidate(
                candidate_id,
                task_prompt=str(input_value["task"]),
                student_a=str(input_value["student_a"]),
                student_b=str(input_value["student_b"]),
            )
            estimated = estimate_chat_input_tokens(prompt)
            if estimated > calibration_cost_ceiling()["max_input_tokens_per_call"]:
                raise ValueError(f"calibration prompt exceeds context budget: {task_id}")
            calls.append(
                CalibrationCall(
                    call_id=(
                        f"{CALIBRATION_CALL_PREFIX}{task_id}-{candidate_id.lower().replace('_', '-')}"
                    ),
                    task_id=task_id,
                    prompt_candidate_id=candidate_id,
                    prompt_candidate_sha256=canonical_sha256(
                        _CANDIDATE_TEMPLATES[candidate_id]
                    ),
                    prompt=prompt,
                    prompt_sha256=canonical_sha256(prompt),
                    input_sha256=canonical_sha256(input_value),
                    estimated_input_tokens=estimated,
                    phase="initial",
                    repetition=0,
                )
            )
    expected = int(dataset.get("labeled_task_count", -1)) * len(PROMPT_CANDIDATE_IDS)
    if len(calls) != expected or len(calls) > CALIBRATION_MAX_INITIAL_CALLS:
        raise ValueError("initial calibration call inventory differs from frozen limit")
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("calibration call IDs are not unique")
    ceiling = calibration_cost_ceiling()
    if ceiling["list_price_ceiling_usd_total"] > CALIBRATION_MAX_USD:
        raise PermissionError("calibration worst-case cost exceeds the authorized maximum")
    return {
        "schema_version": CALIBRATION_PLAN_SCHEMA,
        "protocol": CALIBRATION_PROTOCOL,
        "phase": "initial",
        "exact_prompt_recovered": False,
        "dataset_sha256": dataset_sha256,
        "candidate_manifest_sha256": canonical_sha256(candidate_manifest),
        "prompt_candidate_ids": list(PROMPT_CANDIDATE_IDS),
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "student_order": ["historical_chemcrow", "historical_no_tools_gpt4"],
        "official_grades_in_model_requests": False,
        "production_ledger_included": False,
        "call_count": len(calls),
        "cost_ceiling": ceiling,
        "calls": [call.model_dump(mode="json") for call in calls],
    }


def validate_calibration_plan(plan: dict[str, Any]) -> list[CalibrationCall]:
    if (
        plan.get("schema_version") != CALIBRATION_PLAN_SCHEMA
        or plan.get("protocol") != CALIBRATION_PROTOCOL
        or plan.get("exact_prompt_recovered") is not False
        or plan.get("model") != PAPER_EVALUATOR_MODEL
        or plan.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or plan.get("provider_only") != [PAPER_EVALUATOR_PROVIDER]
        or plan.get("allow_fallbacks") is not False
        or plan.get("require_parameters") is not True
        or plan.get("data_collection") != PAPER_EVALUATOR_DATA_COLLECTION
        or plan.get("official_grades_in_model_requests") is not False
        or plan.get("production_ledger_included") is not False
    ):
        raise ValueError("calibration plan authority differs")
    calls = [CalibrationCall.model_validate(item) for item in plan.get("calls", [])]
    if len(calls) != plan.get("call_count") or len(calls) > CALIBRATION_MAX_CALLS:
        raise ValueError("calibration plan call count differs")
    if len({call.call_id for call in calls}) != len(calls):
        raise ValueError("calibration plan contains duplicate call IDs")
    if plan.get("phase") == "initial" and (
        len(calls) > CALIBRATION_MAX_INITIAL_CALLS
        or any(call.phase != "initial" or call.repetition != 0 for call in calls)
    ):
        raise ValueError("initial calibration plan contains invalid calls")
    if plan.get("phase") == "repeatability" and (
        len(calls) > CALIBRATION_MAX_REPEATABILITY_CALLS
        or any(call.phase != "repeatability" for call in calls)
    ):
        raise ValueError("repeatability calibration plan contains invalid calls")
    return calls


def build_repeatability_plan(
    *,
    dataset: dict[str, Any],
    candidate_manifest: dict[str, Any],
    selected_candidate_id: str,
) -> dict[str, Any]:
    """Build six post-selection calls from the already frozen prompt set."""
    validate_prompt_candidate_manifest(candidate_manifest)
    if selected_candidate_id not in PROMPT_CANDIDATE_IDS:
        raise ValueError("repeatability prompt is not one of the frozen candidates")
    by_task = {record["repo_task_id"]: record for record in dataset.get("tasks", [])}
    calls: list[CalibrationCall] = []
    for task_id in REPEATABILITY_TASK_IDS:
        record = by_task.get(task_id)
        if not isinstance(record, dict) or record.get("historical_evaluator_label_available") is not True:
            raise ValueError(f"repeatability task is unavailable: {task_id}")
        input_value = {
            "task": record["task_text"],
            "student_a": record["historical_chemcrow_final_answer"],
            "student_b": record["historical_no_tools_gpt4_final_answer"],
            "student_order": ["historical_chemcrow", "historical_no_tools_gpt4"],
        }
        prompt = render_prompt_candidate(
            selected_candidate_id,
            task_prompt=str(input_value["task"]),
            student_a=str(input_value["student_a"]),
            student_b=str(input_value["student_b"]),
        )
        for repetition in (1, 2):
            calls.append(
                CalibrationCall(
                    call_id=(
                        f"{CALIBRATION_CALL_PREFIX}{task_id}-"
                        f"{selected_candidate_id.lower().replace('_', '-')}-repeat-{repetition}"
                    ),
                    task_id=task_id,
                    prompt_candidate_id=selected_candidate_id,
                    prompt_candidate_sha256=canonical_sha256(
                        _CANDIDATE_TEMPLATES[selected_candidate_id]
                    ),
                    prompt=prompt,
                    prompt_sha256=canonical_sha256(prompt),
                    input_sha256=canonical_sha256(input_value),
                    estimated_input_tokens=estimate_chat_input_tokens(prompt),
                    phase="repeatability",
                    repetition=repetition,
                )
            )
    if len(calls) != CALIBRATION_MAX_REPEATABILITY_CALLS:
        raise AssertionError("repeatability plan must contain exactly six calls")
    plan = {
        "schema_version": CALIBRATION_PLAN_SCHEMA,
        "protocol": CALIBRATION_PROTOCOL,
        "phase": "repeatability",
        "exact_prompt_recovered": False,
        "dataset_sha256": canonical_sha256(dataset),
        "candidate_manifest_sha256": canonical_sha256(candidate_manifest),
        "prompt_candidate_ids": [selected_candidate_id],
        "selected_candidate_id": selected_candidate_id,
        "repeatability_task_ids": list(REPEATABILITY_TASK_IDS),
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "student_order": ["historical_chemcrow", "historical_no_tools_gpt4"],
        "official_grades_in_model_requests": False,
        "production_ledger_included": False,
        "call_count": len(calls),
        "cost_ceiling": calibration_cost_ceiling(),
        "calls": [call.model_dump(mode="json") for call in calls],
    }
    validate_calibration_plan(plan)
    return plan


def assert_calibration_ledger_separation(
    calibration_root: Path, production_root: Path
) -> None:
    calibration = calibration_root.resolve()
    production = production_root.resolve()
    if calibration == production or production in calibration.parents or calibration in production.parents:
        raise ValueError("calibration and production ledgers must be separate non-nested roots")
    if calibration.name != "paper-evaluator-calibration-v1":
        raise ValueError("calibration ledger root has an unexpected identity")
    if production.name != "paper-evaluator-full-v4-three-pipeline":
        raise ValueError("production ledger root has an unexpected identity")


def require_calibration_authorization() -> None:
    if os.environ.get("CHEMCROW_PAPER_CALIBRATION_AUTHORIZATION") != CALIBRATION_AUTHORIZATION:
        raise PermissionError("calibration-specific paid authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise PermissionError("calibration model environment differs from openai/gpt-4")
    budget = float(os.environ.get("CHEMCROW_PAPER_CALIBRATION_MAX_USD", "0"))
    if budget < calibration_cost_ceiling()["list_price_ceiling_usd_total"]:
        raise PermissionError("calibration budget is below the frozen total ceiling")
    if budget > CALIBRATION_MAX_USD:
        raise PermissionError("calibration budget exceeds the authorized maximum")


def build_calibration_task_request(
    *, call: CalibrationCall, runtime: dict[str, Any]
) -> dict[str, Any]:
    require_calibration_authorization()
    return {
        "task_id": call.call_id,
        "instruction": call.prompt,
        "num_samples": 1,
        "timeout_seconds": 420.0,
        "runtime": runtime,
        "agent": {
            "harness": None,
            "import_path": "openevo_chemcrow.paper_harness:PaperCalibrationHarness",
            "model_name": PAPER_EVALUATOR_MODEL,
            "settings": {
                "capture_mode": "transcript",
                "temperature": PAPER_EVALUATOR_TEMPERATURE,
                "max_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
                "runtime_gateway_base_url": CALIBRATION_RUNTIME_GATEWAY_BASE_URL,
            },
            "env": {"PAPER_EVALUATOR_CALL_ID": call.call_id},
            "mcp_servers": [],
            "skills_path": None,
            "custom_shell": None,
        },
        "builder": {"strategy": "agent_transcript", "config": {}},
        "evaluator": None,
        "callback_url": None,
        "metadata": {
            "execution_route": CALIBRATION_CORE_ROUTE,
            "calibration_protocol": CALIBRATION_PROTOCOL,
            "prompt_candidate_id": call.prompt_candidate_id,
            "prompt_candidate_sha256": call.prompt_candidate_sha256,
            "prompt_sha256": call.prompt_sha256,
            "official_grades_in_request": False,
            "reflector_access": False,
            "evolution_context_forbidden": True,
            "production_ledger": False,
        },
        "workspace_handoff": None,
        "runtime_context_binding": None,
    }


def run_calibration_plan(
    *,
    plan_path: Path,
    rollout_base_url: str,
    runtime: dict[str, Any],
    result_root: Path,
    shim_receipt_root: Path,
    calibration_root: Path,
    production_root: Path,
    allow_paid: bool,
) -> dict[str, Any]:
    assert_calibration_ledger_separation(calibration_root, production_root)
    if not allow_paid:
        raise PermissionError("calibration requires --allow-paid")
    require_calibration_authorization()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_calibration_plan(plan)
    _assert_calibration_core_node(rollout_base_url)
    result_root.mkdir(parents=True, exist_ok=True)
    results = []
    for call in calls:
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("call_id") != call.call_id or result.get("prompt_sha256") != call.prompt_sha256:
                raise ValueError("existing calibration result differs from frozen call")
            results.append(result)
            continue
        if claim_path.exists() or receipt_path.exists():
            raise RuntimeError(
                f"unsealed or ambiguous calibration call will not be retried: {call.call_id}"
            )
        status = _submit_calibration_once(
            rollout_base_url,
            build_calibration_task_request(call=call, runtime=runtime),
        )
        assessment = _assessment_from_status(status)
        if not receipt_path.is_file():
            raise RuntimeError(f"calibration usage receipt is missing: {call.call_id}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "terminal_success" or receipt.get("call_id") != call.call_id:
            raise RuntimeError(f"calibration OpenRouter receipt is not successful: {call.call_id}")
        result = {
            "schema_version": "chemcrow_paper_calibration_call_result_v1",
            "call_id": call.call_id,
            "task_id": call.task_id,
            "phase": call.phase,
            "repetition": call.repetition,
            "prompt_candidate_id": call.prompt_candidate_id,
            "prompt_candidate_sha256": call.prompt_candidate_sha256,
            "prompt_sha256": call.prompt_sha256,
            "input_sha256": call.input_sha256,
            "assessment": assessment.model_dump(mode="json"),
            "assessment_sha256": canonical_sha256(assessment.model_dump(mode="json")),
            "parse_valid": True,
            "schema_valid": True,
            "model": receipt.get("model"),
            "provider": receipt.get("provider"),
            "temperature": receipt.get("temperature"),
            "allow_fallbacks": receipt.get("allow_fallbacks"),
            "usage": {
                "prompt_tokens": receipt.get("prompt_tokens"),
                "completion_tokens": receipt.get("completion_tokens"),
                "total_tokens": receipt.get("total_tokens"),
            },
            "cost": {
                "list_price_cost_usd": receipt.get("list_price_cost_usd"),
                "openrouter_reported_cost_usd": receipt.get(
                    "openrouter_reported_cost_usd"
                ),
            },
            "receipt_sha256": file_sha256(receipt_path),
            "execution_route": CALIBRATION_CORE_ROUTE,
            "official_grades_in_model_request": False,
            "production_ledger_included": False,
        }
        _exclusive_json_write(result_path, result)
        results.append(result)
    return {
        "schema_version": CALIBRATION_RESULTS_SCHEMA,
        "status": "COMPLETE" if len(results) == len(calls) else "INCOMPLETE",
        "phase": plan["phase"],
        "call_count": len(calls),
        "result_count": len(results),
        "plan_sha256": file_sha256(plan_path),
        "result_sha256s": {
            result["call_id"]: file_sha256(result_root / f"{result['call_id']}.result.json")
            for result in results
        },
        "production_ledger_included": False,
    }


def compute_candidate_metrics(
    *, dataset: dict[str, Any], outputs: list[CandidateOutput]
) -> dict[str, Any]:
    records, failures = _paired_metric_records(dataset, outputs)
    if not records:
        raise ValueError("candidate has no complete calibration outputs")
    metrics = _metrics_from_records(records)
    expected = sum(
        item.get("historical_evaluator_label_available") is True
        for item in dataset.get("tasks", [])
    )
    return {
        **metrics,
        "expected_task_count": expected,
        "evaluated_task_count": len(records),
        "json_parse_failures": failures["parse"],
        "pydantic_schema_failures": failures["schema"],
        "provider_failures": failures["provider"],
        "json_parse_failure_rate": failures["parse"] / expected,
        "pydantic_schema_failure_rate": failures["schema"] / expected,
        "provider_failure_rate": failures["provider"] / expected,
        "parse_schema_failure_rate": (failures["parse"] + failures["schema"]) / expected,
    }


def bootstrap_confidence_intervals(
    *,
    dataset: dict[str, Any],
    outputs: list[CandidateOutput],
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    records, _ = _paired_metric_records(dataset, outputs)
    if not records:
        raise ValueError("bootstrap requires complete task records")
    rng = random.Random(seed)
    observed: dict[str, list[float]] = {
        "score_mae": [],
        "pairwise_preference_agreement": [],
        "delta_mae": [],
        "score_spearman": [],
    }
    for _ in range(samples):
        sampled = [records[rng.randrange(len(records))] for _ in records]
        metrics = _metrics_from_records(sampled)
        for key, values in observed.items():
            value = metrics[key]
            if value is not None:
                values.append(float(value))
    return {
        "seed": seed,
        "samples": samples,
        "unit": "task_with_student_pair_preserved",
        "confidence_intervals_95": {
            key: {
                "lower": _percentile(values, 0.025),
                "upper": _percentile(values, 0.975),
            }
            for key, values in observed.items()
        },
    }


def select_prompt_candidate(metrics_by_candidate: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(metrics_by_candidate) != set(PROMPT_CANDIDATE_IDS):
        raise ValueError("prompt selection requires exactly the three frozen candidates")
    simplicity = {
        candidate_id: len(SIMPLICITY_TIE_ORDER) - SIMPLICITY_TIE_ORDER.index(candidate_id)
        for candidate_id in SIMPLICITY_TIE_ORDER
    }

    def key(candidate_id: str) -> tuple[float, ...]:
        metrics = metrics_by_candidate[candidate_id]
        spearman = metrics.get("score_spearman")
        failure = (
            float(metrics.get("json_parse_failure_rate", 0.0))
            + float(metrics.get("pydantic_schema_failure_rate", 0.0))
            + float(metrics.get("provider_failure_rate", 0.0))
        )
        return (
            float(metrics["pairwise_preference_agreement"]),
            -float(metrics["delta_mae"]),
            -float(metrics["score_mae"]),
            float(spearman) if spearman is not None else -math.inf,
            -failure,
            float(simplicity[candidate_id]),
        )

    ranking = sorted(PROMPT_CANDIDATE_IDS, key=key, reverse=True)
    return {
        "selected_candidate_id": ranking[0],
        "selection_rule": list(SELECTION_RULE),
        "ranking": ranking,
        "selection_keys": {candidate_id: list(key(candidate_id)) for candidate_id in ranking},
    }


def leave_one_task_out_selection(
    *, dataset: dict[str, Any], outputs_by_candidate: dict[str, list[CandidateOutput]]
) -> dict[str, Any]:
    task_ids = [
        item["repo_task_id"]
        for item in dataset.get("tasks", [])
        if item.get("historical_evaluator_label_available") is True
    ]
    folds = []
    winners: Counter[str] = Counter()
    for held_out in task_ids:
        training_dataset = {
            **dataset,
            "tasks": [
                item for item in dataset["tasks"] if item["repo_task_id"] != held_out
            ],
        }
        training_metrics = {
            candidate_id: compute_candidate_metrics(
                dataset=training_dataset,
                outputs=[output for output in outputs if output.task_id != held_out],
            )
            for candidate_id, outputs in outputs_by_candidate.items()
        }
        selected = select_prompt_candidate(training_metrics)["selected_candidate_id"]
        winners[selected] += 1
        held_dataset = {
            **dataset,
            "tasks": [item for item in dataset["tasks"] if item["repo_task_id"] == held_out],
        }
        held_metrics = compute_candidate_metrics(
            dataset=held_dataset,
            outputs=[
                output for output in outputs_by_candidate[selected] if output.task_id == held_out
            ],
        )
        folds.append(
            {
                "held_out_task_id": held_out,
                "selected_candidate_id": selected,
                "held_out_pairwise_agreement": held_metrics[
                    "pairwise_preference_agreement"
                ],
                "held_out_score_mae": held_metrics["score_mae"],
                "held_out_delta_error": held_metrics["delta_mae"],
            }
        )
    return {
        "fold_count": len(folds),
        "folds_won_by_candidate": {
            candidate_id: winners[candidate_id] for candidate_id in PROMPT_CANDIDATE_IDS
        },
        "held_out_pairwise_agreement": statistics.mean(
            fold["held_out_pairwise_agreement"] for fold in folds
        ),
        "held_out_score_error": statistics.mean(
            fold["held_out_score_mae"] for fold in folds
        ),
        "held_out_delta_error": statistics.mean(
            fold["held_out_delta_error"] for fold in folds
        ),
        "folds": folds,
        "independent_benchmark_claimed": False,
    }


def classify_historical_agreement(metrics: dict[str, Any]) -> str:
    failures = int(metrics["json_parse_failures"]) + int(metrics["pydantic_schema_failures"])
    if (
        metrics["pairwise_preference_agreement"] >= 0.85
        and metrics["score_mae"] <= 1.0
        and metrics["delta_mae"] <= 1.0
        and failures == 0
    ):
        return "HIGH"
    if (
        metrics["pairwise_preference_agreement"] >= 0.70
        and metrics["score_mae"] <= 1.5
        and failures == 0
    ):
        return "MODERATE"
    return "LOW"


def analyze_calibration_results(
    *,
    dataset: dict[str, Any],
    candidate_manifest: dict[str, Any],
    initial_plan: dict[str, Any],
    initial_result_root: Path,
    repeatability_plan: dict[str, Any] | None = None,
    repeatability_result_root: Path | None = None,
) -> dict[str, Any]:
    """Join public historical labels outside the model and compute frozen metrics."""
    validate_prompt_candidate_manifest(candidate_manifest)
    calls = validate_calibration_plan(initial_plan)
    if initial_plan.get("phase") != "initial":
        raise ValueError("primary calibration analysis requires the initial plan")
    historical = {item["repo_task_id"]: item for item in dataset["tasks"]}
    outputs_by_candidate: dict[str, list[CandidateOutput]] = {
        candidate_id: [] for candidate_id in PROMPT_CANDIDATE_IDS
    }
    rows = []
    total_reported_cost = 0.0
    total_list_cost = 0.0
    for call in calls:
        path = initial_result_root / f"{call.call_id}.result.json"
        if not path.is_file():
            raise FileNotFoundError(f"calibration result is missing: {call.call_id}")
        result = json.loads(path.read_text(encoding="utf-8"))
        _validate_result_against_call(result, call)
        official = historical[call.task_id]["historical_evaluator"]
        assessment = result["assessment"]
        official_a = float(official["student_a"]["grade"])
        official_b = float(official["student_b"]["grade"])
        current_a = float(assessment["student_a"]["grade"])
        current_b = float(assessment["student_b"]["grade"])
        official_delta = official_a - official_b
        current_delta = current_a - current_b
        reported_cost = result["cost"].get("openrouter_reported_cost_usd")
        list_cost = result["cost"].get("list_price_cost_usd")
        if isinstance(reported_cost, int | float):
            total_reported_cost += float(reported_cost)
        if isinstance(list_cost, int | float):
            total_list_cost += float(list_cost)
        rows.append(
            {
                "call_id": call.call_id,
                "task_id": call.task_id,
                "prompt_candidate_id": call.prompt_candidate_id,
                "prompt_candidate_sha256": call.prompt_candidate_sha256,
                "prompt_sha256": call.prompt_sha256,
                "input_sha256": call.input_sha256,
                "official_student_a_grade": official_a,
                "official_student_b_grade": official_b,
                "current_student_a_grade": current_a,
                "current_student_b_grade": current_b,
                "official_delta": official_delta,
                "current_delta": current_delta,
                "official_preference": _preference(official_delta),
                "current_preference": _preference(current_delta),
                "absolute_error_a": abs(current_a - official_a),
                "absolute_error_b": abs(current_b - official_b),
                "delta_error": abs(current_delta - official_delta),
                "parse_valid": result["parse_valid"],
                "schema_valid": result["schema_valid"],
                "model": result["model"],
                "provider": result["provider"],
                "temperature": result["temperature"],
                "usage": result["usage"],
                "cost": result["cost"],
                "result_sha256": file_sha256(path),
            }
        )
        outputs_by_candidate[call.prompt_candidate_id].append(
            CandidateOutput(
                task_id=call.task_id,
                candidate_id=call.prompt_candidate_id,
                assessment=assessment,
                parse_valid=bool(result["parse_valid"]),
                schema_valid=bool(result["schema_valid"]),
                provider_valid=(
                    result.get("model") == PAPER_EVALUATOR_MODEL
                    and str(result.get("provider", "")).casefold() == "openai"
                ),
            )
        )
    metrics = {
        candidate_id: compute_candidate_metrics(
            dataset=dataset, outputs=outputs_by_candidate[candidate_id]
        )
        for candidate_id in PROMPT_CANDIDATE_IDS
    }
    bootstrap = {
        candidate_id: bootstrap_confidence_intervals(
            dataset=dataset, outputs=outputs_by_candidate[candidate_id]
        )
        for candidate_id in PROMPT_CANDIDATE_IDS
    }
    selection = select_prompt_candidate(metrics)
    selected_id = selection["selected_candidate_id"]
    loo = leave_one_task_out_selection(
        dataset=dataset, outputs_by_candidate=outputs_by_candidate
    )
    repeatability = _repeatability_analysis(
        selected_candidate_id=selected_id,
        primary_rows=rows,
        repeatability_plan=repeatability_plan,
        repeatability_result_root=repeatability_result_root,
    )
    total_reported_cost += repeatability["proven_openrouter_reported_cost_usd"]
    total_list_cost += repeatability["list_price_cost_usd"]
    selected_metrics = metrics[selected_id]
    agreement = classify_historical_agreement(selected_metrics)
    final_label = {
        "HIGH": "CALIBRATED_COMPATIBLE_HIGH",
        "MODERATE": "CALIBRATED_COMPATIBLE_MODERATE",
        "LOW": "MODERN_JUDGE_LOW_HISTORICAL_AGREEMENT",
    }[agreement]
    return {
        "schema_version": CALIBRATION_RESULTS_SCHEMA,
        "status": "COMPLETE" if repeatability["status"] == "COMPLETE" else "PRIMARY_COMPLETE",
        "exact_prompt_recovered": False,
        "historical_task_count": dataset["task_count"],
        "historical_labeled_task_count": dataset["labeled_task_count"],
        "dataset_sha256": canonical_sha256(dataset),
        "candidate_manifest_sha256": canonical_sha256(candidate_manifest),
        "initial_plan_sha256": canonical_sha256(initial_plan),
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "selection": selection,
        "selected_candidate_id": selected_id,
        "selected_prompt_sha256": canonical_sha256(_CANDIDATE_TEMPLATES[selected_id]),
        "historical_agreement": agreement,
        "final_label": final_label,
        "metrics_by_candidate": metrics,
        "bootstrap_by_candidate": bootstrap,
        "leave_one_task_out": loo,
        "repeatability": repeatability,
        "per_call_results": rows,
        "paid_calls": len(rows) + repeatability["paid_calls"],
        "proven_openrouter_reported_cost_usd": round(total_reported_cost, 8),
        "list_price_cost_usd": round(total_list_cost, 8),
        "calibration_budget_usd": CALIBRATION_MAX_USD,
        "production_42_call_ledger_included": False,
        "official_grades_in_model_requests": False,
        "prompt_modified_after_results": False,
    }


def selected_prompt_from_results(results: dict[str, Any]) -> SelectedPrompt:
    candidate_id = str(results["selected_candidate_id"])
    protocol = (
        "CHEMCROW_EVALUATORGPT_PROMPT_COMPATIBLE_V1"
        if candidate_id == "CURRENT_V1"
        else "CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2"
    )
    return SelectedPrompt(
        protocol=protocol,
        candidate_id=candidate_id,
        instruction_template=_CANDIDATE_TEMPLATES[candidate_id],
        prompt_sha256=canonical_sha256(_CANDIDATE_TEMPLATES[candidate_id]),
        exact_prompt_recovered=False,
    )


def _validate_result_against_call(result: dict[str, Any], call: CalibrationCall) -> None:
    if (
        result.get("schema_version") != "chemcrow_paper_calibration_call_result_v1"
        or result.get("call_id") != call.call_id
        or result.get("task_id") != call.task_id
        or result.get("prompt_candidate_id") != call.prompt_candidate_id
        or result.get("prompt_candidate_sha256") != call.prompt_candidate_sha256
        or result.get("prompt_sha256") != call.prompt_sha256
        or result.get("input_sha256") != call.input_sha256
        or result.get("parse_valid") is not True
        or result.get("schema_valid") is not True
        or result.get("model") != PAPER_EVALUATOR_MODEL
        or str(result.get("provider", "")).casefold() != "openai"
        or result.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or result.get("allow_fallbacks") is not False
        or result.get("production_ledger_included") is not False
    ):
        raise ValueError(f"calibration result differs from frozen call: {call.call_id}")
    DualStudentAssessment.model_validate(result.get("assessment"))


def _repeatability_analysis(
    *,
    selected_candidate_id: str,
    primary_rows: list[dict[str, Any]],
    repeatability_plan: dict[str, Any] | None,
    repeatability_result_root: Path | None,
) -> dict[str, Any]:
    if repeatability_plan is None or repeatability_result_root is None:
        return {
            "status": "PENDING",
            "task_ids": list(REPEATABILITY_TASK_IDS),
            "paid_calls": 0,
            "proven_openrouter_reported_cost_usd": 0.0,
            "list_price_cost_usd": 0.0,
            "tasks": [],
        }
    calls = validate_calibration_plan(repeatability_plan)
    if repeatability_plan.get("selected_candidate_id") != selected_candidate_id:
        raise ValueError("repeatability plan selected candidate differs")
    by_task: dict[str, list[tuple[float, float]]] = {task_id: [] for task_id in REPEATABILITY_TASK_IDS}
    for row in primary_rows:
        if (
            row["prompt_candidate_id"] == selected_candidate_id
            and row["task_id"] in by_task
        ):
            by_task[row["task_id"]].append(
                (row["current_student_a_grade"], row["current_student_b_grade"])
            )
    reported = list_cost = 0.0
    for call in calls:
        path = repeatability_result_root / f"{call.call_id}.result.json"
        if not path.is_file():
            return {
                "status": "PENDING",
                "task_ids": list(REPEATABILITY_TASK_IDS),
                "paid_calls": 0,
                "proven_openrouter_reported_cost_usd": 0.0,
                "list_price_cost_usd": 0.0,
                "tasks": [],
            }
        result = json.loads(path.read_text(encoding="utf-8"))
        _validate_result_against_call(result, call)
        assessment = result["assessment"]
        by_task[call.task_id].append(
            (
                float(assessment["student_a"]["grade"]),
                float(assessment["student_b"]["grade"]),
            )
        )
        value = result["cost"].get("openrouter_reported_cost_usd")
        if isinstance(value, int | float):
            reported += float(value)
        value = result["cost"].get("list_price_cost_usd")
        if isinstance(value, int | float):
            list_cost += float(value)
    task_rows = []
    for task_id in REPEATABILITY_TASK_IDS:
        grades = by_task[task_id]
        if len(grades) != 3:
            raise ValueError("repeatability task does not have primary plus two repetitions")
        deltas = [a - b for a, b in grades]
        preferences = [_preference(delta) for delta in deltas]
        task_rows.append(
            {
                "task_id": task_id,
                "student_a_grade_range": [min(a for a, _ in grades), max(a for a, _ in grades)],
                "student_b_grade_range": [min(b for _, b in grades), max(b for _, b in grades)],
                "delta_range": [min(deltas), max(deltas)],
                "preference_stable": len(set(preferences)) == 1,
                "preferences": preferences,
            }
        )
    return {
        "status": "COMPLETE",
        "task_ids": list(REPEATABILITY_TASK_IDS),
        "paid_calls": len(calls),
        "proven_openrouter_reported_cost_usd": round(reported, 8),
        "list_price_cost_usd": round(list_cost, 8),
        "all_preferences_stable": all(row["preference_stable"] for row in task_rows),
        "tasks": task_rows,
    }


def _teacher_call(cells: list[Any]) -> tuple[int, str]:
    found: list[tuple[int, str]] = []
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue
        source = "".join(cell.get("source", []))
        if "Evaluator(" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "teacher"
                and len(node.args) == 3
                and isinstance(node.args[0], ast.Name)
            ):
                found.append((index, node.args[0].id))
    if len(found) != 1:
        raise ValueError("historical teacher call is not unique or has unsupported arguments")
    return found[0]


def _resolve_notebook_value(
    cells: list[Any], *, variable: str, before_index: int, role: str
) -> _ResolvedNotebookValue:
    assignments: list[tuple[int, dict[str, Any], ast.AST]] = []
    for index, cell in enumerate(cells[:before_index]):
        if not isinstance(cell, dict):
            continue
        source = "".join(cell.get("source", []))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == variable for target in targets):
                assignments.append((index, cell, node.value))
    if not assignments:
        raise ValueError(f"historical assignment is missing: {variable}")
    index, cell, value = assignments[-1]
    source = "".join(cell.get("source", []))
    raw_output = _cell_stream_text(cell)
    common = {
        "variable_name": variable,
        "cell_index": index,
        "cell_id": cell.get("id"),
        "cell_execution_count": cell.get("execution_count"),
        "cell_source_sha256": canonical_sha256(source),
        "cell_output_sha256": canonical_sha256(raw_output) if raw_output else None,
    }
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return _ResolvedNotebookValue(
            text=_clean_text(value.value), resolution="literal_assignment", **common
        )
    if not isinstance(value, ast.Call):
        raise TypeError(f"unsupported historical assignment: {variable}")
    if role == "student_a":
        text = _extract_chemcrow_output(raw_output)
    else:
        text = _clean_text(raw_output)
    if not text:
        raise ValueError(f"historical captured output is empty: {variable}")
    return _ResolvedNotebookValue(text=text, resolution="captured_call_output", **common)


def _cell_stream_text(cell: dict[str, Any]) -> str:
    parts = []
    for output in cell.get("outputs", []):
        if isinstance(output, dict) and output.get("output_type") == "stream":
            value = output.get("text", [])
            parts.append(value if isinstance(value, str) else "".join(value))
    return "\n".join(parts)


def _extract_chemcrow_output(text: str) -> str:
    cleaned = _clean_text(text)
    marker = "ChemCrow output:"
    if marker not in cleaned:
        raise ValueError("historical ChemCrow final-answer marker is missing")
    return cleaned.rsplit(marker, 1)[-1].strip()


def _clean_text(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text).strip()


def _paired_metric_records(
    dataset: dict[str, Any], outputs: list[CandidateOutput]
) -> tuple[list[dict[str, float]], dict[str, int]]:
    by_task = {output.task_id: output for output in outputs}
    records: list[dict[str, float]] = []
    failures = {"parse": 0, "schema": 0, "provider": 0}
    for item in dataset.get("tasks", []):
        if item.get("historical_evaluator_label_available") is not True:
            continue
        task_id = str(item["repo_task_id"])
        output = by_task.get(task_id)
        if output is None:
            failures["provider"] += 1
            continue
        failures["parse"] += int(not output.parse_valid)
        failures["schema"] += int(not output.schema_valid)
        failures["provider"] += int(not output.provider_valid)
        if (
            not output.parse_valid
            or not output.schema_valid
            or not output.provider_valid
            or output.assessment is None
        ):
            continue
        historical = item["historical_evaluator"]
        records.append(
            {
                "official_a": float(historical["student_a"]["grade"]),
                "official_b": float(historical["student_b"]["grade"]),
                "current_a": float(output.assessment["student_a"]["grade"]),
                "current_b": float(output.assessment["student_b"]["grade"]),
            }
        )
    return records, failures


def _metrics_from_records(records: list[dict[str, float]]) -> dict[str, Any]:
    official_scores = [value for record in records for value in (record["official_a"], record["official_b"])]
    current_scores = [value for record in records for value in (record["current_a"], record["current_b"])]
    errors = [current - official for current, official in zip(current_scores, official_scores, strict=True)]
    abs_errors = [abs(value) for value in errors]
    official_deltas = [record["official_a"] - record["official_b"] for record in records]
    current_deltas = [record["current_a"] - record["current_b"] for record in records]
    delta_errors = [current - official for current, official in zip(current_deltas, official_deltas, strict=True)]
    labels = ("a_wins", "tie", "b_wins")
    confusion = {official: {current: 0 for current in labels} for official in labels}
    agreements = 0
    for official, current in zip(official_deltas, current_deltas, strict=True):
        official_label = _preference(official)
        current_label = _preference(current)
        confusion[official_label][current_label] += 1
        agreements += int(official_label == current_label)
    return {
        "score_mae": statistics.mean(abs_errors),
        "score_rmse": math.sqrt(statistics.mean(value * value for value in errors)),
        "score_bias": statistics.mean(errors),
        "score_median_absolute_error": statistics.median(abs_errors),
        "score_within_0_5": sum(value <= 0.5 for value in abs_errors) / len(abs_errors),
        "score_within_1_0": sum(value <= 1.0 for value in abs_errors) / len(abs_errors),
        "score_pearson": _pearson(official_scores, current_scores),
        "score_spearman": _spearman(official_scores, current_scores),
        "pairwise_preference_agreement": agreements / len(records),
        "preference_confusion": confusion,
        "delta_mae": statistics.mean(abs(value) for value in delta_errors),
        "delta_rmse": math.sqrt(statistics.mean(value * value for value in delta_errors)),
        "delta_pearson": _pearson(official_deltas, current_deltas),
        "delta_spearman": _spearman(official_deltas, current_deltas),
        "delta_sign_agreement": agreements / len(records),
    }


def _preference(delta: float) -> str:
    if delta > 0:
        return "a_wins"
    if delta < 0:
        return "b_wins"
    return "tie"


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_sum = sum((value - left_mean) ** 2 for value in left)
    right_sum = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else None


def _spearman(left: list[float], right: list[float]) -> float | None:
    return _pearson(_average_ranks(left), _average_ranks(right))


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average = ((start + 1) + end) / 2
        for index, _ in ordered[start:end]:
            ranks[index] = average
        start = end
    return ranks


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _assert_calibration_core_node(base_url: str) -> None:
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        response = client.get(base_url.rstrip("/") + "/nodes")
        response.raise_for_status()
        nodes = response.json()
    if not isinstance(nodes, list) or len(nodes) != 1:
        raise RuntimeError("calibration requires exactly one dedicated Core Gateway node")
    node = nodes[0]
    if (
        not isinstance(node, dict)
        or node.get("node_id") != "chemcrow-paper-calibration-gateway-01"
        or node.get("healthy") is not True
    ):
        raise RuntimeError("dedicated paper calibration Gateway is not healthy")


def _submit_calibration_once(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    task_id = str(payload["task_id"])
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        response = client.post(base_url.rstrip("/") + "/rollout/task/submit", json=payload)
        response.raise_for_status()
        status, _ = _poll_rollout_until_terminal(
            client=client,
            url=base_url.rstrip("/") + f"/rollout/task/{task_id}",
            started=started,
            admitted_timeout_seconds=float(payload["timeout_seconds"]),
            poll_seconds=1.0,
        )
    return status


def _assessment_from_status(status: dict[str, Any]) -> DualStudentAssessment:
    if status.get("status") != "COMPLETED":
        raise RuntimeError("calibration Core call did not complete")
    result = status.get("result")
    if not isinstance(result, dict):
        raise TypeError("calibration Core result is missing")
    samples = result.get("samples")
    if not isinstance(samples, list) or len(samples) != 1 or not isinstance(samples[0], dict):
        raise RuntimeError("calibration Core sample inventory differs")
    content = _message_content(samples[0].get("response_messages"))
    if not content:
        raise RuntimeError("calibration response content is missing")
    return DualStudentAssessment.model_validate_json(content)


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
