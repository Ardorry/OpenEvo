from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.models import ArtifactKind
from openevo_chemcrow.three_artifact_models import ThreeArtifactPairResult

EXPECTED_PREFLIGHT_TASK_IDS = ("chemcrow-02", "chemcrow-05", "chemcrow-13")
_EXPECTED_PAIR_COUNT = len(EXPECTED_PREFLIGHT_TASK_IDS)
_EXPECTED_ARTIFACT_COUNT = _EXPECTED_PAIR_COUNT * 3


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{label} must be a JSON object")
    return payload


def _require_exact_int(payload: dict[str, Any], *, key: str, expected: int, label: str) -> None:
    value = payload.get(key)
    if type(value) is not int or value != expected:
        raise ValueError(f"{label} {key} must equal {expected}")


def _validate_completed_run_audit(path: Path) -> dict[str, Any]:
    audit = _json_object(path, label="completed-run audit")
    if (
        audit.get("schema_version") != "chemcrow_three_artifact_completed_run_audit_v1"
        or audit.get("status") != "PASS"
    ):
        raise ValueError("completed-run audit must be a three-artifact PASS authority")
    if audit.get("task_ids") != list(EXPECTED_PREFLIGHT_TASK_IDS):
        raise ValueError("completed-run audit task IDs must be exactly 02/05/13 in order")
    for key, expected in (
        ("task_count", _EXPECTED_PAIR_COUNT),
        ("unique_artifact_count", _EXPECTED_ARTIFACT_COUNT),
        ("independent_reflector_job_count", _EXPECTED_ARTIFACT_COUNT),
        ("artifact_registration_count", _EXPECTED_ARTIFACT_COUNT),
        ("sibling_isolation_evidence_count", _EXPECTED_ARTIFACT_COUNT),
        ("core_evolved_injection_receipt_count", _EXPECTED_PAIR_COUNT),
        ("reset_receipt_count", _EXPECTED_PAIR_COUNT),
        ("mock_or_fixture_observations", 0),
    ):
        _require_exact_int(audit, key=key, expected=expected, label="completed-run audit")
    expected_type_counts = {
        ArtifactKind.TEXT_MEMORY.value: _EXPECTED_PAIR_COUNT,
        ArtifactKind.SKILL_BUNDLE.value: _EXPECTED_PAIR_COUNT,
        ArtifactKind.AGENT_SYSTEM.value: _EXPECTED_PAIR_COUNT,
    }
    if audit.get("artifact_type_counts") != expected_type_counts:
        raise ValueError("completed-run audit artifact type counts must be exactly 3/3/3")
    experiment_id = audit.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise ValueError("completed-run audit experiment ID is absent")
    return audit


def _load_exact_pairs(
    *, run_root: Path, completed_run_audit: dict[str, Any]
) -> list[tuple[Path, ThreeArtifactPairResult]]:
    result_paths = sorted(run_root.rglob("pair.result.json"))
    if len(result_paths) != _EXPECTED_PAIR_COUNT:
        raise ValueError("run root must contain exactly three pair.result.json files")

    by_task: dict[str, tuple[Path, ThreeArtifactPairResult]] = {}
    for result_path in result_paths:
        pair = ThreeArtifactPairResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        if pair.task_id in by_task:
            raise ValueError(f"duplicate pair result for task: {pair.task_id}")
        if pair.task_id not in EXPECTED_PREFLIGHT_TASK_IDS:
            raise ValueError(f"unexpected preflight task in run root: {pair.task_id}")
        expected_pair_id = f"{completed_run_audit['experiment_id']}--{pair.task_id}"
        if pair.pair_id != expected_pair_id or result_path.parent.name != expected_pair_id:
            raise ValueError(
                f"pair path/identity differs from completed-run audit: {pair.task_id}"
            )
        reset_path = result_path.parent / "reset.receipt.json"
        if not reset_path.is_file() or file_sha256(reset_path) != pair.reset_receipt_sha256:
            raise ValueError(f"reset receipt is absent or differs: {pair.task_id}")
        by_task[pair.task_id] = (result_path, pair)

    if set(by_task) != set(EXPECTED_PREFLIGHT_TASK_IDS):
        raise ValueError("run pair task inventory must be exactly 02/05/13")

    ordered = [by_task[task_id] for task_id in EXPECTED_PREFLIGHT_TASK_IDS]
    receipts = [receipt for _, pair in ordered for receipt in pair.artifact_bundle.artifacts]
    job_ids = {receipt.reflector_job_id for receipt in receipts}
    run_ids = {receipt.reflector_run_id for receipt in receipts}
    artifact_ids = {receipt.artifact_id for receipt in receipts}
    if len(receipts) != _EXPECTED_ARTIFACT_COUNT:
        raise ValueError("three preflight pairs must contain exactly nine artifact receipts")
    if len(job_ids) != _EXPECTED_ARTIFACT_COUNT:
        raise ValueError("preflight must contain nine unique Reflector job IDs")
    if len(run_ids) != _EXPECTED_ARTIFACT_COUNT:
        raise ValueError("preflight must contain nine unique Reflector run IDs")
    if len(artifact_ids) != _EXPECTED_ARTIFACT_COUNT:
        raise ValueError("preflight must contain nine unique artifact IDs")
    if any(getattr(pair, "injection_receipt", None) is None for _, pair in ordered):
        raise ValueError("each preflight pair must contain one injection receipt")

    source_counts: dict[str, int] = {}
    for _, pair in ordered:
        for source, count in _observation_sources(pair).items():
            source_counts[source] = source_counts.get(source, 0) + count
    if source_counts.get("mock", 0) or source_counts.get("fixture", 0):
        raise ValueError("mock or fixture observations entered the preflight pairs")
    return ordered


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _score(value: Any) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    payload["total"] = sum(payload.values())
    return payload


