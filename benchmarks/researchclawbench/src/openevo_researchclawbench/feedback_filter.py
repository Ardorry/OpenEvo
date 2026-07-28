"""Closed community-score feedback boundary."""

from __future__ import annotations

import re
from typing import Any


ALLOWED_KEYS = {
    "task_id", "attempt_id", "completed", "exit_code", "artifact_valid",
    "total_score", "generic_failure_tags", "runtime_bucket_seconds",
    "cost_total_usd", "artifact_root_sha256",
}
GENERIC_FAILURE_TAGS = {
    "DATA_LOAD_ERROR", "DEPENDENCY_ERROR", "EXECUTION_ERROR", "TIMEOUT",
    "REPORT_MISSING", "REPORT_INVALID", "IMAGE_MISSING", "IMAGE_INVALID",
    "PATH_POLICY_VIOLATION", "TRACEABILITY_MISSING", "RESOURCE_LIMIT",
    "NETWORK_POLICY_VIOLATION", "UNKNOWN_OPERATIONAL_FAILURE",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^[A-Za-z]+_[0-9]{3}$")
ATTEMPT_RE = re.compile(r"^[A-Za-z]+_[0-9]{3}_a[0-2](?:_[A-Za-z0-9-]+)?$")


class FeedbackPolicyError(ValueError):
    """Private or malformed evaluator fields reached the release boundary."""


def validate_feedback(payload: dict[str, Any], *, allowed_tasks: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != ALLOWED_KEYS:
        extra = sorted(set(payload) - ALLOWED_KEYS) if isinstance(payload, dict) else []
        raise FeedbackPolicyError(f"feedback fields are not closed; rejected={extra}")
    if payload["task_id"] not in allowed_tasks or not TASK_RE.fullmatch(payload["task_id"]):
        raise FeedbackPolicyError("task is outside Community Dev experiment")
    if not isinstance(payload["attempt_id"], str) or not ATTEMPT_RE.fullmatch(payload["attempt_id"]):
        raise FeedbackPolicyError("invalid attempt_id")
    for name in ("completed", "artifact_valid"):
        if type(payload[name]) is not bool:
            raise FeedbackPolicyError(f"{name} must be boolean")
    if type(payload["exit_code"]) is not int or not -255 <= payload["exit_code"] <= 255:
        raise FeedbackPolicyError("invalid exit_code")
    if type(payload["total_score"]) not in {int, float} or not 0 <= float(payload["total_score"]) <= 100:
        raise FeedbackPolicyError("invalid total_score")
    tags = payload["generic_failure_tags"]
    if not isinstance(tags, list) or len(tags) > 16 or len(tags) != len(set(tags)) or not set(tags) <= GENERIC_FAILURE_TAGS:
        raise FeedbackPolicyError("invalid generic_failure_tags")
    if type(payload["runtime_bucket_seconds"]) is not int or not 0 <= payload["runtime_bucket_seconds"] <= 604800:
        raise FeedbackPolicyError("invalid runtime bucket")
    if type(payload["cost_total_usd"]) not in {int, float} or not 0 <= float(payload["cost_total_usd"]) <= 100000:
        raise FeedbackPolicyError("invalid cost")
    if not isinstance(payload["artifact_root_sha256"], str) or not SHA256_RE.fullmatch(payload["artifact_root_sha256"]):
        raise FeedbackPolicyError("invalid artifact hash")
    return dict(payload)


def filter_private_score(
    raw_score: dict[str, Any],
    *,
    task_id: str,
    attempt_id: str,
    completed: bool,
    exit_code: int,
    artifact_valid: bool,
    generic_failure_tags: list[str],
    runtime_seconds: int,
    cost_total_usd: float,
    artifact_root_sha256: str,
    allowed_tasks: set[str],
) -> dict[str, Any]:
    if "total_score" not in raw_score or type(raw_score["total_score"]) not in {int, float}:
        raise FeedbackPolicyError("private scorer result lacks numeric total_score")
    bucket = ((max(0, runtime_seconds) + 59) // 60) * 60
    released = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "completed": completed,
        "exit_code": exit_code,
        "artifact_valid": artifact_valid,
        "total_score": float(raw_score["total_score"]),
        "generic_failure_tags": generic_failure_tags,
        "runtime_bucket_seconds": bucket,
        "cost_total_usd": float(cost_total_usd),
        "artifact_root_sha256": artifact_root_sha256,
    }
    return validate_feedback(released, allowed_tasks=allowed_tasks)
