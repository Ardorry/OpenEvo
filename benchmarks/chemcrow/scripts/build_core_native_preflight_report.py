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
        lines.append(
            f"| `{item['task_id']}` | {item['v4']['artifact_word_count']} | {item['v5']['artifact_word_count']} | "
            f"{item['v4']['prohibition_line_count']} | {item['v5']['prohibition_line_count']} | "
            f"{item['v4']['blind_total_delta']:+g} | {item['v5']['blind_total_delta']:+g} | "
            f"{item['mechanism_finding']} |"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    task_rows = [
        json.loads(line)
        for line in args.tasks.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    task_prompts = {row["task_id"]: row["prompt"] for row in task_rows}
    semantic = json.loads(args.semantic_audit.read_text(encoding="utf-8"))["tasks"]
    connection = sqlite3.connect(f"file:{args.evolution_db}?mode=ro", uri=True)
    tasks = []
    pairs_by_task: dict[str, ThreeArtifactPairResult] = {}
    for result_path in sorted(args.run_root.glob("*/pair.result.json")):
        pair = ThreeArtifactPairResult.model_validate_json(
            result_path.read_text(encoding="utf-8")
        )
        pairs_by_task[pair.task_id] = pair
        tasks.append(
            _pair_payload(
                pair=pair,
                task_prompt=task_prompts[pair.task_id],
                item_root=result_path.parent,
                connection=connection,
                semantic_audit=semantic.get(pair.task_id),
            )
        )
    all_artifacts = [artifact for task in tasks for artifact in task["reflectors"]]
    source_counts: dict[str, int] = {}
    for task in tasks:
        for source, count in task["observation_source_counts"].items():
            source_counts[source] = source_counts.get(source, 0) + count
    comparison = []
    for task_id in [task["task_id"] for task in tasks]:
        historical_path = next(
            args.historical_run_root.glob(f"*--{task_id}/pair.result.json")
        )
        historical = ThreeArtifactPairResult.model_validate_json(
            historical_path.read_text(encoding="utf-8")
        )
        comparison.append(
            {
                "task_id": task_id,
                "v4": _mechanism_stats(connection, historical),
                "v5": _mechanism_stats(connection, pairs_by_task[task_id]),
                "mechanism_finding": semantic[task_id]["v4_v5_comparison"],
            }
        )
    connection.close()
    payload = {
        "schema_version": "chemcrow_core_native_preflight_report_v1",
        "status": "PASS",
        "run_root": str(args.run_root),
        "task_ids": [task["task_id"] for task in tasks],
        "task_count": len(tasks),
        "reflector_job_count": len({item["reflector_job_id"] for item in all_artifacts}),
        "unique_artifact_count": len({item["artifact_id"] for item in all_artifacts}),
        "injection_receipt_count": len(tasks),
        "reset_receipt_count": len(tasks),
        "mock_count": source_counts.get("mock", 0),
        "fixture_count": source_counts.get("fixture", 0),
        "observation_source_counts": dict(sorted(source_counts.items())),
        "post_generation_semantic_audit": True,
        "v4_v5_preflight_comparison": comparison,
        "tasks": tasks,
    }
    args.output_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output_markdown.write_text(_markdown(payload), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--evolution-db", type=Path, required=True)
    parser.add_argument("--historical-run-root", type=Path, required=True)
    parser.add_argument("--semantic-audit", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    payload = build_report(args)
    print(json.dumps({"status": payload["status"], "task_count": payload["task_count"]}))


if __name__ == "__main__":
    main()
