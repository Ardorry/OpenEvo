from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from .hashing import canonical_sha256
from .models import ObservationSource, TaskItem, ToolCall, ToolObservation, Trajectory


class OpenEvoRolloutError(RuntimeError):
    pass


def s0_config_hash(candidate: dict[str, Any]) -> str:
    frozen = json.loads(json.dumps(candidate))
    frozen.pop("task_id", None)
    frozen.pop("instruction", None)
    metadata = frozen.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("evolution", None)
        metadata.pop("run_id", None)
        metadata.pop("pair_id", None)
    return canonical_sha256(frozen)


def build_task_request(
    *,
    task: TaskItem,
    run_id: str,
    role: str,
    candidate: dict[str, Any],
    artifact_ids: list[str],
    mcp_url: str | None,
) -> dict[str, Any]:
    if role not in {"baseline", "evolved"}:
        raise ValueError("candidate role must be baseline or evolved")
    if role == "baseline" and artifact_ids:
        raise ValueError("baseline cannot receive artifacts")
    if role == "evolved" and len(artifact_ids) != 1:
        raise ValueError("evolved candidate requires exactly one task-local artifact")
    agent = json.loads(json.dumps(candidate["agent"]))
    servers = list(agent.get("mcp_servers", []))
    if mcp_url:
        servers.append(
            {
                "name": "chemcrow-tools",
                "transport": "streamable-http",
                "url": mcp_url,
            }
        )
    agent["mcp_servers"] = servers
    metadata = json.loads(json.dumps(candidate.get("metadata", {})))
    metadata.update(
        {
            "run_id": run_id,
            "task_tags": ["chemcrow", task.broad_category, task.task_id],
            "evolution": {"context_artifact_ids": list(artifact_ids)},
        }
    )
    return {
        "task_id": run_id,
        "instruction": task.prompt,
        "num_samples": 1,
        "timeout_seconds": candidate.get("timeout_seconds", 900.0),
        "runtime": candidate.get("runtime"),
        "agent": agent,
        "builder": candidate.get("builder", {"strategy": "agent_transcript", "config": {}}),
        "evaluator": None,
        "callback_url": None,
        "metadata": metadata,
        "workspace_handoff": candidate.get("workspace_handoff"),
        "runtime_context_binding": candidate.get("runtime_context_binding"),
    }


class OpenEvoRolloutPort:
    def __init__(
        self,
        *,
        base_url: str,
        candidate: dict[str, Any],
        poll_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.candidate = json.loads(json.dumps(candidate))
        self.poll_seconds = poll_seconds
        self.config_sha256 = s0_config_hash(self.candidate)

    def run_candidate(
        self,
        *,
        task: TaskItem,
        role: str,
        artifact_ids: list[str],
        pair_id: str,
        mcp_url: str | None,
    ) -> Trajectory:
        run_id = f"{pair_id}-{role}-{uuid.uuid4().hex[:10]}"
        payload = build_task_request(
            task=task,
            run_id=run_id,
            role=role,
            candidate=self.candidate,
            artifact_ids=artifact_ids,
            mcp_url=mcp_url,
        )
        started = time.monotonic()
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{self.base_url}/rollout/task/submit", json=payload)
            response.raise_for_status()
            while True:
                status_response = client.get(f"{self.base_url}/rollout/task/{run_id}")
                status_response.raise_for_status()
                status = status_response.json()
                if status.get("status") in {"completed", "failed", "cancelled"}:
                    break
                if time.monotonic() - started > float(payload["timeout_seconds"]) + 60:
                    raise OpenEvoRolloutError("rollout polling exceeded the admitted timeout")
                time.sleep(self.poll_seconds)
        return _normalize_rollout(
            status,
            run_id=run_id,
            task_id=task.task_id,
            role=role,
            artifact_ids=artifact_ids,
            config_sha256=self.config_sha256,
            wall_time=time.monotonic() - started,
        )


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _normalize_rollout(
    payload: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    role: str,
    artifact_ids: list[str],
    config_sha256: str,
    wall_time: float,
) -> Trajectory:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise OpenEvoRolloutError("rollout did not return exactly one session result")
    result = results[0]
    trajectory = result.get("trajectory") if isinstance(result.get("trajectory"), dict) else {}
    traces = trajectory.get("traces") if isinstance(trajectory.get("traces"), list) else []
    answer = ""
    tool_calls: list[ToolCall] = []
    observations: list[ToolObservation] = []
    visible_summaries: list[str] = []
    for trace_index, trace in enumerate(traces):
        if not isinstance(trace, dict):
            continue
        for message in trace.get("response_messages", []):
            if not isinstance(message, dict):
                continue
            content = _message_content(message).strip()
            if message.get("role") == "assistant" and content:
                answer = content
                visible_summaries.append(content[:2000])
            for raw_call in message.get("tool_calls", []) or []:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
                raw_arguments = function.get("arguments", {})
                if isinstance(raw_arguments, str):
                    try:
                        raw_arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        raw_arguments = {"raw": raw_arguments}
                call_id = str(raw_call.get("id") or f"trace-{trace_index}-{len(tool_calls)}")
                tool_calls.append(
                    ToolCall(
                        call_id=call_id,
                        tool_name=str(function.get("name") or raw_call.get("name") or "unknown"),
                        arguments=raw_arguments if isinstance(raw_arguments, dict) else {"value": raw_arguments},
                    )
                )
        for message in trace.get("prompt_messages", []):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or f"observation-{len(observations)}")
            matching = next((item for item in tool_calls if item.call_id == call_id), None)
            arguments = matching.arguments if matching else {}
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            source_value = metadata.get("chemcrow_observation_source", "live")
            try:
                source = ObservationSource(source_value)
            except ValueError:
                source = ObservationSource.LIVE
            observations.append(
                ToolObservation(
                    call_id=call_id,
                    tool_name=str(message.get("name") or (matching.tool_name if matching else "unknown")),
                    canonical_arguments_sha256=canonical_sha256(arguments),
                    result=_message_content(message),
                    source=source,
                )
            )
    status_value = str(trajectory.get("status") or result.get("status") or "ERROR").upper()
    if status_value not in {"COMPLETED", "ERROR", "TIMEOUT"}:
        status_value = "ERROR"
    metadata = trajectory.get("metadata") if isinstance(trajectory.get("metadata"), dict) else {}
    return Trajectory(
        run_id=run_id,
        task_id=task_id,
        role=role,
        status=status_value,
        answer=answer,
        tool_calls=tool_calls,
        observations=observations,
        visible_reasoning_summaries=visible_summaries,
        retries=int(metadata.get("retries") or 0),
        wall_time_seconds=wall_time,
        token_metadata=metadata.get("token_usage", {}),
        cost_metadata=metadata.get("cost", {}),
        candidate_config_sha256=config_sha256,
        artifact_ids=artifact_ids,
    )
