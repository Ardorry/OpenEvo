"""Rejudge sealed ResearchClawBench outputs without Candidate/Evolution calls.

This module only scores already-sealed source workspaces.  It never invokes a
Candidate port or an Evolution port, never edits the source workspaces, and
writes every result into an independent rejudge namespace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .artifact_validator import candidate_artifact_root_sha256
from .community_evaluator import (
    REQUESTED_JUDGE_PROVIDER,
    run_community_evaluator_detailed,
    run_judge_probe_subprocess,
)
from .hashing import iter_regular_files, sha256_file
from .minimal_per_item_runner import CANARY_TASK_ID
from .run_manifest import atomic_write_json
from .training_state_store import TrainingStateStore

REJUDGE_SCHEMA_VERSION = "openevo.researchclawbench.rejudge_sealed.v1"


class RejudgeSealedError(RuntimeError):
    """A sealed rejudge failed closed with a typed code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SealedSource:
    baseline_root: Path
    evolved_root: Path
    baseline_sealed_hash: str
    evolved_sealed_hash: str


def _file_inventory(root: Path) -> dict[str, tuple[str, int, int]]:
    inventory: dict[str, tuple[str, int, int]] = {}
    for path in iter_regular_files(root):
        relative = path.relative_to(root).as_posix()
        metadata = path.stat()
        inventory[relative] = (
            sha256_file(path),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    return inventory


def _verify_sealed_hash(root: Path, expected_hash: str, label: str) -> str:
    if (
        not isinstance(expected_hash, str)
        or len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
    ):
        raise RejudgeSealedError(
            f"{label}_SEALED_HASH_INVALID",
            f"{label} sealed hash is invalid",
        )
    actual = candidate_artifact_root_sha256(root)
    if actual != expected_hash:
        raise RejudgeSealedError(
            f"{label}_SEALED_HASH_MISMATCH",
            f"{label} sealed hash differs from the source state",
        )
    return actual


def _validate_pass_payload(
    payload: dict[str, Any],
    *,
    pass_name: str,
    expected_model: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]
    if payload.get("score_valid") is not True:
        errors.append("score_valid is not true")
    if payload.get("judge_completed") is not True:
        errors.append("judge_completed is not true")
    if payload.get("failure_category") is not None:
        errors.append("failure_category is present")
    if payload.get("requested_model") != expected_model:
        errors.append("requested_model differs")
    if payload.get("requested_provider") != REQUESTED_JUDGE_PROVIDER:
        errors.append("requested_provider differs")
    total_score = payload.get("total_score")
    if type(total_score) not in {int, float} or not 0 <= float(total_score) <= 100:
        errors.append("total_score is invalid")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items are missing")
    else:
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"item {index} is not an object")
                continue
            if item.get("score_valid") is not True:
                errors.append(f"item {index} score_valid is false")
            if item.get("judge_completed") is not True:
                errors.append(f"item {index} judge_completed is false")
            if item.get("failure_category") is not None:
                errors.append(f"item {index} failure_category is present")
            if item.get("http_status") != 200:
                errors.append(f"item {index} http_status is not 200")
            if item.get("response_body_present") is not True:
                errors.append(f"item {index} response body is missing")
            if item.get("content_present") is not True:
                errors.append(f"item {index} content is missing")
            if item.get("json_parse_success") is not True:
                errors.append(f"item {index} JSON parse failed")
            if item.get("schema_valid") is not True:
                errors.append(f"item {index} schema is invalid")
            if item.get("requested_model") != expected_model:
                errors.append(f"item {index} requested_model differs")
            if item.get("requested_provider") != REQUESTED_JUDGE_PROVIDER:
                errors.append(f"item {index} requested_provider differs")
            returned_provider = item.get("returned_provider")
            if (
                not isinstance(returned_provider, str)
                or not returned_provider
            ):
                errors.append(f"item {index} returned_provider is missing")
            score = item.get("score")
            if type(score) not in {int, float} or not 0 <= float(score) <= 100:
                errors.append(f"item {index} score is invalid")
            reasoning = item.get("reasoning")
            if not isinstance(reasoning, str) or not reasoning.strip():
                errors.append(f"item {index} reasoning is empty")
    return errors


def _judge_environment_identity() -> tuple[str, str]:
    base_url = os.environ.get("RCB_JUDGE_BASE_URL") or os.environ.get(
        "JUDGE_API_BASE"
    )
    model = os.environ.get("RCB_JUDGE_MODEL") or os.environ.get("JUDGE_MODEL_NAME")
    if not base_url or not model:
        raise RejudgeSealedError(
            "BLOCKED_JUDGE_AUTH",
            "Judge credentials are incomplete",
        )
    return base_url, model


