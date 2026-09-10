from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from openevo_chemcrow.artifact_similarity import (
    ARTIFACT_TYPES,
    SEMANTIC_FLAGS,
    extract_semantic_units,
)
from openevo_chemcrow.hashing import canonical_sha256, file_sha256

_ARTIFACT_DIRECTORIES = {
    "text_memory": "text_memory",
    "skill_bundle": "skills",
    "agent_system": "agent_system",
}


def _json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def _artifact_content(
    *, artifact_root: Path, artifact_type: str, artifact_id: str
) -> str:
    manifest_path = (
        artifact_root
        / "artifacts"
        / _ARTIFACT_DIRECTORIES[artifact_type]
        / artifact_id
        / "manifest.json"
    )
    wrapper = _json_object(manifest_path)
    if wrapper.get("artifact_id") != artifact_id or wrapper.get("type") != artifact_type:
        raise ValueError(f"artifact manifest authority differs: {artifact_id}")
    manifest = wrapper.get("manifest")
    if not isinstance(manifest, dict):
        raise TypeError(f"artifact inner manifest is absent: {artifact_id}")
    parsed = urlparse(str(wrapper.get("uri", "")))
    if parsed.scheme != "file" or parsed.netloc:
        raise ValueError(f"artifact URI is not a local file: {artifact_id}")
    content_path = Path(unquote(parsed.path))
    if artifact_type == "skill_bundle":
        if manifest.get("entrypoint") != "SKILL.md":
            raise ValueError(f"skill entrypoint differs: {artifact_id}")
        content_path /= "SKILL.md"
    resolved = content_path.resolve(strict=True)
    resolved.relative_to(artifact_root.resolve(strict=True))
    before = resolved.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"artifact content is not a link-count-one regular file: {artifact_id}")
    payload = resolved.read_bytes()
    after = resolved.stat(follow_symlinks=False)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f"artifact changed while reading: {artifact_id}")
    return payload.decode("utf-8")


def _tasks(path: Path) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        tasks[item["task_id"]] = item
    return tasks


def _sort_key(task_id: str) -> int:
    return int(task_id.rsplit("-", 1)[-1])


def prepare(
    *,
    run_root: Path,
    artifact_root: Path,
    task_manifest: Path,
    obligation_source: Path,
    output_dir: Path,
) -> dict:
    run_root = run_root.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    tasks = _tasks(task_manifest.resolve(strict=True))
    prior_annotations = _json_object(obligation_source.resolve(strict=True))["tasks"]
    result_paths = sorted(
        run_root.glob("*/artifact.study.result.json"),
        key=lambda path: _sort_key(_json_object(path)["task_id"]),
    )
    if len(result_paths) != 14:
        raise ValueError(f"expected 14 generation results, found {len(result_paths)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError(f"review output directory is not empty: {output_dir}")

    annotation_tasks: dict[str, dict] = {}
    unit_inventory: list[dict] = []
    lines = [
        "# Full-worker artifact manual-review worksheet",
        "",
        (
            "Classification rule: General means reusable chemistry background or work behavior "
            "across many tasks; Hybrid combines such a rule with current-task chemistry; "
            "Task-specific depends on the current target, reaction, property, safety category, "
            "or explicit obligation. Repetition within one task is not evidence of Generality."
        ),
        "",
    ]
    global_index = 0
    for result_path in result_paths:
        result = _json_object(result_path)
        task_id = result["task_id"]
        task = tasks[task_id]
        obligations = prior_annotations[task_id]["task_obligations"]
        task_start = global_index
        lines.extend([f"## {task_id}", "", "### Prompt", "", task["prompt"], ""])
        lines.extend(["### Obligations", ""])
        lines.extend(f"- {obligation}" for obligation in obligations)
        artifact_findings: dict[str, dict] = {}
        receipts = {
            item["artifact_type"]: item for item in result["artifact_bundle"]["artifacts"]
        }
        for artifact_type in ARTIFACT_TYPES:
            receipt = receipts[artifact_type]
            content = _artifact_content(
                artifact_root=artifact_root,
                artifact_type=artifact_type,
                artifact_id=receipt["artifact_id"],
            )
            units = extract_semantic_units(content)
            lines.extend(
                [
                    "",
                    f"### {artifact_type} — {receipt['artifact_id']}",
                    "",
                    "#### Exact native content",
                    "",
                    "```markdown",
                    content.rstrip(),
                    "```",
                    "",
                    "#### Semantic units",
                    "",
                ]
            )
            for local_index, unit in enumerate(units):
                unit_inventory.append(
                    {
                        "global_unit_index": global_index,
                        "task_id": task_id,
                        "artifact_type": artifact_type,
                        "artifact_id": receipt["artifact_id"],
                        "local_unit_index": local_index,
                        "word_count": unit.word_count,
                        "content": unit.text,
                    }
                )
                lines.append(f"- [{global_index}] {unit.text}")
                global_index += 1
            artifact_findings[artifact_type] = {
                **{flag: False for flag in SEMANTIC_FLAGS},
                "reason": "REVIEW_REQUIRED",
            }
        annotation_tasks[task_id] = {
            "task_prompt": task["prompt"],
            "task_obligations": obligations,
            "task_level": {flag: False for flag in SEMANTIC_FLAGS},
            "summary": "REVIEW_REQUIRED",
            "artifact_findings": artifact_findings,
            "unit_range": [task_start, global_index - 1],
        }

    draft = {
        "schema_version": "chemcrow_core_full_worker_manual_semantic_annotations_v1",
        "status": "DRAFT_REVIEW_REQUIRED",
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "criterion_note": (
            "Post-generation manual audit only; semantic flags and reasons must be reviewed "
            "before the status can become FULL_CORPUS_ARTIFACT_AUDIT_COMPLETE."
        ),
        "tasks": annotation_tasks,
    }
    inventory = {
        "schema_version": "chemcrow_manual_scope_review_inventory_v1",
        "status": "DRAFT_REVIEW_REQUIRED",
        "semantic_unit_count": len(unit_inventory),
        "semantic_unit_inventory_sha256": canonical_sha256(
            [
                {
                    "global_unit_index": row["global_unit_index"],
                    "artifact_id": row["artifact_id"],
                    "local_unit_index": row["local_unit_index"],
                    "word_count": row["word_count"],
                    "content": row["content"],
                }
                for row in unit_inventory
            ]
        ),
        "units": unit_inventory,
    }
    worksheet_path = output_dir / "artifact_review_worksheet.md"
    draft_path = output_dir / "artifact_semantic_annotations.draft.json"
    inventory_path = output_dir / "semantic_unit_inventory.draft.json"
    worksheet_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    draft_path.write_text(json.dumps(draft, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "DRAFT_REVIEW_PREPARED",
        "task_count": len(annotation_tasks),
        "artifact_count": len(annotation_tasks) * len(ARTIFACT_TYPES),
        "semantic_unit_count": len(unit_inventory),
        "worksheet": str(worksheet_path),
        "worksheet_sha256": file_sha256(worksheet_path),
        "draft_annotations": str(draft_path),
        "unit_inventory": str(inventory_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--obligation-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                run_root=args.run_root,
                artifact_root=args.artifact_root,
                task_manifest=args.task_manifest,
                obligation_source=args.obligation_source,
                output_dir=args.output_dir,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