def _artifact_content(
    connection: sqlite3.Connection,
    *,
    artifact_id: str,
    artifact_type: ArtifactKind,
) -> tuple[str, str]:
    row = connection.execute(
        "SELECT uri FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Core artifact is absent: {artifact_id}")
    path = Path(unquote(urlparse(str(row[0])).path))
    if artifact_type is ArtifactKind.SKILL_BUNDLE:
        path /= "SKILL.md"
    return path.read_text(encoding="utf-8"), str(path)


def _prohibition_line_count(content: str) -> int:
    pattern = re.compile(
        r"\b(?:do not|don't|never|refuse|avoid|omit|must not|cannot|can't|no actionable|no procedure|no shopping)\b",
        re.IGNORECASE,
    )
    return sum(bool(pattern.search(line)) for line in content.splitlines() if line.strip())


def _artifact_inventory(
    connection: sqlite3.Connection,
    pair: ThreeArtifactPairResult,
) -> list[dict[str, Any]]:
    result = []
    for receipt in pair.artifact_bundle.artifacts:
        content, source_path = _artifact_content(
            connection,
            artifact_id=receipt.artifact_id,
            artifact_type=receipt.artifact_type,
        )
        if file_sha256(Path(source_path)) != receipt.artifact_hash:
            raise ValueError(f"artifact content hash mismatch: {receipt.artifact_id}")
        result.append(
            {
                "reflector_job_id": receipt.reflector_job_id,
                "reflector_run_id": receipt.reflector_run_id,
                "prompt_sha256": receipt.prompt_hash,
                "native_template_sha256": receipt.system_prompt_hash,
                "artifact_id": receipt.artifact_id,
                "artifact_type": receipt.artifact_type.value,
                "content_sha256": receipt.artifact_hash,
                "content_size_bytes": len(content.encode("utf-8")),
                "content_word_count": len(content.split()),
                "prohibition_line_count": _prohibition_line_count(content),
                "content": content,
            }
        )
    return result


def _observation_sources(pair: ThreeArtifactPairResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trajectory in (pair.baseline, pair.evolved):
        for observation in trajectory.observations:
            source = str(observation.source)
            counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))


def _blind_total_delta(pair: ThreeArtifactPairResult) -> float:
    baseline = sum(pair.final_evaluation.baseline_scores.model_dump().values())
    evolved = sum(pair.final_evaluation.evolved_scores.model_dump().values())
    return evolved - baseline


def _mechanism_stats(
    connection: sqlite3.Connection,
    pair: ThreeArtifactPairResult,
) -> dict[str, Any]:
    artifacts = _artifact_inventory(connection, pair)
    return {
        "artifact_word_count": sum(item["content_word_count"] for item in artifacts),
        "prohibition_line_count": sum(item["prohibition_line_count"] for item in artifacts),
        "blind_total_delta": _blind_total_delta(pair),
        "blind_winner": pair.final_evaluation.winner,
    }