def _blocked_result(
    *,
    source: SealedSource,
    rejudge_id: str,
    source_run_id: str,
    requested_model: str,
    failure_category: str,
    failure_detail: str,
    probe: dict[str, Any] | None,
    judge_logical_jobs: int,
    actual_http_requests: int,
    passes: dict[str, dict[str, Any]],
    baseline_score: float | None,
    evolved_score: float | None,
    source_evidence_unchanged: bool,
) -> dict[str, Any]:
    return {
        "schema_version": REJUDGE_SCHEMA_VERSION,
        "status": "REJUDGE_SEALED_BLOCKED",
        "rejudge_id": rejudge_id,
        "source_run_id": source_run_id,
        "task_id": CANARY_TASK_ID,
        "requested_model": requested_model,
        "requested_provider": REQUESTED_JUDGE_PROVIDER,
        "source_baseline_sealed_hash": source.baseline_sealed_hash,
        "source_evolved_sealed_hash": source.evolved_sealed_hash,
        "baseline_sealed_hash_matches": True,
        "evolved_sealed_hash_matches": True,
        "source_evidence_unchanged": source_evidence_unchanged,
        "candidate_jobs": 0,
        "evolution_jobs": 0,
        "judge_logical_jobs": judge_logical_jobs,
        "actual_http_requests": actual_http_requests,
        "baseline_score": baseline_score,
        "evolved_score": evolved_score,
        "delta": None,
        "score_valid": False,
        "paired_score_valid": False,
        "failure_category": failure_category,
        "failure_detail": failure_detail,
        "probe": probe,
        "baseline": passes.get("baseline"),
        "evolved": passes.get("evolved"),
        "created_at": datetime.now(UTC).isoformat(),
    }


