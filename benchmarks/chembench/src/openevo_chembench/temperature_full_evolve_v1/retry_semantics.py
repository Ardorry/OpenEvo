"""Attempt-independent TaskRequest identity for bounded formal retries."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from openevo.rollout.models import TaskRequest

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes

RETRY_SEMANTICS_SCHEMA = "TemperatureTaskRequestRetrySemanticsV1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ATTEMPT_TASK_ID = "<attempt-task-id>"
_ATTEMPT_CALL_ID = "<attempt-call-id>"
_LOGICAL_CALL_ID = "<ledger-bound-logical-call-id>"
_SESSION_CONTEXT_BINDING = "<attempt-session-context-binding>"


class TemperatureRetrySemanticsError(RuntimeError):
    """TaskRequest cannot be reduced under the closed retry identity rules."""


def task_request_retry_semantics_sha256_v1(request: TaskRequest) -> str:
    """Hash full request semantics while normalizing proven attempt identities.

    Prompt, agent, runtime, tool policy, metadata, context artifact IDs and all
    other fields remain byte-bound.  Only the top-level task ID, ledger
    call/logical IDs, and session-bound context-binding receipts are replaced.
    The latter remain semantically covered by the unchanged instruction,
    context artifact/target IDs, arm, runtime upload and task metadata.
    """

    if type(request) is not TaskRequest:
        raise TypeError("retry semantics requires an exact TaskRequest")
    payload: dict[str, Any] = request.model_dump(
        mode="json",
        exclude_defaults=False,
        exclude_none=False,
        exclude_unset=False,
    )
    task_id = payload.get("task_id")
    if type(task_id) is not str or not task_id:
        raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_TASK_ID_INVALID")
    payload["task_id"] = _ATTEMPT_TASK_ID
    metadata = payload.get("metadata")
    if type(metadata) is not dict:
        raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_METADATA_INVALID")
    benchmark = metadata.get("openevo_chembench")
    if type(benchmark) is not dict:
        raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_METADATA_INVALID")
    _normalize_context_binding(benchmark, "context_binding_sha256")
    temperature = benchmark.get("temperature_full_evolve")
    if temperature is not None:
        if type(temperature) is not dict:
            raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_METADATA_INVALID")
        _normalize_required_string(temperature, "call_id", _ATTEMPT_CALL_ID)
        _normalize_required_string(
            temperature,
            "logical_call_id",
            _LOGICAL_CALL_ID,
        )
        _normalize_context_binding(temperature, "context_binding_sha256")
    body = {
        "schema_version": RETRY_SEMANTICS_SCHEMA,
        "normalized_task_request": payload,
    }
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _normalize_context_binding(value: dict[str, Any], field: str) -> None:
    if field not in value:
        return
    digest = value[field]
    if type(digest) is not str or _SHA256.fullmatch(digest) is None:
        raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_CONTEXT_BINDING_INVALID")
    value[field] = _SESSION_CONTEXT_BINDING


def _normalize_required_string(
    value: dict[str, Any],
    field: str,
    replacement: str,
) -> None:
    current = value.get(field)
    if type(current) is not str or not current:
        raise TemperatureRetrySemanticsError("RETRY_SEMANTICS_METADATA_INVALID")
    value[field] = replacement


__all__ = [
    "RETRY_SEMANTICS_SCHEMA",
    "TemperatureRetrySemanticsError",
    "task_request_retry_semantics_sha256_v1",
]
