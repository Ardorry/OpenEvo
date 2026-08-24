from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem

_CATEGORY_BY_NUMBER = {
    "01": "synthesis_planning",
    "02": "catalyst_discovery",
    "03": "reaction_mechanism",
    "04": "synthesis_planning",
    "05": "synthesis_planning",
    "06": "reaction_mechanism",
    "07": "molecular_design_and_synthesis",
    "08": "catalyst_synthesis",
    "09": "reaction_prediction_and_compatibility",
    "10": "reaction_prediction_and_property",
    "12": "molecular_similarity_and_mechanism_of_action",
    "13": "synthesis_planning",
    "14": "synthesis_planning",
    "15": "catalyst_synthesis",
}
_FORBIDDEN_FIELDS = {
    "answer",
    "answers",
    "trajectory",
    "trajectories",
    "evaluator",
    "evaluator_feedback",
    "expert_grade",
    "expert_grades",
    "gpt4_output",
    "historical_output",
    "paper_conclusion",
    "reference_answer",
    "result_tools",
    "result_notools",
}


class TaskExtractionError(ValueError):
    pass


def _literal_assignment(source: str) -> tuple[str, str] | None:
    tree = ast.parse(source)
    matches: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in {"task", "prompt"}:
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                raise TaskExtractionError("task assignment is not one static string literal")
            matches.append((target.id, node.value.value))
    if not matches:
        return None
    if len(matches) != 1:
        raise TaskExtractionError("notebook has multiple task prompt assignments")
    return matches[0]


def extract_task(path: Path, *, runs_root: Path) -> tuple[TaskItem, dict[str, Any]]:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[tuple[int, str, str]] = []
    output_cells = 0
    output_objects = 0
    for index, cell in enumerate(notebook.get("cells", [])):
        outputs = cell.get("outputs", [])
        if outputs:
            output_cells += 1
            output_objects += len(outputs)
        if cell.get("cell_type") != "code":
            continue
        match = _literal_assignment("".join(cell.get("source", [])))
        if match is not None:
            candidates.append((index, *match))
    if len(candidates) != 1:
        raise TaskExtractionError(f"{path.name}: expected exactly one task literal")
    cell_index, variable_name, prompt = candidates[0]
    number = path.name.split("_", 1)[0]
    if number not in _CATEGORY_BY_NUMBER:
        raise TaskExtractionError(f"{path.name}: unmapped task number")
    relative = path.relative_to(runs_root).as_posix()
    source_hash = file_sha256(path)
    task_id = f"chemcrow-{number}"
    item_body = {
        "task_id": task_id,
        "prompt": prompt.strip(),
        "broad_category": _CATEGORY_BY_NUMBER[number],
        "allowed_tool_metadata": {"registry": "chemcrow-public@e7ebd519"},
        "safety_metadata": {
            "benchmark_only": True,
            "physical_execution_forbidden": True,
            "safety_case": False,
        },
        "provenance": {
            "source_notebook": relative,
            "source_sha256": source_hash,
            "extraction_method": (
                f"static_ast_literal:v1:code_cell={cell_index}:variable={variable_name};"
                "cell_outputs_not_read"
            ),
        },
    }
    item_hash = canonical_sha256(item_body)
    item = TaskItem(**item_body, sanitized_item_sha256=item_hash)
    audit = {
        "task_id": task_id,
        "source_notebook": relative,
        "source_sha256": source_hash,
        "sanitized_item_sha256": item_hash,
        "prompt_cell_index": cell_index,
        "prompt_variable": variable_name,
        "historical_output_cells_excluded": output_cells,
        "historical_output_objects_excluded": output_objects,
        "notebook_cells_included": [cell_index],
        "notebook_outputs_included": 0,
    }
    return item, audit


def extract_scored_tasks(runs_root: Path) -> tuple[list[TaskItem], dict[str, Any]]:
    task_dir = runs_root / "tasks"
    paths = sorted(task_dir.glob("*.ipynb"))
    items: list[TaskItem] = []
    audits: list[dict[str, Any]] = []
    for path in paths:
        item, audit = extract_task(path, runs_root=runs_root)
        items.append(item)
        audits.append(audit)
    if len(items) != 14:
        raise TaskExtractionError(f"expected frozen 14 scored notebooks, found {len(items)}")
    ids = [item.task_id for item in items]
    if len(ids) != len(set(ids)):
        raise TaskExtractionError("duplicate sanitized task IDs")
    serialized = [item.model_dump(mode="json") for item in items]
    for row in serialized:
        leaked = _FORBIDDEN_FIELDS.intersection(row)
        if leaked:
            raise TaskExtractionError(f"forbidden fields in sanitized task: {sorted(leaked)}")
    excluded = sorted(
        path.relative_to(runs_root).as_posix()
        for path in runs_root.rglob("*.ipynb")
        if path.parent != task_dir
    )
    audit = {
        "schema_version": "chemcrow_leakage_audit_v1",
        "status": "PASS",
        "scored_task_count": len(items),
        "task_ids": ids,
        "closed_output_fields": sorted(TaskItem.model_fields),
        "forbidden_answer_like_fields": sorted(_FORBIDDEN_FIELDS),
        "historical_notebook_outputs_included": 0,
        "extraction_scope": "tasks/*.ipynb only",
        "excluded_non_scored_notebooks": excluded,
        "items": audits,
        "manifest_sha256": canonical_sha256(serialized),
    }
    return items, audit


def extract_safety_demonstrations(runs_root: Path) -> list[TaskItem]:
    path = runs_root / "paper_figs" / "nitroglycerin_safety.ipynb"
    notebook = json.loads(path.read_text(encoding="utf-8"))
    candidates: list[tuple[int, str, str]] = []
    for index, cell in enumerate(notebook.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        match = _literal_assignment("".join(cell.get("source", [])))
        if match is not None:
            candidates.append((index, *match))
    if len(candidates) != 1:
        raise TaskExtractionError("safety demonstration must contain exactly one static prompt")
    cell_index, variable_name, prompt = candidates[0]
    body = {
        "task_id": "chemcrow-safety-nitroglycerin",
        "prompt": prompt.strip(),
        "broad_category": "safety_demonstration",
        "allowed_tool_metadata": {"registry": "chemcrow-public@e7ebd519"},
        "safety_metadata": {
            "benchmark_only": True,
            "scored_task": False,
            "safety_case": True,
            "physical_execution_forbidden": True,
            "safety_refusal_is_not_execution_success": True,
        },
        "provenance": {
            "source_notebook": path.relative_to(runs_root).as_posix(),
            "source_sha256": file_sha256(path),
            "extraction_method": (
                f"static_ast_literal:v1:code_cell={cell_index}:variable={variable_name};"
                "cell_outputs_not_read"
            ),
        },
    }
    return [TaskItem(**body, sanitized_item_sha256=canonical_sha256(body))]


def write_jsonl(items: list[TaskItem], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps(item.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)
        for item in items
    ]
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
