from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class RolloutClientProtocol(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def cancel_task(self, task_id: str) -> dict[str, Any]: ...


class EvolutionClientProtocol(Protocol):
    def get_internal_health(self) -> dict[str, Any]: ...

    def get_completed_dataset_authority(self, dataset_id: str) -> dict[str, Any]: ...

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_dataset(self, dataset_id: str) -> dict[str, Any]: ...

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def retry_plan_bound_job(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any]: ...

    def create_training_feedback_attachment(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_training_feedback_attachment(self, attachment_id: str) -> dict[str, Any]: ...

    def list_training_feedback_attachments_for_session(
        self, session_id: str
    ) -> dict[str, Any]: ...

    def resolve_evolution_dataset_view(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]: ...

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_materialized_context(self, context_id: str) -> dict[str, Any]: ...

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def get_internal_failed_plan_bound_job_authority(
        self,
        job_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_succeeded_plan_bound_job_authority(
        self,
        successor_transition_id: str,
        target_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_transition_job_inventory(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_reflector_inference_reservation(
        self,
        job_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]: ...

    def get_internal_successor_artifact_authority(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]: ...

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]: ...

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]: ...

    def apply_internal_artifact_admission(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]: ...

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]: ...

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RolloutHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float | None = None,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        timeout = (
            httpx.Timeout(timeout_seconds, connect=30.0)
            if timeout_seconds
            else httpx.Timeout(None, connect=30.0)
        )
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            headers=dict(headers or {}),
            transport=transport,
        )

    def __enter__(self) -> RolloutHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def submit_task(self, payload: dict[str, Any]) -> str:
        response = self._client.post(f"{self.base_url}/rollout/task/submit", json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout submit response was not a JSON object")
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("rollout submit response did not include task_id")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.get(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout task response was not a JSON object")
        return result

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        encoded_task_id = quote(task_id, safe="")
        response = self._client.delete(f"{self.base_url}/rollout/task/{encoded_task_id}")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("rollout cancellation response was not a JSON object")
        if result.get("task_id") != task_id or result.get("status") != "cancelled":
            raise ValueError("rollout cancellation response did not prove termination")
        return result


class EvolutionHttpStatusError(RuntimeError):
    def __init__(self, *, status_code: int, detail_code: str = "unspecified") -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise ValueError("evolution HTTP status code is invalid")
        if detail_code not in {
            "conflict",
            "core_config_contains_non_core_owned_fields",
            "not_found",
            "request_validation_failed",
            "server_failure",
            "unspecified",
        }:
            raise ValueError("evolution HTTP detail code is invalid")
        self.status_code = status_code
        self.detail_code = detail_code
        self.retryable = status_code >= 500 or status_code in {408, 425, 429}
        super().__init__(
            f"evolution service returned HTTP status {status_code} ({detail_code})"
        )


class EvolutionHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            trust_env=False,
            headers=dict(headers or {}),
            transport=transport,
        )

    def __enter__(self) -> EvolutionHttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_internal_health(self) -> dict[str, Any]:
        response = self._client.get(f"{self.base_url}/v1/health")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("evolution health response was not a JSON object")
        return result

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        detail_code = "unspecified"
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if (
            isinstance(detail, str)
            and detail.startswith("core config may only contain Core-owned fields:")
        ):
            detail_code = "core_config_contains_non_core_owned_fields"
        elif response.status_code == 404:
            detail_code = "not_found"
        elif response.status_code == 409:
            detail_code = "conflict"
        elif response.status_code == 422:
            detail_code = "request_validation_failed"
        elif response.status_code >= 500:
            detail_code = "server_failure"
        raise EvolutionHttpStatusError(
            status_code=response.status_code,
            detail_code=detail_code,
        )

    def create_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/datasets", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution dataset response was not a JSON object")
        return result

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        encoded_dataset_id = quote(dataset_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/datasets/{encoded_dataset_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution dataset response was not a JSON object")
        return result

    def get_completed_dataset_authority(self, dataset_id: str) -> dict[str, Any]:
        encoded = quote(dataset_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/datasets/{encoded}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("completed dataset authority was not a JSON object")
        return result

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/jobs", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job response was not a JSON object")
        return result

    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/planned-jobs", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("planned evolution job response was not a JSON object")
        return result

    def retry_plan_bound_job(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.post(
            f"{self.base_url}/v1/planned-jobs/{encoded_job_id}/retry",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("planned evolution job retry response was not a JSON object")
        return result

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/artifacts/{encoded_artifact_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact response was not a JSON object")
        return result

    def create_training_feedback_attachment(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/training-feedback/attachments",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback response was not a JSON object")
        return result

    def get_training_feedback_attachment(self, attachment_id: str) -> dict[str, Any]:
        encoded = quote(attachment_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/attachments/{encoded}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback response was not a JSON object")
        return result

    def list_training_feedback_attachments_for_session(self, session_id: str) -> dict[str, Any]:
        encoded = quote(session_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/training-feedback/sessions/{encoded}/attachments"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("training feedback list was not a JSON object")
        return result

    def resolve_evolution_dataset_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/training-feedback/resolve",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("resolved evolution dataset view was not a JSON object")
        return result

    def get_context_runtime_authority(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/contexts/{encoded_context_id}/runtime-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("context runtime authority was not a JSON object")
        return result

    def create_materialized_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/materialized-contexts",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_materialized_context(self, context_id: str) -> dict[str, Any]:
        encoded_context_id = quote(context_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/materialized-contexts/{encoded_context_id}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("materialized context response was not a JSON object")
        return result

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/internal/jobs/{encoded_job_id}")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution job result was not a JSON object")
        return result

    def get_internal_failed_plan_bound_job_authority(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/jobs/{encoded_job_id}/failed-plan-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("failed plan-bound job authority was not a JSON object")
        return result

    def get_internal_succeeded_plan_bound_job_authority(
        self,
        successor_transition_id: str,
        target_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        encoded_target_id = quote(target_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/targets/{encoded_target_id}/"
            "succeeded-plan-authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("succeeded plan-bound job authority was not an object")
        return result

    def get_internal_successor_transition_job_inventory(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/jobs"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition job inventory was not a JSON object")
        return result

    def get_internal_reflector_inference_reservation(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        encoded_job_id = quote(job_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/jobs/{encoded_job_id}/reflector-inference"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("reflector inference reservation was not a JSON object")
        return result

    def apply_internal_artifact_admission(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/internal/artifact-admission/decisions",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("artifact admission response was not a JSON object")
        return result

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(
            successor_transition_id,
            safe="",
        )
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/artifacts/{encoded_artifact_id}"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition artifact was not a JSON object")
        return result

    def get_internal_successor_artifact_authority(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(successor_transition_id, safe="")
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.get(
            f"{self.base_url}/v1/internal/successor-transitions/"
            f"{encoded_transition_id}/artifacts/{encoded_artifact_id}/authority"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor artifact authority was not a JSON object")
        return result

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict[str, Any]:
        encoded_transition_id = quote(
            successor_transition_id,
            safe="",
        )
        response = self._client.post(
            f"{self.base_url}/v1/internal/successor-transitions/{encoded_transition_id}/discard"
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("successor transition discard was not a JSON object")
        return result

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> dict[str, Any]:
        encoded_artifact_id = quote(artifact_id, safe="")
        response = self._client.patch(
            f"{self.base_url}/v1/artifacts/{encoded_artifact_id}/promotion",
            json={"promoted": promoted},
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution artifact promotion response was not a JSON object")
        return result

    def create_review_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/reviews", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution review response was not a JSON object")
        return result

    def list_review_requests(self, **filters: Any) -> list[dict[str, Any]]:
        params = {key: value for key, value in filters.items() if value is not None}
        response = self._client.get(f"{self.base_url}/v1/reviews", params=params)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution review list response was not a JSON array")
        return result

    def submit_human_feedback(
        self,
        review_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.post(
            f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution human feedback response was not a JSON object")
        return result

    def list_human_feedback(self, *, review_id: str) -> list[dict[str, Any]]:
        encoded_review_id = quote(review_id, safe="")
        response = self._client.get(f"{self.base_url}/v1/reviews/{encoded_review_id}/feedback")
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, list):
            raise ValueError("evolution human feedback list response was not a JSON array")
        return result

    def create_feedback_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1/feedback-applications",
            json=payload,
        )
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution feedback application response was not a JSON object")
        return result

    def create_human_query_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"{self.base_url}/v1/query-decisions", json=payload)
        self._raise_for_status(response)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("evolution query decision response was not a JSON object")
        return result
