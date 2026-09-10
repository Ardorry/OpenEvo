from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from openevo_chemcrow.artifact_similarity import ARTIFACT_TYPES, SEMANTIC_FLAGS
from openevo_chemcrow.hashing import file_sha256

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


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _task_sort_key(task_id: str) -> int:
    return int(task_id.rsplit("-", 1)[-1])


def _read_regular_text(path: Path, *, allowed_root: Path) -> tuple[str, str]:
    path = path.resolve(strict=True)
    allowed_root = allowed_root.resolve(strict=True)
    path.relative_to(allowed_root)
    metadata = path.stat(follow_symlinks=False)
    _require(stat.S_ISREG(metadata.st_mode), f"artifact content is not regular: {path}")
    _require(metadata.st_nlink == 1, f"artifact content link count differs: {path}")
    payload = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    _require(
        (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        f"artifact content changed while reading: {path}",
    )
    return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest()


def _artifact_content(
    *,
    artifact_root: Path,
    artifact_type: str,
    artifact_id: str,
) -> tuple[str, str, Path, dict]:
    manifest_path = (
        artifact_root
        / "artifacts"
        / _ARTIFACT_DIRECTORIES[artifact_type]
        / artifact_id
        / "manifest.json"
    )
    wrapper = _json_object(manifest_path)
    _require(wrapper.get("artifact_id") == artifact_id, "artifact manifest ID differs")
    _require(wrapper.get("type") == artifact_type, "artifact manifest type differs")
    manifest = wrapper.get("manifest")
    _require(isinstance(manifest, dict), "artifact inner manifest is absent")
    uri = wrapper.get("uri")
    parsed = urlparse(uri if isinstance(uri, str) else "")
    _require(parsed.scheme == "file" and not parsed.netloc, "artifact URI is not local file")
    uri_path = Path(unquote(parsed.path))
    if artifact_type == "skill_bundle":
        entrypoint = manifest.get("entrypoint")
        _require(entrypoint == "SKILL.md", "skill entrypoint differs")
        content_path = uri_path / entrypoint
    else:
        content_path = uri_path
    content, content_sha256 = _read_regular_text(content_path, allowed_root=artifact_root)
    return content, content_sha256, manifest_path.resolve(), wrapper


def build_audit(
    *,
    run_root: Path,
    artifact_root: Path,
    annotations_path: Path,
) -> dict:
    run_root = run_root.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    annotations = _json_object(annotations_path.resolve(strict=True))
    _require(
        annotations.get("status") == "FULL_CORPUS_ARTIFACT_AUDIT_COMPLETE",
        "manual artifact annotations are not complete",
    )
    annotated_tasks = annotations.get("tasks")
    _require(isinstance(annotated_tasks, dict), "manual task annotations are absent")
    result_paths = sorted(
        run_root.glob("*/artifact.study.result.json"),
        key=lambda path: _task_sort_key(_json_object(path)["task_id"]),
    )
    _require(len(result_paths) == 14, f"expected 14 sealed task results, found {len(result_paths)}")
    tasks: dict[str, dict] = {}
    artifact_ids: set[str] = set()
    job_ids: set[str] = set()
    for result_path in result_paths:
        result = _json_object(result_path)
        task_id = result.get("task_id")
        _require(isinstance(task_id, str) and task_id, "task result ID is absent")
        boundary_path = result_path.parent / "generation.boundary.receipt.json"
        reset_path = result_path.parent / "reset.receipt.json"
        boundary = _json_object(boundary_path)
        reset = _json_object(reset_path)
        _require(boundary.get("status") == "G1_AND_ARTIFACTS_SEALED", "boundary is unsealed")
        _require(
            boundary.get("g2_dispatched") is False
            and boundary.get("g2_evaluator_dispatched") is False
            and boundary.get("final_evaluator_dispatched") is False,
            "generation-only boundary dispatched a post-artifact evaluator",
        )
        _require(
            reset.get("runtime_context_after") == "bare_s0"
            and reset.get("active_artifact_ids_after") == [],
            "task-local reset differs",
        )
        annotation = annotated_tasks.get(task_id)
        _require(isinstance(annotation, dict), f"manual task annotation is absent: {task_id}")
        artifact_annotations = annotation.get("artifact_findings")
        _require(
            isinstance(artifact_annotations, dict)
            and set(artifact_annotations) == set(ARTIFACT_TYPES),
            f"manual artifact inventory differs: {task_id}",
        )
        receipts = {
            item["artifact_type"]: item
            for item in result["artifact_bundle"]["artifacts"]
        }
        _require(set(receipts) == set(ARTIFACT_TYPES), f"artifact inventory differs: {task_id}")
        findings: dict[str, dict] = {}
        for artifact_type in ARTIFACT_TYPES:
            receipt = receipts[artifact_type]
            manual = artifact_annotations[artifact_type]
            _require(isinstance(manual, dict), "manual artifact finding is invalid")
            flags = {flag: manual.get(flag) for flag in SEMANTIC_FLAGS}
            _require(
                all(isinstance(value, bool) for value in flags.values()),
                f"manual semantic flags differ: {task_id}/{artifact_type}",
            )
            artifact_id = receipt["artifact_id"]
            job_id = receipt["reflector_job_id"]
            _require(artifact_id not in artifact_ids, f"duplicate artifact ID: {artifact_id}")
            _require(job_id not in job_ids, f"duplicate Reflector job ID: {job_id}")
            artifact_ids.add(artifact_id)
            job_ids.add(job_id)
            content, content_sha256, manifest_path, wrapper = _artifact_content(
                artifact_root=artifact_root,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
            )
            _require(
                receipt["artifact_hash"] == content_sha256,
                f"registered artifact hash differs: {task_id}/{artifact_type}",
            )
            inner = wrapper["manifest"]
            _require(
                inner.get("reflector_prompt_profile") == "core_full_worker_v1"
                and inner.get("sibling_outputs_visible") is False,
                f"full-worker provenance differs: {task_id}/{artifact_type}",
            )
            findings[artifact_type] = {
                "artifact_type": artifact_type,
                "artifact_id": artifact_id,
                "job_id": job_id,
                "reflector_run_id": receipt["reflector_run_id"],
                "reflector_attempt_run_ids": receipt.get("reflector_attempt_run_ids", []),
                "prompt_sha256": receipt["prompt_hash"],
                "content": content,
                "content_bytes": len(content.encode("utf-8")),
                "content_sha256": content_sha256,
                "registered_artifact_hash": receipt["artifact_hash"],
                "exact_native_content_preserved": True,
                "source_manifest_path": str(manifest_path),
                "source_manifest_sha256": file_sha256(manifest_path),
                "reason": manual.get("reason"),
                **flags,
            }
        task_flags = annotation.get("task_level")
        _require(
            isinstance(task_flags, dict)
            and set(task_flags) == set(SEMANTIC_FLAGS)
            and all(isinstance(task_flags[flag], bool) for flag in SEMANTIC_FLAGS),
            f"manual task-level flags differ: {task_id}",
        )
        tasks[task_id] = {
            "task_prompt": annotation.get("task_prompt"),
            "task_obligations": annotation.get("task_obligations"),
            "source_generation_result_path": str(result_path.resolve()),
            "source_generation_result_sha256": file_sha256(result_path),
            "source_boundary_sha256": file_sha256(boundary_path),
            "source_reset_sha256": file_sha256(reset_path),
            "task_level": task_flags,
            "artifact_findings": findings,
            "summary": annotation.get("summary"),
        }
    _require(set(tasks) == set(annotated_tasks), "manual annotation task inventory differs")
    return {
        "schema_version": "chemcrow_core_native_post_generation_semantic_audit_v1",
        "status": "POST_GENERATION_AUDIT_COMPLETE",
        "execution_scope": "g1_and_artifact_generation_only",
        "reflector_prompt_profile": "core_full_worker_v1",
        "created_after_all_pairs_sealed": True,
        "fed_to_candidate_or_reflector": False,
        "model_calls_for_semantic_audit": 0,
        "run_root": str(run_root),
        "artifact_root": str(artifact_root),
        "manual_annotations_path": str(annotations_path.resolve()),
        "manual_annotations_sha256": file_sha256(annotations_path.resolve()),
        "task_count": len(tasks),
        "artifact_count": len(artifact_ids),
        "unique_artifact_count": len(artifact_ids),
        "unique_reflector_job_count": len(job_ids),
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit = build_audit(
        run_root=args.run_root,
        artifact_root=args.artifact_root,
        annotations_path=args.annotations,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": audit["status"], "artifact_count": audit["artifact_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