def _validate_mechanism_stats(value: Any, *, task_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"historical mechanism stats are not an object: {task_id}")
    word_count = value.get("artifact_word_count")
    prohibition_count = value.get("prohibition_line_count")
    blind_delta = value.get("blind_total_delta")
    if type(word_count) is not int or word_count < 0:
        raise ValueError(f"historical artifact word count is invalid: {task_id}")
    if type(prohibition_count) is not int or prohibition_count < 0:
        raise ValueError(f"historical prohibition count is invalid: {task_id}")
    if isinstance(blind_delta, bool) or not isinstance(blind_delta, (int, float)):
        raise TypeError(f"historical blind delta is invalid: {task_id}")
    winner = value.get("blind_winner")
    if winner is not None and not isinstance(winner, str):
        raise ValueError(f"historical blind winner is invalid: {task_id}")
    return {
        "artifact_word_count": word_count,
        "prohibition_line_count": prohibition_count,
        "blind_total_delta": blind_delta,
        "blind_winner": winner,
    }


def _historical_stats_from_report(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    report = _json_object(path, label="historical preflight report")
    if (
        report.get("schema_version")
        not in {
            "chemcrow_core_native_preflight_report_v1",
            "chemcrow_core_native_preflight_report_v2",
        }
        or report.get("status") != "PASS"
        or report.get("task_ids") != list(EXPECTED_PREFLIGHT_TASK_IDS)
    ):
        raise ValueError("historical report is not a frozen PASS authority for tasks 02/05/13")
    rows = report.get("v4_v5_preflight_comparison")
    if not isinstance(rows, list):
        raise TypeError("historical report comparison rows are absent")
    by_task: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
            raise TypeError("historical report comparison row is invalid")
        task_id = row["task_id"]
        if task_id in by_task:
            raise ValueError(f"duplicate historical comparison row: {task_id}")
        by_task[task_id] = _validate_mechanism_stats(row.get("v4"), task_id=task_id)
    if set(by_task) != set(EXPECTED_PREFLIGHT_TASK_IDS):
        raise ValueError("historical report comparison task inventory differs from 02/05/13")
    return by_task, {
        "status": "AVAILABLE",
        "source_kind": "frozen_report_json",
        "source_path": str(path),
        "source_sha256": file_sha256(path),
        "raw_artifacts_revalidated": False,
        "limitations": [
            "Historical v4 mechanism statistics were read from a frozen report.",
            "This v5 report did not reread or rehash raw v4 artifact payloads.",
        ],
    }


def _historical_stats_from_raw(
    *, run_root: Path, evolution_db: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{evolution_db}?mode=ro", uri=True)
    try:
        by_task: dict[str, dict[str, Any]] = {}
        for task_id in EXPECTED_PREFLIGHT_TASK_IDS:
            matches = list(run_root.glob(f"*--{task_id}/pair.result.json"))
            if len(matches) != 1:
                raise ValueError(f"historical raw pair authority is not unique: {task_id}")
            pair = ThreeArtifactPairResult.model_validate_json(
                matches[0].read_text(encoding="utf-8")
            )
            if pair.task_id != task_id:
                raise ValueError(f"historical raw pair task identity differs: {task_id}")
            by_task[task_id] = _mechanism_stats(connection, pair)
    finally:
        connection.close()
    return by_task, {
        "status": "AVAILABLE",
        "source_kind": "raw_historical_authority",
        "source_path": str(run_root),
        "source_sha256": None,
        "historical_evolution_db": str(evolution_db),
        "historical_evolution_db_sha256": file_sha256(evolution_db),
        "raw_artifacts_revalidated": True,
        "limitations": [],
    }


def _load_historical_mechanism_stats(
    args: argparse.Namespace,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any]]:
    report_path = getattr(args, "historical_report_json", None)
    run_root = getattr(args, "historical_run_root", None)
    evolution_db = getattr(args, "historical_evolution_db", None)
    if report_path is not None:
        if evolution_db is not None:
            raise ValueError(
                "historical report JSON cannot be combined with a historical evolution DB"
            )
        if report_path.is_file():
            return _historical_stats_from_report(report_path)
        return None, {
            "status": "UNAVAILABLE",
            "source_kind": "frozen_report_json",
            "source_path": str(report_path),
            "source_sha256": None,
            "raw_artifacts_revalidated": False,
            "limitations": [
                "The requested frozen historical report was unavailable.",
                "The v5 preflight evidence remains complete; v4 comparison fields are null.",
            ],
        }
    if run_root is not None or evolution_db is not None:
        if (
            run_root is not None
            and evolution_db is not None
            and run_root.is_dir()
            and evolution_db.is_file()
        ):
            return _historical_stats_from_raw(
                run_root=run_root,
                evolution_db=evolution_db,
            )
        return None, {
            "status": "UNAVAILABLE",
            "source_kind": "raw_historical_authority",
            "source_path": str(run_root) if run_root is not None else None,
            "source_sha256": None,
            "historical_evolution_db": (str(evolution_db) if evolution_db is not None else None),
            "raw_artifacts_revalidated": False,
            "limitations": [
                "Raw historical run and SQLite authorities were not both available.",
                "The v5 preflight evidence remains complete; v4 comparison fields are null.",
            ],
        }
    return None, {
        "status": "UNAVAILABLE",
        "source_kind": "none",
        "source_path": None,
        "source_sha256": None,
        "raw_artifacts_revalidated": False,
        "limitations": [
            "No historical comparison authority was supplied.",
            "The v5 preflight evidence remains complete; v4 comparison fields are null.",
        ],
    }


def _pair_payload(
    *,
    pair: ThreeArtifactPairResult,
    task_prompt: str,
    item_root: Path,
    connection: sqlite3.Connection,
    semantic_audit: dict[str, Any] | None,
) -> dict[str, Any]:
    artifacts = _artifact_inventory(connection, pair)
    reset = json.loads((item_root / "reset.receipt.json").read_text(encoding="utf-8"))
    return {
        "task_id": pair.task_id,
        "task_prompt": task_prompt,
        "task_prompt_sha256": pair.task_prompt_hash,
        "g1": {
            "run_id": pair.baseline.run_id,
            "final_answer_sha256": _text_sha256(pair.baseline.answer),
            "internal_evaluator_score": _score(pair.baseline_internal_evaluation.scores),
            "internal_evaluator_critique_sha256": canonical_sha256(
                pair.baseline_internal_evaluation.model_dump(mode="json")
            ),
        },
        "reflectors": artifacts,
        "artifact_bundle": {
            "protocol": pair.artifact_bundle.protocol,
            "input_evidence_sha256": pair.artifact_bundle.input_evidence_hash,
            "byte_identical_pairs": pair.artifact_bundle.byte_identical_pairs,
            "normalized_identical_pairs": pair.artifact_bundle.normalized_identical_pairs,
            "near_duplicate_pairs": pair.artifact_bundle.near_duplicate_pairs,
            "baseline_answer_copy": pair.artifact_bundle.baseline_answer_copy,
        },
        "g2": {
            "run_id": pair.evolved.run_id,
            "final_answer_sha256": _text_sha256(pair.evolved.answer),
            "internal_evaluator_score": _score(pair.evolved_internal_evaluation.scores),
            "internal_evaluator_critique_sha256": canonical_sha256(
                pair.evolved_internal_evaluation.model_dump(mode="json")
            ),
        },
        "blind_evaluator": pair.final_evaluation.model_dump(mode="json"),
        "g2_injection_receipt": pair.injection_receipt.model_dump(mode="json"),
        "reset_receipt_sha256": pair.reset_receipt_sha256,
        "reset_receipt": reset,
        "observation_source_counts": _observation_sources(pair),
        "semantic_audit": semantic_audit,
    }


def _markdown(payload: dict[str, Any]) -> str:
    historical = payload["historical_comparison_authority"]
    lines = [
        "# ChemCrow Core-native preflight 02/05/13",
        "",
        "All semantic audit fields were added after G2, pair seal, and reset. They were not fed to Candidate or Reflectors.",
        "",
        "## Engineering summary",
        "",
        f"- Status: `{payload['status']}`",
        f"- Tasks: `{', '.join(payload['task_ids'])}`",
        f"- Reflector jobs / unique artifacts: `{payload['reflector_job_count']} / {payload['unique_artifact_count']}`",
        f"- Injection / reset receipts: `{payload['injection_receipt_count']} / {payload['reset_receipt_count']}`",
        f"- MOCK / fixture: `{payload['mock_count']} / {payload['fixture_count']}`",
        f"- Observation sources: `{json.dumps(payload['observation_source_counts'], sort_keys=True)}`",
        f"- Completed-run audit: `{payload['completed_run_audit']['sha256']}`",
        f"- Historical comparison source: `{historical['status']} / {historical['source_kind']}`",
        "",
    ]
    for task in payload["tasks"]:
        lines.extend([f"## {task['task_id']}", "", "### Task", "", task["task_prompt"], ""])
        semantic = task["semantic_audit"]
        if semantic:
            lines.extend(["### Explicit obligations", ""])
            lines.extend(f"- {item}" for item in semantic["task_obligations"])
            lines.extend(["", "### G1", ""])
        else:
            lines.extend(["### G1", ""])
        lines.extend(
            [
                f"- Final-answer SHA256: `{task['g1']['final_answer_sha256']}`",
                f"- Internal score: `{json.dumps(task['g1']['internal_evaluator_score'], sort_keys=True)}`",
                f"- Evaluator critique SHA256: `{task['g1']['internal_evaluator_critique_sha256']}`",
                "",
                "### Exact native artifacts",
                "",
            ]
        )
        for artifact in task["reflectors"]:
            lines.extend(
                [
                    f"#### {artifact['artifact_type']}",
                    "",
                    f"- Job: `{artifact['reflector_job_id']}`",
                    f"- Run: `{artifact['reflector_run_id']}`",
                    f"- Prompt/template hashes: `{artifact['prompt_sha256']}` / `{artifact['native_template_sha256']}`",
                    f"- Artifact/content: `{artifact['artifact_id']}` / `{artifact['content_sha256']}`",
                    f"- Bytes/words/prohibition-lines: `{artifact['content_size_bytes']} / {artifact['content_word_count']} / {artifact['prohibition_line_count']}`",
                    "",
                    "````markdown",
                    artifact["content"].rstrip("\n"),
                    "````",
                    "",
                ]
            )
        if semantic:
            lines.extend(["### Post-generation semantic audit", ""])
            lines.append("| Property | Finding |")
            lines.append("|---|---|")
            for key, value in semantic["task_level"].items():
                lines.append(f"| `{key}` | `{str(value).lower()}` |")
            lines.extend(["", semantic["summary"], ""])
        lines.extend(
            [
                "### G2 and seals",
                "",
                f"- Final-answer SHA256: `{task['g2']['final_answer_sha256']}`",
                f"- Internal score: `{json.dumps(task['g2']['internal_evaluator_score'], sort_keys=True)}`",
                f"- Blind result: `{json.dumps(task['blind_evaluator'], sort_keys=True)}`",
                f"- Injection receipt: `{json.dumps(task['g2_injection_receipt'], sort_keys=True)}`",
                f"- Reset receipt SHA256: `{task['reset_receipt_sha256']}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Historical v4 vs Core-native v5 preflight mechanism table",
            "",
            "Prohibition-line counts are a mechanical descriptive count; task-conflict judgments above are semantic post-generation audits.",
            "",
            "| Task | v4 words | v5 words | v4 prohibitions | v5 prohibitions | v4 blind delta | v5 blind delta | Mechanism finding |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in payload["v4_v5_preflight_comparison"]:
        historical_stats = item["v4"]
        historical_words = (
            historical_stats["artifact_word_count"] if historical_stats is not None else "n/a"
        )
        historical_prohibitions = (
            historical_stats["prohibition_line_count"] if historical_stats is not None else "n/a"
        )
        historical_delta = (
            f"{historical_stats['blind_total_delta']:+g}"
            if historical_stats is not None
            else "n/a"
        )
        lines.append(
            f"| `{item['task_id']}` | {historical_words} | {item['v5']['artifact_word_count']} | "
            f"{historical_prohibitions} | {item['v5']['prohibition_line_count']} | "
            f"{historical_delta} | {item['v5']['blind_total_delta']:+g} | "
            f"{item['mechanism_finding']} |"
        )
    if historical["limitations"]:
        lines.extend(["", "Historical comparison limitations:", ""])
        lines.extend(f"- {item}" for item in historical["limitations"])
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    completed_run_audit = _validate_completed_run_audit(args.completed_run_audit)
    if args.output_json.exists() or args.output_markdown.exists():
        raise ValueError("refusing to overwrite an existing preflight report")
    bound_pairs = _load_exact_pairs(
        run_root=args.run_root,
        completed_run_audit=completed_run_audit,
    )
    task_rows = [
        json.loads(line)
        for line in args.tasks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    task_prompts = {row["task_id"]: row["prompt"] for row in task_rows}
    missing_prompts = sorted(set(EXPECTED_PREFLIGHT_TASK_IDS) - set(task_prompts))
    if missing_prompts:
        raise ValueError(f"task manifest is missing preflight tasks: {missing_prompts}")
    semantic_payload = _json_object(args.semantic_audit, label="semantic audit")
    semantic = semantic_payload.get("tasks")
    if not isinstance(semantic, dict):
        raise TypeError("semantic audit tasks must be an object")
    missing_semantic = sorted(set(EXPECTED_PREFLIGHT_TASK_IDS) - set(semantic))
    if missing_semantic:
        raise ValueError(f"semantic audit is missing preflight tasks: {missing_semantic}")
    historical_stats, historical_authority = _load_historical_mechanism_stats(args)
    connection = sqlite3.connect(f"file:{args.evolution_db}?mode=ro", uri=True)
    try:
        tasks = []
        pairs_by_task: dict[str, ThreeArtifactPairResult] = {}
        for result_path, pair in bound_pairs:
            pairs_by_task[pair.task_id] = pair
            tasks.append(
                _pair_payload(
                    pair=pair,
                    task_prompt=task_prompts[pair.task_id],
                    item_root=result_path.parent,
                    connection=connection,
                    semantic_audit=semantic[pair.task_id],
                )
            )
        all_artifacts = [artifact for task in tasks for artifact in task["reflectors"]]
        source_counts: dict[str, int] = {}
        for task in tasks:
            for source, count in task["observation_source_counts"].items():
                source_counts[source] = source_counts.get(source, 0) + count
        comparison = []
        for task_id in EXPECTED_PREFLIGHT_TASK_IDS:
            mechanism_finding = (
                semantic[task_id]["v4_v5_comparison"]
                if historical_stats is not None
                else "Not evaluated because historical comparison authority was unavailable."
            )
            comparison.append(
                {
                    "task_id": task_id,
                    "v4": historical_stats[task_id] if historical_stats is not None else None,
                    "v5": _mechanism_stats(connection, pairs_by_task[task_id]),
                    "mechanism_finding": mechanism_finding,
                }
            )
    finally:
        connection.close()
    payload = {
        "schema_version": "chemcrow_core_native_preflight_report_v2",
        "status": "PASS",
        "run_root": str(args.run_root),
        "task_ids": list(EXPECTED_PREFLIGHT_TASK_IDS),
        "task_count": len(tasks),
        "pair_count": len(tasks),
        "reflector_job_count": len({item["reflector_job_id"] for item in all_artifacts}),
        "reflector_run_count": len({item["reflector_run_id"] for item in all_artifacts}),
        "unique_artifact_count": len({item["artifact_id"] for item in all_artifacts}),
        "injection_receipt_count": len(tasks),
        "reset_receipt_count": len(tasks),
        "mock_count": source_counts.get("mock", 0),
        "fixture_count": source_counts.get("fixture", 0),
        "observation_source_counts": dict(sorted(source_counts.items())),
        "post_generation_semantic_audit": True,
        "completed_run_audit": {
            "path": str(args.completed_run_audit),
            "sha256": file_sha256(args.completed_run_audit),
            "schema_version": completed_run_audit["schema_version"],
            "status": completed_run_audit["status"],
        },
        "historical_comparison_authority": historical_authority,
        "v4_v5_preflight_comparison": comparison,
        "tasks": tasks,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    with args.output_json.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    with args.output_markdown.open("x", encoding="utf-8") as handle:
        handle.write(_markdown(payload))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--evolution-db", type=Path, required=True)
    parser.add_argument("--completed-run-audit", type=Path, required=True)
    history = parser.add_mutually_exclusive_group()
    history.add_argument("--historical-report-json", type=Path)
    history.add_argument("--historical-run-root", type=Path)
    parser.add_argument("--historical-evolution-db", type=Path)
    parser.add_argument("--semantic-audit", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    payload = build_report(args)
    print(json.dumps({"status": payload["status"], "task_count": payload["task_count"]}))


if __name__ == "__main__":
    main()