def run_rejudge_sealed(
    *,
    experiment_root: str | Path,
    source_run_id: str,
    rejudge_id: str,
    researchclawbench_root: str | Path,
    judge_executor: Callable[..., Any] = run_community_evaluator_detailed,
    probe_executor: Callable[..., dict[str, Any]] = run_judge_probe_subprocess,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Rejudge exactly the sealed baseline/evolved workspaces of a source run."""

    experiment_root_path = Path(experiment_root).resolve(strict=True)
    researchclawbench_root_path = Path(researchclawbench_root).resolve(strict=True)
    if not rejudge_id or "/" in rejudge_id or "\\" in rejudge_id:
        raise RejudgeSealedError("REJUDGE_ID_INVALID", "rejudge id is unsafe")
    source_store = TrainingStateStore(
        experiment_root_path / "supervisor" / source_run_id
    )
    state = source_store.load(source_run_id)
    if state.get("task_id") != CANARY_TASK_ID:
        raise RejudgeSealedError(
            "SOURCE_TASK_INVALID",
            "source run is not the Life_005 canary",
        )
    try:
        baseline_root = Path(
            state["baseline_receipt"]["candidate_output_root"]
        ).resolve(strict=True)
        evolved_root = Path(
            state["evolved_receipt"]["candidate_output_root"]
        ).resolve(strict=True)
        expected_baseline_hash = str(state["baseline_sealed_hash"])
        expected_evolved_hash = str(state["evolved_sealed_hash"])
    except (KeyError, TypeError) as exc:
        raise RejudgeSealedError(
            "SOURCE_STATE_INCOMPLETE",
            "source state lacks sealed workspace evidence",
        ) from exc
    source = SealedSource(
        baseline_root=baseline_root,
        evolved_root=evolved_root,
        baseline_sealed_hash=expected_baseline_hash,
        evolved_sealed_hash=expected_evolved_hash,
    )
    baseline_actual = _verify_sealed_hash(
        baseline_root, expected_baseline_hash, "BASELINE"
    )
    evolved_actual = _verify_sealed_hash(
        evolved_root, expected_evolved_hash, "EVOLVED"
    )
    baseline_before = _file_inventory(baseline_root)
    evolved_before = _file_inventory(evolved_root)

    base_url, model = _judge_environment_identity()
    project_root = researchclawbench_root_path.parent
    namespace = (
        experiment_root_path
        / "items"
        / CANARY_TASK_ID
        / "rejudges"
        / rejudge_id
    )
    result_path = namespace / "rejudge_result.json"
    if result_path.exists():
        raise RejudgeSealedError(
            "REJUDGE_ALREADY_EXISTS",
            "rejudge namespace already has a result",
        )
    namespace.mkdir(parents=True, exist_ok=True)
    evaluator_private_root = (
        experiment_root_path / "evaluator_private" / rejudge_id
    )
    evaluator_private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evaluator_private_root, 0o700)

    probe = probe_executor(
        project_root=project_root,
        output_root=namespace / "probe",
        expected_model=model,
        expected_api_base=base_url,
    )
    atomic_write_json(namespace / "probe.json", probe)
    if probe.get("status") != "JUDGE_SUBPROCESS_OK":
        result = _blocked_result(
            source=source,
            rejudge_id=rejudge_id,
            source_run_id=source_run_id,
            requested_model=model,
            failure_category=str(probe.get("failure_category") or "JUDGE_PROVIDER_FAILED"),
            failure_detail="Judge subprocess probe failed",
            probe=probe,
            judge_logical_jobs=0,
            actual_http_requests=1,
            passes={},
            baseline_score=None,
            evolved_score=None,
            source_evidence_unchanged=True,
        )
        atomic_write_json(result_path, result)
        return result

    passes: dict[str, dict[str, Any]] = {}
    judge_logical_jobs = 0
    actual_http_requests = 1
    baseline_score: float | None = None
    evolved_score: float | None = None
    failure_category: str | None = None
    failure_detail = ""
    for pass_name, root, attempt_id in (
        ("baseline", baseline_root, f"{CANARY_TASK_ID}_a0_baseline"),
        ("evolved", evolved_root, f"{CANARY_TASK_ID}_a0_evolved"),
    ):
        try:
            execution = judge_executor(
                project_root=project_root,
                workspace=root,
                evaluator_private_root=(
                    evaluator_private_root
                ),
                task_id=CANARY_TASK_ID,
                attempt_id=attempt_id,
                expected_model=model,
                expected_api_base=base_url,
                expected_provider="openai_compatible",
                expected_requested_provider=REQUESTED_JUDGE_PROVIDER,
                timeout_seconds=timeout_seconds,
            )
            judge_logical_jobs += 1
            payload = execution.raw_score
        except Exception as exc:
            failure_category = getattr(exc, "code", None) or "JUDGE_PROVIDER_FAILED"
            failure_detail = str(exc)
            break
        if not isinstance(payload, dict):
            failure_category = "JUDGE_RESPONSE_INVALID"
            failure_detail = f"{pass_name} Judge output is invalid"
            passes[pass_name] = {"error": failure_detail}
            atomic_write_json(namespace / f"{pass_name}_score.json", passes[pass_name])
            break
        passes[pass_name] = payload
        atomic_write_json(namespace / f"{pass_name}_score.json", payload)
        if isinstance(payload.get("http_requests"), int):
            actual_http_requests += max(0, payload["http_requests"])
        errors = _validate_pass_payload(payload, pass_name=pass_name, expected_model=model)
        if errors:
            failure_category = (
                payload.get("failure_category")
                or "JUDGE_RESPONSE_INVALID"
            )
            failure_detail = "; ".join(errors)
            break
        if pass_name == "baseline":
            baseline_score = float(payload["total_score"])
        else:
            evolved_score = float(payload["total_score"])

    baseline_after = _file_inventory(baseline_root)
    evolved_after = _file_inventory(evolved_root)
    source_unchanged = baseline_before == baseline_after and evolved_before == evolved_after

    if failure_category is not None:
        result = _blocked_result(
            source=source,
            rejudge_id=rejudge_id,
            source_run_id=source_run_id,
            requested_model=model,
            failure_category=failure_category,
            failure_detail=failure_detail,
            probe=probe,
            judge_logical_jobs=judge_logical_jobs,
            actual_http_requests=actual_http_requests,
            passes=passes,
            baseline_score=baseline_score,
            evolved_score=evolved_score,
            source_evidence_unchanged=source_unchanged,
        )
        result["baseline_sealed_hash_matches"] = (
            baseline_actual == expected_baseline_hash
        )
        result["evolved_sealed_hash_matches"] = evolved_actual == expected_evolved_hash
        atomic_write_json(result_path, result)
        return result

    if not source_unchanged:
        result = _blocked_result(
            source=source,
            rejudge_id=rejudge_id,
            source_run_id=source_run_id,
            requested_model=model,
            failure_category="SOURCE_EVIDENCE_CHANGED",
            failure_detail="source sealed workspace changed during rejudge",
            probe=probe,
            judge_logical_jobs=judge_logical_jobs,
            actual_http_requests=actual_http_requests,
            passes=passes,
            baseline_score=baseline_score,
            evolved_score=evolved_score,
            source_evidence_unchanged=False,
        )
        result["baseline_sealed_hash_matches"] = (
            baseline_actual == expected_baseline_hash
        )
        result["evolved_sealed_hash_matches"] = evolved_actual == expected_evolved_hash
        atomic_write_json(result_path, result)
        return result

    delta = round(evolved_score - baseline_score, 2)  # type: ignore[operator]
    result = {
        "schema_version": REJUDGE_SCHEMA_VERSION,
        "status": "REJUDGE_SEALED_CLOSED",
        "rejudge_id": rejudge_id,
        "source_run_id": source_run_id,
        "task_id": CANARY_TASK_ID,
        "requested_model": model,
        "requested_provider": REQUESTED_JUDGE_PROVIDER,
        "source_baseline_sealed_hash": expected_baseline_hash,
        "source_evolved_sealed_hash": expected_evolved_hash,
        "baseline_sealed_hash_matches": baseline_actual == expected_baseline_hash,
        "evolved_sealed_hash_matches": evolved_actual == expected_evolved_hash,
        "source_evidence_unchanged": True,
        "candidate_jobs": 0,
        "evolution_jobs": 0,
        "judge_logical_jobs": judge_logical_jobs,
        "actual_http_requests": actual_http_requests,
        "baseline_score": baseline_score,
        "evolved_score": evolved_score,
        "delta": delta,
        "score_valid": True,
        "paired_score_valid": True,
        "failure_category": None,
        "failure_detail": None,
        "probe": probe,
        "baseline": passes.get("baseline"),
        "evolved": passes.get("evolved"),
        "created_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_json(result_path, result)
    return result
