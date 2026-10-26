from __future__ import annotations

from typing import Any

from .hashing import canonical_sha256
from .models import EvaluatorFeedback, FeedbackMode, RuntimeFeedback, Trajectory

_ERROR_MARKERS = ("error", "invalid", "failed", "unavailable", "not found")


def runtime_feedback(trajectory: Trajectory) -> RuntimeFeedback:
    tool_errors: list[str] = []
    invalid_calls: list[str] = []
    empty: list[str] = []
    for observation in trajectory.observations:
        if observation.error:
            tool_errors.append(f"{observation.tool_name}:{observation.error}")
        rendered = str(observation.result or "").strip()
        if not rendered:
            empty.append(observation.tool_name)
        elif any(marker in rendered.lower() for marker in _ERROR_MARKERS):
            invalid_calls.append(f"{observation.tool_name}:{rendered[:240]}")
    completion_failures = []
    if trajectory.status != "COMPLETED":
        completion_failures.append(f"run_status={trajectory.status}")
    if not trajectory.answer.strip():
        completion_failures.append("empty_final_answer")
    return RuntimeFeedback(
        status=trajectory.status,
        tool_errors=tool_errors,
        invalid_calls=invalid_calls,
        empty_observations=empty,
        completion_failures=completion_failures,
    )


def reflector_feedback_payload(
    *,
    mode: FeedbackMode,
    trajectory: Trajectory,
    runtime: RuntimeFeedback,
    evaluator: EvaluatorFeedback | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": mode,
        "observable_trajectory": trajectory.model_dump(mode="json"),
    }
    if mode in {FeedbackMode.F1, FeedbackMode.F2}:
        payload["runtime_feedback"] = runtime.model_dump(mode="json")
    if mode is FeedbackMode.F2:
        if evaluator is None:
            raise ValueError("F2 requires evolution evaluator feedback")
        if evaluator.evaluator_role != "evolution_evaluator":
            raise ValueError("F2 cannot consume final evaluator output")
        payload["evaluator_feedback"] = evaluator.model_dump(mode="json")
    return payload


def feedback_hash(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)
